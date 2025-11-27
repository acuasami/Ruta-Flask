from flask import Flask, request, jsonify, render_template_string
import os
import math
import pandas as pd
import psycopg2
from psycopg2 import pool
from geopy.distance import geodesic
import networkx as nx
from urllib.parse import urlparse
from contextlib import contextmanager
import time


app = Flask(__name__)


# --- CONFIGURACIÓN BD CON POOL ---
uri = 'postgresql://postgres:KAGJhRklTcsevGqKEgCNPfmdDiGzsLyQ@switchyard.proxy.rlwy.net:13155/railway'
result = urlparse(uri)
DB_CONFIG = {
    'user': result.username,
    'password': result.password,
    'host': result.hostname,
    'port': result.port,
    'dbname': result.path.lstrip('/')
}

connection_pool = pool.SimpleConnectionPool(1, 5, **DB_CONFIG)

@contextmanager
def get_db_connection():
    conn = None
    try:
        conn = connection_pool.getconn()
        yield conn
    finally:
        if conn:
            connection_pool.putconn(conn)


# --- CACHÉ GLOBAL ---
class DataCache:
    def __init__(self):
        self.nodos = None
        self.riesgo = None
        self.last_update = 0
        self.TTL = 3600
    
    def is_expired(self):
        return (time.time() - self.last_update) > self.TTL
    
    def invalidate(self):
        self.nodos = None
        self.riesgo = None
        self.last_update = 0


cache = DataCache()


# --- FUNCIONES DE CARGA ---
def conectar_y_leer_sql(query):
    try:
        with get_db_connection() as conn:
            df = pd.read_sql(query, conn)
            return df
    except Exception as e:
        print(f"❌ Error BD: {e}")
        return pd.DataFrame()


def cargar_nodos():
    """Carga ONGs con caché"""
    if cache.nodos is not None and not cache.is_expired():
        return cache.nodos
    
    query = """
    SELECT o.id_ong, o.nom_ong, o.tipo, o.latitud, o.longitud, m.nom_municipio 
    FROM public.ongs o
    JOIN public.municipio m ON o.id_municipio = m.id_municipio;
    """
    df = conectar_y_leer_sql(query)
    
    nodos = []
    for _, row in df.iterrows():
        try:
            nodos.append({
                'id': row['id_ong'],
                'name': row['nom_ong'],
                'type': str(row['tipo']).strip().lower(),
                'lat': float(row['latitud']),
                'lon': float(row['longitud']),
                'municipio': row['nom_municipio']
            })
        except Exception as e:
            print(f"⚠️ Fila ignorada: {e}")
            continue
    
    cache.nodos = nodos
    cache.last_update = time.time()
    print(f"[INFO] Cargados {len(nodos)} nodos")
    return nodos


def cargar_datos_riesgo():
    """Carga riesgo por municipio"""
    if cache.riesgo is not None and not cache.is_expired():
        return cache.riesgo
    
    try:
        df_fecha = conectar_y_leer_sql("SELECT * FROM public.fecha;")
        df_municipio = conectar_y_leer_sql("SELECT * FROM public.municipio;")
        
        if df_fecha.empty or df_municipio.empty:
            return {}
        
        df_fecha['fecha'] = pd.to_datetime(df_fecha['fecha'])
        ultimo_mes = df_fecha['fecha'].max().month
        ultimo_ano = df_fecha['fecha'].max().year
        
        df_ultimo = df_fecha[(df_fecha['fecha'].dt.month == ultimo_mes) &
                             (df_fecha['fecha'].dt.year == ultimo_ano)]
        
        df_riesgo = pd.merge(df_ultimo, df_municipio, on='id_municipio')
        riesgo_dict = dict(zip(df_riesgo['nom_municipio'], df_riesgo['grado']))
        
        cache.riesgo = riesgo_dict
        return riesgo_dict
    except Exception as e:
        print(f"❌ Error cargando riesgo: {e}")
        return {}


# --- LÓGICA DE BÚSQUEDA ---
def ong_mas_cercana(pos_actual, nodos):
    """Encuentra la ONG más cercana (cualquier tipo)"""
    if not nodos:
        return None
    
    min_dist = float('inf')
    ong_cercana = None
    
    for ong in nodos:
        dist = geodesic(pos_actual, (ong['lat'], ong['lon'])).kilometers
        if dist < min_dist:
            min_dist = dist
            ong_cercana = ong.copy()
            ong_cercana['distancia'] = dist
    
    return ong_cercana


def find_ongs_north(start, nodos, current_ong=None):
    """
    Encuentra ONGs al NORTE de la posición actual,
    ordenadas por distancia
    """
    candidates = []
    start_lat, start_lon = start
    
    for ong in nodos:
        # Excluir ONG destino actual
        if current_ong and ong['name'] == current_ong.get('name'):
            continue
        
        # FILTRO: Latitud mayor (más al norte)
        if ong['lat'] > start_lat:
            dist = geodesic(start, (ong['lat'], ong['lon'])).kilometers
            ong_copy = ong.copy()
            ong_copy['distancia'] = dist
            candidates.append(ong_copy)
    
    candidates.sort(key=lambda x: x['distancia'])
    return candidates


def obtener_municipio_por_proximidad(lat, lon, nodos):
    """Encuentra el municipio más cercano"""
    min_dist = float('inf')
    municipio = 'Desconocido'
    
    for ong in nodos:
        dist = geodesic((lat, lon), (ong['lat'], ong['lon'])).kilometers
        if dist < min_dist:
            min_dist = dist
            municipio = ong.get('municipio', 'Desconocido')
    
    return municipio


# --- ENDPOINTS ---
@app.route('/api/calcular-ruta', methods=['GET'])
def api_ruta():
    """
    Devuelve:
    - ong_cercana: ONG destino más cercana
    - siguiente_recomendacion: Siguiente ONG hacia el norte
    - todas_ongs: Lista de TODAS las ONGs para mostrar en mapa
    - riesgo_por_municipio: Diccionario de riesgos
    """
    try:
        lat = request.args.get('lat', type=float)
        lon = request.args.get('lon', type=float)
        
        if lat is None or lon is None:
            return jsonify({"success": False, "msg": "Faltan parámetros lat y lon"}), 400
        
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            return jsonify({"success": False, "msg": "Coordenadas fuera de rango"}), 400
        
        nodos = cargar_nodos()
        riesgo = cargar_datos_riesgo()
        
        if not nodos:
            return jsonify({"success": False, "msg": "No hay ONGs en la BD"}), 400
        
        # Calcular ONG cercana y recomendaciones
        ong_cercana = ong_mas_cercana((lat, lon), nodos)
        ongs_al_norte = find_ongs_north((lat, lon), nodos, ong_cercana)
        siguiente_recomendacion = ongs_al_norte[0] if ongs_al_norte else None
        
        return jsonify({
            "success": True,
            "ong_cercana": ong_cercana,
            "siguiente_recomendacion": siguiente_recomendacion,
            "todas_ongs": nodos,
            "riesgo_por_municipio": riesgo,
            "ongs_al_norte": ongs_al_norte[:5]  # Top 5
        })
        
    except Exception as e:
        return jsonify({"success": False, "msg": f"Error: {str(e)}"}), 500


@app.route('/mapa', methods=['GET'])
def mapa_google():
    """Sirve el mapa con Google Maps mejorado"""
    lat = request.args.get('lat', default='19.325521', type=str)
    lon = request.args.get('lon', default='-99.167807', type=str)
    id_usuario = request.args.get('id_usuario', default='0', type=str)
    
    html = render_template_string(
        HTML_GOOGLE_MAPS,
        lat=lat,
        lon=lon,
        id_usuario=id_usuario
    )
    return html


@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "ok"}), 200


@app.route('/favicon.ico')
def favicon():
    return '', 204


# --- PLANTILLA HTML CON MARCADORES Y PANEL ---
HTML_GOOGLE_MAPS = """
<!DOCTYPE html>
<html>
<head>
    <title>Ruta Migrante - Google Maps</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body, html { height: 100%; margin: 0; padding: 0; font-family: Arial, sans-serif; }
        #map { height: 100%; width: 100%; }
        
        #info-box {
            position: fixed; bottom: 20px; left: 20px; right: 20px;
            background: white; padding: 15px; border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.3); z-index: 10;
            max-width: 500px; margin: auto; max-height: 300px; overflow-y: auto;
        }
        
        #toggle-btn {
            position: fixed; bottom: 340px; left: 20px;
            background: #4285F4; color: white; padding: 10px 15px;
            border-radius: 4px; cursor: pointer; z-index: 10; border: none;
            font-weight: bold;
        }
        
        .btn {
            background: #4285F4; color: white; border: none; padding: 10px 20px;
            border-radius: 4px; cursor: pointer; width: 100%; font-size: 14px;
            margin-top: 10px;
        }
        
        .info-section {
            background: #f5f5f5; padding: 10px; margin: 5px 0;
            border-left: 4px solid #4285F4; border-radius: 4px;
        }
        
        .ong-item {
            font-size: 12px; margin: 3px 0; padding: 5px;
            background: white; border-left: 3px solid #FF9800;
            border-radius: 3px;
        }
        
        .risk-high { color: red; font-weight: bold; }
        .risk-medium { color: orange; font-weight: bold; }
        .risk-low { color: green; font-weight: bold; }
        
        .legend {
            position: fixed; top: 20px; right: 20px;
            background: white; padding: 10px; border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.3); z-index: 10;
            font-size: 12px;
        }
    </style>
</head>
<body>
    <div id="map"></div>
    
    <button id="toggle-btn" onclick="toggleInfoBox()">📋 Información</button>
    
    <div id="info-box" style="display:none;">
        <h3 style="margin-top:0; color:#4285F4;">🗺️ Análisis de Ruta</h3>
        
        <div id="user-info" class="info-section">
            <strong>👤 Tu Ubicación:</strong>
            <div id="user-coords"></div>
            <div id="user-id"></div>
        </div>
        
        <div id="dest-info" class="info-section">
            <strong>🎯 ONG Más Cercana:</strong>
            <div id="dest-name"></div>
            <div id="dest-distance"></div>
            <div id="dest-type"></div>
        </div>
        
        <div id="rec-info" class="info-section">
            <strong>⭐ Siguiente Recomendación (Norte):</strong>
            <div id="rec-name"></div>
            <div id="rec-distance"></div>
        </div>
        
        <div id="risk-info" class="info-section">
            <strong>⚠️ Riesgo del Municipio:</strong>
            <div id="risk-level"></div>
        </div>
        
        <div id="nearby-ongs" class="info-section">
            <strong>📍 ONGs Cercanas al Norte:</strong>
            <div id="nearby-list"></div>
        </div>
    </div>
    
    <div class="legend">
        <div><strong>🎯 Marcadores</strong></div>
        <div>🔵 Tu ubicación</div>
        <div>🟢 ONG Destino</div>
        <div>🟠 Recomendación Norte</div>
        <div>🔴 Otros puntos</div>
    </div>

    <script src="https://maps.googleapis.com/maps/api/js?key=AIzaSyAHQChFMbUZcIKS3srHRzEoIHSPEtJ5GFQ"></script>
    
    <script>
        const initialLat = parseFloat("{{ lat }}");
        const initialLon = parseFloat("{{ lon }}");
        const idUsuario = "{{ id_usuario }}";
        
        let map;
        let markers = [];
        let userPos = { lat: initialLat, lng: initialLon };
        
        function initMap() {
            map = new google.maps.Map(document.getElementById("map"), {
                zoom: 11,
                center: userPos,
                mapTypeControl: false,
                streetViewControl: false
            });
            
            // Marcador del usuario
            new google.maps.Marker({
                position: userPos,
                map: map,
                title: "Tu ubicación",
                icon: "http://maps.google.com/mapfiles/ms/icons/blue-dot.png"
            });
            
            document.getElementById("user-coords").textContent = 
                `${userPos.lat.toFixed(4)}, ${userPos.lng.toFixed(4)}`;
            document.getElementById("user-id").textContent = `Usuario ID: ${idUsuario}`;
            
            // Cargar datos
            cargarDatosRuta();
        }
        
        async function cargarDatosRuta() {
            try {
                const response = await fetch(
                    `/api/calcular-ruta?lat=${userPos.lat}&lon=${userPos.lng}`
                );
                const data = await response.json();
                
                if (!data.success) {
                    document.getElementById("dest-name").textContent = "❌ " + data.msg;
                    return;
                }
                
                const { ong_cercana, siguiente_recomendacion, todas_ongs, riesgo_por_municipio, ongs_al_norte } = data;
                
                // Mostrar información en panel
                if (ong_cercana) {
                    document.getElementById("dest-name").textContent = ong_cercana.name;
                    document.getElementById("dest-distance").textContent = 
                        `📏 ${ong_cercana.distancia.toFixed(2)} km`;
                    document.getElementById("dest-type").textContent = 
                        `Tipo: ${ong_cercana.type}`;
                    
                    // Marcador ONG destino
                    new google.maps.Marker({
                        position: { lat: ong_cercana.lat, lng: ong_cercana.lon },
                        map: map,
                        title: ong_cercana.name,
                        icon: "http://maps.google.com/mapfiles/ms/icons/green-dot.png"
                    });
                    
                    // Línea a destino
                    new google.maps.Polyline({
                        path: [userPos, { lat: ong_cercana.lat, lng: ong_cercana.lon }],
                        geodesic: true,
                        strokeColor: '#4285F4',
                        strokeOpacity: 0.7,
                        strokeWeight: 3,
                        map: map
                    });
                }
                
                if (siguiente_recomendacion) {
                    document.getElementById("rec-name").textContent = siguiente_recomendacion.name;
                    document.getElementById("rec-distance").textContent = 
                        `📏 ${siguiente_recomendacion.distancia.toFixed(2)} km`;
                    
                    // Marcador recomendación
                    new google.maps.Marker({
                        position: { lat: siguiente_recomendacion.lat, lng: siguiente_recomendacion.lon },
                        map: map,
                        title: siguiente_recomendacion.name + " (Recomendación)",
                        icon: "http://maps.google.com/mapfiles/ms/icons/orange-dot.png"
                    });
                }
                
                // Mostrar municipio y riesgo
                const mun = ong_cercana ? ong_cercana.municipio : "Desconocido";
                const riesgo = riesgo_por_municipio[mun] || "Desconocido";
                const riesgoClass = riesgo === 'Alto' ? 'risk-high' : 
                                  riesgo === 'Medio' ? 'risk-medium' : 'risk-low';
                document.getElementById("risk-level").innerHTML = 
                    `<span class="${riesgoClass}">${riesgo}</span> en ${mun}`;
                
                // Mostrar ONGs al norte
                let nearbyHtml = '';
                if (ongs_al_norte && ongs_al_norte.length > 0) {
                    for (let ong of ongs_al_norte.slice(0, 5)) {
                        nearbyHtml += `
                            <div class="ong-item">
                                <strong>${ong.name}</strong> (${ong.type})<br>
                                📍 ${ong.municipio} - ${ong.distancia.toFixed(1)} km
                            </div>
                        `;
                        
                        // Marcador para cada ONG
                        new google.maps.Marker({
                            position: { lat: ong.lat, lng: ong.lon },
                            map: map,
                            title: ong.name,
                            icon: "http://maps.google.com/mapfiles/ms/icons/red-dot.png"
                        });
                    }
                } else {
                    nearbyHtml = '<div class="ong-item">No hay ONGs al norte cercanas</div>';
                }
                document.getElementById("nearby-list").innerHTML = nearbyHtml;
                
                // Mostrar TODAS las ONGs (puntos de referencia)
                if (todas_ongs) {
                    for (let ong of todas_ongs) {
                        // Evitar duplicar marcadores ya mostrados
                        if (ong_cercana && ong.id === ong_cercana.id) continue;
                        if (siguiente_recomendacion && ong.id === siguiente_recomendacion.id) continue;
                        
                        new google.maps.Marker({
                            position: { lat: ong.lat, lng: ong.lon },
                            map: map,
                            title: ong.name,
                            icon: "http://maps.google.com/mapfiles/ms/icons/yellow-dot.png"
                        });
                    }
                }
                
            } catch (e) {
                console.error(e);
                document.getElementById("dest-name").textContent = "❌ Error: " + e.message;
            }
        }
        
        function toggleInfoBox() {
            const box = document.getElementById("info-box");
            box.style.display = box.style.display === 'none' ? 'block' : 'none';
        }
        
        window.onload = initMap;
    </script>
</body>
</html>
"""


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
