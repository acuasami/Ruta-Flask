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
        riesgo_dict = {}
        for _, row in df_riesgo.iterrows():
            mun = str(row['nom_municipio']).strip()
            grado = str(row['grado']).strip().capitalize()
            riesgo_dict[mun] = grado
        cache.riesgo = riesgo_dict
        return riesgo_dict
    except Exception as e:
        print(f"❌ Error cargando riesgo: {e}")
        return {}

# --- LÓGICA DE BÚSQUEDA Y ENCADENAMIENTO ---

def ong_mas_cercana(pos_actual, nodos):
    """Encuentra la ONG más cercana a una coordenada"""
    if not nodos: return None
    min_dist = float('inf')
    ong_cercana = None
    for ong in nodos:
        dist = geodesic(pos_actual, (ong['lat'], ong['lon'])).kilometers
        if dist < min_dist:
            min_dist = dist
            ong_cercana = ong.copy()
            ong_cercana['distancia'] = dist
    return ong_cercana

def obtener_siguiente_al_norte(nodo_actual, todos_nodos):
    """Busca el nodo más cercano estrictamente al norte del actual"""
    mejor_candidato = None
    min_dist = float('inf')
    
    lat_actual = nodo_actual['lat']
    
    for nodo in todos_nodos:
        # Debe estar al norte (latitud mayor) y no ser el mismo nodo
        if nodo['lat'] > lat_actual and nodo['id'] != nodo_actual['id']:
            dist = geodesic((lat_actual, nodo_actual['lon']), (nodo['lat'], nodo['lon'])).kilometers
            if dist < min_dist:
                min_dist = dist
                mejor_candidato = nodo.copy()
                mejor_candidato['distancia_desde_anterior'] = dist
                
    return mejor_candidato

def traza_ruta_norte_hasta_frontera(nodo_inicio, todos_nodos):
    """
    Genera una cadena de nodos: Inicio -> Siguiente(Norte) -> ... -> Frontera
    """
    ruta = [nodo_inicio]
    nodo_actual = nodo_inicio
    
    # Límite de seguridad para evitar bucles infinitos
    max_iteraciones = 120 
    
    for _ in range(max_iteraciones):
        # 1. Verificar si ya estamos en la frontera
        tipo = nodo_actual.get('type', '').lower()
        if 'frontera' in tipo:
            break # Terminamos la ruta
            
        # 2. Buscar el siguiente salto al norte
        siguiente = obtener_siguiente_al_norte(nodo_actual, todos_nodos)
        
        if siguiente:
            ruta.append(siguiente)
            nodo_actual = siguiente
        else:
            # No hay más nodos al norte
            break
            
    return ruta

# --- ENDPOINTS ---
# ... (código anterior igual)

@app.route('/api/calcular-ruta', methods=['GET'])
def api_ruta():
    try:
        lat = request.args.get('lat', type=float)
        lon = request.args.get('lon', type=float)
        
        if lat is None or lon is None:
            return jsonify({"success": False, "msg": "Faltan parámetros lat y lon"}), 400
        
        nodos = cargar_nodos() # Aquí ya tienes TODOS los nodos
        riesgo_dict = cargar_datos_riesgo()
        
        if not nodos:
            return jsonify({"success": False, "msg": "No hay ONGs en la BD"}), 400
        
        # 1. Encontrar la primera ONG más cercana
        primera_ong = ong_mas_cercana((lat, lon), nodos)
        
        if not primera_ong:
            return jsonify({"success": False, "msg": "No se pudo localizar una ONG cercana"}), 404

        # 2. Generar la cadena completa hasta la frontera
        ruta_secuencial = traza_ruta_norte_hasta_frontera(primera_ong, nodos)
        
        # Inyectar riesgo en la ruta secuencial
        for nodo in ruta_secuencial:
            municipio = str(nodo.get('municipio', '')).strip()
            nodo['riesgo_nivel'] = riesgo_dict.get(municipio, "Desconocido")
        
        # --- CAMBIO IMPORTANTE AQUÍ ---
        # También inyectamos el riesgo en la lista de TODOS los nodos para que
        # los marcadores fuera de la ruta también tengan info si la quieres.
        todos_nodos_con_info = []
        for nodo in nodos:
            n_copy = nodo.copy()
            mun = str(n_copy.get('municipio', '')).strip()
            n_copy['riesgo_nivel'] = riesgo_dict.get(mun, "Desconocido")
            todos_nodos_con_info.append(n_copy)

        return jsonify({
            "success": True,
            "ong_cercana": primera_ong,
            "ruta_secuencial": ruta_secuencial,
            "todos_nodos": todos_nodos_con_info, # <--- AGREGADO: Enviamos todo el universo de ONGs
            "destino_final": ruta_secuencial[-1] if ruta_secuencial else None
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

# --- PLANTILLA HTML MEJORADA PARA WAYPOINTS ---
# --- PLANTILLA HTML MEJORADA PARA WAYPOINTS ---
HTML_GOOGLE_MAPS = """
<!DOCTYPE html>
<html>
<head>
    <title>Ruta Migrante Completa</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body, html { height: 100%; margin: 0; padding: 0; font-family: 'Segoe UI', Arial, sans-serif; }
        #map { height: 100%; width: 100%; }
        
        #info-box {
            position: fixed; bottom: 20px; left: 20px; right: 20px;
            background: white; padding: 15px; border-radius: 12px;
            box-shadow: 0 4px 15px rgba(0,0,0,0.2); z-index: 10;
            max-width: 400px; max-height: 50vh; /* Aumentamos un poco la altura máxima */
            overflow-y: auto;
            margin-left: auto; margin-right: auto; display: none;
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

        /* Estilos nuevos para la lista de ruta */
        .route-list-container {
            margin-top: 10px;
            background-color: #f9f9f9;
            border-radius: 8px;
            padding: 10px;
            max-height: 200px; /* Scroll interno para la lista */
            overflow-y: auto;
        }
        .route-item {
            margin-bottom: 8px;
            padding-bottom: 8px;
            border-bottom: 1px solid #e0e0e0;
            font-size: 0.9em;
        }
        .route-item:last-child { border-bottom: none; margin-bottom: 0; }
        .route-item-name { font-weight: bold; color: #333; }
        .route-item-details { color: #666; font-size: 0.85em; }
        
        .legend {
            position: fixed; top: 10px; left: 10px;
            background: rgba(255, 255, 255, 0.95); padding: 10px; border-radius: 8px;
            box-shadow: 0 2px 5px rgba(0,0,0,0.1); z-index: 10;
            font-size: 12px; max-width: 150px;
        }
        .legend-title { font-weight: bold; margin-bottom: 5px; border-bottom: 1px solid #ccc; padding-bottom: 2px; }
        .legend-item { display: flex; align-items: center; margin-bottom: 4px; }
        .dot { width: 12px; height: 12px; border-radius: 50%; margin-right: 8px; display: inline-block; border: 1px solid #fff; }
        .line { width: 20px; height: 4px; margin-right: 8px; display: inline-block; border-radius: 2px; }
    </style>
</head>
<body>
    <div id="map"></div>
    <button id="toggle-btn" onclick="toggleInfoBox()">ℹ️</button>
    
    <div id="info-box">
        <h3 style="margin-top:0; color:#4285F4;">Ruta Sugerida a Frontera</h3>
        <div id="loading">Calculando ruta completa...</div>
        <div id="content" style="display:none;">
            <div class="info-section">
                <div class="info-title">Punto de Partida (ONG más cercana):</div>
                <div class="info-content" id="start-name"></div>
            </div>
             <div class="info-section">
                <div class="info-title">Destino Final (Frontera):</div>
                <div class="info-content" id="end-name" style="font-weight:bold; color:red;"></div>
            </div>
            <div class="info-section">
                <div class="info-title">Total de paradas:</div>
                <div class="info-content" id="total-stops"></div>
            </div>
            
            <div class="info-section" style="border-bottom: none;">
                <div class="info-title">Detalle del recorrido:</div>
                <div id="route-list" class="route-list-container">
                    </div>
            </div>
        </div>
    </div>
    
    <div class="legend">
        <div class="legend-title">Tipo de ONG</div>
        <div class="legend-item"><span class="dot" style="background:blue;"></span>Albergue</div>
        <div class="legend-item"><span class="dot" style="background:orange;"></span>Comedor</div>
        <div class="legend-item"><span class="dot" style="background:red;"></span>Frontera</div>
        
        <div class="legend-title" style="margin-top:8px;">Riesgo del Tramo</div>
        <div class="legend-item"><span class="line" style="background:green;"></span>Riesgo Bajo</div>
        <div class="legend-item"><span class="line" style="background:orange;"></span>Riesgo Medio</div>
        <div class="legend-item"><span class="line" style="background:red;"></span>Riesgo Alto</div>
        <div class="legend-item"><span class="line" style="background:gray;"></span>Desconocido</div>
    </div>

    <script src="https://maps.googleapis.com/maps/api/js?key=AIzaSyAHQChFMbUZcIKS3srHRzEoIHSPEtJ5GFQ"></script>
    
    <script>
        const initialLat = parseFloat("{{ lat }}");
        const initialLon = parseFloat("{{ lon }}");
        
        let map;
        let directionsService;
        let directionsRenderer;
        let routePolylines = [];
        
        const markerColorMap = {
            'Albergue': 'blue', 'Comedor': 'orange', 'Frontera': 'red', 'default': 'gray'
        };

        const riskColorMap = {
            'Bajo': '#008000',   // Verde
            'Medio': '#FFA500',  // Naranja
            'Alto': '#FF0000',   // Rojo
            'Desconocido': '#808080' // Gris
        };

        function getMarkerColor(type) {
            if (!type) return markerColorMap['default'];
            if (type.includes('Albergue')) return markerColorMap['Albergue'];
            if (type.includes('Comedor')) return markerColorMap['Comedor'];
            if (type.includes('Frontera')) return markerColorMap['Frontera'];
            return markerColorMap['default'];
        }

        const mapStyles = [
            { "featureType": "poi", "stylers": [{ "visibility": "off" }] },
            { "featureType": "transit", "stylers": [{ "visibility": "off" }] }
        ];

        function initMap() {
            const userPos = { lat: initialLat, lng: initialLon };

            map = new google.maps.Map(document.getElementById("map"), {
                zoom: 6, 
                center: userPos,
                styles: mapStyles,
                mapTypeControl: false, streetViewControl: false, fullscreenControl: false
            });

            directionsService = new google.maps.DirectionsService();
            directionsRenderer = new google.maps.DirectionsRenderer({
                map: map,
                suppressMarkers: true,
                suppressPolylines: true
            });

            crearMarcador({
                lat: initialLat, lon: initialLon,
                name: "Tu ubicación actual", type: "Usuario", municipio: "Inicio"
            }, "purple", 1.2);

            cargarDatos();
        }

        function crearMarcador(ongData, colorOverride=null, escala=1) {
            const color = colorOverride || getMarkerColor(ongData.type);
            const pos = { lat: ongData.lat, lng: ongData.lon };
            const opacity = escala > 1 ? 1.0 : 0.6; 
            
            const marker = new google.maps.Marker({
                position: pos, map: map, title: ongData.name,
                icon: {
                    path: google.maps.SymbolPath.CIRCLE,
                    scale: 6 * escala, 
                    fillColor: color, 
                    fillOpacity: opacity,
                    strokeWeight: 1, 
                    strokeColor: 'white'
                },
                zIndex: escala > 1 ? 100 : 1
            });
            
            const contentString = `
                <div style="font-family: Arial, sans-serif; padding: 5px; min-width: 150px;">
                    <h3 style="margin: 0 0 5px 0; color: #333; font-size: 16px;">${ongData.name}</h3>
                    <p style="margin: 2px 0; font-size: 13px;"><strong>Tipo:</strong> ${ongData.type || 'N/A'}</p>
                    <p style="margin: 2px 0; font-size: 13px;"><strong>Municipio:</strong> ${ongData.municipio || 'N/A'}</p>
                    <p style="margin: 2px 0; font-size: 12px; color: #666;"><strong>Riesgo:</strong> ${ongData.riesgo_nivel || 'N/A'}</p>
                </div>
            `;
            
            const infoWindow = new google.maps.InfoWindow({ content: contentString });
            marker.addListener("click", () => { infoWindow.open(map, marker); });
            return marker;
        }

        async function cargarDatos() {
            try {
                const response = await fetch(`/api/calcular-ruta?lat=${initialLat}&lon=${initialLon}`);
                const data = await response.json();

                document.getElementById("loading").style.display = "none";
                document.getElementById("content").style.display = "block";

                if (!data.success) {
                    alert("Aviso: " + data.msg);
                    return;
                }

                if (data.todos_nodos) {
                    data.todos_nodos.forEach(ong => {
                        crearMarcador(ong, null, 0.8);
                    });
                }

                const ruta = data.ruta_secuencial;
                if (ruta && ruta.length > 0) {
                    
                    document.getElementById("start-name").innerText = ruta[0].name;
                    document.getElementById("end-name").innerText = ruta[ruta.length - 1].name;
                    document.getElementById("total-stops").innerText = ruta.length;
                    
                    // --- AQUÍ ESTÁ EL CAMBIO PRINCIPAL (JS) ---
                    const routeListEl = document.getElementById("route-list");
                    routeListEl.innerHTML = ""; // Limpiar
                    
                    ruta.forEach((ong, index) => {
                        const itemHtml = `
                            <div class="route-item">
                                <div class="route-item-name">${index + 1}. ${ong.name}</div>
                                <div class="route-item-details">
                                    Tipo: ${ong.type || 'N/A'} <br>
                                    Ubicación: ${ong.municipio || 'N/A'}
                                </div>
                            </div>
                        `;
                        routeListEl.insertAdjacentHTML('beforeend', itemHtml);
                    });
                    // ------------------------------------------

                    document.getElementById("toggle-btn").click();

                    ruta.forEach(ong => {
                         crearMarcador(ong, null, 1.3);
                    });

                    trazarRutaConColores({ lat: initialLat, lng: initialLon }, ruta);
                }

            } catch (e) {
                console.error(e);
                document.getElementById("loading").innerText = "Error de conexión";
            }
        }

        function trazarRutaConColores(origenUsuario, listaOngs) {
            if (!listaOngs || listaOngs.length === 0) return;

            const destinoFinal = listaOngs[listaOngs.length - 1];
            const coordsDestino = { lat: destinoFinal.lat, lng: destinoFinal.lon };

            const waypoints = [];
            for (let i = 0; i < listaOngs.length - 1 && i < 23; i++) {
                waypoints.push({
                    location: { lat: listaOngs[i].lat, lng: listaOngs[i].lon },
                    stopover: true
                });
            }

            const request = {
                origin: origenUsuario,
                destination: coordsDestino,
                waypoints: waypoints,
                optimizeWaypoints: false, 
                travelMode: google.maps.TravelMode.DRIVING
            };

            directionsService.route(request, function(result, status) {
                if (status == google.maps.DirectionsStatus.OK) {
                    directionsRenderer.setDirections(result);
                    renderColoredLegs(result, listaOngs);
                } else {
                    console.error("Ruta fallida: " + status);
                }
            });
        }

        function renderColoredLegs(directionResult, rutaSecuencial) {
            const legs = directionResult.routes[0].legs;
            
            for (let i = 0; i < legs.length; i++) {
                const leg = legs[i];
                let colorTramo = riskColorMap['Desconocido'];
                
                if (i < rutaSecuencial.length) {
                    const nodoDestino = rutaSecuencial[i];
                    const nivelRiesgo = nodoDestino.riesgo_nivel;
                    colorTramo = riskColorMap[nivelRiesgo] || riskColorMap['Desconocido'];
                }

                const legPolyline = new google.maps.Polyline({
                    path: [],
                    strokeColor: colorTramo,
                    strokeOpacity: 0.8,
                    strokeWeight: 6,
                    map: map
                });

                const path = [];
                leg.steps.forEach(step => {
                    step.path.forEach(latlng => {
                        path.push(latlng);
                    });
                });
                legPolyline.setPath(path);
                routePolylines.push(legPolyline);
            }
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