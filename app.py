from flask import Flask, request, render_template, url_for, redirect, jsonify, Response
import os
import math
import pandas as pd
import osmnx as ox
import networkx as nx
import re
import psycopg2
from geopy.distance import geodesic
import folium
from folium.plugins import MarkerCluster
from shapely.geometry import Point, LineString
import geopandas as gpd
import numpy as np
from networkx.exception import NetworkXNoPath
from sqlalchemy import create_engine, text
import sys 
import io 

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
    # Fallback para desarrollo local si la variable de entorno no está establecida
    FINAL_DATABASE_URL = "postgresql://user:password@localhost:5432/mydatabase"
else:
    # Usar la URL tal cual si no es PostgreSQL o tiene otro formato
    FINAL_DATABASE_URL = DATABASE_URL

# Crear el motor de SQLAlchemy
engine = create_engine(FINAL_DATABASE_URL)
# -----------------------------------------------------------------------------

# Funciones de lógica (sin cambios aquí)
def cargar_waypoints_ongs():
    # ... (Tu función completa de cargar_waypoints_ongs aquí)
    pass 

def cargar_datos_riesgo():
    # ... (Tu función completa de cargar_datos_riesgo aquí)
    pass

def ong_mas_cercana(pos_actual, waypoints):
    # ... (Tu función completa de ong_mas_cercana aquí)
    pass

def find_sorted_ongs(start, waypoints_list):
    # ... (Tu función completa de find_sorted_ongs aquí)
    pass

def haversine_heuristic(u, v, G):
    # ... (Tu función completa de haversine_heuristic aquí)
    pass

def obtener_municipio_por_proximidad(lat, lon, waypoints):
    # ... (Tu función completa de obtener_municipio_por_proximidad aquí)
    pass

def generar_mapa_movil_con_recomendaciones(ubicacion_usuario, ong_cercana, segmentos_ruta, waypoints, id_usuario, colores_riesgo, ongs_cercanas, siguiente_recomendacion):
    # ... (Tu función completa de generar_mapa_movil_con_recomendaciones aquí)
    pass


@app.route('/')
def index():
    return render_template('login.html')

@app.route('/login', methods=['POST'])
def login():
    usuario = request.form['usuario']
    password = request.form['password']

    try:
        with engine.connect() as connection:
            # Consulta para buscar el usuario
            query = text("SELECT id_usuario FROM usuarios WHERE usuario = :usuario AND password = :password")
            result = connection.execute(query, {"usuario": usuario, "password": password}).fetchone()

            if result:
                id_usuario = result[0]
                # Redirigir al mapa con el id_usuario
                return redirect(url_for('mapa', id_usuario=id_usuario))
            else:
                return render_template('login.html', error="Usuario o contraseña incorrectos")
    except Exception as e:
        # Esto envía un mensaje legible si la BD falla (Error 500)
        print(f"Error al intentar conectar o ejecutar la consulta: {e}")
        return jsonify({"error_code": "DB_CONNECTION_FAILED", "message": f"Error de conexión o consulta a la BD: {e}"}), 500


@app.route('/mapa')
def mapa():
    lat = request.args.get('lat', type=float)
    lon = request.args.get('lon', type=float)
    id_usuario = request.args.get('id_usuario', type=int)

    if lat is None or lon is None or id_usuario is None:
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

    # 4. Renderizar el mapa a HTML
    data = io.BytesIO()
    m.save(data, close_file=False)
    mapa_html = data.getvalue().decode('utf-8')

    # Renderizar la plantilla HTML con el mapa
    return render_template('ruta_movil_1.html', mapa_html=mapa_html)

# --- Bloque de ejecución final eliminado, ya que Gunicorn lo maneja ---
# El servidor ahora se inicia con el Procfile: web: gunicorn --bind 0.0.0.0:$PORT app:app --workers 1
