from flask import Flask, request, jsonify, render_template_string
import pandas as pd
import osmnx as ox
import networkx as nx
from geopy.distance import geodesic
import folium
import json
import psycopg2
from urllib.parse import urlparse
import logging
import tempfile
import os

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

def cargar_datos_ongs():
    """Carga los datos de ONGs desde la base de datos"""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        query_ongs = """
        SELECT o.nom_ong, o.tipo, o.latitud, o.longitud, m.nom_municipio 
        FROM public.ongs o
        JOIN public.municipio m ON o.id_municipio = m.id_municipio;
        """
        df_ongs = pd.read_sql(query_ongs, conn)
        
        # Cargar datos de riesgo
        query_riesgo = """
        SELECT f.id_municipio, f.grado, m.nom_municipio
        FROM public.fecha f
        JOIN public.municipio m ON f.id_municipio = m.id_municipio
        WHERE f.fecha = (SELECT MAX(fecha) FROM public.fecha);
        """
        df_riesgo = pd.read_sql(query_riesgo, conn)
        conn.close()
        
        # Procesar datos de ONGs
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
        
        # Crear diccionario de riesgo
        riesgo_por_municipio = {}
        for _, row in df_riesgo.iterrows():
            riesgo_por_municipio[row['nom_municipio']] = row['grado']
        
        logger.info(f"Cargadas {len(waypoints)} ONGs y {len(riesgo_por_municipio)} municipios con riesgo")
        return waypoints, riesgo_por_municipio
        
    except Exception as e:
        logger.error(f"Error al cargar datos: {e}")
        return [], {}

def ong_mas_cercana(pos_actual, waypoints):
    """Encuentra la ONG más cercana a la posición actual"""
    ongs = [w for w in waypoints if str(w.get('type', '')).strip().lower() != 'frontera']
    
    if not ongs:
        return None
    
    for o in ongs:
        o['distancia'] = geodesic(pos_actual, (o['lat'], o['lon'])).kilometers
    
    return min(ongs, key=lambda x: x['distancia'])

def calcular_ruta_y_mapa(lat_usuario, lon_usuario):
    """Función principal que calcula la ruta y genera el mapa"""
    try:
        logger.info(f"Calculando ruta para usuario en ({lat_usuario}, {lon_usuario})")
        
        # Cargar datos
        waypoints, riesgo_por_municipio = cargar_datos_ongs()
        if not waypoints:
            return {"error": "No se pudieron cargar los datos de ONGs"}
        
        # Configurar ubicación del usuario
        start = (float(lat_usuario), float(lon_usuario))
        
        # Encontrar ONG más cercana
        ong_cercana = ong_mas_cercana(start, waypoints)
        if not ong_cercana:
            return {"error": "No se encontró ninguna ONG cercana"}
        
        logger.info(f"ONG más cercana: {ong_cercana['name']} ({ong_cercana['distancia']:.2f} km)")
        
        # Descargar grafo de OSM
        dest_point = (ong_cercana['lat'], ong_cercana['lon'])
        distance_km = geodesic(start, dest_point).km
        buffer_m = (distance_km + 0.3) * 1000
        
        logger.info("Descargando grafo de OpenStreetMap...")
        G = ox.graph_from_point(start, dist=buffer_m, network_type="drive")
        
        # Encontrar nodos más cercanos
        orig_node = ox.distance.nearest_nodes(G, start[1], start[0])
        dest_node = ox.distance.nearest_nodes(G, dest_point[1], dest_point[0])
        
        # Calcular ruta
        logger.info("Calculando ruta óptima...")
        route = nx.shortest_path(G, orig_node, dest_node, weight='length')
        
        # Convertir ruta a coordenadas
        route_coords = [(G.nodes[n]['y'], G.nodes[n]['x']) for n in route]
        
        # Crear segmentos simplificados (para este ejemplo)
        segmentos_ruta = [{
            'coords': route_coords,
            'municipio': ong_cercana.get('municipio', 'Desconocido'),
            'grado_riesgo': riesgo_por_municipio.get(ong_cercana.get('municipio', 'Desconocido'), 'Desconocido')
        }]
        
        # Colores de riesgo
        colores_riesgo = {
            'Alto': 'red',
            'Medio': 'orange', 
            'Bajo': 'green',
            'Desconocido': 'gray'
        }
        
        # Generar mapa
        logger.info("Generando mapa interactivo...")
        mapa_html = generar_mapa_html(
            start, ong_cercana, segmentos_ruta, waypoints, 
            colores_riesgo, riesgo_por_municipio, route_coords
        )
        
        return {
            "success": True,
            "html_content": mapa_html,
            "ong_destino": ong_cercana['name'],
            "distancia": ong_cercana['distancia'],
            "tiempo_estimado": "Calculando..."
        }
        
    except Exception as e:
        logger.error(f"Error en cálculo de ruta: {e}")
        return {"error": f"Error al calcular la ruta: {str(e)}"}

def generar_mapa_html(ubicacion_usuario, ong_cercana, segmentos_ruta, waypoints, colores_riesgo, riesgo_por_municipio, route_coords):
    """Genera el HTML del mapa con Folium"""
    
    # Crear mapa base
    m = folium.Map(
        location=ubicacion_usuario,
        zoom_start=13,
        tiles="CartoDB positron",
        width='100%',
        height='98vh'
    )
    
    # Dibujar ruta
    if segmentos_ruta:
        for segmento in segmentos_ruta:
            color = colores_riesgo.get(segmento['grado_riesgo'], 'gray')
            folium.PolyLine(
                segmento['coords'],
                color=color,
                weight=8,
                opacity=0.9,
                tooltip=f"Ruta hacia {ong_cercana['name']}"
            ).add_to(m)
    
    # Marcador del usuario
    folium.Marker(
        location=ubicacion_usuario,
        popup=folium.Popup(f"📍 Tu ubicación\nLat: {ubicacion_usuario[0]:.4f}\nLon: {ubicacion_usuario[1]:.4f}", max_width=250),
        tooltip="Tu ubicación actual",
        icon=folium.Icon(color="blue", icon="user", prefix="fa")
    ).add_to(m)
    
    # Marcador de la ONG destino
    folium.Marker(
        location=(ong_cercana['lat'], ong_cercana['lon']),
        popup=folium.Popup(
            f"🏠 {ong_cercana['name']}\n"
            f"🎯 {ong_cercana['type']}\n"
            f"🏙️ {ong_cercana.get('municipio', 'Desconocido')}\n"
            f"📏 {ong_cercana['distancia']:.1f} km",
            max_width=250
        ),
        tooltip=f"ONG Destino: {ong_cercana['name']}",
        icon=folium.Icon(color="green", icon="home", prefix="fa")
    ).add_to(m)
    
    # Otras ONGs
    for ong in waypoints:
        if ong['name'] != ong_cercana['name']:
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
                    f"🏠 {ong['name']}\n🎯 {ong['type']}",
                    max_width=200
                ),
                tooltip=ong['name'],
                color=color_ong,
                fillColor=color_ong,
                weight=2,
                fillOpacity=0.7
            ).add_to(m)
    
    # Leyenda
    legend_html = '''
    <div style="position: fixed; bottom: 20px; left: 10px; width: 220px; background: white; 
                border: 2px solid grey; z-index: 9999; font-size: 11px; padding: 10px; border-radius: 5px;">
        <h4 style="margin-top:0">Leyenda</h4>
        <p>📍 Tu ubicación</p>
        <p>🏠 ONG destino</p>
        <p>● Otras ONGs</p>
    </div>
    '''
    m.get_root().html.add_child(folium.Element(legend_html))
    
    return m.get_root().render()

@app.route('/')
def home():
    return jsonify({
        "message": "Servidor de rutas para migrantes",
        "endpoints": {
            "calcular_ruta": "POST /calcular-ruta con JSON: {lat: xx.xx, lon: xx.xx}",
            "health": "GET /health"
        }
    })

@app.route('/health')
def health():
    return jsonify({"status": "healthy", "service": "ruta-migrante"})

@app.route('/calcular-ruta', methods=['POST'])
def calcular_ruta_endpoint():
    """Endpoint principal que recibe la ubicación del usuario y devuelve el HTML del mapa"""
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({"error": "Se requiere JSON con lat y lon"}), 400
        
        lat = data.get('lat')
        lon = data.get('lon')
        
        if not lat or not lon:
            return jsonify({"error": "Se requieren latitud (lat) y longitud (lon)"}), 400
        
        logger.info(f"Solicitud de ruta recibida: lat={lat}, lon={lon}")
        
        # Calcular ruta y generar mapa
        resultado = calcular_ruta_y_mapa(lat, lon)
        
        if "error" in resultado:
            return jsonify({"error": resultado["error"]}), 500
        
        # Devolver el HTML directamente
        return resultado["html_content"]
        
    except Exception as e:
        logger.error(f"Error en endpoint: {e}")
        return jsonify({"error": f"Error interno del servidor: {str(e)}"}), 500

@app.route('/calcular-ruta-json', methods=['POST'])
def calcular_ruta_json():
    """Endpoint alternativo que devuelve JSON con información de la ruta"""
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({"error": "Se requiere JSON con lat y lon"}), 400
        
        lat = data.get('lat')
        lon = data.get('lon')
        
        if not lat or not lon:
            return jsonify({"error": "Se requieren latitud (lat) y longitud (lon)"}), 400
        
        resultado = calcular_ruta_y_mapa(lat, lon)
        
        return jsonify(resultado)
        
    except Exception as e:
        return jsonify({"error": f"Error interno: {str(e)}"}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
