from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS
import math
import pandas as pd
import osmnx as ox
import networkx as nx
import psycopg2
from geopy.distance import geodesic
import folium
from urllib.parse import urlparse

app = Flask(_name_)
CORS(app)  # Permitir requests desde Android

# --- CONFIGURACIÓN DE BASE DE DATOS ---
uri = 'postgresql://postgres:KAGJhRklTcsevGqKEgCNPfmdDiGzsLyQ@switchyard.proxy.rlwy.net:13155/railway'
result = urlparse(uri)
DB_CONFIG = {
    'user': result.username,
    'password': result.password,
    'host': result.hostname,
    'port': result.port,
    'dbname': result.path.lstrip('/')
}

# --- CARGAR DATOS AL INICIAR EL SERVIDOR ---
print("🔄 Cargando datos iniciales...")

def cargar_datos_iniciales():
    """Carga ONGs y datos de riesgo una vez al iniciar el servidor"""
    # Cargar ONGs
    QUERY_ONG = """
    SELECT o.nom_ong, o.tipo, o.latitud, o.longitud, m.nom_municipio 
    FROM public.ongs o
    JOIN public.municipio m ON o.id_municipio = m.id_municipio;
    """
    
    conn = psycopg2.connect(**DB_CONFIG)
    df_ongs = pd.read_sql(QUERY_ONG, conn)
    
    # Cargar datos de riesgo
    QUERY_FECHA = "SELECT * FROM public.fecha;"
    QUERY_MUNICIPIO = "SELECT * FROM public.municipio;"
    
    df_fecha = pd.read_sql(QUERY_FECHA, conn)
    df_municipio = pd.read_sql(QUERY_MUNICIPIO, conn)
    conn.close()
    
    # Procesar ONGs
    df_ongs.rename(columns={
        'nom_ong': 'name', 'tipo': 'type', 'latitud': 'lat', 
        'longitud': 'lon', 'nom_municipio': 'municipio'
    }, inplace=True)
    
    df_ongs['lat'] = pd.to_numeric(df_ongs['lat'], errors='coerce')
    df_ongs['lon'] = pd.to_numeric(df_ongs['lon'], errors='coerce')
    df_ongs = df_ongs.dropna(subset=['lat', 'lon'])
    
    waypoints = []
    for _, row in df_ongs.iterrows():
        waypoints.append({
            'name': row['name'],
            'type': row['type'],
            'lat': row['lat'],
            'lon': row['lon'],
            'municipio': row['municipio']
        })
    
    # Procesar datos de riesgo
    df_fecha['fecha'] = pd.to_datetime(df_fecha['fecha'])
    ultimo_mes = df_fecha['fecha'].max().month
    ultimo_ano = df_fecha['fecha'].max().year
    
    df_ultimo = df_fecha[(df_fecha['fecha'].dt.month == ultimo_mes) & 
                         (df_fecha['fecha'].dt.year == ultimo_ano)]
    
    riesgo_por_municipio_nombre = {}
    for _, row in df_ultimo.iterrows():
        id_municipio = row['id_municipio']
        municipio_match = df_municipio[df_municipio['id_municipio'] == id_municipio]
        if not municipio_match.empty:
            municipio_nombre = municipio_match['nom_municipio'].iloc[0]
            riesgo_por_municipio_nombre[municipio_nombre] = row['grado']
    
    return waypoints, riesgo_por_municipio_nombre

# Cargar datos al iniciar
waypoints, riesgo_por_municipio_nombre = cargar_datos_iniciales()
colores_riesgo = {'Alto': 'red', 'Medio': 'orange', 'Bajo': 'green', 'Desconocido': 'gray'}

print(f"✅ Servidor listo: {len(waypoints)} ONGs, {len(riesgo_por_municipio_nombre)} municipios con riesgo")

# --- ENDPOINT PRINCIPAL ---
@app.route('/generar_ruta', methods=['POST'])
def generar_ruta():
    """Endpoint que recibe ubicación y devuelve HTML con mapa"""
    try:
        data = request.get_json()
        lat = float(data['lat'])
        lon = float(data['lon'])
        
        print(f"📍 Nueva solicitud: {lat}, {lon}")
        
        # Generar mapa
        html_content = generar_mapa_completo(lat, lon)
        
        return jsonify({
            'status': 'success',
            'html': html_content,
            'message': f'Mapa generado para {lat}, {lon}'
        })
        
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': f'Error: {str(e)}'
        }), 400

# --- LÓGICA PARA GENERAR MAPA ---
def generar_mapa_completo(lat_usuario, lon_usuario):
    """Genera el mapa completo con ruta coloreada"""
    
    start = (lat_usuario, lon_usuario)
    
    # 1. Encontrar ONG más cercana
    def ong_mas_cercana(pos_actual, waypoints):
        ongs = [w for w in waypoints if str(w.get('type', '')).strip().lower() != 'frontera']
        if not ongs:
            return None
        for o in ongs:
            o['distancia'] = geodesic(pos_actual, (o['lat'], o['lon'])).kilometers
        return min(ongs, key=lambda x: x['distancia'])
    
    ong_cercana = ong_mas_cercana(start, waypoints)
    if not ong_cercana:
        return "<h1>No se encontró ONG cercana</h1>"
    
    print(f"🎯 ONG destino: {ong_cercana['name']}")
    
    # 2. Descargar red vial
    dest_point = (ong_cercana['lat'], ong_cercana['lon'])
    distance_km = geodesic(start, dest_point).km
    buffer_m = (distance_km + 0.3) * 1000
    
    G = ox.graph_from_point(start, dist=buffer_m, network_type="drive")
    
    # 3. Calcular ruta
    def haversine_heuristic(u, v, G):
        lat1, lon1 = G.nodes[u]['y'], G.nodes[u]['x']
        lat2, lon2 = G.nodes[v]['y'], G.nodes[v]['x']
        R = 6371000
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        a = math.sin(dphi/2)*2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda/2)*2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
        return R * c
    
    orig_node = ox.distance.nearest_nodes(G, lon_usuario, lat_usuario)
    dest_node = ox.distance.nearest_nodes(G, dest_point[1], dest_point[0])
    
    route = nx.astar_path(
        G, orig_node, dest_node,
        heuristic=lambda u, v: haversine_heuristic(u, v, G),
        weight='length'
    )
    
    route_coords = [(G.nodes[n]['y'], G.nodes[n]['x']) for n in route]
    
    # 4. Segmentar ruta por riesgo
    def obtener_riesgo_por_proximidad(lat, lon):
        min_dist = float('inf')
        riesgo = 'Desconocido'
        for ong in waypoints:
            if ong.get('municipio', 'Desconocido') == 'Desconocido':
                continue
            dist = geodesic((lat, lon), (ong['lat'], ong['lon'])).kilometers
            if dist < min_dist:
                min_dist = dist
                municipio = ong['municipio']
                riesgo = riesgo_por_municipio_nombre.get(municipio, 'Desconocido')
        if min_dist > 50:
            return 'Desconocido'
        return riesgo
    
    segmentos_ruta = []
    segmento_actual = []
    riesgo_actual = None
    
    for i in range(0, len(route_coords), 10):
        if i >= len(route_coords):
            break
        lat, lon = route_coords[i]
        riesgo = obtener_riesgo_por_proximidad(lat, lon)
        
        if riesgo_actual is None:
            riesgo_actual = riesgo
            segmento_actual = [route_coords[i]]
        elif riesgo == riesgo_actual:
            segmento_actual.append(route_coords[i])
        else:
            if segmento_actual:
                segmentos_ruta.append({
                    'coords': segmento_actual,
                    'riesgo': riesgo_actual,
                    'color': colores_riesgo.get(riesgo_actual, 'gray')
                })
            riesgo_actual = riesgo
            segmento_actual = [route_coords[i]]
    
    if segmento_actual:
        segmentos_ruta.append({
            'coords': segmento_actual,
            'riesgo': riesgo_actual,
            'color': colores_riesgo.get(riesgo_actual, 'gray')
        })
    
    # 5. Crear mapa Folium
    m = folium.Map(
        location=start,
        zoom_start=12,
        tiles="CartoDB positron",
        width='100%', 
        height='100vh'
    )
    
    # Dibujar segmentos de ruta coloreados
    for segmento in segmentos_ruta:
        folium.PolyLine(
            segmento['coords'],
            color=segmento['color'],
            weight=8,
            opacity=0.8,
            tooltip=f"Riesgo: {segmento['riesgo']}"
        ).add_to(m)
    
    # Marcador del usuario
    folium.Marker(
        location=start,
        popup="📍 Tu ubicación",
        tooltip="Tu ubicación",
        icon=folium.Icon(color="blue", icon="user")
    ).add_to(m)
    
    # Marcador de la ONG destino
    folium.Marker(
        location=(ong_cercana['lat'], ong_cercana['lon']),
        popup=f"🏠 {ong_cercana['name']}",
        tooltip=f"Destino: {ong_cercana['name']}",
        icon=folium.Icon(color="green", icon="home")
    ).add_to(m)
    
    # Leyenda
    legend_html = '''
    <div style="position: fixed; bottom: 20px; left: 10px; background: white; padding: 10px; border: 2px solid grey; z-index: 9999; font-size: 12px;">
        <p><strong>🎯 Leyenda de Riesgo</strong></p>
        <p><span style="color:red">🔴</span> Alto</p>
        <p><span style="color:orange">🟠</span> Medio</p>
        <p><span style="color:green">🟢</span> Bajo</p>
        <p><span style="color:gray">⚫</span> Desconocido</p>
    </div>
    '''
    m.get_root().html.add_child(folium.Element(legend_html))
    
    return m.get_root().render()

# --- ENDPOINT DE ESTADO ---
@app.route('/status', methods=['GET'])
def status():
    return jsonify({
        'status': 'running',
        'ongs_cargadas': len(waypoints),
        'municipios_riesgo': len(riesgo_por_municipio_nombre)
    })

if _name_ == '_main_':
    app.run(host='0.0.0.0', port=5000, debug=True)
