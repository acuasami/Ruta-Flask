from flask import Flask, request, jsonify, render_template_string
import os
import math
import pandas as pd
import psycopg2
from geopy.distance import geodesic
import networkx as nx
from urllib.parse import urlparse

app = Flask(__name__)

# --- CONFIGURACIÓN BD ---
uri = 'postgresql://postgres:KAGJhRklTcsevGqKEgCNPfmdDiGzsLyQ@switchyard.proxy.rlwy.net:13155/railway'
result = urlparse(uri)
DB_CONFIG = {
    'user': result.username,
    'password': result.password,
    'host': result.hostname,
    'port': result.port,
    'dbname': result.path.lstrip('/')
}

# --- CARGA DE DATOS ---
def conectar_y_leer_sql(query):
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        df = pd.read_sql(query, conn)
        conn.close()
        return df
    except Exception as e:
        print(f"❌ Error BD: {e}")
        return pd.DataFrame()

def cargar_nodos():
    """Carga ONGs y Fronteras para construir el grafo."""
    query = """
    SELECT o.id_ong, o.nom_ong, o.tipo, o.latitud, o.longitud, m.nom_municipio 
    FROM public.ongs o
    JOIN public.municipio m ON o.id_municipio = m.id_municipio;
    """
    df = conectar_y_leer_sql(query)
    # Convertir a lista de diccionarios
    nodos = []
    for _, row in df.iterrows():
        try:
            nodos.append({
                'id': row['id_ong'],
                'name': row['nom_ong'],
                'type': row['tipo'], # Importante: 'Frontera', 'Albergue', etc.
                'lat': float(row['latitud']),
                'lon': float(row['longitud']),
                'municipio': row['nom_municipio']
            })
        except:
            continue
    return nodos

# --- LÓGICA DE GRAFOS Y A* ---

def construir_grafo_logico(nodos, radio_conexion_km=150):
    """
    Crea un grafo NetworkX conectando nodos que estén dentro de un radio.
    Esto define "quién puede conectarse con quién".
    """
    G = nx.DiGraph() # Grafo dirigido (siempre vamos al norte)
    
    # Agregar nodos
    for n in nodos:
        G.add_node(n['id'], **n)
        
    # Crear aristas (conexiones)
    # NOTA: Esto es O(n^2), para 120 puntos está bien. Para miles, usar KDTree.
    for i in nodos:
        for j in nodos:
            if i['id'] == j['id']: continue
            
            # Regla de negocio: Solo avanzar hacia el Norte (latitud mayor)
            # O permitir ligero retroceso si es necesario, pero priorizar norte.
            if j['lat'] > i['lat']: 
                dist = geodesic((i['lat'], i['lon']), (j['lat'], j['lon'])).km
                if dist <= radio_conexion_km:
                    # El peso es la distancia
                    G.add_edge(i['id'], j['id'], weight=dist)
                    
    return G

def encontrar_ruta_optima(origen_lat, origen_lon, nodos):
    """
    1. Encuentra el nodo más cercano al usuario.
    2. Encuentra el nodo 'Frontera' más accesible.
    3. Ejecuta A* entre ellos.
    """
    G = construir_grafo_logico(nodos)
    
    # 1. Nodo inicio (ONG más cercana al usuario)
    nodo_inicio = min(nodos, key=lambda x: geodesic((origen_lat, origen_lon), (x['lat'], x['lon'])).km)
    
    # 2. Nodo fin (Cualquier nodo tipo 'Frontera')
    fronteras = [n for n in nodos if str(n.get('type','')).strip().lower() == 'frontera']
    
    if not fronteras:
        return {"error": "No hay fronteras definidas en la BD"}
        
    # Buscar la ruta más corta a CUALQUIER frontera
    mejor_ruta = []
    menor_costo = float('inf')
    
    for frontera in fronteras:
        try:
            # Heurística: Distancia directa a esta frontera
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
            
    if not mejor_ruta:
        return {"error": "No se encontró camino a la frontera"}

    # Convertir IDs a objetos completos para Google Maps
    ruta_detallada = []
    
    # Agregar posición actual del usuario como punto 0
    ruta_detallada.append({'lat': origen_lat, 'lon': origen_lon, 'type': 'User'})
    
    for nid in mejor_ruta:
        nodo = G.nodes[nid]
        ruta_detallada.append({
            'lat': nodo['lat'],
            'lon': nodo['lon'],
            'name': nodo['name'],
            'type': nodo['type'],
            'municipio': nodo['municipio']
        })
        
    return {"ruta": ruta_detallada}

# --- ENDPOINTS ---

@app.route('/api/calcular-ruta', methods=['GET'])
def api_ruta():
    try:
        lat = request.args.get('lat', type=float)
        lon = request.args.get('lon', type=float)
        
        nodos = cargar_nodos()
        resultado = encontrar_ruta_optima(lat, lon, nodos)
        
        if "error" in resultado:
            return jsonify({"success": False, "msg": resultado["error"]})
            
        return jsonify({"success": True, "data": resultado["ruta"]})
        
    except Exception as e:
        return jsonify({"success": False, "msg": str(e)}), 500

@app.route('/mapa')
def mapa_google():
    """Sirve la plantilla HTML que contiene el JS de Google Maps"""
    return render_template_string(HTML_GOOGLE_MAPS)

# --- PLANTILLA HTML (FRONTEND) ---
# En un proyecto grande, esto iría en la carpeta 'templates/mapa.html'
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
        <div id="status">Esperando ubicación...</div>
        <button id="btn-ruta" class="btn" onclick="iniciarCalculo()" style="display:none; margin-top:10px;">
            📍 Trazar Ruta
        </button>
    </div>
    <div id="map"></div>

    <script src="https://maps.googleapis.com/maps/api/js?key=AIzaSyAHQChFMbUZcIKS3srHRzEoIHSPEtJ5GFQ&libraries=places"></script>
    
    <script>
        let map;
        let directionsService;
        let userPos;
        let renderers = []; // Para guardar las líneas dibujadas

        function initMap() {
            directionsService = new google.maps.DirectionsService();
            map = new google.maps.Map(document.getElementById("map"), {
                zoom: 5,
                center: { lat: 23.6345, lng: -102.5528 }, // Centro de México
                mapTypeControl: false,
                streetViewControl: false
            });

            // Obtener ubicación del navegador
            if (navigator.geolocation) {
                navigator.geolocation.getCurrentPosition(
                    (position) => {
                        userPos = {
                            lat: position.coords.latitude,
                            lng: position.coords.longitude
                        };
                        map.setCenter(userPos);
                        map.setZoom(10);
                        
                        new google.maps.Marker({
                            position: userPos,
                            map: map,
                            title: "Tu ubicación",
                            icon: "http://maps.google.com/mapfiles/ms/icons/blue-dot.png"
                        });
                        
                        document.getElementById("status").innerText = "Ubicación detectada.";
                        document.getElementById("btn-ruta").style.display = "block";
                    },
                    () => { handleLocationError(true); }
                );
            } else {
                handleLocationError(false);
            }
        }

        async function iniciarCalculo() {
            document.getElementById("status").innerText = "⏳ Calculando mejor ruta en servidor...";
            
            try {
                // 1. Pedir la secuencia de nodos al Backend (Python + A*)
                const response = await fetch(`/api/calcular-ruta?lat=${userPos.lat}&lon=${userPos.lng}`);
                const data = await response.json();
                
                if (!data.success) throw new Error(data.msg);
                
                const nodos = data.data;
                document.getElementById("status").innerText = `✅ Ruta encontrada: ${nodos.length} puntos. Dibujando...`;
                
                // 2. Dibujar la ruta en lotes (Chunking) para evitar límites de Google
                dibujarRutaCompleta(nodos);
                
            } catch (e) {
                document.getElementById("status").innerText = "❌ Error: " + e.message;
            }
        }

        async function dibujarRutaCompleta(puntos) {
            // Limpiar mapa anterior
            renderers.forEach(r => r.setMap(null));
            renderers = [];
            
            // Google Maps permite max 25 waypoints (1 inicio, 1 fin, 23 intermedios)
            const MAX_WAYPOINTS = 23; 
            
            // Iterar sobre la lista de puntos dividiéndola en segmentos
            for (let i = 0; i < puntos.length - 1; i += MAX_WAYPOINTS) {
                // Tomamos un subconjunto. El fin de un segmento debe ser el inicio del siguiente
                const chunk = puntos.slice(i, i + MAX_WAYPOINTS + 2);
                
                if (chunk.length < 2) continue;

                const origin = { lat: chunk[0].lat, lng: chunk[0].lon };
                const destination = { lat: chunk[chunk.length - 1].lat, lng: chunk[chunk.length - 1].lon };
                
                // Puntos intermedios
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
                    travelMode: google.maps.TravelMode.DRIVING, // O WALKING
                }, (result, status) => {
                    if (status === 'OK') {
                        const renderer = new google.maps.DirectionsRenderer({
                            map: map,
                            directions: result,
                            preserveViewport: true,
                            suppressMarkers: false, // Dejar markers predeterminados A,B,C...
                            polylineOptions: { strokeColor: "#4285F4", strokeWeight: 5 }
                        });
                        renderers.push(renderer);
                    } else {
                        console.error('Fallo segmento:', status);
                    }
                    // Resolvemos siempre para no detener el bucle
                    resolve(); 
                });
            });
        }

        function handleLocationError(browserHasGeolocation) {
             document.getElementById("status").innerText = browserHasGeolocation
                ? "Error: Falló el servicio de geolocalización."
                : "Error: Tu navegador no soporta geolocalización.";
        }

        window.onload = initMap;
    </script>
</body>
</html>
"""

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)