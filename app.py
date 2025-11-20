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

if DATABASE_URL:
    FINAL_DATABASE_URL = DATABASE_URL
else:
    FINAL_DATABASE_URL = "postgresql://user:password@localhost:5432/mydatabase"
    
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
    
    # Dibujar ruta (si existe)
    for segmento in segmentos_ruta:
        color = colores_riesgo.get(segmento['grado_riesgo'], 'gray')
        folium.PolyLine(segmento['coords'], color=color, weight=5, opacity=0.8).add_to(m)
    
    # Marcador Usuario
    folium.Marker(location=ubicacion_usuario, popup=f"Tu Ubicación", icon=folium.Icon(color="blue", icon="user", prefix="fa")).add_to(m)
    
    # Marcador Destino
    if ong_cercana:
        color = 'red' if ong_cercana.get('type') == 'Frontera' else 'green'
        folium.Marker(location=(ong_cercana['lat'], ong_cercana['lon']), popup=f"Destino: {ong_cercana['name']}", icon=folium.Icon(color=color, icon="home", prefix="fa")).add_to(m)

    # Marcador Recomendación
    if siguiente_recomendacion:
        folium.Marker(location=(siguiente_recomendacion['lat'], siguiente_recomendacion['lon']), popup=f"Recomendación: {siguiente_recomendacion['name']}", icon=folium.Icon(color="orange", icon="star", prefix="fa")).add_to(m)

    return m.get_root().render()

# --- 3. RUTAS ---

@app.route('/')
def index():
    return redirect(url_for('mapa', lat=19.325521, lon=-99.167807, id_usuario=1))

@app.route('/login', methods=['POST'])
def login():
    id_usuario = request.form.get('id_usuario', default=1, type=int)
    return redirect(url_for('mapa', lat=19.325521, lon=-99.167807, id_usuario=id_usuario))

@app.route('/test-db')
def test_db_connection():
    try:
        query = text("SELECT COUNT(*) AS total_ongs FROM public.ongs;")
        df = conectar_y_leer_sql(query) 
        if df.empty: return jsonify({"status": "ERROR", "message": "Tabla vacía"}), 500
        return jsonify({"status": "OK", "total_ongs": int(df.iloc[0,0])}), 200
    except Exception as e:
        return jsonify({"status": "ERROR", "details": str(e)}), 500

@app.route('/mapa')
def mapa():
    try:
        lat = request.args.get('lat', type=float)
        lon = request.args.get('lon', type=float)
        id_usuario = request.args.get('id_usuario', default=1, type=int)

        if lat is None or lon is None: return "Faltan coordenadas", 400
        start_point = (lat, lon)
        
        waypoints = cargar_waypoints_ongs()
        riesgo_por_municipio_nombre = cargar_datos_riesgo()
        
        if not waypoints: return "Error: Base de datos vacía o sin conexión.", 500

        ong_cercana = ong_mas_cercana(start_point, waypoints)
        if not ong_cercana: return "No se encontraron ONGs.", 500

        ongs_ordenadas = find_sorted_ongs(start_point, waypoints)
        siguiente_recomendacion = ongs_ordenadas[1] if len(ongs_ordenadas) > 1 else None
        ongs_cercanas = ongs_ordenadas[:5]

        segmentos_ruta = []
        
        # --- LÓGICA DE PROTECCIÓN DE MEMORIA ---
        try:
            dest_point = (ong_cercana['lat'], ong_cercana['lon'])
            distancia_km = geodesic(start_point, dest_point).km
            
            # LÍMITE: Si es mayor a 5 KM, NO calculamos ruta compleja (ahorra RAM)
            if distancia_km > 5:
                print(f"⚠️ Distancia larga ({distancia_km:.2f}km). Usando línea recta para ahorrar memoria.")
                # Dibujamos una línea recta simple
                segmentos_ruta.append({
                    'coords': [start_point, dest_point],
                    'municipio': 'Ruta Directa',
                    'grado_riesgo': 'Desconocido'
                })
            else:
                # Si es corta, usamos OSMnx con padding muy pequeño (200m)
                padding = 0.002 
                G = ox.graph_from_bbox(
                    max(lat, dest_point[0]) + padding, min(lat, dest_point[0]) - padding, 
                    max(lon, dest_point[1]) + padding, min(lon, dest_point[1]) - padding, 
                    network_type="drive"
                )
                orig_node = ox.distance.nearest_nodes(G, lon, lat)
                dest_node = ox.distance.nearest_nodes(G, dest_point[1], dest_point[0])
                route = nx.shortest_path(G, orig_node, dest_node, weight='length')
                route_coords = [(G.nodes[n]['y'], G.nodes[n]['x']) for n in route]
                
                segmentos_ruta.append({
                    'coords': route_coords,
                    'municipio': 'Calculado',
                    'grado_riesgo': 'Desconocido' # Simplificado para evitar errores
                })

        except Exception as e:
            print(f"⚠️ Error cálculo ruta: {e}")
            # Fallback: línea recta si falla el cálculo
            segmentos_ruta.append({
                'coords': [start_point, (ong_cercana['lat'], ong_cercana['lon'])],
                'municipio': 'Error Ruta',
                'grado_riesgo': 'Desconocido'
            })

        return generar_mapa_movil_con_recomendaciones(
            start_point, ong_cercana, segmentos_ruta, waypoints, 
            id_usuario, {'Alto': 'red', 'Medio': 'orange', 'Bajo': 'green', 'Desconocido': 'gray'}, 
            ongs_cercanas, siguiente_recomendacion
        )

    except Exception as e:
        print(f"💥 Error fatal: {e}")
        return f"Error del servidor: {e}", 500
