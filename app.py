from flask import Flask, render_template, request, url_for, redirect, jsonify
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
import sys

app = Flask(__name__)

# --- 1. CONFIGURACIÓN DE BASE DE DATOS (SEGURA Y DEFINITIVA) ---
DATABASE_URL = os.environ.get("DATABASE_URL")

# Forzar sslmode=require para Railway
if DATABASE_URL and DATABASE_URL.startswith("postgresql://"):
    base_url_only = DATABASE_URL.split('?')[0]
    FINAL_DATABASE_URL = base_url_only + "?sslmode=require"
elif not DATABASE_URL:
    # Fallback local (solo para desarrollo en tu PC)
    FINAL_DATABASE_URL = "postgresql://user:password@localhost:5432/mydatabase"
else:
    FINAL_DATABASE_URL = DATABASE_URL

# Crear el motor de conexión único
engine = create_engine(FINAL_DATABASE_URL)


# --- 2. FUNCIONES DE LÓGICA (Recuperadas y Corregidas) ---

def cargar_waypoints_ongs():
    """Carga datos de la BD usando el motor SQLAlchemy"""
    try:
        with engine.connect() as conn:
            query = text("""
                SELECT o.nom_ong, o.tipo, o.latitud, o.longitud, m.nom_municipio 
                FROM public.ongs o
                JOIN public.municipio m ON o.id_municipio = m.id_municipio;
            """)
            df = pd.read_sql(query, conn)
            
            # Normalizar nombres
            df.rename(columns={
                'nom_ong': 'name', 'tipo': 'type', 'latitud': 'lat', 
                'longitud': 'lon', 'nom_municipio': 'municipio'
            }, inplace=True)
            
            # Limpiar datos
            df['lat'] = pd.to_numeric(df['lat'], errors='coerce')
            df['lon'] = pd.to_numeric(df['lon'], errors='coerce')
            df = df.dropna(subset=['lat', 'lon'])
            
            return [row.to_dict() for _, row in df.iterrows()]
    except Exception as e:
        print(f"⚠️ Error cargando ONGs: {e}")
        return []

def ong_mas_cercana(pos_actual, waypoints):
    ongs = [w for w in waypoints if str(w.get('type', '')).strip().lower() != 'frontera']
    if not ongs: return None
    for o in ongs:
        o['distancia'] = geodesic(pos_actual, (o['lat'], o['lon'])).kilometers
    return min(ongs, key=lambda x: x['distancia'])

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

# --- 3. RUTAS DE LA APLICACIÓN ---

@app.route('/')
def index():
    return render_template('login.html')

@app.route('/login', methods=['POST'])
def login():
    usuario = request.form['usuario']
    password = request.form['password']

    try:
        with engine.connect() as connection:
            query = text("SELECT id_usuario FROM usuarios WHERE usuario = :usuario AND password = :password")
            result = connection.execute(query, {"usuario": usuario, "password": password}).fetchone()

            if result:
                return redirect(url_for('mapa', id_usuario=result[0]))
            else:
                return render_template('login.html', error="Usuario o contraseña incorrectos")
    except Exception as e:
        print(f"Error BD: {e}")
        return jsonify({"error": "Fallo en base de datos", "detalle": str(e)}), 500

@app.route('/mapa')
def mapa():
    # 1. Obtener parámetros
    lat = request.args.get('lat', type=float)
    lon = request.args.get('lon', type=float)
    id_usuario = request.args.get('id_usuario', type=int)

    if lat is None or lon is None:
        return "Faltan coordenadas GPS.", 400

    start_point = (lat, lon)
    waypoints = cargar_waypoints_ongs()
    
    if not waypoints:
        return "Error: No hay ONGs cargadas en la base de datos.", 500

    # 2. Calcular ONG más cercana
    ong_cercana = ong_mas_cercana(start_point, waypoints)
    if not ong_cercana:
        return "No se encontraron ONGs cercanas.", 500

    # 3. Calcular Ruta con OSMnx (Aquí estaba el error de traducción)
    segmentos_ruta = []
    try:
        dest_point = (ong_cercana['lat'], ong_cercana['lon'])
        padding = 0.02 
        
        print("🗺️ Descargando grafo OSMnx...")
        
        # CORRECCIÓN IMPORTANTE: Usar graph_from_bbox (NO 'gráfico')
        G = ox.graph_from_bbox(
            max(lat, dest_point[0]) + padding, 
            min(lat, dest_point[0]) - padding, 
            max(lon, dest_point[1]) + padding, 
            min(lon, dest_point[1]) - padding, 
            network_type="drive"
        )
        
        # Asegurar longitudes para evitar errores de NetworkX
        G = ox.distance.add_edge_lengths(G)
        
        orig_node = ox.distance.nearest_nodes(G, lon, lat)
        dest_node = ox.distance.nearest_nodes(G, dest_point[1], dest_point[0])
        
        route = nx.astar_path(G, orig_node, dest_node, weight='length')
        route_coords = [(G.nodes[n]['y'], G.nodes[n]['x']) for n in route]
        
        # Guardamos la ruta para dibujarla
        segmentos_ruta.append({'coords': route_coords, 'color': 'blue'})

    except Exception as e:
        print(f"⚠️ Advertencia: No se pudo calcular la ruta callejera: {e}")
        # Si falla la ruta, la lista queda vacía pero el mapa se carga igual con marcadores

    # 4. Generar Mapa
    m = folium.Map(location=[lat, lon], zoom_start=14, tiles="cartodbpositron")
    
    # Ruta
    for seg in segmentos_ruta:
        folium.PolyLine(seg['coords'], color=seg['color'], weight=5).add_to(m)

    # Usuario
    folium.Marker([lat, lon], popup="Tú", icon=folium.Icon(color="blue", icon="user")).add_to(m)
    
    # Destino
    folium.Marker(
        [ong_cercana['lat'], ong_cercana['lon']], 
        popup=f"Destino: {ong_cercana['name']}", 
        icon=folium.Icon(color="green", icon="home")
    ).add_to(m)

    # Renderizar
    data = io.BytesIO()
    m.save(data, close_file=False)
    return render_template('ruta_movil_1.html', mapa_html=data.getvalue().decode('utf-8'))

# Nota: Gunicorn ejecutará 'app:app', no este bloque main.
