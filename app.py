from flask import Flask, request, jsonify
import pandas as pd
import osmnx as ox
import networkx as nx
from geopy.distance import geodesic
import folium
import psycopg2
import logging
import os
import math
import time

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Configuración de la base de datos
DB_CONFIG = {
    'user': 'postgres',
    'password': 'KAGJhRklTcsevGqKEgCNPfmdDiGzsLyQ',
    'host': 'switchyard.proxy.rlwy.net',
    'port': '13155',
    'dbname': 'railway'
}

# Configuración de OSM
ox.settings.log_console = True
ox.settings.use_cache = True
ox.settings.timeout = 300

@lru_cache(maxsize=1)
def cargar_datos_ongs():
    """Carga los datos de ONGs desde la base de datos con cache"""
    try:
        logger.info("📂 Cargando datos de ONGs desde la base de datos...")
        conn = psycopg2.connect(**DB_CONFIG)
        
        # Consulta para ONGs
        query_ongs = """
        SELECT o.nom_ong, o.tipo, o.latitud, o.longitud, m.nom_municipio 
        FROM public.ongs o
        JOIN public.municipio m ON o.id_municipio = m.id_municipio;
        """
        df_ongs = pd.read_sql(query_ongs, conn)
        
        # Consulta para riesgo
        query_riesgo = """
        SELECT DISTINCT ON (f.id_municipio) 
               f.id_municipio, f.grado, m.nom_municipio
        FROM public.fecha f
        JOIN public.municipio m ON f.id_municipio = m.id_municipio
        ORDER BY f.id_municipio, f.fecha DESC;
        """
        df_riesgo = pd.read_sql(query_riesgo, conn)
        conn.close()
        
        # Procesar datos de ONGs
        df_ongs.rename(columns={
            'nom_ong': 'name', 'tipo': 'type', 'latitud': 'lat', 
            'longitud': 'lon', 'nom_municipio': 'municipio'
        }, inplace=True)
        
        # Convertir coordenadas a numérico
        df_ongs['lat'] = pd.to_numeric(df_ongs['lat'], errors='coerce')
        df_ongs['lon'] = pd.to_numeric(df_ongs['lon'], errors='coerce')
        df_ongs = df_ongs.dropna(subset=['lat', 'lon'])
        
        # Crear lista de waypoints
        waypoints = []
        for _, row in df_ongs.iterrows():
            waypoints.append({
                'name': row['name'],
                'type': row['type'],
                'lat': float(row['lat']),
                'lon': float(row['lon']),
                'municipio': row['municipio']
            })
        
        # Crear diccionario de riesgo
        riesgo_por_municipio = {}
        for _, row in df_riesgo.iterrows():
            riesgo_por_municipio[row['nom_municipio']] = row['grado']
        
        logger.info(f"✅ Cargadas {len(waypoints)} ONGs y {len(riesgo_por_municipio)} municipios con riesgo")
        return waypoints, riesgo_por_municipio
        
    except Exception as e:
        logger.error(f"❌ Error al cargar datos: {e}")
        return [], {}

def haversine_heuristic(u, v, G):
    """Heurística de Haversine para el algoritmo A*"""
    try:
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
    except Exception as e:
        logger.error(f"Error en heurística: {e}")
        return float('inf')

def ong_mas_cercana(pos_actual, waypoints):
    """Encuentra la ONG más cercana a la posición actual"""
    try:
        ongs = [w for w in waypoints if str(w.get('type', '')).strip().lower() != 'frontera']
        
        if not ongs:
            return None
        
        for o in ongs:
            o['distancia'] = geodesic(pos_actual, (o['lat'], o['lon'])).kilometers
        
        mas_cercana = min(ongs, key=lambda x: x['distancia'])
        logger.info(f"📍 ONG más cercana: {mas_cercana['name']} ({mas_cercana['distancia']:.2f} km)")
        return mas_cercana
        
    except Exception as e:
        logger.error(f"Error al encontrar ONG cercana: {e}")
        return None

def obtener_recomendacion_norte(start, waypoints, ong_actual):
    """Encuentra ONGs al norte para recomendación"""
    try:
        start_lat, start_lon = start
        candidates = []
        
        for ong in waypoints:
            # Excluir fronteras y la ONG actual
            if str(ong.get('type', '')).strip().lower() == 'frontera':
                continue
            if ong_actual and ong['name'] == ong_actual.get('name'):
                continue
            
            # Verificar que esté al norte
            if ong["lat"] > start_lat:
                distancia = geodesic(start, (ong["lat"], ong["lon"])).kilometers
                candidates.append({
                    "name": ong["name"],
                    "lat": ong["lat"],
                    "lon": ong["lon"],
                    "type": ong["type"],
                    "municipio": ong.get("municipio", "Desconocido"),
                    "distancia": distancia,
                    "direccion_norte": ong["lat"] - start_lat
                })
        
        # Ordenar por distancia
        candidates.sort(key=lambda x: x['distancia'])
        return candidates[0] if candidates else None
        
    except Exception as e:
        logger.error(f"Error al obtener recomendación: {e}")
        return None

def calcular_ruta(start_point, dest_point):
    """Calcula la ruta entre dos puntos usando OSMnx"""
    try:
        # Calcular distancia para determinar el área de búsqueda
        distance_km = geodesic(start_point, dest_point).km
        buffer_m = (distance_km + 0.5) * 1000  # +500m de margen
        
        logger.info(f"🗺️ Descargando grafo OSM para área de {buffer_m:.0f}m...")
        
        # Descargar grafo
        G = ox.graph_from_point(
            start_point, 
            dist=buffer_m, 
            network_type="drive",
            simplify=True
        )
        
        # Encontrar nodos más cercanos
        orig_node = ox.distance.nearest_nodes(G, start_point[1], start_point[0])
        dest_node = ox.distance.nearest_nodes(G, dest_point[1], dest_point[0])
        
        logger.info("🔄 Calculando ruta con algoritmo A*...")
        
        # Calcular ruta con A*
        route = nx.astar_path(
            G,
            orig_node,
            dest_node,
            heuristic=lambda u, v: haversine_heuristic(u, v, G),
            weight='length'
        )
        
        # Convertir a coordenadas
        route_coords = [(G.nodes[n]['y'], G.nodes[n]['x']) for n in route]
        
        logger.info(f"✅ Ruta calculada con {len(route)} nodos")
        return route_coords, G
        
    except Exception as e:
        logger.error(f"❌ Error al calcular ruta: {e}")
        raise e

def generar_mapa_completo(start, ong_cercana, waypoints, riesgo_por_municipio, route_coords, recomendacion=None):
    """Genera el mapa HTML completo con todos los elementos"""
    try:
        # Crear mapa base
        m = folium.Map(
            location=start,
            zoom_start=12,
            tiles="CartoDB positron",
            width='100%',
            height='98vh'
        )
        
        # Configuración para móviles
        m.options['touchZoom'] = True
        m.options['dragging'] = True
        m.options['scrollWheelZoom'] = True
        
        # --- DIBUJAR RUTA ---
        if route_coords and len(route_coords) > 1:
            logger.info("🎨 Dibujando ruta en el mapa...")
            folium.PolyLine(
                route_coords,
                color='#4A00E0',
                weight=6,
                opacity=0.8,
                tooltip="Ruta hacia la ONG más cercana"
            ).add_to(m)
        
        # --- MARCADOR DEL USUARIO ---
        folium.Marker(
            location=start,
            popup=folium.Popup(
                f"""
                <div style='font-size:14px; max-width:250px;'>
                    <div style='background:#4A00E0; color:white; padding:8px; border-radius:5px 5px 0 0; margin:-10px -10px 10px -10px;'>
                        <b>📍 Tu Ubicación</b>
                    </div>
                    <p><b>Lat:</b> {start[0]:.4f}</p>
                    <p><b>Lon:</b> {start[1]:.4f}</p>
                    <p><b>Destino:</b> {ong_cercana['name']}</p>
                </div>
                """,
                max_width=300
            ),
            tooltip="📍 Tu ubicación actual",
            icon=folium.Icon(color="blue", icon="user", prefix="fa")
        ).add_to(m)
        
        # --- MARCADOR ONG DESTINO ---
        riesgo_ong = riesgo_por_municipio.get(ong_cercana.get('municipio', 'Desconocido'), 'Desconocido')
        color_riesgo = {'Alto': 'red', 'Medio': 'orange', 'Bajo': 'green'}.get(riesgo_ong, 'gray')
        
        folium.Marker(
            location=(ong_cercana['lat'], ong_cercana['lon']),
            popup=folium.Popup(
                f"""
                <div style='font-size:14px; max-width:280px;'>
                    <div style='background:{color_riesgo}; color:white; padding:8px; border-radius:5px 5px 0 0; margin:-10px -10px 10px -10px;'>
                        <b>🏠 ONG Destino</b>
                    </div>
                    <p><b>Nombre:</b> {ong_cercana['name']}</p>
                    <p><b>Tipo:</b> {ong_cercana['type']}</p>
                    <p><b>Municipio:</b> {ong_cercana.get('municipio', 'Desconocido')}</p>
                    <p><b>Riesgo:</b> {riesgo_ong}</p>
                    <p><b>Distancia:</b> {ong_cercana['distancia']:.1f} km</p>
                </div>
                """,
                max_width=320
            ),
            tooltip=f"🎯 {ong_cercana['name']}",
            icon=folium.Icon(color="green", icon="home", prefix="fa")
        ).add_to(m)
        
        # --- MARCADOR RECOMENDACIÓN ---
        if recomendacion:
            folium.Marker(
                location=(recomendacion['lat'], recomendacion['lon']),
                popup=folium.Popup(
                    f"""
                    <div style='font-size:14px; max-width:280px;'>
                        <div style='background:#FF9800; color:white; padding:8px; border-radius:5px 5px 0 0; margin:-10px -10px 10px -10px;'>
                            <b>⭐ Próxima Recomendación</b>
                        </div>
                        <p><b>Nombre:</b> {recomendacion['name']}</p>
                        <p><b>Tipo:</b> {recomendacion['type']}</p>
                        <p><b>Distancia:</b> {recomendacion['distancia']:.1f} km</p>
                    </div>
                    """,
                    max_width=320
                ),
                tooltip=f"⭐ {recomendacion['name']}",
                icon=folium.Icon(color="orange", icon="star", prefix="fa")
            ).add_to(m)
        
        # --- OTRAS ONGs ---
        ongs_marcadas = 0
        for ong in waypoints:
            if ong['name'] != ong_cercana['name'] and (not recomendacion or ong['name'] != recomendacion.get('name')):
                color_ong = {
                    'Albergue': 'lightblue',
                    'Comedor': 'orange',
                    'Frontera': 'red',
                    'default': 'gray'
                }.get(ong.get('type', ''), 'gray')
                
                folium.CircleMarker(
                    location=(ong['lat'], ong['lon']),
                    radius=6,
                    popup=folium.Popup(
                        f"<b>{ong['name']}</b><br>{ong['type']}",
                        max_width=200
                    ),
                    tooltip=ong['name'],
                    color=color_ong,
                    fillColor=color_ong,
                    weight=2,
                    fillOpacity=0.7
                ).add_to(m)
                ongs_marcadas += 1
        
        logger.info(f"📍 Marcadas {ongs_marcadas} ONGs adicionales")
        
        # --- LEYENDA ---
        legend_html = '''
        <div style="position: fixed; bottom: 20px; left: 10px; width: 220px; background: white; 
                    border: 2px solid #4A00E0; z-index: 9999; font-size: 11px; padding: 10px; border-radius: 5px;">
            <h4 style="margin:0 0 8px 0; color:#4A00E0; font-size:12px;">🗺️ Leyenda</h4>
            <p style="margin:2px 0;">📍 Tu ubicación</p>
            <p style="margin:2px 0;">🏠 ONG destino</p>
            <p style="margin:2px 0;">⭐ Recomendación</p>
            <p style="margin:2px 0;">● Otras ONGs</p>
        </div>
        '''
        m.get_root().html.add_child(folium.Element(legend_html))
        
        return m
        
    except Exception as e:
        logger.error(f"❌ Error al generar mapa: {e}")
        raise e

@app.route('/')
def home():
    """Página de inicio"""
    return jsonify({
        "message": "🚀 Servidor de Rutas para Migrantes - ONG Finder",
        "version": "1.0",
        "endpoints": {
            "calcular_ruta": "POST /calcular-ruta",
            "health": "GET /health",
            "info": "GET /"
        },
        "usage": {
            "calcular_ruta": "Envía JSON con {lat: xx.xx, lon: xx.xx}",
            "response": "Devuelve HTML del mapa interactivo"
        }
    })

@app.route('/health')
def health_check():
    """Endpoint de salud del servidor"""
    try:
        # Verificar conexión a la base de datos
        conn = psycopg2.connect(**DB_CONFIG)
        conn.close()
        
        return jsonify({
            "status": "healthy",
            "service": "ruta-migrante",
            "database": "connected",
            "timestamp": time.time()
        })
    except Exception as e:
        return jsonify({
            "status": "unhealthy",
            "service": "ruta-migrante", 
            "database": "disconnected",
            "error": str(e)
        }), 500

@app.route('/calcular-ruta', methods=['POST'])
def calcular_ruta_endpoint():
    """Endpoint principal para calcular rutas"""
    start_time = time.time()
    
    try:
        # Obtener datos de la solicitud
        data = request.get_json()
        
        if not data:
            return jsonify({"error": "Se requiere JSON con lat y lon"}), 400
        
        lat = data.get('lat')
        lon = data.get('lon')
        
        if not lat or not lon:
            return jsonify({"error": "Se requieren latitud (lat) y longitud (lon)"}), 400
        
        logger.info(f"📍 Solicitud de ruta recibida: ({lat}, {lon})")
        
        # Validar coordenadas
        try:
            lat = float(lat)
            lon = float(lon)
            if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                return jsonify({"error": "Coordenadas fuera de rango válido"}), 400
        except ValueError:
            return jsonify({"error": "Coordenadas deben ser números válidos"}), 400
        
        # Cargar datos
        waypoints, riesgo_por_municipio = cargar_datos_ongs()
        if not waypoints:
            return jsonify({"error": "No se pudieron cargar los datos de ONGs"}), 500
        
        # Configurar ubicación del usuario
        start_point = (lat, lon)
        
        # Encontrar ONG más cercana
        ong_cercana = ong_mas_cercana(start_point, waypoints)
        if not ong_cercana:
            return jsonify({"error": "No se encontró ninguna ONG cercana"}), 404
        
        # Obtener recomendación
        recomendacion = obtener_recomendacion_norte(start_point, waypoints, ong_cercana)
        
        # Calcular ruta
        dest_point = (ong_cercana['lat'], ong_cercana['lon'])
        route_coords, G = calcular_ruta(start_point, dest_point)
        
        # Generar mapa
        mapa = generar_mapa_completo(
            start_point, ong_cercana, waypoints, 
            riesgo_por_municipio, route_coords, recomendacion
        )
        
        # Generar HTML
        html_content = mapa.get_root().render()
        
        # Agregar meta tags para móviles
        html_content = html_content.replace('<head>', '''
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
            <title>ONG Finder - Ruta Recomendada</title>
            <style>
                body { margin: 0; padding: 0; font-family: Arial, sans-serif; }
                #map { position: absolute; top: 0; bottom: 0; width: 100%; }
            </style>
        ''')

        processing_time = time.time() - start_time
        logger.info(f"✅ Ruta calculada en {processing_time:.2f} segundos")
        
        return html_content
        
    except Exception as e:
        logger.error(f"❌ Error en endpoint: {e}")
        return jsonify({
            "error": "Error interno del servidor",
            "message": str(e)
        }), 500

@app.route('/calcular-ruta-json', methods=['POST'])
def calcular_ruta_json():
    """Endpoint alternativo que devuelve JSON en lugar de HTML"""
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({"error": "Se requiere JSON con lat y lon"}), 400
        
        lat = data.get('lat')
        lon = data.get('lon')
        
        if not lat or not lon:
            return jsonify({"error": "Se requieren latitud (lat) y longitud (lon)"}), 400
        
        # Cargar datos
        waypoints, riesgo_por_municipio = cargar_datos_ongs()
        if not waypoints:
            return jsonify({"error": "No se pudieron cargar los datos de ONGs"}), 500
        
        # Encontrar ONG más cercana
        start_point = (float(lat), float(lon))
        ong_cercana = ong_mas_cercana(start_point, waypoints)
        
        if not ong_cercana:
            return jsonify({"error": "No se encontró ninguna ONG cercana"}), 404
        
        # Obtener recomendación
        recomendacion = obtener_recomendacion_norte(start_point, waypoints, ong_cercana)
        
        return jsonify({
            "success": True,
            "usuario": {"lat": lat, "lon": lon},
            "ong_destino": {
                "nombre": ong_cercana['name'],
                "tipo": ong_cercana['type'],
                "municipio": ong_cercana.get('municipio', 'Desconocido'),
                "lat": ong_cercana['lat'],
                "lon": ong_cercana['lon'],
                "distancia_km": ong_cercana['distancia']
            },
            "recomendacion": recomendacion,
            "total_ongs": len(waypoints)
        })
        
    except Exception as e:
        return jsonify({"error": f"Error interno: {str(e)}"}), 500

# Manejo de errores
@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Endpoint no encontrado"}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": "Error interno del servidor"}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    debug = os.environ.get('DEBUG', 'False').lower() == 'true'
    
    logger.info(f"🚀 Iniciando servidor en puerto {port}")
    app.run(host='0.0.0.0', port=port, debug=debug)
