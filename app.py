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

# PLANTILLA HTML MEJORADA CON INFORMACIÓN DE KILÓMETROS
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
            background: white; padding: 20px; border-radius: 12px;
            box-shadow: 0 4px 15px rgba(0,0,0,0.2); z-index: 10;
            max-width: 450px; max-height: 60vh;
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

        .info-section { margin-bottom: 15px; border-bottom: 1px solid #eee; padding-bottom: 10px; }
        .info-title { font-weight: bold; color: #555; font-size: 0.9em; margin-bottom: 5px; }
        .info-content { font-size: 1.1em; margin-top: 2px; }
        .highlight { color: #4285F4; font-weight: bold; }
        .distance-badge {
            background: #e8f4ff;
            color: #4285F4;
            padding: 3px 8px;
            border-radius: 12px;
            font-size: 0.85em;
            margin-left: 5px;
            font-weight: bold;
        }

        /* Estilos para la lista de ruta */
        .route-list-container {
            margin-top: 10px;
            background-color: #f9f9f9;
            border-radius: 8px;
            padding: 10px;
            max-height: 250px;
            overflow-y: auto;
        }
        .route-item {
            margin-bottom: 10px;
            padding-bottom: 10px;
            border-bottom: 1px solid #e0e0e0;
            font-size: 0.9em;
        }
        .route-item:last-child { border-bottom: none; margin-bottom: 0; }
        .route-item-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 5px;
        }
        .route-item-name { font-weight: bold; color: #333; font-size: 1em; }
        .route-item-distance { 
            background: #e8f4ff;
            color: #4285F4;
            padding: 2px 8px;
            border-radius: 10px;
            font-size: 0.85em;
            font-weight: bold;
        }
        .route-item-details { 
            color: #666; font-size: 0.85em;
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 5px;
            margin-top: 5px;
        }
        .detail-label { color: #888; }
        .detail-value { font-weight: 500; }
        .risk-high { color: #ff4444; }
        .risk-medium { color: #ffaa00; }
        .risk-low { color: #00aa44; }
        
        .legend {
            position: fixed; top: 10px; left: 10px;
            background: rgba(255, 255, 255, 0.95); padding: 12px; border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1); z-index: 10;
            font-size: 12px; max-width: 180px;
        }
        .legend-title { 
            font-weight: bold; margin-bottom: 8px; 
            border-bottom: 1px solid #ccc; padding-bottom: 4px;
            color: #333;
        }
        .legend-item { display: flex; align-items: center; margin-bottom: 6px; }
        .dot { width: 14px; height: 14px; border-radius: 50%; margin-right: 8px; display: inline-block; border: 1px solid #fff; }
        .line { width: 24px; height: 5px; margin-right: 8px; display: inline-block; border-radius: 2px; }
        
        /* Estadísticas */
        .stats-container {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
            margin-top: 15px;
            background: #f0f7ff;
            padding: 12px;
            border-radius: 8px;
        }
        .stat-item {
            text-align: center;
        }
        .stat-value {
            font-size: 1.4em;
            font-weight: bold;
            color: #4285F4;
        }
        .stat-label {
            font-size: 0.8em;
            color: #666;
        }
    </style>
</head>
<body>
    <div id="map"></div>
    <button id="toggle-btn" onclick="toggleInfoBox()">ℹ</button>
    
    <div id="info-box">
        <h3 style="margin-top:0; color:#4285F4; border-bottom: 2px solid #4285F4; padding-bottom: 8px;">
            📍 Ruta Sugerida a Frontera
        </h3>
        <div id="loading">Calculando ruta completa...</div>
        <div id="content" style="display:none;">
            <div class="info-section">
                <div class="info-title">📍 Punto de Partida</div>
                <div class="info-content" id="start-name"></div>
                <div id="start-distance" style="font-size:0.9em; color:#666; margin-top:3px;"></div>
            </div>
            
            <div class="info-section">
                <div class="info-title">🎯 Destino Final (Frontera)</div>
                <div class="info-content" id="end-name" style="font-weight:bold; color:#d32f2f;"></div>
                <div id="total-distance" style="font-size:0.9em; color:#666; margin-top:3px;"></div>
            </div>
            
            <div class="stats-container">
                <div class="stat-item">
                    <div class="stat-value" id="stat-stops">0</div>
                    <div class="stat-label">Paradas</div>
                </div>
                <div class="stat-item">
                    <div class="stat-value" id="stat-distance">0 km</div>
                    <div class="stat-label">Distancia Total</div>
                </div>
                <div class="stat-item">
                    <div class="stat-value" id="stat-avg-distance">0 km</div>
                    <div class="stat-label">Promedio por tramo</div>
                </div>
                <div class="stat-item">
                    <div class="stat-value" id="stat-avg-risk">-</div>
                    <div class="stat-label">Riesgo Promedio</div>
                </div>
            </div>
            
            <div class="info-section" style="border-bottom: none; margin-top: 15px;">
                <div class="info-title">🗺 Detalle del Recorrido</div>
                <div id="route-list" class="route-list-container"></div>
            </div>
        </div>
    </div>
    
    <div class="legend">
        <div class="legend-title">📍 Tipo de ONG</div>
        <div class="legend-item"><span class="dot" style="background:#4285F4;"></span>Albergue</div>
        <div class="legend-item"><span class="dot" style="background:#FF9800;"></span>Comedor</div>
        <div class="legend-item"><span class="dot" style="background:#F44336;"></span>Frontera</div>
        <div class="legend-item"><span class="dot" style="background:#9C27B0;"></span>Usuario</div>
        
        <div class="legend-title" style="margin-top:10px;">⚠ Nivel de Riesgo</div>
        <div class="legend-item"><span class="line" style="background:#4CAF50;"></span>Bajo</div>
        <div class="legend-item"><span class="line" style="background:#FF9800;"></span>Medio</div>
        <div class="legend-item"><span class="line" style="background:#F44336;"></span>Alto</div>
        <div class="legend-item"><span class="line" style="background:#9E9E9E;"></span>Desconocido</div>
    </div>

    <script src="https://maps.googleapis.com/maps/api/js?key=AIzaSyAHQChFMbUZcIKS3srHRzEoIHSPEtJ5GFQ"></script>
    
    <script>
        const initialLat = parseFloat("{{ lat }}");
        const initialLon = parseFloat("{{ lon }}");
        
        let map;
        let directionsService;
        let directionsRenderer;
        let routePolylines = [];
        let routeWithDistances = [];
        
        const markerColorMap = {
            'Albergue': '#4285F4', 'Comedor': '#FF9800', 'Frontera': '#F44336', 
            'Usuario': '#9C27B0', 'default': '#9E9E9E'
        };

        const riskColorMap = {
            'Bajo': '#4CAF50',      // Verde
            'Medio': '#FF9800',     // Naranja
            'Alto': '#F44336',      // Rojo
            'Desconocido': '#9E9E9E' // Gris
        };

        const riskTextMap = {
            'Bajo': '🟢 Bajo',
            'Medio': '🟡 Medio', 
            'Alto': '🔴 Alto',
            'Desconocido': '⚫ Desconocido'
        };

        function getMarkerColor(type) {
            if (!type) return markerColorMap['default'];
            if (type.includes('Albergue')) return markerColorMap['Albergue'];
            if (type.includes('Comedor')) return markerColorMap['Comedor'];
            if (type.includes('Frontera')) return markerColorMap['Frontera'];
            if (type.includes('Usuario')) return markerColorMap['Usuario'];
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
            }, markerColorMap['Usuario'], 1.5);

            cargarDatos();
        }

        function calcularDistanciaEntrePuntos(puntoA, puntoB) {
            const rad = (x) => x * Math.PI / 180;
            const R = 6371; // Radio de la Tierra en km
            
            const dLat = rad(puntoB.lat - puntoA.lat);
            const dLng = rad(puntoB.lng - puntoA.lng);
            
            const a = Math.sin(dLat/2) * Math.sin(dLat/2) +
                      Math.cos(rad(puntoA.lat)) * Math.cos(rad(puntoB.lat)) *
                      Math.sin(dLng/2) * Math.sin(dLng/2);
            
            const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
            return R * c;
        }

        function crearMarcador(ongData, colorOverride=null, escala=1) {
            const color = colorOverride || getMarkerColor(ongData.type);
            const pos = { lat: ongData.lat, lng: ongData.lon };
            const opacity = escala > 1 ? 1.0 : 0.7; 
            const size = escala > 1 ? 8 * escala : 6;
            
            const marker = new google.maps.Marker({
                position: pos, map: map, title: ongData.name,
                icon: {
                    path: google.maps.SymbolPath.CIRCLE,
                    scale: size, 
                    fillColor: color, 
                    fillOpacity: opacity,
                    strokeWeight: 1.5, 
                    strokeColor: '#FFFFFF'
                },
                zIndex: escala > 1 ? 100 : 1
            });
            
            const contentString = `
                <div style="font-family: Arial, sans-serif; padding: 8px; min-width: 200px; max-width: 250px;">
                    <h3 style="margin: 0 0 8px 0; color: #333; font-size: 16px; border-bottom: 1px solid #eee; padding-bottom: 5px;">
                        ${ongData.name}
                    </h3>
                    <div style="margin: 5px 0;">
                        <strong style="color: #555;">📍 Tipo:</strong> ${ongData.type || 'N/A'}<br>
                        <strong style="color: #555;">🏙 Municipio:</strong> ${ongData.municipio || 'N/A'}<br>
                        <strong style="color: #555;">⚠ Riesgo:</strong> ${ongData.riesgo_nivel || 'N/A'}<br>
                        ${ongData.distancia_desde_anterior ? 
                            <strong style="color: #555;">📏 Distancia desde anterior:</strong> ${ongData.distancia_desde_anterior.toFixed(1)} km<br> : ''}
                        ${ongData.distancia_acumulada ? 
                            <strong style="color: #555;">🛣 Distancia acumulada:</strong> ${ongData.distancia_acumulada.toFixed(1)} km : ''}
                    </div>
                    <div style="margin-top: 8px; font-size: 12px; color: #777;">
                        Coord: ${ongData.lat.toFixed(4)}°, ${ongData.lon.toFixed(4)}°
                    </div>
                </div>
            `;
            
            const infoWindow = new google.maps.InfoWindow({ content: contentString });
            marker.addListener("click", () => { infoWindow.open(map, marker); });
            return marker;
        }

        async function cargarDatos() {
            try {
                const response = await fetch(/api/calcular-ruta?lat=${initialLat}&lon=${initialLon});
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
                    // Calcular distancias para cada tramo
                    const rutaConDistancias = [];
                    let distanciaAcumulada = 0;
                    let distanciaTotal = 0;
                    let riesgoCount = {Bajo:0, Medio:0, Alto:0, Desconocido:0};
                    
                    // Distancia del usuario a la primera ONG
                    const userPos = {lat: initialLat, lng: initialLon};
                    const primeraOngPos = {lat: ruta[0].lat, lng: ruta[0].lon};
                    const distanciaInicial = calcularDistanciaEntrePuntos(userPos, primeraOngPos);
                    
                    // Primera ONG
                    rutaConDistancias.push({
                        ...ruta[0],
                        distancia_desde_anterior: distanciaInicial,
                        distancia_acumulada: distanciaInicial
                    });
                    distanciaAcumulada = distanciaInicial;
                    distanciaTotal += distanciaInicial;
                    
                    // Calcular riesgo para estadísticas
                    const riesgo = ruta[0].riesgo_nivel || 'Desconocido';
                    if (riesgoCount.hasOwnProperty(riesgo)) riesgoCount[riesgo]++;

                    // ONGs siguientes
                    for (let i = 1; i < ruta.length; i++) {
                        const puntoAnterior = {lat: ruta[i-1].lat, lng: ruta[i-1].lon};
                        const puntoActual = {lat: ruta[i].lat, lng: ruta[i].lon};
                        const distanciaTramo = calcularDistanciaEntrePuntos(puntoAnterior, puntoActual);
                        
                        distanciaAcumulada += distanciaTramo;
                        distanciaTotal += distanciaTramo;
                        
                        rutaConDistancias.push({
                            ...ruta[i],
                            distancia_desde_anterior: distanciaTramo,
                            distancia_acumulada: distanciaAcumulada
                        });
                        
                        const riesgo = ruta[i].riesgo_nivel || 'Desconocido';
                        if (riesgoCount.hasOwnProperty(riesgo)) riesgoCount[riesgo]++;
                    }
                    
                    routeWithDistances = rutaConDistancias;

                    // Actualizar información principal
                    document.getElementById("start-name").innerText = ruta[0].name;
                    document.getElementById("start-distance").innerHTML = 
                        📍 ${distanciaInicial.toFixed(1)} km desde tu ubicación;
                    
                    document.getElementById("end-name").innerText = ruta[ruta.length - 1].name;
                    document.getElementById("total-distance").innerHTML = 
                        🛣 Distancia total: <span class="highlight">${distanciaTotal.toFixed(1)} km</span>;
                    
                    document.getElementById("stat-stops").innerText = ruta.length;
                    document.getElementById("stat-distance").innerText = ${distanciaTotal.toFixed(1)} km;
                    document.getElementById("stat-avg-distance").innerText = 
                        ${(distanciaTotal / Math.max(1, ruta.length - 1)).toFixed(1)} km;
                    
                    // Calcular riesgo promedio
                    const riesgoPromedio = calcularRiesgoPromedio(riesgoCount);
                    document.getElementById("stat-avg-risk").innerText = riesgoPromedio;

                    // Mostrar lista detallada de la ruta
                    const routeListEl = document.getElementById("route-list");
                    routeListEl.innerHTML = "";
                    
                    rutaConDistancias.forEach((ong, index) => {
                        const riesgoText = riskTextMap[ong.riesgo_nivel] || riskTextMap['Desconocido'];
                        const riesgoClass = ong.riesgo_nivel === 'Alto' ? 'risk-high' : 
                                           ong.riesgo_nivel === 'Medio' ? 'risk-medium' : 
                                           ong.riesgo_nivel === 'Bajo' ? 'risk-low' : '';
                        
                        const itemHtml = `
                            <div class="route-item">
                                <div class="route-item-header">
                                    <div class="route-item-name">${index + 1}. ${ong.name}</div>
                                    <div class="route-item-distance">${ong.distancia_desde_anterior.toFixed(1)} km</div>
                                </div>
                                <div class="route-item-details">
                                    <div>
                                        <div class="detail-label">Tipo</div>
                                        <div class="detail-value">${ong.type || 'N/A'}</div>
                                    </div>
                                    <div>
                                        <div class="detail-label">Municipio</div>
                                        <div class="detail-value">${ong.municipio || 'N/A'}</div>
                                    </div>
                                    <div>
                                        <div class="detail-label">Riesgo</div>
                                        <div class="detail-value ${riesgoClass}">${riesgoText}</div>
                                    </div>
                                    <div>
                                        <div class="detail-label">Acumulado</div>
                                        <div class="detail-value">${ong.distancia_acumulada.toFixed(1)} km</div>
                                    </div>
                                </div>
                            </div>
                        `;
                        routeListEl.insertAdjacentHTML('beforeend', itemHtml);
                    });

                    // Mostrar caja de información automáticamente
                    document.getElementById("toggle-btn").click();

                    // Crear marcadores para la ruta (más grandes)
                    rutaConDistancias.forEach(ong => {
                        crearMarcador(ong, null, 1.3);
                    });

                    // Trazar la ruta en el mapa
                    trazarRutaConColores({ lat: initialLat, lng: initialLon }, rutaConDistancias);
                }

            } catch (e) {
                console.error(e);
                document.getElementById("loading").innerText = "Error de conexión";
            }
        }

        function calcularRiesgoPromedio(riesgoCount) {
            const total = riesgoCount.Bajo + riesgoCount.Medio + riesgoCount.Alto + riesgoCount.Desconocido;
            if (total === 0) return "-";
            
            // Ponderar riesgos (Bajo=1, Medio=2, Alto=3)
            const score = (riesgoCount.Bajo * 1 + riesgoCount.Medio * 2 + riesgoCount.Alto * 3) / total;
            
            if (score < 1.5) return "🟢 Bajo";
            if (score < 2.5) return "🟡 Medio";
            return "🔴 Alto";
        }

        async function trazarRutaConColores(origenUsuario, listaOngs) {
            if (!listaOngs || listaOngs.length === 0) return;

            const todosLosPuntos = [
                origenUsuario, 
                ...listaOngs.map(ong => ({ lat: ong.lat, lng: ong.lon }))
            ];

            const MAX_WAYPOINTS_PER_REQUEST = 23;
            const peticiones = [];
            let indexActual = 0;

            while (indexActual < todosLosPuntos.length - 1) {
                const inicioBatch = indexActual;
                const finBatch = Math.min(indexActual + MAX_WAYPOINTS_PER_REQUEST + 1, todosLosPuntos.length - 1);

                const puntoOrigen = todosLosPuntos[inicioBatch];
                const puntoDestino = todosLosPuntos[finBatch];
                
                const waypointsBatch = [];
                for (let i = inicioBatch + 1; i < finBatch; i++) {
                    waypointsBatch.push({
                        location: todosLosPuntos[i],
                        stopover: true
                    });
                }

                const promesa = new Promise((resolve, reject) => {
                    const request = {
                        origin: puntoOrigen,
                        destination: puntoDestino,
                        waypoints: waypointsBatch,
                        optimizeWaypoints: false,
                        travelMode: google.maps.TravelMode.DRIVING
                    };

                    const baseIndexParaColores = inicioBatch;

                    directionsService.route(request, function(result, status) {
                        if (status === google.maps.DirectionsStatus.OK) {
                            renderColoredLegs(result, listaOngs, baseIndexParaColores);
                            resolve();
                        } else {
                            console.error("Fallo en tramo " + baseIndexParaColores + ": " + status);
                            resolve();
                        }
                    });
                });

                peticiones.push(promesa);
                indexActual = finBatch;
            }

            await Promise.all(peticiones);
        }

        function renderColoredLegs(directionResult, rutaSecuencial, offsetIndex) {
            const legs = directionResult.routes[0].legs;
            
            for (let i = 0; i < legs.length; i++) {
                const leg = legs[i];
                const indiceGlobal = offsetIndex + i;
                let colorTramo = riskColorMap['Desconocido'];

                if (indiceGlobal < rutaSecuencial.length) {
                    const nodoDestino = rutaSecuencial[indiceGlobal];
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