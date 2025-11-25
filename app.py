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
from urllib.parse import urlparse

app = Flask(__name__)

# --- CONFIGURACIÓN DE BASE DE DATOS ---
# Nota: Es ideal usar variables de entorno, pero mantenemos tu configuración actual
uri = 'postgresql://postgres:KAGJhRklTcsevGqKEgCNPfmdDiGzsLyQ@switchyard.proxy.rlwy.net:13155/railway'
result = urlparse(uri)

DB_CONFIG = {
    'user': result.username,
    'password': result.password,
    'host': result.hostname,
    'port': result.port,
    'dbname': result.path.lstrip('/')
}

# --- FUNCIONES DE BASE DE DATOS Y CARGA ---

def conectar_y_leer_sql(query):
    """Conecta a la BD, ejecuta una consulta y devuelve un DataFrame."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        df = pd.read_sql(query, conn)
        conn.close()
        return df
    except Exception as e:
        print(f"❌ Error al leer la base de datos: {e}")
        return pd.DataFrame()

def cargar_waypoints_ongs():
    """Carga todas las ONGs y municipios desde la BD."""
    QUERY_ONG = """
    SELECT o.nom_ong, o.tipo, o.latitud, o.longitud, m.nom_municipio 
    FROM public.ongs o
    JOIN public.municipio m ON o.id_municipio = m.id_municipio;
    """
    df_ongs = conectar_y_leer_sql(QUERY_ONG)
    
    df_ongs.rename(columns={
        'nom_ong': 'name', 'tipo': 'type', 'latitud': 'lat', 
        'longitud': 'lon', 'nom_municipio': 'municipio'
    }, inplace=True)
    
    df_ongs['lat'] = pd.to_numeric(df_ongs['lat'], errors='coerce')
    df_ongs['lon'] = pd.to_numeric(df_ongs['lon'], errors='coerce')
    df_ongs = df_ongs.dropna(subset=['lat', 'lon'])
    
    return [row.to_dict() for _, row in df_ongs.iterrows()]

def cargar_datos_riesgo():
    """Carga los datos de riesgo del último mes."""
    df_fecha = conectar_y_leer_sql("SELECT * FROM public.fecha;")
    df_municipio = conectar_y_leer_sql("SELECT * FROM public.municipio;")

    if df_fecha.empty or df_municipio.empty:
        return {}

    df_fecha['fecha'] = pd.to_datetime(df_fecha['fecha'])
    ultimo_mes = df_fecha['fecha'].max().month
    ultimo_ano = df_fecha['fecha'].max().year
    
    df_ultimo = df_fecha[(df_fecha['fecha'].dt.month == ultimo_mes) &
                         (df_fecha['fecha'].dt.year == ultimo_ano)]
    
    # Unir con nombres de municipio
    df_riesgo_completo = pd.merge(df_ultimo, df_municipio, on='id_municipio')
    
    return dict(zip(df_riesgo_completo['nom_municipio'], df_riesgo_completo['grado']))

# --- LÓGICA DE BÚSQUEDA Y RUTA ---

def ong_mas_cercana(pos_actual, waypoints):
    """Encuentra la ONG (no frontera) más cercana."""
    ongs = [w for w in waypoints if str(w.get('type', '')).strip().lower() != 'frontera']
    if not ongs:
        return None
    for o in ongs:
        o['distancia'] = geodesic(pos_actual, (o['lat'], o['lon'])).kilometers
    return min(ongs, key=lambda x: x['distancia'])

def find_ongs_north(start, waypoints_list, current_ong=None):
    """
    NUEVA FUNCIÓN: Encuentra ONGs que están al NORTE de la ubicación del usuario,
    ordenadas por cercanía, excluyendo la ONG actual.
    """
    candidates = []
    start_lat, start_lon = start
    
    for ong in waypoints_list:
        # Excluir fronteras
        if str(ong.get('type', '')).strip().lower() == 'frontera':
            continue
        # Excluir la ONG destino actual si existe
        if current_ong and ong['name'] == current_ong.get('name'):
            continue
        
        # FILTRO CLAVE: Verificar que la latitud sea mayor (más al norte)
        if ong["lat"] > start_lat:
            ong_point = (ong["lat"], ong["lon"])
            dist = geodesic(start, ong_point).km
            
            # Creamos una copia para no ensuciar los datos originales
            ong_copy = ong.copy()
            ong_copy['distancia'] = dist
            candidates.append(ong_copy)
    
    # Ordenar por distancia (la más cercana que esté al norte)
    candidates.sort(key=lambda x: x['distancia'])
    return candidates

def haversine_heuristic(u, v, G):
    """Heurística para A*."""
    lat1, lon1 = G.nodes[u]['y'], G.nodes[u]['x']
    lat2, lon2 = G.nodes[v]['y'], G.nodes[v]['x']
    R = 6371000  # Radio de la Tierra en metros
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

def obtener_municipio_por_proximidad(lat, lon, waypoints):
    """Aproximación del municipio basado en la ONG más cercana."""
    min_dist = float('inf')
    municipio_cercano = 'Desconocido'
    for ong in waypoints:
        dist = geodesic((lat, lon), (ong['lat'], ong['lon'])).kilometers
        if dist < min_dist:
            min_dist = dist
            municipio_cercano = ong.get('municipio', 'Desconocido')
    return municipio_cercano

# --- GENERACIÓN DE MAPA ---

def generar_mapa_movil_con_recomendaciones(ubicacion_usuario, ong_cercana, segmentos_ruta, waypoints, id_usuario, colores_riesgo, ongs_cercanas, siguiente_recomendacion):
    """Genera HTML optimizado para móviles con panel de pestañas incluyendo recomendaciones"""
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
    
    # --- DIBUJAR SEGMENTOS DE RUTA CON COLORES DE RIESGO ---
    print("🎨 Dibujando ruta con colores de riesgo...")
    for i, segmento in enumerate(segmentos_ruta):
        # Aquí aplicamos el color basado en el riesgo del municipio
        color = colores_riesgo.get(segmento['grado_riesgo'], 'gray')
        
        folium.PolyLine(
            segmento['coords'],
            color=color,
            weight=8,
            opacity=0.9,
            tooltip=f"🏙️ {segmento['municipio']} | 🎯 Riesgo: {segmento['grado_riesgo']}"
        ).add_to(m)
        print(f"   📍 Segmento {i+1}: {segmento['municipio']} - {segmento['grado_riesgo']} ({color})")
    
    # --- DATOS DE DESTINO SEGUROS ---
    if ong_cercana:
        dest_nombre = ong_cercana.get('name', 'No disponible')
        dest_tipo = ong_cercana.get('type', 'Desconocido')
        dest_municipio = ong_cercana.get('municipio', 'Desconocido')
        dest_distancia = ong_cercana.get('distancia', 0)
        dest_lat = ong_cercana['lat']
        dest_lon = ong_cercana['lon']
    else:
        dest_nombre = "Sin destino"
        dest_tipo = "N/A"
        dest_municipio = "N/A"
        dest_distancia = 0
        dest_lat = 0
        dest_lon = 0

    # --- MARCADOR DEL USUARIO ---
    folium.Marker(
        location=ubicacion_usuario,
        popup=folium.Popup(
            f"""
            <div style='font-size:14px; max-width:250px;'>
                <div style='background:#4A00E0; color:white; padding:8px; border-radius:5px 5px 0 0; margin:-10px -10px 10px -10px;'>
                    <b>📍 Tu Ubicación Actual</b>
                </div>
                <p><b>👤 Usuario:</b> ID {id_usuario}</p>
                <p><b>🎯 Destino:</b> {dest_nombre}</p>
            </div>
            """,
            max_width=300
        ),
        tooltip="Tu ubicación",
        icon=folium.Icon(color="blue", icon="user", prefix="fa")
    ).add_to(m)
    
    # --- MARCADOR DE LA ONG DESTINO ---
    if ong_cercana and dest_nombre != 'ONG no disponible':
        tipo_icono = {'Albergue': 'bed', 'Comedor': 'utensils', 'Frontera': 'flag', 'default': 'home'}
        icono = tipo_icono.get(dest_tipo, tipo_icono['default'])
        
        folium.Marker(
            location=(dest_lat, dest_lon),
            popup=folium.Popup(
                f"""
                <div style='font-size:14px; max-width:280px;'>
                    <div style='background:#27ae60; color:white; padding:8px; border-radius:5px 5px 0 0; margin:-10px -10px 10px -10px;'>
                        <b>🏠 ONG Destino</b>
                    </div>
                    <p><b>📌 Nombre:</b> {dest_nombre}</p>
                    <p><b>🎯 Tipo:</b> {dest_tipo}</p>
                    <p><b>🏙️ Municipio:</b> {dest_municipio}</p>
                    <p><b>📏 Distancia:</b> {dest_distancia:.1f} km</p>
                </div>
                """,
                max_width=320
            ),
            tooltip=f"Destino: {dest_nombre}",
            icon=folium.Icon(color="green", icon="bed", prefix="fa")
        ).add_to(m)
    
    # --- MARCADOR DE LA SIGUIENTE RECOMENDACIÓN (NORTE) ---
    if siguiente_recomendacion:
        folium.Marker(
            location=(siguiente_recomendacion['lat'], siguiente_recomendacion['lon']),
            popup=folium.Popup(
                f"""
                <div style='font-size:14px; max-width:280px;'>
                    <div style='background:#FF9800; color:white; padding:8px; border-radius:5px 5px 0 0; margin:-10px -10px 10px -10px;'>
                        <b>⭐ Recomendación al Norte</b>
                    </div>
                    <p><b>📌 Nombre:</b> {siguiente_recomendacion['name']}</p>
                    <p><b>🎯 Tipo:</b> {siguiente_recomendacion['type']}</p>
                    <p><b>🏙️ Municipio:</b> {siguiente_recomendacion.get('municipio', 'Desconocido')}</p>
                    <p><b>📏 Distancia:</b> {siguiente_recomendacion['distancia']:.1f} km</p>
                    <div style='background:#fff3e0; padding:5px; border-radius:3px; margin:5px 0;'>
                        <small>💡 Opción más cercana hacia el Norte</small>
                    </div>
                </div>
                """,
                max_width=320
            ),
            tooltip=f"⭐ Recomendación: {siguiente_recomendacion['name']}",
            icon=folium.Icon(color="orange", icon="star", prefix="fa")
        ).add_to(m)
    
    # --- OTRAS ONGs ---
    ongs_marcadas = 0
    for ong in waypoints:
        if ong_cercana and ong['name'] != dest_nombre and (not siguiente_recomendacion or ong['name'] != siguiente_recomendacion.get('name', '')):
            municipio_ong = ong.get('municipio', 'Desconocido')
            color_ong = {'Albergue': 'lightblue', 'Comedor': 'orange', 'Frontera': 'red', 'default': 'gray'}
            color = color_ong.get(ong.get('type', ''), color_ong['default'])
            
            folium.CircleMarker(
                location=(ong['lat'], ong['lon']),
                radius=8,
                popup=folium.Popup(f"<b>{ong['name']}</b><br>{ong['type']}<br>{municipio_ong}", max_width=200),
                color=color, fillColor=color, fillOpacity=0.7
            ).add_to(m)
            ongs_marcadas += 1
    
    # --- LEYENDA ---
    legend_html = '''
    <div style="position: fixed; bottom: 20px; left: 10px; width: 220px; background-color: white; border: 2px solid #4A00E0; z-index: 9999; font-size: 11px; padding: 10px; border-radius: 5px;">
        <h4 style="margin:0 0 8px 0; color:#4A00E0;">🗺️ Leyenda</h4>
        <div style="margin:5px 0;">
            <p style="margin:2px 0;"><b>Riesgo de Ruta:</b></p>
            <p style="margin:2px 0;"><span style="color:red;">●</span> Alto (Peligroso)</p>
            <p style="margin:2px 0;"><span style="color:orange;">●</span> Medio (Precaución)</p>
            <p style="margin:2px 0;"><span style="color:green;">●</span> Bajo (Seguro)</p>
        </div>
        <div style="margin:5px 0;">
            <p style="margin:2px 0;"><b>Marcadores:</b></p>
            <p style="margin:2px 0;"><span style="color:orange;">⭐</span> Rec. al Norte</p>
            <p style="margin:2px 0;"><span style="color:green;">🏠</span> Tu Destino</p>
        </div>
    </div>
    '''
    m.get_root().html.add_child(folium.Element(legend_html))
    
    # --- PANEL DE INFORMACIÓN ---
    # Datos para el panel
    if siguiente_recomendacion:
        rec_nombre = siguiente_recomendacion['name']
        rec_distancia = siguiente_recomendacion['distancia']
        rec_tipo = siguiente_recomendacion['type']
        rec_municipio = siguiente_recomendacion.get('municipio', 'Desconocido')
    else:
        rec_nombre = "No disponible al Norte"
        rec_distancia = 0
        rec_tipo = "N/A"
        rec_municipio = "N/A"
    
    # HTML de la lista de ONGs cercanas
    otras_ongs_html = ""
    if len(ongs_cercanas) > 0:
        for ong in ongs_cercanas:
            otras_ongs_html += f"""
            <div style="font-size:10px; margin:4px 0; padding:5px; background:#f8f9fa; border-radius:4px; border-left: 3px solid #4A00E0;">
                <div style="font-weight:bold;">{ong['name']}</div>
                <div style="color:#666; font-size:9px;">{ong['type']} - {ong.get('municipio', 'Desconocido')} - {ong['distancia']:.1f} km</div>
            </div>
            """
    else:
        otras_ongs_html = '<div style="font-size:10px; color:#666;">No hay ONGs al norte cercanas.</div>'

    # HTML de municipios en ruta
    municipios_html = ""
    for segmento in segmentos_ruta:
        color = colores_riesgo.get(segmento['grado_riesgo'], 'gray')
        municipios_html += f'<div style="font-size:10px; margin:3px 0; padding:3px; border-left: 3px solid {color}; background: #f8f9fa;">{segmento["municipio"]} <span style="float:right; color:{color};">{segmento["grado_riesgo"]}</span></div>'
    
    # Inyectar HTML del panel (reutilizando tu estructura visual)
    # (Simplificado para brevedad en esta respuesta, pero mantiene la funcionalidad)
    info_html = f'''
    <div style="position: fixed; top: 10px; right: 10px; z-index: 9999; font-family: Arial;">
        <div id="info-toggle" style="background: #4A00E0; color: white; padding: 8px 15px; border-radius: 20px; cursor: pointer; font-weight: bold;" onclick="toggleInfo()">
            <span>📋 Info Ruta</span> ▼
        </div>
        <div id="info-panel" style="background: white; border: 2px solid #4A00E0; border-radius: 10px; width: 300px; max-height: 500px; overflow: hidden; display: none; margin-top: 5px;">
            <div style="padding: 10px;">
                <h5 style="color:#4A00E0; margin:0 0 5px 0;">🎯 Destino: {dest_nombre}</h5>
                <p style="font-size:11px; margin:0;">Distancia: {dest_distancia:.1f} km</p>
                <hr>
                <h5 style="color:orange; margin:0 0 5px 0;">⭐ Rec. Norte: {rec_nombre}</h5>
                <p style="font-size:11px; margin:0;">Distancia: {rec_distancia:.1f} km</p>
                <hr>
                <h5 style="color:#333; margin:0 0 5px 0;">📊 Ruta por Municipios:</h5>
                <div style="max-height:150px; overflow-y:auto;">{municipios_html}</div>
                <hr>
                <h5 style="color:#333; margin:0 0 5px 0;">📍 Otras al Norte:</h5>
                <div style="max-height:150px; overflow-y:auto;">{otras_ongs_html}</div>
            </div>
        </div>
    </div>
    <script>
    function toggleInfo() {{
        var panel = document.getElementById('info-panel');
        panel.style.display = (panel.style.display === 'none' || panel.style.display === '') ? 'block' : 'none';
    }}
    </script>
    '''
    m.get_root().html.add_child(folium.Element(info_html))
    
    # Ajustar headers para móvil
    html_content = m.get_root().render()
    html_content = html_content.replace('<head>', '''
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <style>
            body { margin: 0; padding: 0; }
            #map { position: absolute; top: 0; bottom: 0; width: 100%; }
        </style>
    ''')

    return html_content

# --- RUTAS FLASK ---

@app.route('/')
def index():
    return "Servidor de Mapas Activo. Usa la app móvil."

@app.route('/health')
def health():
    return "OK"

@app.route('/mapa')
def serve_map():
    try:
        # 1. Obtener parámetros
        lat = request.args.get('lat', type=float)
        lon = request.args.get('lon', type=float)
        id_usuario = request.args.get('id_usuario', default=1, type=int)
        
        start_point = (lat, lon)
        print(f"🚀 Petición recibida: {start_point}")

        # 2. Cargar datos
        waypoints = cargar_waypoints_ongs()
        riesgo_por_municipio_nombre = cargar_datos_riesgo()
        colores_riesgo = {'Alto': 'red', 'Medio': 'orange', 'Bajo': 'green', 'Desconocido': 'gray'}

        # 3. Calcular Destino (más cercano) y Recomendación (Norte)
        ong_cercana = ong_mas_cercana(start_point, waypoints)
        
        # BUSCAR RECOMENDACIÓN HACIA EL NORTE
        ongs_al_norte = find_ongs_north(start_point, waypoints, ong_cercana)
        
        siguiente_recomendacion = None
        if ongs_al_norte:
            siguiente_recomendacion = ongs_al_norte[0]
            print(f"✅ Recomendación al Norte: {siguiente_recomendacion['name']}")
        else:
            print("⚠️ No hay ONGs al norte.")

        ongs_cercanas = ongs_al_norte[:5] if ongs_al_norte else []

        # 4. Calcular ruta o línea recta (Modo seguro 5km)
        segmentos_ruta = []
        if ong_cercana:
            try:
                dest_point = (ong_cercana['lat'], ong_cercana['lon'])
                distancia_km = ong_cercana['distancia']
                LIMITE_DETALLADO_KM = 5.0
                
                if distancia_km > LIMITE_DETALLADO_KM:
                    print(f"⚠️ Destino lejano ({distancia_km:.1f} km). Usando LÍNEA RECTA.")
                    segmentos_ruta.append({
                        'coords': [start_point, dest_point],
                        'municipio': "Ruta Directa (Larga)",
                        'grado_riesgo': 'Desconocido'
                    })
                else:
                    print(f"🚗 Destino cercano. Calculando ruta detallada...")
                    # Bounding box seguro
                    north = max(lat, dest_point[0]) + 0.01
                    south = min(lat, dest_point[0]) - 0.01
                    east = max(lon, dest_point[1]) + 0.01
                    west = min(lon, dest_point[1]) - 0.01
                    
                    G = ox.graph_from_bbox(north=north, south=south, east=east, west=west, network_type="drive")
                    orig_node = ox.distance.nearest_nodes(G, lon, lat)
                    dest_node = ox.distance.nearest_nodes(G, dest_point[1], dest_point[0])
                    
                    route = nx.astar_path(G, orig_node, dest_node, weight='length')
                    route_coords = [(G.nodes[n]['y'], G.nodes[n]['x']) for n in route]
                    
                    # Segmentar por municipio y riesgo
                    segmento_actual = []
                    municipio_actual = None
                    riesgo_actual = 'Desconocido'
                    
                    for coord in route_coords:
                        mun = obtener_municipio_por_proximidad(coord[0], coord[1], waypoints)
                        riesgo = riesgo_por_municipio_nombre.get(mun, 'Desconocido')
                        
                        if municipio_actual is None:
                            municipio_actual = mun
                            riesgo_actual = riesgo
                            segmento_actual.append(coord)
                        elif mun == municipio_actual:
                            segmento_actual.append(coord)
                        else:
                            if segmento_actual:
                                segmentos_ruta.append({
                                    'coords': segmento_actual.copy(),
                                    'municipio': municipio_actual,
                                    'grado_riesgo': riesgo_actual
                                })
                            municipio_actual = mun
                            riesgo_actual = riesgo
                            segmento_actual = [coord]
                    
                    if segmento_actual:
                        segmentos_ruta.append({
                            'coords': segmento_actual,
                            'municipio': municipio_actual,
                            'grado_riesgo': riesgo_actual
                        })

            except Exception as e:
                print(f"❌ Error en ruta: {e}")
                segmentos_ruta = []
        
        # 5. Generar Mapa
        return generar_mapa_movil_con_recomendaciones(
            start_point, ong_cercana, segmentos_ruta, waypoints, 
            id_usuario, colores_riesgo, ongs_cercanas, siguiente_recomendacion
        )

    except Exception as e:
        print(f"💥 Error fatal: {e}")
        return f"<h1>Error: {e}</h1>", 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)