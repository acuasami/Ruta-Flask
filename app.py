from flask import Flask, request, jsonify
import pandas as pd
from geopy.distance import geodesic
import folium
import psycopg2
import logging
import os
import time

# Configuración MÍNIMA inicial
logging.basicConfig(level=logging.WARNING)
app = Flask(__name__)

DB_CONFIG = {
    'user': 'postgres',
    'password': 'KAGJhRklTcsevGqKEgCNPfmdDiGzsLyQ',
    'host': 'switchyard.proxy.rlwy.net',
    'port': '13155',
    'dbname': 'railway'
}

# Importar OSMnx SOLO cuando se necesite
def import_osmnx():
    global ox, nx
    import osmnx as ox
    import networkx as nx
    # Configurar OSMnx para free tier
    ox.settings.log_console = False
    ox.settings.use_cache = True
    ox.settings.timeout = 45
    return ox, nx

# Cache de datos
_cache_data = None

def get_cached_data():
    global _cache_data
    if _cache_data is None:
        _cache_data = cargar_datos_ongs()
    return _cache_data

def cargar_datos_ongs():
    """Carga ultra ligera de datos"""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        query = "SELECT nom_ong, tipo, latitud, longitud FROM public.ongs WHERE latitud IS NOT NULL LIMIT 30"
        df = pd.read_sql(query, conn)
        conn.close()
        
        return [{
            'name': row['nom_ong'],
            'type': row['tipo'],
            'lat': float(row['latitud']),
            'lon': float(row['longitud'])
        } for _, row in df.iterrows()]
    except:
        return []

def calcular_ruta_inteligente(start_point, dest_point):
    """Calcula ruta con fallbacks inteligentes"""
    try:
        # Importar OSMnx solo aquí (ahorra RAM inicial)
        ox, nx = import_osmnx()
        
        distance_km = geodesic(start_point, dest_point).km
        
        # Si la distancia es grande, usar línea recta
        if distance_km > 20:
            return [start_point, dest_point]
        
        # Área pequeña para free tier
        buffer_m = min(distance_km * 1500, 5000)  # Máximo 5km
        
        try:
            G = ox.graph_from_point(
                start_point, 
                dist=buffer_m,
                network_type="drive",
                simplify=True
            )
            
            if len(G.nodes) < 5:
                return [start_point, dest_point]
                
            orig_node = ox.distance.nearest_nodes(G, start_point[1], start_point[0])
            dest_node = ox.distance.nearest_nodes(G, dest_point[1], dest_point[0])
            
            route = nx.shortest_path(G, orig_node, dest_node, weight='length')
            return [(G.nodes[n]['y'], G.nodes[n]['x']) for n in route]
            
        except Exception as e:
            return [start_point, dest_point]
            
    except Exception as e:
        return [start_point, dest_point]

@app.route('/calcular-ruta', methods=['POST'])
def calcular_ruta_endpoint():
    """Endpoint principal con manejo de memoria"""
    start_time = time.time()
    
    try:
        data = request.get_json()
        lat = float(data.get('lat', 0))
        lon = float(data.get('lon', 0))
        
        waypoints = get_cached_data()
        if not waypoints:
            return jsonify({"error": "No hay datos de ONGs"}), 500
        
        # Encontrar ONG más cercana
        start_point = (lat, lon)
        ong_cercana = min(
            [o for o in waypoints if o.get('type') != 'Frontera'],
            key=lambda o: geodesic(start_point, (o['lat'], o['lon'])).km,
            default=None
        )
        
        if not ong_cercana:
            return jsonify({"error": "No hay ONGs cercanas"}), 404
        
        ong_cercana['distancia'] = geodesic(start_point, (ong_cercana['lat'], ong_cercana['lon'])).km
        dest_point = (ong_cercana['lat'], ong_cercana['lon'])
        
        # Calcular ruta
        route_coords = calcular_ruta_inteligente(start_point, dest_point)
        
        # Mapa simple
        m = folium.Map(location=start_point, zoom_start=12, tiles="CartoDB positron")
        
        # Ruta
        folium.PolyLine(route_coords, color='#4A00E0', weight=6, opacity=0.8).add_to(m)
        
        # Marcadores
        folium.Marker(start_point, popup="📍 Tu ubicación", icon=folium.Icon(color='blue')).add_to(m)
        folium.Marker(dest_point, popup=f"🏠 {ong_cercana['name']}", icon=folium.Icon(color='green')).add_to(m)
        
        html_content = m.get_root().render()
        html_content = html_content.replace('<head>', '''
        <head>
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <style>body, html { margin: 0; padding: 0; height: 100%; }</style>
        ''')
        
        logger.warning(f"✅ Ruta calculada en {time.time() - start_time:.2f}s")
        return html_content
        
    except Exception as e:
        logger.error(f"❌ Error: {e}")
        return jsonify({"error": "Error interno"}), 500

@app.route('/calcular-ruta-simple', methods=['POST'])
def calcular_ruta_simple():
    """Endpoint sin OSMnx - solo datos básicos"""
    try:
        data = request.get_json()
        lat = float(data.get('lat', 0))
        lon = float(data.get('lon', 0))
        
        waypoints = get_cached_data()
        start_point = (lat, lon)
        
        ong_cercana = min(
            [o for o in waypoints if o.get('type') != 'Frontera'],
            key=lambda o: geodesic(start_point, (o['lat'], o['lon'])).km,
            default=None
        )
        
        if not ong_cercana:
            return jsonify({"error": "No hay ONGs cercanas"}), 404
        
        return jsonify({
            "success": True,
            "ong": {
                "nombre": ong_cercana['name'],
                "tipo": ong_cercana['type'],
                "lat": ong_cercana['lat'],
                "lon": ong_cercana['lon'],
                "distancia_km": round(geodesic(start_point, (ong_cercana['lat'], ong_cercana['lon'])).km, 2)
            }
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/health')
def health():
    return jsonify({"status": "ok", "osmnx": "lazy-loaded"})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
