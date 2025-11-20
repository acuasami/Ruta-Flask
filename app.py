from flask import Flask, request, render_template, url_for, redirect, jsonify
from flask_cors import CORS
import os
import math
import pandas as pd
import osmnx as ox
import networkx as nx
import folium
from folium.plugins import MarkerCluster
from geopy.distance import geodesic
from sqlalchemy import create_engine, text
import io

app = Flask(__name__)
CORS(app)

# --- 1. CONFIGURACIÓN DE BASE DE DATOS ---
DATABASE_URL = os.environ.get("DATABASE_URL")

# Si la variable de entorno existe (Render), la usamos directamente.
if DATABASE_URL:
    FINAL_DATABASE_URL = DATABASE_URL
# Si no existe (fallback para pruebas locales), usamos un valor por defecto.
else:
    FINAL_DATABASE_URL = "postgresql://user:password@localhost:5432/mydatabase"

# Crear el motor de base de datos
engine = create_engine(FINAL_DATABASE_URL)

# --- 2. FUNCIONES DE LÓGICA ---

def conectar_y_leer_sql(query, params=None):
    try:
        with engine.connect() as conn:
            return pd.read_sql(query, conn, params=params)
    except Exception as e:
        print(f"❌ Error leyendo BD: {e}")
        return pd.DataFrame()

def cargar_waypoints_ongs():
    query = text("""
        SELECT o.nom_ong, o.tipo, o.latitud, o.longitud, m.nom_municipio 
        FROM public.ongs o
        JOIN public.municipio m ON o.id_municipio = m.id_municipio;
    """)
    df = conectar_y_leer_sql(query)
    
    if df.empty: return []

    df.rename(columns={
        'nom_ong': 'name', 'tipo': 'type', 'latitud': 'lat', 
        'longitud': 'lon', 'nom_municipio': 'municipio'
    }, inplace=True)
    
    df['lat'] = pd.to_numeric(df['lat'], errors='coerce')
    df['lon'] = pd.to_numeric(df['lon'], errors='coerce')
    df = df.dropna(subset=['lat', 'lon'])
    
    return [row.to_dict() for _, row in df.iterrows()]

def cargar_datos_riesgo():
    df_fecha = conectar_y_leer_sql(text("SELECT * FROM public.fecha;"))
    df_municipio = conectar_y_leer_sql(text("SELECT * FROM public.municipio;"))

    if df_fecha.empty or df_municipio.empty: return {}

    df_fecha['fecha'] = pd.to_datetime(df_fecha['fecha'])
    if df_fecha.empty: return {}
        
    ultimo_mes = df_fecha['fecha'].max().month
    ultimo_ano = df_fecha['fecha'].max().year
    
    df_ultimo = df_fecha[(df_fecha['fecha'].dt.month == ultimo_mes) &
                         (df_fecha['fecha'].dt.year == ultimo_ano)]
    
    df_riesgo = pd.merge(df_ultimo, df_municipio, on='id_municipio')
    return dict(zip(df_riesgo['nom_municipio'], df_riesgo['grado']))

def ong_mas_cercana(pos_actual, waypoints):
    ongs = [w for w in waypoints if str(w.get('type', '')).strip().lower() != 'frontera']
    if not ongs: return None
    for o in ongs:
        o['distancia'] = geodesic(pos_actual, (o['lat'], o['lon'])).kilometers
    return min(ongs, key=lambda x: x['distancia'])

def find_sorted_ongs(start, waypoints_list):
    candidates = []
    for ong in waypoints_list:
        if str(ong.get('type', '')).strip().lower() != 'frontera':
            ong_point = (ong["lat"], ong["lon"])
            ong['distancia'] = geodesic(start, ong_point).km
            candidates.append(ong)
    candidates.sort(key=lambda x: x['distancia'])
    return candidates

def haversine_heuristic(u, v, G):
    lat1, lon1 = G.nodes[u]['y'], G.nodes[u]['x']
    lat2, lon2 = G.nodes[v]['y'], G.nodes[v]['x']
    R = 6371000 
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

def obtener_municipio_por_proximidad(lat, lon, waypoints):
    min_dist = float('inf')
    municipio_cercano = 'Desconocido'
    for ong in waypoints:
        dist = geodesic((lat, lon), (ong['lat'], ong['lon'])).kilometers
        if dist < min_dist:
            min_dist = dist
            municipio_cercano = ong.get('municipio', 'Desconocido')
    return municipio_cercano

def generar_mapa_movil_con_recomendaciones(ubicacion_usuario, ong_cercana, segmentos_ruta, waypoints, id_usuario, colores_riesgo, ongs_cercanas, siguiente_recomendacion):
    m = folium.Map(location=ubicacion_usuario, zoom_start=13, tiles="CartoDB positron", width='100%', height='100vh')
    
    # Ruta
    for segmento in segmentos_ruta:
        color = colores_riesgo.get(segmento['grado_riesgo'], 'gray')
        folium.PolyLine(segmento['coords'], color=color, weight=8, opacity=0.9).add_to(m)
    
    # Marcadores
    folium.Marker(location=ubicacion_usuario, popup=f"Tu Ubicación", icon=folium.Icon(color="blue", icon="user", prefix="fa")).add_to(m)
    
    if ong_cercana:
        tipo = ong_cercana.get('type', 'default')
        color = 'red' if tipo == 'Frontera' else 'green'
        folium.Marker(location=(ong_cercana['lat'], ong_cercana['lon']), popup=f"Destino: {ong_cercana['name']}", icon=folium.Icon(color=color, icon="home", prefix="fa")).add_to(m)

    if siguiente_recomendacion:
        folium.Marker(location=(siguiente_recomendacion['lat'], siguiente_recomendacion['lon']), popup=f"Recomendación: {siguiente_recomendacion['name']}", icon=folium.Icon(color="orange", icon="star", prefix="fa")).add_to(m)

    return m.get_root().render()

# --- 3. RUTAS PRINCIPALES ---

@app.route('/')
def index():
    # Redirigir automáticamente al mapa con coordenadas por defecto (CDMX)
    return redirect(url_for('mapa', lat=19.325521, lon=-99.167807, id_usuario=1))

@app.route('/login', methods=['POST'])
def login():
    # Login simulado: redirige al mapa directamente
    id_usuario = request.form.get('id_usuario', default=1, type=int)
    return redirect(url_for('mapa', lat=19.325521, lon=-99.167807, id_usuario=id_usuario))

# RUTA DE DIAGNÓSTICO DB
@app.route('/test-db')
def test_db_connection():
    try:
        query = text("SELECT COUNT(*) AS total_ongs FROM public.ongs;")
        df = conectar_y_leer_sql(query) 

        if df.empty or 'total_ongs' not in df.columns:
            return jsonify({"status": "ERROR", "message": "Conexión OK, pero tabla vacía o ilegible."}), 500
        
        total = df['total_ongs'].iloc[0]
        return jsonify({
            "status": "OK", 
            "message": "¡Conexión a la base de datos exitosa!",
            "total_ongs": int(total)
        }), 200
    except Exception as e:
        return jsonify({"status": "DB_CONNECTION_FAILED", "error_details": str(e)}), 500

@app.route('/mapa')
def mapa():
    try:
        lat = request.args.get('lat', type=float)
        lon = request.args.get('lon', type=float)
        id_usuario = request.args.get('id_usuario', default=1, type=int)

        if lat is None or lon is None: 
            return "Error: Faltan coordenadas GPS.", 400

        start_point = (lat, lon)
        
        # Cargar datos
        waypoints = cargar_waypoints_ongs()
        riesgo_por_municipio_nombre = cargar_datos_riesgo()
        
        if not waypoints: 
            return "Error: No hay ONGs en la base de datos (o error de conexión).", 500

        # Lógica de negocio
        ong_cercana = ong_mas_cercana(start_point, waypoints)
        if not ong_cercana: 
            return "Error: No se encontraron ONGs cercanas.", 500

        ongs_ordenadas = find_sorted_ongs(start_point, waypoints)
        siguiente_recomendacion = ongs_ordenadas[1] if len(ongs_ordenadas) > 1 else None
        ongs_cercanas = ongs_ordenadas[:5]

        # Cálculo de Ruta (Simplificado con try-except para evitar crasheos)
        # --- VERSIÓN OPTIMIZADA PARA AHORRAR MEMORIA RAM ---
        segmentos_ruta = []
        try:
            dest_point = (ong_cercana['lat'], ong_cercana['lon'])
            
            # 1. Calcular distancia lineal primero para evitar sobrecarga
            distancia_lineal = geodesic(start_point, dest_point).km
            
            # SI LA DISTANCIA ES MAYOR A 15 KM, EVITAR CÁLCULO PESADO (Prevención de Crasheo)
            if distancia_lineal > 15:
                 print("⚠️ Distancia muy larga para servidor gratuito. Saltando cálculo detallado.")
                 # Aquí podrías simplemente dibujar una línea recta si quisieras, 
                 # pero por ahora dejamos la lista vacía para que no falle.
            else:
                # 2. REDUCIMOS EL PADDING (Margen) DRÁSTICAMENTE
                # Antes: 0.02 (~2.2km) -> Causa OOM (Out of Memory)
                # Ahora: 0.003 (~300m) -> Suficiente para la calle y mucho más ligero
                padding = 0.003 
                
                G = ox.graph_from_bbox(
                    max(lat, dest_point[0]) + padding, min(lat, dest_point[0]) - padding, 
                    max(lon, dest_point[1]) + padding, min(lon, dest_point[1]) - padding, 
                    network_type="drive"
                )
                G = ox.distance.add_edge_lengths(G)
                
                orig_node = ox.distance.nearest_nodes(G, lon, lat)
                dest_node = ox.distance.nearest_nodes(G, dest_point[1], dest_point[0])
                
                route = nx.astar_path(G, orig_node, dest_node, weight='length')
                route_coords = [(G.nodes[n]['y'], G.nodes[n]['x']) for n in route]
                
                # Segmentación por riesgo (Lógica original)
                segmento_actual = []
                municipio_actual = None
                riesgo_actual = 'Desconocido'
                
                for coord in route_coords:
                    municipio = obtener_municipio_por_proximidad(coord[0], coord[1], waypoints)
                    
                    if municipio_actual is None: 
                        municipio_actual = municipio
                        riesgo_actual = riesgo_por_municipio_nombre.get(municipio, 'Desconocido')

                    if municipio != municipio_actual:
                        if segmento_actual:
                            segmentos_ruta.append({
                                'coords': segmento_actual, 
                                'municipio': municipio_actual, 
                                'grado_riesgo': riesgo_actual
                            })
                        municipio_actual = municipio
                        riesgo_actual = riesgo_por_municipio_nombre.get(municipio, 'Desconocido')
                        segmento_actual = [coord]
                    else:
                        segmento_actual.append(coord)
                
                if segmento_actual:
                    segmentos_ruta.append({
                        'coords': segmento_actual, 
                        'municipio': municipio_actual, 
                        'grado_riesgo': riesgo_actual
                    })

        except Exception as e:
            print(f"⚠️ Advertencia Ruta (Memoria o error): {e}")
            # No hacemos 'raise' para que el servidor NO se caiga y al menos muestre el mapa y marcadores
            segmentos_ruta = []
            
            G = ox.graph_from_bbox(
                max(lat, dest_point[0]) + padding, min(lat, dest_point[0]) - padding, 
                max(lon, dest_point[1]) + padding, min(lon, dest_point[1]) - padding, 
                network_type="drive"
            )
            G = ox.distance.add_edge_lengths(G)
            
            orig_node = ox.distance.nearest_nodes(G, lon, lat)
            dest_node = ox.distance.nearest_nodes(G, dest_point[1], dest_point[0])
            
            route = nx.astar_path(G, orig_node, dest_node, weight='length')
            route_coords = [(G.nodes[n]['y'], G.nodes[n]['x']) for n in route]
            
            # Segmentación
            segmento_actual = []
            municipio_actual = None
            riesgo_actual = 'Desconocido'
            
            for coord in route_coords:
                municipio = obtener_municipio_por_proximidad(coord[0], coord[1], waypoints)
                if municipio_actual is None: 
                    municipio_actual = municipio
                    riesgo_actual = riesgo_por_municipio_nombre.get(municipio, 'Desconocido')

                if municipio != municipio_actual:
                    if segmento_actual:
                        segmentos_ruta.append({
                            'coords': segmento_actual, 
                            'municipio': municipio_actual, 
                            'grado_riesgo': riesgo_actual
                        })
                    municipio_actual = municipio
                    riesgo_actual = riesgo_por_municipio_nombre.get(municipio, 'Desconocido')
                    segmento_actual = [coord]
                else:
                    segmento_actual.append(coord)
            
            if segmento_actual:
                segmentos_ruta.append({
                    'coords': segmento_actual, 
                    'municipio': municipio_actual, 
                    'grado_riesgo': riesgo_actual
                })

        except Exception as e:
            print(f"⚠️ Advertencia Ruta: {e}")
            # Si falla el cálculo de ruta, enviamos listas vacías pero NO crasheamos
            segmentos_ruta = []

        map_html = generar_mapa_movil_con_recomendaciones(
            start_point, ong_cercana, segmentos_ruta, waypoints, 
            id_usuario, {'Alto': 'red', 'Medio': 'orange', 'Bajo': 'green', 'Desconocido': 'gray'}, 
            ongs_cercanas, siguiente_recomendacion
        )
        return map_html

    except Exception as e:
        print(f"💥 Error fatal: {e}")
        return f"Error del servidor: {e}", 500


