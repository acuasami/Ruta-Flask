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

# Pool de conexiones
connection_pool = pool.SimpleConnectionPool(1, 5, **DB_CONFIG)

@contextmanager
def get_db_connection():
    """Context manager para conexiones seguras"""
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
        self.grafo = None
        self.last_update = 0
        self.TTL = 3600  # 1 hora
    
    def is_expired(self):
        return (time.time() - self.last_update) > self.TTL
    
    def invalidate(self):
        self.nodos = None
        self.grafo = None
        self.last_update = 0


cache = DataCache()


# --- CARGA DE DATOS ---
def conectar_y_leer_sql(query):
    """Lee de BD con manejo seguro de conexiones"""
    try:
        with get_db_connection() as conn:
            df = pd.read_sql(query, conn)
            return df
    except Exception as e:
        print(f"❌ Error BD: {e}")
        return pd.DataFrame()


def cargar_nodos():
    """Carga ONGs y Fronteras con caché"""
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
    return nodos


# --- LÓGICA DE GRAFOS Y A* ---
def construir_grafo_logico(nodos, radio_conexion_km=150):
    """Crea grafo con caché"""
    if cache.grafo is not None and not cache.is_expired():
        return cache.grafo
    
    G = nx.DiGraph()
    
    for n in nodos:
        G.add_node(n['id'], **n)
    
    print(f"[INFO] Construyendo grafo con {len(nodos)} nodos...")
    
    for i, nodo_i in enumerate(nodos):
        for j, nodo_j in enumerate(nodos):
            if i >= j:
                continue
            
            if nodo_j['lat'] > nodo_i['lat']:
                dist = geodesic(
                    (nodo_i['lat'], nodo_i['lon']), 
                    (nodo_j['lat'], nodo_j['lon'])
                ).km
                
                if dist <= radio_conexion_km:
                    G.add_edge(nodo_i['id'], nodo_j['id'], weight=dist)
    
    cache.grafo = G
    print(f"[INFO] Grafo listo: {G.number_of_nodes()} nodos, {G.number_of_edges()} aristas")
    return G


def encontrar_ruta_optima(origen_lat, origen_lon, nodos):
    """Encuentra ruta con manejo robusto de errores"""
    if not nodos:
        return {"error": "No se pudieron cargar los nodos de la base de datos."}
    
    G = construir_grafo_logico(nodos)
    
    if G.number_of_nodes() == 0:
        return {"error": "El grafo está vacío. Verifica la BD."}
    
    try:
        nodo_inicio = min(
            nodos, 
            key=lambda x: geodesic((origen_lat, origen_lon), (x['lat'], x['lon'])).km
        )
    except Exception as e:
        return {"error": f"No se pudo encontrar nodo de inicio: {e}"}
    
    fronteras = [n for n in nodos if n['type'] == 'frontera']
    
    if not fronteras:
        return {"error": "No hay fronteras definidas en la BD (verifica tipo='frontera')"}
    
    mejor_ruta = []
    menor_costo = float('inf')
    
    for frontera in fronteras:
        try:
            def heuristica(u, v):
                n1 = G.nodes[u]
                n2 = G.nodes[v]
                return geodesic((n1['lat'], n1['lon']), (n2['lat'], n2['lon'])).km
            
            ruta_ids = nx.astar_path(G, nodo_inicio['id'], frontera['id'], heuristic=heuristica, weight='weight')
            costo = nx.path_weight(G, ruta_ids, weight='weight')
            
            if costo < menor_costo:
                menor_costo = costo
                mejor_ruta = ruta_ids
        except nx.NetworkXNoPath:
            continue
        except nx.NodeNotFound as e:
            print(f"⚠️ Nodo no encontrado en grafo: {e}")
            continue
    
    if not mejor_ruta:
        return {"error": "No se encontró camino a la frontera"}
    
    ruta_detallada = [{'lat': origen_lat, 'lon': origen_lon, 'type': 'User'}]
    
    for nid in mejor_ruta:
        if nid not in G.nodes:
            continue
        nodo = G.nodes[nid]
        ruta_detallada.append({
            'lat': nodo['lat'],
            'lon': nodo['lon'],
            'name': nodo.get('name', 'N/A'),
            'type': nodo.get('type', 'N/A'),
            'municipio': nodo.get('municipio', 'N/A')
        })
    
    return {"ruta": ruta_detallada}


# --- ENDPOINTS ---
@app.route('/api/calcular-ruta', methods=['GET'])
def api_ruta():
    try:
        lat = request.args.get('lat', type=float)
        lon = request.args.get('lon', type=float)
        
        if lat is None or lon is None:
            return jsonify({"success": False, "msg": "Faltan parámetros lat y lon"}), 400
        
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            return jsonify({"success": False, "msg": "Coordenadas fuera de rango"}), 400
        
        nodos = cargar_nodos()
        resultado = encontrar_ruta_optima(lat, lon, nodos)
        
        if "error" in resultado:
            return jsonify({"success": False, "msg": resultado["error"]}), 400
        
        return jsonify({"success": True, "data": resultado["ruta"]})
        
    except Exception as e:
        return jsonify({"success": False, "msg": f"Error del servidor: {str(e)}"}), 500


@app.route('/api/auth/login', methods=['POST'])
def api_login():
    """Endpoint de autenticación básico"""
    try:
        data = request.get_json() or {}
        usuario = data.get('usuario')
        contraseña = data.get('contraseña')
        
        if not usuario or not contraseña:
            return jsonify({"success": False, "msg": "Usuario y contraseña requeridos"}), 400
        
        return jsonify({
            "success": True,
            "token": "demo_token_123",
            "mensaje": "Login exitoso"
        })
        
    except Exception as e:
        return jsonify({"success": False, "msg": str(e)}), 500


# ✅ NUEVO: Endpoint /mapa mejorado que ACEPTA parámetros GET
@app.route('/mapa', methods=['GET'])
def mapa_google():
    """
    Acepta parámetros GET:
    - lat: Latitud del usuario
    - lon: Longitud del usuario
    - id_usuario: ID del usuario (para logging)
    
    Si no se proporcionan, usa valores por defecto
    """
    lat = request.args.get('lat', default='19.325521', type=str)
    lon = request.args.get('lon', default='-99.167807', type=str)
    id_usuario = request.args.get('id_usuario', default='0', type=str)
    
    # Pasar las variables al template HTML
    html = render_template_string(
        HTML_GOOGLE_MAPS,
        lat=lat,
        lon=lon,
        id_usuario=id_usuario
    )
    return html


@app.route('/health', methods=['GET'])
def health_check():
    """Health check para Cloud Run"""
    return jsonify({"status": "ok"}), 200


# ✅ NUEVO: Servir favicon.ico (elimina errores 404)
@app.route('/favicon.ico')
def favicon():
    """Devuelve un favicon vacío para evitar errores 404"""
    return '', 204


# --- PLANTILLA HTML MEJORADA ---
HTML_GOOGLE_MAPS = """
<!DOCTYPE html>
<html>
<head>
    <title>Ruta Migrante - Google Maps</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body, html { height: 100%; margin: 0; padding: 0; }
        #map { height: 100%; width: 100%; }
        #info-box {
            position: absolute; bottom: 20px; left: 20px; right: 20px;
            background: white; padding: 15px; border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.3); z-index: 10;
            max-width: 400px; margin: auto;
        }
        .btn {
            background: #4285F4; color: white; border: none; padding: 10px 20px;
            border-radius: 4px; cursor: pointer; width: 100%; font-size: 16px;
        }
    </style>
</head>
<body>
    <div id="info-box">
        <h3 style="margin-top:0">🗺️ Ruta Segura a Frontera</h3>
        <div id="status">Inicializando...</div>
        <button id="btn-ruta" class="btn" onclick="iniciarCalculo()" style="display:none; margin-top:10px;">
            📍 Trazar Ruta
        </button>
    </div>
    <div id="map"></div>

    <script src="https://maps.googleapis.com/maps/api/js?key=AIzaSyAHQChFMbUZcIKS3srHRzEoIHSPEtJ5GFQ&libraries=places"></script>
    
    <script>
        // ✅ NUEVAS VARIABLES: Recibir parámetros del servidor Python
        const initialLat = parseFloat("{{ lat }}");
        const initialLon = parseFloat("{{ lon }}");
        const idUsuario = "{{ id_usuario }}";
        
        let map;
        let directionsService;
        let userPos;
        let renderers = [];

        function initMap() {
            directionsService = new google.maps.DirectionsService();
            map = new google.maps.Map(document.getElementById("map"), {
                zoom: 10,
                center: { lat: initialLat, lng: initialLon },
                mapTypeControl: false,
                streetViewControl: false
            });

            // ✅ CAMBIO: Usar ubicación enviada por Android en lugar de pedir permiso
            userPos = {
                lat: initialLat,
                lng: initialLon
            };
            
            map.setCenter(userPos);
            map.setZoom(10);
            
            new google.maps.Marker({
                position: userPos,
                map: map,
                title: "Tu ubicación",
                icon: "http://maps.google.com/mapfiles/ms/icons/blue-dot.png"
            });
            
            document.getElementById("status").innerText = 
                `Ubicación: ${userPos.lat.toFixed(4)}, ${userPos.lng.toFixed(4)}\nUsuario: ${idUsuario}`;
            document.getElementById("btn-ruta").style.display = "block";
        }

        async function iniciarCalculo() {
            document.getElementById("status").innerText = "⏳ Calculando mejor ruta en servidor...";
            
            try {
                const response = await fetch(`/api/calcular-ruta?lat=${userPos.lat}&lon=${userPos.lng}`);
                const data = await response.json();
                
                if (!data.success) throw new Error(data.msg);
                
                const nodos = data.data;
                document.getElementById("status").innerText = 
                    `✅ Ruta encontrada: ${nodos.length} puntos. Dibujando...`;
                
                dibujarRutaCompleta(nodos);
                
            } catch (e) {
                document.getElementById("status").innerText = "❌ Error: " + e.message;
            }
        }

        async function dibujarRutaCompleta(puntos) {
            renderers.forEach(r => r.setMap(null));
            renderers = [];
            
            const MAX_WAYPOINTS = 23; 
            
            for (let i = 0; i < puntos.length - 1; i += MAX_WAYPOINTS) {
                const chunk = puntos.slice(i, i + MAX_WAYPOINTS + 2);
                
                if (chunk.length < 2) continue;

                const origin = { lat: chunk[0].lat, lng: chunk[0].lon };
                const destination = { lat: chunk[chunk.length - 1].lat, lng: chunk[chunk.length - 1].lon };
                
                const waypoints = chunk.slice(1, -1).map(p => ({
                    location: { lat: p.lat, lng: p.lon },
                    stopover: true
                }));

                await trazarSegmento(origin, destination, waypoints);
            }
        }

        function trazarSegmento(origin, destination, waypoints) {
            return new Promise((resolve) => {
                directionsService.route({
                    origin: origin,
                    destination: destination,
                    waypoints: waypoints,
                    travelMode: google.maps.TravelMode.DRIVING,
                }, (result, status) => {
                    if (status === 'OK') {
                        const renderer = new google.maps.DirectionsRenderer({
                            map: map,
                            directions: result,
                            preserveViewport: true,
                            suppressMarkers: false,
                            polylineOptions: { strokeColor: "#4285F4", strokeWeight: 5 }
                        });
                        renderers.push(renderer);
                    } else {
                        console.error('Fallo segmento:', status);
                    }
                    resolve(); 
                });
            });
        }

        window.onload = initMap;
    </script>
</body>
</html>
"""


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
