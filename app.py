from flask import Flask, request, render_template, url_for, redirect, jsonify
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

# --- 1. CONFIGURACIÓN DE BASE DE DATOS ---
DATABASE_URL = os.environ.get("DATABASE_URL")

# Configuración de conexión a prueba de fallos para Railway
if DATABASE_URL and DATABASE_URL.startswith("postgresql://"):
    base_url_only = DATABASE_URL.split('?')[0]
    FINAL_DATABASE_URL = base_url_only + "?sslmode=require"
elif not DATABASE_URL:
    # Fallback solo para desarrollo local
    FINAL_DATABASE_URL = "postgresql://user:password@localhost:5432/mydatabase"
else:
    FINAL_DATABASE_URL = DATABASE_URL

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
    # CORRECCIÓN: Usamos 'id_municipio' (singular) que es el estándar generado por tu otro script.
    # Si tu base de datos REALMENTE tiene 'id_municipios' (plural), cambia la línea del JOIN abajo.
    query = text("""
        SELECT o.nom_ong, o.tipo, o.latitud, o.longitud, m.nom_municipio 
        FROM public.ongs o
        JOIN public.municipio m ON o.id_municipio = m.id_municipio;
    """)
    df = conectar_y_leer_sql(query)
    
    if df.empty: 
        print("⚠️ No se encontraron ONGs o la tabla está vacía.")
        return []

    # Normalizar nombres de columnas para el uso interno de la app
    df.rename(columns={
        'nom_ong': 'name', 
        'tipo': 'type', 
        'latitud': 'lat', 
        'longitud': 'lon', 
        'nom_municipio': 'municipio'
    }, inplace=True)
    
    # Limpiar datos numéricos
    df['lat'] = pd.to_numeric(df['lat'], errors='coerce')
    df['lon'] = pd.to_numeric(df['lon'], errors='coerce')
    df = df.dropna(subset=['lat', 'lon'])
    
    return [row.to_dict() for _, row in df.iterrows()]

def cargar_datos_riesgo():
    # Cargamos datos de riesgo si existen
    df_fecha = conectar_y_leer_sql(text("SELECT * FROM public.fecha;"))
    df_municipio = conectar_y_leer_sql(text("SELECT * FROM public.municipio;"))

    if df_fecha.empty or df_municipio.empty:
        return {}

    df_fecha['fecha'] = pd.to_datetime(df_fecha['fecha'])
    if df_fecha.empty: return {}
        
    ultimo_mes = df_fecha['fecha'].max().month
    ultimo_ano = df_fecha['fecha'].max().year
    
    df_ultimo = df_fecha[(df_fecha['fecha'].dt.month == ultimo_mes) &
                         (df_fecha['fecha'].dt.year == ultimo_ano)]
    
    # JOIN para obtener nombre del municipio
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
    m = folium.Map(
        location=ubicacion_usuario, 
        zoom_start=13, 
        tiles="CartoDB positron", 
        width='100%', 
        height='100vh'
    )
    
    # Dibujar ruta
    for segmento in segmentos_ruta:
        color = colores_riesgo.get(segmento['grado_riesgo'], 'gray')
        folium.PolyLine(
            segmento['coords'], 
            color=color, 
            weight=8, 
            opacity=0.9,
            tooltip=f"Riesgo: {segmento['grado_riesgo']}"
        ).add_to(m)
    
    # Marcador Usuario
    folium.Marker(
        location=ubicacion_usuario, 
        popup=f"Tu Ubicación", 
        icon=folium.Icon(color="blue", icon="user", prefix="fa")
    ).add_to(m)
    
    # Marcador Destino
    if ong_cercana:
        tipo = ong_cercana.get('type', 'default')
        color = 'red' if tipo == 'Frontera' else 'green'
        folium.Marker(
            location=(ong_cercana['lat'], ong_cercana['lon']), 
            popup=f"Destino: {ong_cercana['name']}", 
            icon=folium.Icon(color=color, icon="home", prefix="fa")
        ).add_to(m)

    # Marcador Recomendación
    if siguiente_recomendacion:
        folium.Marker(
            location=(siguiente_recomendacion['lat'], siguiente_recomendacion['lon']), 
            popup=f"Recomendación: {siguiente_recomendacion['name']}", 
            icon=folium.Icon(color="orange", icon="star", prefix="fa")
        ).add_to(m)
        
    # Otros marcadores cercanos
    for ong in ongs_cercanas:
        if ong['name'] == ong_cercana.get('name') or (siguiente_recomendacion and ong['name'] == siguiente_recomendacion.get('name')):
            continue
        folium.CircleMarker(
            location=(ong['lat'], ong['lon']),
            radius=5,
            popup=ong['name'],
            color="gray",
            fill=True,
            fillOpacity=0.7
        ).add_to(m)

    return m.get_root().render()

# --- 3. RUTAS DE LA APLICACIÓN ---

@app.route('/')
def index():
    return render_template('login.html')

@app.route('/login', methods=['POST'])
def login():
    usuario = request.form.get('usuario')
    password_input = request.form.get('password') # Renombrado para evitar confusión

    if not usuario or not password_input:
        return jsonify({"error": "Datos incompletos"}), 400

    try:
        with engine.connect() as connection:
            # ✅ CORRECCIÓN: Usamos los nombres de campo que me diste:
            # Tabla: usuario
            # Columnas: nombre_usuario, contraseña (usé contraseña con ñ que es el estándar de tu otro script)
            # Si tu columna realmente es 'constraseña' (con s), cambia la palabra abajo.
            query = text("""
                SELECT id_usuario 
                FROM usuario 
                WHERE nombre_usuario = :usuario AND contraseña = :password
            """)
            result = connection.execute(query, {"usuario": usuario, "password": password_input}).fetchone()

            if result:
                return redirect(url_for('mapa', id_usuario=result[0]))
            else:
                return render_template('login.html', error="Usuario o contraseña incorrectos")

    except Exception as e:
        print(f"💥 Error en Login: {e}")
        return jsonify({"error_code": "DB_ERROR", "message": str(e)}), 500

@app.route('/mapa')
def mapa():
    try:
        lat = request.args.get('lat', type=float)
        lon = request.args.get('lon', type=float)
        id_usuario = request.args.get('id_usuario', default=1, type=int)

        if lat is None or lon is None: 
            return "Error: Faltan coordenadas GPS.", 400

        start_point = (lat, lon)
        
        # Cargar datos de la BD
        waypoints = cargar_waypoints_ongs()
        riesgo_por_municipio_nombre = cargar_datos_riesgo()
        
        if not waypoints: 
            return "Error: No se pudieron cargar las ONGs. Verifica la base de datos.", 500

        # Lógica de negocio
        ong_cercana = ong_mas_cercana(start_point, waypoints)
        if not ong_cercana: 
            return "Error: No hay ONGs cercanas disponibles.", 500

        ongs_ordenadas = find_sorted_ongs(start_point, waypoints)
        siguiente_recomendacion = ongs_ordenadas[1] if len(ongs_ordenadas) > 1 else None
        ongs_cercanas = ongs_ordenadas[:5]

        # Cálculo de Ruta
        segmentos_ruta = []
        try:
            dest_point = (ong_cercana['lat'], ong_cercana['lon'])
            padding = 0.02 
            
            # Descarga de grafo
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
            
            # Segmentación simple por riesgo
            segmento_actual = []
            municipio_actual = None
            
            for coord in route_coords:
                municipio = obtener_municipio_por_proximidad(coord[0], coord[1], waypoints)
                riesgo = riesgo_por_municipio_nombre.get(municipio, 'Desconocido')
                
                if municipio_actual is None: municipio_actual = municipio
                
                if municipio == municipio_actual:
                    segmento_actual.append(coord)
                else:
