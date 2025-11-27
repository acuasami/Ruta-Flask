from flask import Flask, request, jsonify, render_template_string
import os
import math
import pandas as pd
import psycopg2
from psycopg2 import pool
from geopy.distance import geodesic
from urllib.parse import urlparse
from contextlib import contextmanager
import time

app = Flask(__name__)

# --- CONFIGURACIÓN BD CON POOL ---
# Reemplaza con tu URL de producción si es necesario
uri = 'postgresql://postgres:KAGJhRklTcsevGqKEgCNPfmdDiGzsLyQ@switchyard.proxy.rlwy.net:13155/railway'
result = urlparse(uri)
DB_CONFIG = {
    'user': result.username,
    'password': result.password,
    'host': result.hostname,
    'port': result.port,
    'dbname': result.path.lstrip('/')
}

try:
    connection_pool = pool.SimpleConnectionPool(1, 5, **DB_CONFIG)
except Exception as e:
    print(f"Error creando pool de conexiones: {e}")
    connection_pool = None

@contextmanager
def get_db_connection():
    conn = None
    try:
        if connection_pool:
            conn = connection_pool.getconn()
            yield conn
        else:
            raise Exception("Pool de conexiones no inicializado")
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
    
    # Obtenemos municipio y tipo
    query = """
    SELECT o.id_ong, o.nom_ong, o.tipo, o.latitud, o.longitud, m.nom_municipio 
    FROM public.ongs o
    JOIN public.municipio m ON o.id_municipio = m.id_municipio;
    """
    df = conectar_y_leer_sql(query)
    
    nodos = []
    for _, row in df.iterrows():
        try:
            tipo_raw = str(row['tipo']).strip()
            tipo_fmt = tipo_raw.capitalize() 

            nodos.append({
                'id': row['id_ong'],
                'name': row['nom_ong'],
                'type': tipo_fmt, 
                'lat': float(row['latitud']),
                'lon': float(row['longitud']),
                'municipio': row['nom_municipio']
            })
        except Exception as e:
            continue
    
    cache.nodos = nodos
    cache.last_update = time.time()
    return nodos

def cargar_datos_riesgo():
    """Carga riesgo por municipio y normaliza el diccionario"""
    if cache.riesgo is not None and not cache.is_expired():
        return cache.riesgo
    
    try:
        df_fecha = conectar_y_leer_sql("SELECT * FROM public.fecha;")
        df_municipio = conectar_y_leer_sql("SELECT * FROM public.municipio;")
        
        if df_fecha.empty or df_municipio.empty:
            return {}
        
        df_fecha['fecha'] = pd.to_datetime(df_fecha['fecha'])
        ultimo_fecha = df_fecha['fecha'].max()
        
        df_ultimo = df_fecha[(df_fecha['fecha'].dt.month == ultimo_fecha.month) &
                             (df_fecha['fecha'].dt.year == ultimo_fecha.year)]
        
        df_riesgo = pd.merge(df_ultimo, df_municipio, on='id_municipio')
        
        # Limpiamos espacios en blanco para asegurar coincidencias
        riesgo_dict = {}
        for _, row in df_riesgo.iterrows():
            mun = str(row['nom_municipio']).strip()
            grado = str(row['grado']).strip().capitalize() # Asegura formato 'Alto', 'Medio', 'Bajo'
            riesgo_dict[mun] = grado
        
        cache.riesgo = riesgo_dict
        return riesgo_dict
    except Exception as e:
        print(f"❌ Error cargando riesgo: {e}")
        return {}

# --- LÓGICA DE BÚSQUEDA ---
def ong_mas_cercana(pos_actual, nodos):
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
    candidates = []
    start_lat, _ = start
    
    for ong in nodos:
        if current_ong and ong['name'] == current_ong.get('name'):
            continue
        
        if ong['lat'] > start_lat:
            dist = geodesic(start, (ong['lat'], ong['lon'])).kilometers
            ong_copy = ong.copy()
            ong_copy['distancia'] = dist
            candidates.append(ong_copy)
    
    candidates.sort(key=lambda x: x['distancia'])
    return candidates

# --- ENDPOINTS ---
@app.route('/api/calcular-ruta', methods=['GET'])
def api_ruta():
    try:
        lat = request.args.get('lat', type=float)
        lon = request.args.get('lon', type=float)
        
        if lat is None or lon is None:
            return jsonify({"success": False, "msg": "Faltan parámetros lat y lon"}), 400
        
        nodos = cargar_nodos()
        riesgo_dict = cargar_datos_riesgo()
        
        if not nodos:
            return jsonify({"success": False, "msg": "No hay ONGs en la BD"}), 400
        
        ong_cercana = ong_mas_cercana((lat, lon), nodos)
        ongs_al_norte = find_ongs_north((lat, lon), nodos, ong_cercana)
        siguiente_recomendacion = ongs_al_norte[0] if ongs_al_norte else None
        
        # --- LÓGICA DE COLOR DE RUTA ---
        route_color = "#808080" # Gris por defecto
        riesgo_detectado = "Desconocido"
        
        if ong_cercana:
            municipio_ong = str(ong_cercana.get('municipio', '')).strip()
            riesgo_detectado = riesgo_dict.get(municipio_ong, "Desconocido")
            
            # Asignar color según riesgo
            if riesgo_detectado == "Alto":
                route_color = "#FF0000" # Rojo
            elif riesgo_detectado == "Medio":
                route_color = "#FFA500" # Naranja
            elif riesgo_detectado == "Bajo":
                route_color = "#008000" # Verde
            
            # Inyectamos datos extra a la ONG para el frontend
            ong_cercana['riesgo_nivel'] = riesgo_detectado
            ong_cercana['ruta_color'] = route_color

        return jsonify({
            "success": True,
            "ong_cercana": ong_cercana,
            "siguiente_recomendacion": siguiente_recomendacion,
            "todas_ongs": nodos,
            "riesgo_por_municipio": riesgo_dict,
            "ongs_al_norte": ongs_al_norte[:5]
        })
        
    except Exception as e:
        print(f"Error api: {e}")
        return jsonify({"success": False, "msg": f"Error: {str(e)}"}), 500

@app.route('/mapa', methods=['GET'])
def mapa_google():
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

# --- PLANTILLA HTML MEJORADA ---
HTML_GOOGLE_MAPS = """
<!DOCTYPE html>
<html>
<head>
    <title>Ruta Migrante</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body, html { height: 100%; margin: 0; padding: 0; font-family: 'Segoe UI', Arial, sans-serif; }
        #map { height: 100%; width: 100%; }
        
        /* Panel de Información Flotante */
        #info-box {
            position: fixed; bottom: 20px; left: 20px; right: 20px;
            background: white; padding: 15px; border-radius: 12px;
            box-shadow: 0 4px 15px rgba(0,0,0,0.2); z-index: 10;
            max-width: 400px; max-height: 40vh; overflow-y: auto;
            margin-left: auto; margin-right: auto;
            display: none;
        }
        
        #toggle-btn {
            position: fixed; bottom: 30px; right: 20px;
            background: #4285F4; color: white; width: 50px; height: 50px;
            border-radius: 50%; border: none; 
            box-shadow: 0 2px 10px rgba(0,0,0,0.3);
            cursor: pointer; z-index: 11; display: flex;
            align-items: center; justify-content: center; font-size: 24px;
        }

        .info-section { margin-bottom: 10px; border-bottom: 1px solid #eee; padding-bottom: 5px; }
        .info-title { font-weight: bold; color: #555; font-size: 0.9em; }
        .info-content { font-size: 1.1em; margin-top: 2px; }
        
        /* Leyenda Mejorada */
        .legend {
            position: fixed; top: 10px; left: 10px;
            background: rgba(255, 255, 255, 0.95); padding: 10px; border-radius: 8px;
            box-shadow: 0 2px 5px rgba(0,0,0,0.1); z-index: 10;
            font-size: 12px;
            max-width: 150px;
        }
        .legend-title { font-weight: bold; margin-bottom: 5px; border-bottom: 1px solid #ccc; padding-bottom: 2px; }
        .legend-item { display: flex; align-items: center; margin-bottom: 4px; }
        
        /* Puntos (Marcadores) */
        .dot { width: 12px; height: 12px; border-radius: 50%; margin-right: 8px; display: inline-block; border: 1px solid #fff; }
        
        /* Líneas (Riesgo) */
        .line { width: 20px; height: 4px; margin-right: 8px; display: inline-block; border-radius: 2px; }
    </style>
</head>
<body>
    <div id="map"></div>
    
    <button id="toggle-btn" onclick="toggleInfoBox()">ℹ️</button>
    
    <div id="info-box">
        <h3 style="margin-top:0; color:#4285F4;">Detalles de Ruta</h3>
        <div id="loading">Cargando datos...</div>
        <div id="content" style="display:none;">
            <div class="info-section">
                <div class="info-title">Destino Sugerido:</div>
                <div class="info-content" id="dest-name"></div>
                <div style="font-size:0.9em; color:#777;" id="dest-meta"></div>
            </div>
             <div class="info-section">
                <div class="info-title">Nivel de Riesgo (Color Ruta):</div>
                <div class="info-content" id="risk-level" style="font-weight:bold;"></div>
            </div>
            <div class="info-section">
                <div class="info-title">Siguiente Parada (Norte):</div>
                <div class="info-content" id="rec-name">--</div>
            </div>
        </div>
    </div>
    
    <div class="legend">
        <div class="legend-title">Tipo de ONG</div>
        <div class="legend-item"><span class="dot" style="background:blue;"></span>Albergue</div>
        <div class="legend-item"><span class="dot" style="background:orange;"></span>Comedor</div>
        <div class="legend-item"><span class="dot" style="background:red;"></span>Frontera</div>
        
        <div class="legend-title" style="margin-top:8px;">Riesgo de Ruta</div>
        <div class="legend-item"><span class="line" style="background:green;"></span>Riesgo Bajo</div>
        <div class="legend-item"><span class="line" style="background:orange;"></span>Riesgo Medio</div>
        <div class="legend-item"><span class="line" style="background:red;"></span>Riesgo Alto</div>
    </div>

    <script src="https://maps.googleapis.com/maps/api/js?key=AIzaSyAHQChFMbUZcIKS3srHRzEoIHSPEtJ5GFQ"></script>
    
    <script>
        const initialLat = parseFloat("{{ lat }}");
        const initialLon = parseFloat("{{ lon }}");
        
        let map;
        let directionsService;
        let directionsRenderer;
        
        // Mapa de colores para MARCADORES (Puntos)
        const markerColorMap = {
            'Albergue': 'blue',
            'Comedor': 'orange',
            'Frontera': 'red',
            'default': 'gray'
        };

        function getMarkerColor(type) {
            if (!type) return markerColorMap['default'];
            const formatted = type.charAt(0).toUpperCase() + type.slice(1).toLowerCase();
            return markerColorMap[formatted] || markerColorMap['default'];
        }

        const mapStyles = [
            { "featureType": "poi", "stylers": [{ "visibility": "off" }] },
            { "featureType": "transit", "stylers": [{ "visibility": "off" }] }
        ];

        function initMap() {
            const userPos = { lat: initialLat, lng: initialLon };

            map = new google.maps.Map(document.getElementById("map"), {
                zoom: 12,
                center: userPos,
                styles: mapStyles,
                mapTypeControl: false,
                streetViewControl: false,
                fullscreenControl: false
            });

            directionsService = new google.maps.DirectionsService();
            // Inicializamos el renderer, pero el color se definirá dinámicamente luego
            directionsRenderer = new google.maps.DirectionsRenderer({
                map: map,
                suppressMarkers: true, 
                preserveViewport: false
            });

            // Marcador del usuario
            // Pasamos un objeto dummy para que funcione la función genérica
            crearMarcador({
                lat: initialLat,
                lon: initialLon,
                name: "Tu ubicación",
                type: "Usuario",
                municipio: "Actual"
            }, "purple", 1.2);

            cargarDatos();
        }

        // --- FUNCIÓN MODIFICADA PARA FICHAS INFORMATIVAS ---
        function crearMarcador(ongData, colorOverride=null, escala=1) {
            const color = colorOverride || getMarkerColor(ongData.type);
            const pos = { lat: ongData.lat, lng: ongData.lon };
            
            const marker = new google.maps.Marker({
                position: pos,
                map: map,
                title: ongData.name,
                icon: {
                    path: google.maps.SymbolPath.CIRCLE,
                    scale: 8 * escala,
                    fillColor: color,
                    fillOpacity: 1,
                    strokeWeight: 2,
                    strokeColor: 'white'
                }
            });
            
            // CONTENIDO HTML DE LA FICHA (Popup)
            const contentString = `
                <div style="font-family: Arial, sans-serif; padding: 5px; min-width: 150px;">
                    <h3 style="margin: 0 0 5px 0; color: #333; font-size: 16px;">${ongData.name}</h3>
                    <p style="margin: 2px 0; font-size: 13px;">
                        <strong>Tipo:</strong> ${ongData.type || 'N/A'}
                    </p>
                    <p style="margin: 2px 0; font-size: 13px;">
                        <strong>Municipio:</strong> ${ongData.municipio || 'N/A'}
                    </p>
                </div>
            `;
            
            const infoWindow = new google.maps.InfoWindow({
                content: contentString
            });
            
            marker.addListener("click", () => {
                infoWindow.open(map, marker);
            });
            
            return marker;
        }

        async function cargarDatos() {
            try {
                const response = await fetch(`/api/calcular-ruta?lat=${initialLat}&lon=${initialLon}`);
                const data = await response.json();

                document.getElementById("loading").style.display = "none";
                document.getElementById("content").style.display = "block";

                if (!data.success) {
                    alert("Error: " + data.msg);
                    return;
                }

                // 1. DIBUJAR MARCADORES
                if (data.todas_ongs) {
                    data.todas_ongs.forEach(ong => {
                        crearMarcador(ong); // Usa la nueva lógica de fichas
                    });
                }

                // 2. TRAZAR RUTA Y PANEL
                if (data.ong_cercana) {
                    const ong = data.ong_cercana;
                    
                    document.getElementById("dest-name").innerText = ong.name;
                    document.getElementById("dest-meta").innerText = `${ong.type} • ${ong.municipio}`;
                    
                    // Mostrar Riesgo en Texto
                    const elRiesgo = document.getElementById("risk-level");
                    elRiesgo.innerText = `${ong.riesgo_nivel}`;
                    elRiesgo.style.color = ong.ruta_color; 

                    document.getElementById("toggle-btn").click();

                    // Trazar ruta pasando el color calculado en backend
                    trazarRuta(
                        { lat: initialLat, lng: initialLon }, 
                        { lat: ong.lat, lng: ong.lon },
                        ong.ruta_color // <--- COLOR DINÁMICO
                    );
                }

                if (data.siguiente_recomendacion) {
                    document.getElementById("rec-name").innerText = data.siguiente_recomendacion.name;
                }

            } catch (e) {
                console.error(e);
                document.getElementById("loading").innerText = "Error de conexión";
            }
        }

        function trazarRuta(origen, destino, colorRuta) {
            const request = {
                origin: origen,
                destination: destino,
                travelMode: google.maps.TravelMode.DRIVING
            };

            // Actualizamos las opciones del renderer con el nuevo color
            directionsRenderer.setOptions({
                polylineOptions: {
                    strokeColor: colorRuta,
                    strokeWeight: 6,
                    strokeOpacity: 0.8
                }
            });

            directionsService.route(request, function(result, status) {
                if (status == google.maps.DirectionsStatus.OK) {
                    directionsRenderer.setDirections(result);
                } else {
                    console.error("Ruta fallida: " + status);
                    map.setCenter(origen);
                }
            });
        }

        function toggleInfoBox() {
            const box = document.getElementById("info-box");
            box.style.display = (box.style.display === "none") ? "block" : "none";
        }

        window.onload = initMap;
    </script>
</body>
</html>
"""

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=True)