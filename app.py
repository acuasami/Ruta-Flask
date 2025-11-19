from flask import Flask, request, render_template_string
import os
import math
import pandas as pd
import osmnx as ox
import networkx as nx
import re
import psycopg2
from geopy.distance import geodesic
import folium
from shapely.geometry import Point, LineString
import geopandas as gpd
import numpy as np
from networkx.exception import NetworkXNoPath
import sys

app = Flask(__name__)

# --- CONFIGURACIÓN DE BASE DE DATOS (SEGURA PARA RAILWAY) ---
DATABASE_URL = os.environ.get('DATABASE_URL')

# Configuración de respaldo (solo si no hay DATABASE_URL)
DB_CONFIG = {
    'host': os.environ.get('PGHOST'),
    'port': os.environ.get('PGPORT', 5432),
    'dbname': os.environ.get('PGDATABASE'),
    'user': os.environ.get('PGUSER'),
    'password': os.environ.get('PGPASSWORD')
}

# --- LÓGICA DE CONEXIÓN ---

def conectar_y_leer_sql(query):
    """
    Conecta a la BD, ejecuta una consulta y devuelve un DataFrame.
    Maneja SSL para Railway.
    """
    conn = None
    try:
        # Prioridad 1: Usar la URL completa (DSN) si existe
        if DATABASE_URL:
            print("🔗 Conectando con DATABASE_URL...")
            conn = psycopg2.connect(DATABASE_URL)
        
        # Prioridad 2: Usar variables individuales
        else:
            print("🔗 Conectando con variables individuales...")
            # Validar credenciales mínimas
            if not all([DB_CONFIG['host'], DB_CONFIG['user'], DB_CONFIG['password']]):
                 print("⚠️ Faltan credenciales de BD. Verifica las variables en Railway.")
                 return pd.DataFrame()

            # ✅ FORZAR SSL para Railway
            conn_params = DB_CONFIG.copy()
            conn_params['sslmode'] = 'require'
            
            conn = psycopg2.connect(**conn_params)

        # Leer datos
        df = pd.read_sql(query, conn)
        return df
        
    except Exception as e:
        print(f"❌ Error de Base de Datos: {e}")
        return pd.DataFrame()
        
    finally:
        if conn:
            conn.close()

# --- FUNCIONES DE LÓGICA GEOESPACIAL (RESTAURADAS) ---

def cargar_waypoints_ongs():
    QUERY_ONG = """
    SELECT o.nom_ong, o.tipo, o.latitud, o.longitud, m.nom_municipio 
    FROM public.ongs o
    JOIN public.municipio m ON o.id_municipio = m.id_municipio;
    """
    df_ongs = conectar_y_leer_sql(QUERY_ONG)
    
    if df_ongs.empty:
        print("⚠️ Tabla de ONGs vacía o error de conexión.")
        return []
        
    df_ongs.rename(columns={
        'nom_ong': 'name', 'tipo': 'type', 'latitud': 'lat', 
        'longitud': 'lon', 'nom_municipio': 'municipio'
    }, inplace=True)
    
    df_ongs['lat'] = pd.to_numeric(df_ongs['lat'], errors='coerce')
    df_ongs['lon'] = pd.to_numeric(df_ongs['lon'], errors='coerce')
    df_ongs = df_ongs.dropna(subset=['lat', 'lon'])
    
    return [row.to_dict() for _, row in df_ongs.iterrows()]

def cargar_datos_riesgo():
    df_fecha = conectar_y_leer_sql("SELECT * FROM public.fecha;")
    df_municipio = conectar_y_leer_sql("SELECT * FROM public.municipio;")

    if df_fecha.empty or df_municipio.empty:
        return {}

    df_fecha['fecha'] = pd.to_datetime(df_fecha['fecha'])
    if df_fecha.empty:
        return {}
        
    ultimo_mes = df_fecha['fecha'].max().month
    ultimo_ano = df_fecha['fecha'].max().year
    
    df_ultimo = df_fecha[(df_fecha['fecha'].dt.month == ultimo_mes) &
                         (df_fecha['fecha'].dt.year == ultimo_ano)]
    
    df_riesgo_completo = pd.merge(df_ultimo, df_municipio, on='id_municipio')
    return dict(zip(df_riesgo_completo['nom_municipio'], df_riesgo_completo['grado']))

def ong_mas_cercana(pos_actual, waypoints):
    ongs = [w for w in waypoints if str(w.get('type', '')).strip().lower() != 'frontera']
    if not ongs:
        return None
    for o in ongs:
        o['distancia'] = geodesic(pos_actual, (o['lat'], o['lon'])).kilometers
    return min(ongs, key=lambda x: x['distancia'])

def find_sorted_ongs(start, waypoints_list):
    candidates = []
    for ong in waypoints_list:
        if str(ong.get('type', '')).strip().lower() != 'frontera':
            ong_point = (ong["lat"], ong["lon"])
            dist = geodesic(start, ong_point).km
            ong['distancia'] = dist
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
    m = folium.Map(
        location=ubicacion_usuario,
        zoom_start=13,
        tiles="CartoDB positron",
        width='100%', 
        height='100vh' 
    )
    
    m.options['touchZoom'] = True
    m.options['dragging'] = True
    m.options['scrollWheelZoom'] = False
    
    print("🎨 Dibujando ruta con colores de riesgo...")
    for i, segmento in enumerate(segmentos_ruta):
        color = colores_riesgo.get(segmento['grado_riesgo'], 'gray')
        folium.PolyLine(
            segmento['coords'],
            color=color,
            weight=8,
            opacity=0.9,
            tooltip=f"🏙️ {segmento['municipio']} | 🎯 Riesgo: {segmento['grado_riesgo']}"
        ).add_to(m)
    
    folium.Marker(
        location=ubicacion_usuario,
        popup=f"Tu Ubicación (ID: {id_usuario})",
        icon=folium.Icon(color="blue", icon="user", prefix="fa")
    ).add_to(m)
    
    if ong_cercana:
        tipo_icono = {'Albergue': 'bed', 'Comedor': 'utensils', 'Frontera': 'flag', 'default': 'home'}
        icono = tipo_icono.get(ong_cercana.get('type', ''), tipo_icono['default'])
        
        folium.Marker(
            location=(ong_cercana['lat'], ong_cercana['lon']),
            popup=f"Destino: {ong_cercana['name']}",
            icon=folium.Icon(color="green", icon=icono, prefix="fa")
        ).add_to(m)
    
    if siguiente_recomendacion:
        folium.Marker(
            location=(siguiente_recomendacion['lat'], siguiente_recomendacion['lon']),
            popup=f"Recomendación: {siguiente_recomendacion['name']}",
            icon=folium.Icon(color="orange", icon="star", prefix="fa")
        ).add_to(m)
    
    for ong in waypoints:
        if ong_cercana and ong['name'] == ong_cercana.get('name'): continue
        if siguiente_recomendacion and ong['name'] == siguiente_recomendacion.get('name'): continue
        
        color_ong = {'Albergue': 'lightblue', 'Comedor': 'orange', 'Frontera': 'red', 'default': 'gray'}
        color = color_ong.get(ong.get('type', ''), color_ong['default'])
        
        folium.CircleMarker(
            location=(ong['lat'], ong['lon']),
            radius=6,
            popup=f"{ong['name']} ({ong.get('type')})",
            color=color,
            fill=True,
            fillColor=color,
            fillOpacity=0.7
        ).add_to(m)
        
    return m.get_root().render()

# --- RUTAS FLASK ---

@app.route('/')
def index():
    return "Servidor de Mapas Activo (Docker + Gunicorn). Usa la app móvil."

@app.route('/health')
def health():
    return "OK"

@app.route('/mapa')
def serve_map():
    try:
        lat = request.args.get('lat', type=float)
        lon = request.args.get('lon', type=float)
        id_usuario = request.args.get('id_usuario', default=1, type=int)
        
        if lat is None or lon is None:
            return "Faltan coordenadas (lat, lon)", 400
            
        start_point = (lat, lon)
        print(f"🚀 Petición recibida: {start_point}")

        waypoints = cargar_waypoints_ongs()
        riesgo_por_municipio_nombre = cargar_datos_riesgo()
        colores_riesgo = {'Alto': 'red', 'Medio': 'orange', 'Bajo': 'green', 'Desconocido': 'gray'}

        if not waypoints:
            return "Error: No hay ONGs en la base de datos.", 500
        
        ong_cercana = ong_mas_cercana(start_point, waypoints)
        if not ong_cercana:
            return "Error: No se encontró ninguna ONG destino.", 500

        ongs_ordenadas = find_sorted_ongs(start_point, waypoints)
        siguiente_recomendacion = ongs_ordenadas[1] if len(ongs_ordenadas) > 1 else (ongs_ordenadas[0] if ongs_ordenadas else None)
        ongs_cercanas = ongs_ordenadas[:5]

        # CÁLCULO DE RUTA
        segmentos_ruta = []
        try:
            dest_point = (ong_cercana['lat'], ong_cercana['lon'])
            padding = 0.02 # Margen para descargar grafo
            
            print("🗺️ Descargando grafo OSMnx...")
            G = ox.graph
