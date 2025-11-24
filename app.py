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
from functools import lru_cache
import time
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

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

# Configuración de OSM optimizada
ox.settings.log_console = False
ox.settings.use_cache = True
ox.settings.timeout = 180

# Thread pool para operaciones concurrentes
executor = ThreadPoolExecutor(max_workers=2)

@lru_cache(maxsize=1)
def cargar_datos_ongs():
    """Carga los datos de ONGs desde la base de datos con cache"""
    try:
        logger.info("📂 Cargando datos de ONGs desde la base de datos...")
        conn = psycopg2.connect(**DB_CONFIG)
        
        query_ongs = """
        SELECT o.nom_ong, o.tipo, o.latitud, o.longitud, m.nom_municipio 
        FROM public.ongs o
        JOIN public.municipio m ON o.id_municipio = m.id_municipio
        WHERE o.latitud IS NOT NULL AND o.longitud IS NOT NULL;
        """
        df_ongs = pd.read_sql(query_ongs, conn)
        
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
        
        df_ongs['lat'] = pd.to_numeric(df_ongs['lat'], errors='coerce')
        df_ongs['lon'] = pd.to_numeric(df_ongs['lon'], errors='coerce')
        df_ongs = df_ongs.dropna(subset=['lat', 'lon'])
        
        waypoints = []
        for _, row in df_ongs.iterrows():
            waypoints.append({
                'name': row['name'],
                'type': row['type'],
                'lat': float(row['lat']),
                'lon': float(row['lon']),
                'municipio': row['municipio']
            })
        
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
        return geodesic((lat1, lon1), (lat2, lon2)).meters
    except Exception as e:
        logger.error(f"Error en heurística: {e}")
        return float('inf')

def ong_mas_cercana(pos_actual, waypoints):
    """Encuentra la ONG más cercana a la posición actual"""
    try:
        ongs = [w for w in waypoints if str(w.get('type', '')).strip().lower() != 'frontera']
        
        if not ongs:
            return None
        
        min_distancia = float('inf')
        mas_cercana = None
        
        for ong in ongs:
            distancia = geodesic(pos_actual, (ong['lat'], ong['lon'])).kilometers
            if distancia < min_distancia:
                min_distancia = distancia
                mas_cercana = ong.copy()
                mas_cercana['distancia'] = distancia
        
        if mas_cercana:
            logger.info(f"📍 ONG más cercana: {mas_cercana['name']} ({mas_cercana['distancia']:.2f} km)")
            return mas_cercana
        return None
        
    except Exception as e:
        logger.error(f"Error al encontrar ONG cercana: {e}")
        return None

def obtener_recomendacion_norte(start, waypoints, ong_actual):
    """Encuentra ONGs al norte para recomendación"""
    try:
        start_lat, start_lon = start
        candidates = []
        
        for ong in waypoints:
            if str(ong.get('type', '')).strip().lower() == 'frontera':
                continue
            if ong_actual and ong['name'] == ong_actual.get('name'):
                continue
            
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
        
        if candidates:
            candidates.sort(key=lambda x: x['distancia'])
            return candidates[0]
        return None
        
    except Exception as e:
        logger.error(f"Error al obtener recomendación: {e}")
        return None

def calcular_ruta_optimizada(start_point, dest_point, timeout=120):
    """Calcula la ruta entre dos puntos con timeout"""
    def _calcular():
        try:
            distance_km = geodesic(start_point, dest_point).km
            
            if distance_km > 100:
                logger.warning(f"📍 Distancia muy grande ({distance_km} km), limitando búsqueda")
                buffer_m = 50000
            else:
                buffer_m = min((distance_km + 5) * 1000, 50000)
            
            logger.info(f"🗺️ Descargando grafo OSM para área de {buffer_m/1000:.1f}km...")
            
            try:
                G = ox.graph_from_point(
                    start_point, 
                    dist=buffer_m, 
                    network_type="drive",
                    simplify=True,
                    retain_all=False
                )
            except Exception as e:
                logger.warning(f"⚠️ Error con red 'drive', intentando con 'all': {e}")
                G = ox.graph_from_point(
                    start_point, 
                    dist=buffer_m, 
                    network_type="all",
                    simplify=True,
                    retain_all=False
                )
            
            if len(G.nodes) == 0:
                raise Exception("No se pudo obtener datos de OSM para esta área")
            
            orig_node = ox.distance.nearest_nodes(G, start_point[1], start_point[0])
            dest_node = ox.distance.nearest_nodes(G, dest_point[1], dest_point[0])
            
            logger.info("🔄 Calculando ruta con algoritmo A*...")
            
            route = nx.astar_path(
                G,
                orig_node,
                dest_node,
                heuristic=lambda u, v: haversine_heuristic(u, v, G),
                weight='length'
            )
            
            route_coords = [(G.nodes[n]['y'], G.nodes[n]['x']) for n in route]
            
            logger.info(f"✅ Ruta calculada con {len(route)} nodos")
            return route_coords, G
            
        except Exception as e:
            logger.error(f"❌ Error al calcular ruta: {e}")
            raise
    
    try:
        future = executor.submit(_calcular)
        return future.result(timeout=timeout)
    except FutureTimeoutError:
        logger.error("⏰ Timeout calculando ruta")
        raise Exception("Tiempo de espera agotado al calcular la ruta")

def generar_mapa_completo(start, ong_cercana, waypoints, riesgo_por_municipio, route_coords=None, recomendacion=None):
    """Genera el mapa HTML completo con todos los elementos"""
    try:
        center_lat = (start[0] + ong_cercana['lat']) / 2
        center_lon = (start[1] + ong_cercana['lon']) / 2
        
        m = folium.Map(
            location=[center_lat, center_lon],
            zoom_start=12,
            tiles="CartoDB positron",
            width='100%',
            height='98vh'
        )
        
        m.options['touchZoom'] = True
        m.options['dragging'] = True
        m.options['scrollWheelZoom'] = True
        
        if route_coords and len(route_coords) > 1:
            logger.info("🎨 Dibujando ruta en el mapa...")
            folium.PolyLine(
                route_coords,
                color='#4A00E0',
                weight=6,
                opacity=0.8,
                tooltip="Ruta hacia la ONG más cercana"
            ).add_to(m)
        else:
            folium.PolyLine(
                [start, [ong_cercana['lat'], ong_cercana['lon']]],
                color='#4A00E0',
                weight=3,
                opacity=0.5,
                dash_array='5, 5',
                tooltip="Línea directa (ruta no disponible)"
            ).add_to(m)
        
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
                    <p><b>Distancia:</b> {ong_cercana['distancia']:.1f} km</p>
                </div>
                """,
                max_width=300
            ),
            tooltip="📍 Tu ubicación actual",
            icon=folium.Icon(color="blue", icon="user", prefix="fa")
        ).add_to(m)
        
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
        
        ongs_marcadas = 0
        for ong in waypoints[:20]:
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
    """Página de inicio con geolocalización automática"""
    return '''
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>ONG Finder - Rutas Seguras</title>
        <link rel="stylesheet" href="https://unpkg.com/leaflet@1.7.1/dist/leaflet.css" />
        <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; }
            .container { display: flex; height: 100vh; }
            .sidebar { width: 400px; background: white; padding: 30px; box-shadow: 2px 0 10px rgba(0,0,0,0.1); overflow-y: auto; }
            .main-content { flex: 1; position: relative; }
            .header { text-align: center; margin-bottom: 30px; }
            .header h1 { color: #333; font-size: 2rem; margin-bottom: 10px; }
            .header p { color: #666; font-size: 1.1rem; }
            .location-card { background: #e8f5e8; border-radius: 10px; padding: 20px; margin-bottom: 20px; border-left: 4px solid #4caf50; }
            .btn { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; border: none; padding: 15px 30px; border-radius: 8px; font-size: 16px; font-weight: 600; cursor: pointer; transition: transform 0.2s; width: 100%; margin: 10px 0; }
            .btn:hover { transform: translateY(-2px); }
            .btn:disabled { background: #cccccc; cursor: not-allowed; transform: none; }
            .btn-success { background: linear-gradient(135deg, #4caf50 0%, #45a049 100%); }
            #map { height: 100%; width: 100%; }
            .results { margin-top: 30px; }
            .info-card { background: #f8f9fa; border-radius: 10px; padding: 20px; margin-bottom: 15px; border-left: 4px solid #667eea; }
            .info-card h3 { color: #333; margin-bottom: 10px; display: flex; align-items: center; gap: 10px; }
            .loading { text-align: center; padding: 20px; display: none; }
            .spinner { border: 4px solid #f3f3f3; border-top: 4px solid #667eea; border-radius: 50%; width: 40px; height: 40px; animation: spin 1s linear infinite; margin: 0 auto 10px; }
            .coordinates-info { background: #e3f2fd; padding: 15px; border-radius: 8px; margin-top: 20px; font-size: 14px; display: none; }
            .error-message { background: #ffebee; color: #c62828; padding: 15px; border-radius: 8px; margin: 10px 0; border-left: 4px solid #c62828; display: none; }
            @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
            @media (max-width: 768px) { .container { flex-direction: column; } .sidebar { width: 100%; height: 40vh; } .main-content { height: 60vh; } }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="sidebar">
                <div class="header">
                    <h1><i class="fas fa-route"></i> ONG Finder</h1>
                    <p>Encuentra la ruta más segura usando tu ubicación actual</p>
                </div>
                <div class="location-card">
                    <h3><i class="fas fa-location-crosshairs"></i> Geolocalización</h3>
                    <p>La aplicación necesita acceso a tu ubicación para encontrar las ONGs más cercanas.</p>
                </div>
                <button id="getLocationBtn" class="btn">
                    <i class="fas fa-map-marker-alt"></i> Obtener Mi Ubicación Actual
                </button>
                <button id="findRouteBtn" class="btn btn-success" disabled>
                    <i class="fas fa-search"></i> Buscar ONG Más Cercana
                </button>
                <div class="coordinates-info" id="coordinatesInfo">
                    <strong><i class="fas fa-info-circle"></i> Tu ubicación:</strong>
                    <div id="locationDetails"></div>
                </div>
                <div class="error-message" id="errorMessage"></div>
                <div class="loading" id="loading">
                    <div class="spinner"></div>
                    <p>Calculando la mejor ruta...</p>
                    <p style="font-size: 12px; color: #666;">Esto puede tomar unos segundos</p>
                </div>
                <div class="results" id="results"></div>
            </div>
            <div class="main-content">
                <div id="map"></div>
            </div>
        </div>

        <script src="https://unpkg.com/leaflet@1.7.1/dist/leaflet.js"></script>
        <script>
            let userLocation = null;
            let userMarker = null;
            let destinationMarker = null;
            let routeLayer = null;
            
            const map = L.map('map').setView([19.4326, -99.1332], 6);
            L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { attribution: '© OpenStreetMap contributors' }).addTo(map);
            
            const getLocationBtn = document.getElementById('getLocationBtn');
            const findRouteBtn = document.getElementById('findRouteBtn');
            const coordinatesInfo = document.getElementById('coordinatesInfo');
            const locationDetails = document.getElementById('locationDetails');
            const errorMessage = document.getElementById('errorMessage');
            const loading = document.getElementById('loading');
            const results = document.getElementById('results');
            
            getLocationBtn.addEventListener('click', function() {
                getLocationBtn.disabled = true;
                getLocationBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Detectando ubicación...';
                
                if (navigator.geolocation) {
                    navigator.geolocation.getCurrentPosition(
                        function(position) {
                            const lat = position.coords.latitude;
                            const lon = position.coords.longitude;
                            userLocation = { lat, lon };
                            
                            coordinatesInfo.style.display = 'block';
                            locationDetails.innerHTML = `<strong>Latitud:</strong> ${lat.toFixed(6)}<br><strong>Longitud:</strong> ${lon.toFixed(6)}<br><strong>Precisión:</strong> ±${position.coords.accuracy.toFixed(0)} metros`;
                            
                            findRouteBtn.disabled = false;
                            getLocationBtn.innerHTML = '<i class="fas fa-check"></i> Ubicación Obtenida';
                            getLocationBtn.style.background = 'linear-gradient(135deg, #4caf50 0%, #45a049 100%)';
                            
                            map.setView([lat, lon], 13);
                            
                            if (userMarker) map.removeLayer(userMarker);
                            userMarker = L.marker([lat, lon]).addTo(map).bindPopup(`<div style="text-align: center;"><strong><i class="fas fa-user"></i> Tu Ubicación</strong><br>Lat: ${lat.toFixed(6)}<br>Lon: ${lon.toFixed(6)}</div>`).openPopup();
                            
                            L.circle([lat, lon], { radius: position.coords.accuracy, color: 'blue', fillColor: '#007bff', fillOpacity: 0.1, weight: 1 }).addTo(map);
                            hideError();
                        },
                        function(error) {
                            let errorMsg = 'Error al obtener la ubicación: ';
                            switch(error.code) {
                                case error.PERMISSION_DENIED: errorMsg += 'Permiso denegado por el usuario.'; break;
                                case error.POSITION_UNAVAILABLE: errorMsg += 'La información de ubicación no está disponible.'; break;
                                case error.TIMEOUT: errorMsg += 'Tiempo de espera agotado.'; break;
                                default: errorMsg += 'Error desconocido.';
                            }
                            showError(errorMsg);
                            resetLocationButton();
                        },
                        { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 }
                    );
                } else {
                    showError('La geolocalización no es soportada por este navegador.');
                    resetLocationButton();
                }
            });
            
            findRouteBtn.addEventListener('click', async function() {
                if (!userLocation) {
                    showError('Primero obtén tu ubicación actual.');
                    return;
                }
                
                loading.style.display = 'block';
                results.innerHTML = '';
                
                try {
                    const response = await fetch('/calcular-ruta-json', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ lat: userLocation.lat, lon: userLocation.lon })
                    });
                    
                    if (!response.ok) {
                        const errorData = await response.json();
                        throw new Error(errorData.error || 'Error del servidor');
                    }
                    
                    const data = await response.json();
                    displayResults(data);
                    
                } catch (error) {
                    showError('Error al calcular la ruta: ' + error.message);
                } finally {
                    loading.style.display = 'none';
                }
            });
            
            function displayResults(data) {
                results.innerHTML = `
                    <div class="info-card">
                        <h3><i class="fas fa-home"></i> ONG Más Cercana</h3>
                        <p><strong>Nombre:</strong> ${data.ong_destino.nombre}</p>
                        <p><strong>Tipo:</strong> ${data.ong_destino.tipo}</p>
                        <p><strong>Municipio:</strong> ${data.ong_destino.municipio}</p>
                        <p><strong>Distancia:</strong> ${data.ong_destino.distancia_km} km</p>
                    </div>
                    ${data.recomendacion ? `<div class="info-card"><h3><i class="fas fa-star"></i> Próxima Recomendación</h3><p><strong>Nombre:</strong> ${data.recomendacion.name}</p><p><strong>Distancia:</strong> ${data.recomendacion.distancia} km</p></div>` : ''}
                `;
                
                if (destinationMarker) map.removeLayer(destinationMarker);
                destinationMarker = L.marker([data.ong_destino.lat, data.ong_destino.lon]).addTo(map).bindPopup(`<div style="text-align: center;"><strong><i class="fas fa-home"></i> ${data.ong_destino.nombre}</strong><br>${data.ong_destino.tipo}<br>Distancia: ${data.ong_destino.distancia_km} km</div>`);
                
                if (routeLayer) map.removeLayer(routeLayer);
                routeLayer = L.polyline([[userLocation.lat, userLocation.lon], [data.ong_destino.lat, data.ong_destino.lon]], { color: 'blue', weight: 4, opacity: 0.7, dashArray: '10, 10' }).addTo(map);
                
                const group = new L.featureGroup([userMarker, destinationMarker]);
                map.fitBounds(group.getBounds().pad(0.1));
            }
            
            function showError(message) {
                errorMessage.innerHTML = `<i class="fas fa-exclamation-triangle"></i> ${message}`;
                errorMessage.style.display = 'block';
            }
            function hideError() { errorMessage.style.display = 'none'; }
            function resetLocationButton() {
                getLocationBtn.disabled = false;
                getLocationBtn.innerHTML = '<i class="fas fa-map-marker-alt"></i> Obtener Mi Ubicación Actual';
                getLocationBtn.style.background = 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)';
            }
            
            window.addEventListener('load', function() {
                setTimeout(() => { getLocationBtn.click(); }, 1000);
            });
        </script>
    </body>
    </html>
    '''

@app.route('/health')
def health_check():
    """Endpoint de salud del servidor"""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        conn.close()
        return jsonify({"status": "healthy", "service": "ruta-migrante", "database": "connected", "timestamp": time.time()})
    except Exception as e:
        return jsonify({"status": "unhealthy", "service": "ruta-migrante", "database": "disconnected", "error": str(e)}), 500

@app.route('/calcular-ruta', methods=['POST'])
def calcular_ruta_endpoint():
    """Endpoint principal para calcular rutas"""
    start_time = time.time()
    
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({"error": "Se requiere JSON con lat y lon"}), 400
        
        lat = data.get('lat')
        lon = data.get('lon')
        
        if not lat or not lon:
            return jsonify({"error": "Se requieren latitud (lat) y longitud (lon)"}), 400
        
        logger.info(f"📍 Solicitud de ruta recibida: ({lat}, {lon})")
        
        try:
            lat = float(lat)
            lon = float(lon)
            if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                return jsonify({"error": "Coordenadas fuera de rango válido"}), 400
        except ValueError:
            return jsonify({"error": "Coordenadas deben ser números válidos"}), 400
        
        waypoints, riesgo_por_municipio = cargar_datos_ongs()
        if not waypoints:
            return jsonify({"error": "No se pudieron cargar los datos de ONGs"}), 500
        
        start_point = (lat, lon)
        
        ong_cercana = ong_mas_cercana(start_point, waypoints)
        if not ong_cercana:
            return jsonify({"error": "No se encontró ninguna ONG cercana"}), 404
        
        recomendacion = obtener_recomendacion_norte(start_point, waypoints, ong_cercana)
        
        dest_point = (ong_cercana['lat'], ong_cercana['lon'])
        route_coords = None
        
        try:
            route_coords, G = calcular_ruta_optimizada(start_point, dest_point, timeout=120)
        except Exception as e:
            logger.warning(f"⚠️ No se pudo calcular ruta detallada: {e}")
        
        mapa = generar_mapa_completo(start_point, ong_cercana, waypoints, riesgo_por_municipio, route_coords, recomendacion)
        
        html_content = mapa.get_root().render()
        
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
        return jsonify({"error": "Error interno del servidor", "message": str(e)}), 500

@app.route('/calcular-ruta-json', methods=['POST'])
def calcular_ruta_json():
    """Endpoint alternativo que devuelve JSON en lugar de HTML"""
    start_time = time.time()
    
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({"error": "Se requiere JSON con lat y lon"}), 400
        
        lat = data.get('lat')
        lon = data.get('lon')
        
        if not lat or not lon:
            return jsonify({"error": "Se requieren latitud (lat) y longitud (lon)"}), 400
        
        waypoints, riesgo_por_municipio = cargar_datos_ongs()
        if not waypoints:
            return jsonify({"error": "No se pudieron cargar los datos de ONGs"}), 500
        
        start_point = (float(lat), float(lon))
        ong_cercana = ong_mas_cercana(start_point, waypoints)
        
        if not ong_cercana:
            return jsonify({"error": "No se encontró ninguna ONG cercana"}), 404
        
        recomendacion = obtener_recomendacion_norte(start_point, waypoints, ong_cercana)
        
        processing_time = time.time() - start_time
        logger.info(f"✅ Respuesta JSON generada en {processing_time:.2f} segundos")
        
        return jsonify({
            "success": True,
            "processing_time": processing_time,
            "usuario": {"lat": lat, "lon": lon},
            "ong_destino": {
                "nombre": ong_cercana['name'],
                "tipo": ong_cercana['type'],
                "municipio": ong_cercana.get('municipio', 'Desconocido'),
                "lat": ong_cercana['lat'],
                "lon": ong_cercana['lon'],
                "distancia_km": round(ong_cercana['distancia'], 2)
            },
            "recomendacion": recomendacion,
            "total_ongs": len(waypoints)
        })
        
    except Exception as e:
        logger.error(f"❌ Error en endpoint JSON: {e}")
        return jsonify({"error": f"Error interno: {str(e)}"}), 500

@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Endpoint no encontrado"}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": "Error interno del servidor"}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    debug = os.environ.get('DEBUG', 'False').lower() == 'true'
    
    logger.info(f"🚀 Iniciando servidor optimizado en puerto {port}")
    app.run(host='0.0.0.0', port=port, debug=debug)
