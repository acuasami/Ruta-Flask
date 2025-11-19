import os
import networkx as nx
import osmnx as ox
import pandas as pd
from flask import Flask, render_template, request, url_for, redirect, jsonify, Response
from sqlalchemy import create_engine, text
from folium.plugins import MarkerCluster
import folium
import json
import io
import geopandas as gpd
from shapely.geometry import Point

app = Flask(__name__)

# --- CONFIGURACIÓN CRÍTICA DE LA BASE DE DATOS (SOLUCIÓN FINAL DE CONEXIÓN) ---
DATABASE_URL = os.environ.get("DATABASE_URL")

# ⚠️ Corrección crucial para Railway: Forzar sslmode=require incondicionalmente.
if DATABASE_URL and DATABASE_URL.startswith("postgresql://"):
    # 1. Eliminar cualquier parámetro de query existente (después de '?')
    base_url_only = DATABASE_URL.split('?')[0]
    
    # 2. Agregar sslmode=require al final
    FINAL_DATABASE_URL = base_url_only + "?sslmode=require"
elif not DATABASE_URL:
    # Fallback para desarrollo local si la variable de entorno no está configurada
    FINAL_DATABASE_URL = "postgresql://user:password@localhost:5432/mydatabase"
else:
    # Usar la URL tal cual si no es PostgreSQL o tiene otro formato
    FINAL_DATABASE_URL = DATABASE_URL

# Crear el motor de SQLAlchemy
engine = create_engine(FINAL_DATABASE_URL)
# -----------------------------------------------------------------------------


@app.route('/')
def index():
    return render_template('login.html')

@app.route('/login', methods=['POST'])
def login():
    usuario = request.form['usuario']
    password = request.form['password']

    try:
        with engine.connect() as connection:
            # Buscar el usuario y la contraseña en la tabla 'usuarios'
            query = text("SELECT id_usuario FROM usuarios WHERE usuario = :usuario AND password = :password")
            result = connection.execute(query, {"usuario": usuario, "password": password}).fetchone()

            if result:
                id_usuario = result[0]
                # Redirigir al mapa con el id_usuario
                return redirect(url_for('mapa', id_usuario=id_usuario))
            else:
                return render_template('login.html', error="Usuario o contraseña incorrectos")
    except Exception as e:
        # Enviar un mensaje de error legible al cliente
        print(f"Error al intentar conectar o ejecutar la consulta: {e}")
        return jsonify({"error_code": "DB_CONNECTION_FAILED", "message": f"Error de conexión o consulta a la BD: {e}"}), 500


@app.route('/mapa')
def mapa():
    lat = request.args.get('lat', type=float)
    lon = request.args.get('lon', type=float)
    id_usuario = request.args.get('id_usuario', type=int)

    if lat is None or lon is None or id_usuario is None:
        # Si faltan parámetros, regresa un error o redirige al login
        return render_template('login.html', error="Faltan parámetros de ubicación o ID de usuario.")

    # 1. Obtener datos de la base de datos (puntos de interés)
    try:
        with engine.connect() as connection:
            # Consulta para obtener todos los puntos de interés
            query_puntos = text("SELECT latitud, longitud, nombre_punto FROM puntos_interes")
            puntos_data = connection.execute(query_puntos).fetchall()
            
            puntos_df = pd.DataFrame(puntos_data, columns=['latitud', 'longitud', 'nombre_punto'])

    except Exception as e:
        return f"Error al obtener puntos de interés: {e}", 500

    # 2. Generación del mapa Folium
    
    # Crear un mapa centrado en la ubicación del usuario
    m = folium.Map(location=[lat, lon], zoom_start=14, tiles="cartodbpositron")
    
    # Añadir un marcador para la ubicación del usuario
    folium.Marker(
        [lat, lon],
        popup="Tu ubicación",
        icon=folium.Icon(color="green", icon="user", prefix='fa')
    ).add_to(m)
    
    # 3. Añadir puntos de interés como un Cluster de Marcadores
    marker_cluster = MarkerCluster().add_to(m)
    
    for index, row in puntos_df.iterrows():
        folium.Marker(
            location=[row['latitud'], row['longitud']],
            popup=row['nombre_punto'],
            icon=folium.Icon(color='blue', icon='info-sign')
        ).add_to(marker_cluster)

    # 4. Generar la ruta óptima (opcional, si es necesario, actualmente comentado)
    # G = ox.graph_from_point((lat, lon), dist=1000, network_type="drive")
    # ... (código de ruteo) ...

    # 5. Renderizar el mapa a HTML
    # Guardar el mapa en un objeto IO para pasarlo al HTML
    data = io.BytesIO()
    m.save(data, close_file=False)
    mapa_html = data.getvalue().decode('utf-8')

    # Renderizar la plantilla HTML con el mapa
    return render_template('ruta_movil_1.html', mapa_html=mapa_html)


if __name__ == '__main__':
    # Usar puerto de Railway si está disponible, sino usar 5000
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
