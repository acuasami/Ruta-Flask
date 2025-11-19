from flask import Flask, request, render_template_string
import os
import math
import pandas as pd
import osmnx as ox
import networkx as nx
import re
import psycopg2
from geopy.distance import geodesic
import folium
from shapely.geometry import Point, LineString
import geopandas as gpd
import numpy as np
from networkx.exception import NetworkXNoPath
import sys

app = Flask(__name__)

# --- CONFIGURACIÓN DE BASE DE DATOS (ARREGLADA Y SEGURA) ---

# 1. Intenta obtener la URL de conexión estándar (usada por Railway/Heroku)
DATABASE_URL = os.environ.get('DATABASE_URL')

# 2. Si no existe, construye la configuración a partir de variables individuales
# **IMPORTANTE: Se eliminaron los valores de fallback inseguros/hardcodeados.**
DB_CONFIG = {
    'host': os.environ.get('PGHOST'),
    'port': os.environ.get('PGPORT', 0), # Usamos 0 como valor seguro si no existe
    'dbname': os.environ.get('PGDATABASE'),
    'user': os.environ.get('PGUSER'),
    'password': os.environ.get('PGPASSWORD')
}

# --- TODA LA LÓGICA DE TU NOTEBOOK VA AQUÍ ---

def conectar_y_leer_sql(query):
    """
    Conecta a la BD, ejecuta una consulta y devuelve un DataFrame.
    Ahora incluye la configuración SSL requerida por Railway.
    """
    conn = None
    try:
        # Prioridad 1: Usar la URL completa (DSN) si está disponible (la más robusta)
        if DATABASE_URL:
            print("🔗 Intentando conectar usando DATABASE_URL...")
            conn = psycopg2.connect(DATABASE_URL)
        
        # Prioridad 2: Usar el diccionario de configuración de variables individuales
        else:
            # 1. Filtrar los parámetros válidos (sin Nones)
            valid_config = {k: v for k, v in DB_CONFIG.items() if v is not None and v != 0}
            
            # 2. Verificar que las claves críticas existan ANTES de conectar
            required_keys = ['host', 'dbname', 'user', 'password']
            if not all(key in valid_config for key in required_keys):
                print("❌ Faltan variables de entorno (PGHOST, PGDATABASE, PGUSER, PGPASSWORD) en el entorno de Railway.")
                print("   Asegúrate de que la base de datos esté enlazada correctamente al servicio.")
                sys.stdout.flush() 
                raise ValueError("Faltan credenciales DB.")

            print("🔗 Intentando conectar usando variables individuales con SSL...")
            
            # 3. Preparar parámetros de conexión
            if 'port' in valid_config:
                 valid_config['port'] = int(valid_config['port'])

            # ✅ LÍNEA CLAVE AGREGADA PARA FORZAR SSL EN RAILWAY:
            valid_config['sslmode'] = 'require' 
            
            # 4. Intentar la conexión
            conn = psycopg2.connect(**valid_config)

        # Si la conexión es exitosa, lee los datos
        df = pd.read_sql(query, conn)
        return df
        
    except Exception as e:
        print(f"❌ Error al conectar o leer la base de datos: {e}")
        # En caso de fallo, devuelve un DataFrame vacío 
        return pd.DataFrame()
        
    finally:
        if conn:
            conn.close() # Asegura que la conexión se cierre

def cargar_waypoints_ongs():
    """Carga todas las ONGs y municipios desde la BD."""
    QUERY_ONG = """
    SELECT o.nom_ong, o.tipo, o.latitud, o.longitud, m.nom_municipio 
    FROM public.ongs o
    JOIN public.municipio m ON o.id_municipio = m.id_municipio;
    """
    df_ongs = conectar_y_leer_sql(QUERY_ONG)
    
    if df_ongs.empty: # Manejo de error si la conexión falló
        print("⚠️ No se pudieron cargar las ONGs debido a un error de conexión/lectura.")
        return []
        
    df_ongs.rename(columns={
        'nom_ong': 'name', 'tipo': 'type', 'latitud': 'lat', 
        'longitud': 'lon', 'nom_municipio': 'municipio'
    }, inplace=True)
    
    df_ongs['lat'] = pd.to_numeric(df_ongs['lat'], errors='coerce')
    df_ongs['lon'] = pd.to_numeric(df_ongs['lon'], errors='coerce')
    df_ongs = df_ongs.dropna(subset=['lat', 'lon'])
    
    return [row.to_dict() for _, row in df_ongs.iterrows()]

def cargar_datos_riesgo():
    """Carga los datos de riesgo del último mes."""
    df_fecha = conectar_y_leer_sql("SELECT * FROM public.fecha;")
    df_municipio = conectar_y_leer_sql("SELECT * FROM public.municipio;")

    if df_fecha.empty or df_municipio.empty:
        print("⚠️ No se pudieron cargar los datos de riesgo debido a un error de conexión/lectura.")
        return {}

    df_fecha['fecha'] = pd.to_datetime(df_fecha['fecha'])
    ultimo_mes = df_fecha['fecha'].max().month
    ultimo_ano = df_fecha['fecha'].max().year
    
    df_ultimo = df_fecha[(df_fecha['fecha'].dt.month == ultimo_mes) &
                         (df_fecha['fecha'].dt.year == ultimo_ano)]
    
    # Unir con nombres de municipio
    df_riesgo_completo = pd.merge(df_ultimo, df_municipio, on='id_municipio')
    
    return dict(zip(df_riesgo_completo['nom_municipio'], df_riesgo_completo['grado']))

def ong_mas_cercana(pos_actual, waypoints):
    """Encuentra la ONG (no frontera) más cercana."""
    ongs = [w for w in waypoints if str(w.get('type', '')).strip().lower() != 'frontera']
    if not ongs:
        return None
    for o in ongs:
        o['distancia'] = geodesic(pos_actual, (o['lat'], o['lon'])).kilometers
    return min(ongs, key=lambda x: x['distancia'])

def find_sorted_ongs(start, waypoints_list):
    """Encuentra ONGs ordenadas por distancia."""
    candidates = []
    for ong in waypoints_list:
        if str(ong.get('type', '')).strip().lower() != 'frontera':
            ong_point = (ong["lat"], ong["lon"])
            dist = geodesic(start, ong_point).km
            ong['distancia'] = dist
            candidates.append(ong)
    candidates.sort(key=lambda x: x['distancia'])
    return candidates

def haversine_heuristic(u, v, G):
    """Heurística para A*."""
    lat1, lon1 = G.nodes[u]['y'], G.nodes[u]['x']
    lat2, lon2 = G.nodes[v]['y'], G.nodes[v]['x']
    R = 6371000  # Radio de la Tierra en metros
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

def obtener_municipio_por_proximidad(lat, lon, waypoints):
    """Aproximación del municipio basado en la ONG más cercana."""
    min_dist = float('inf')
    municipio_cercano = 'Desconocido'
    for ong in waypoints:
        dist = geodesic((lat, lon), (ong['lat'], ong['lon'])).kilometers
        if dist < min_dist:
            min_dist = dist
            municipio_cercano = ong.get('municipio', 'Desconocido')
    return municipio_cercano

def generar_mapa_movil_con_recomendaciones(ubicacion_usuario, ong_cercana, segmentos_ruta, waypoints, id_usuario, colores_riesgo, ongs_cercanas, siguiente_recomendacion):
    """Genera HTML optimizado para móviles con panel de pestañas incluyendo recomendaciones"""
    m = folium.Map(
        location=ubicacion_usuario,
        zoom_start=13,
        tiles="CartoDB positron",
        # Asegúrate de que el mapa se ajuste al WebView
        width='100%', 
        height='100vh' 
    )
    
    # Configuración para móviles
    m.options['touchZoom'] = True
    m.options['dragging'] = True
    m.options['scrollWheelZoom'] = False
    
    # --- DIBUJAR SEGMENTOS DE RUTA CON COLORES DE RIESGO ---
    print("🎨 Dibujando ruta con colores de riesgo...")
    for i, segmento in enumerate(segmentos_ruta):
        color = colores_riesgo.get(segmento['grado_riesgo'], 'gray')
        
        folium.PolyLine(
            segmento['coords'],
            color=color,
            weight=8,
            opacity=0.9,
            tooltip=f"🏙️ {segmento['municipio']} | 🎯 Riesgo: {segmento['grado_riesgo']}"
        ).add_to(m)
        print(f"   📍 Segmento {i+1}: {segmento['municipio']} - {segmento['grado_riesgo']} ({color})")
    
    # --- MARCADOR DEL USUARIO ---
    folium.Marker(
        location=ubicacion_usuario,
        popup=folium.Popup(
            f"""
            <div style='font-size:14px; max-width:250px;'>
                <div style='background:#4A00E0; color:white; padding:8px; border-radius:5px 5px 0 0; margin:-10px -10px 10px -10px;'>
                    <b>📍 Tu Ubicación Actual</b>
                </div>
                <p><b>👤 Usuario:</b> ID {id_usuario}</p>
                <p><b>🎯 Destino:</b> {ong_cercana.get('name', 'No disponible')}</p>
            </div>
            """,
            max_width=300
        ),
        tooltip="Tu ubicación",
        icon=folium.Icon(color="blue", icon="user", prefix="fa")
    ).add_to(m)
    
    # --- MARCADOR DE LA ONG DESTINO CON FICHA COMPLETA ---
    if ong_cercana and ong_cercana.get('name') != 'ONG no disponible':
        municipio_ong = ong_cercana.get('municipio', 'Desconocido')
        
        # Determinar icono según tipo
        tipo_icono = {
            'Albergue': 'bed',
            'Comedor': 'utensils',
            'Frontera': 'flag',
            'default': 'home'
        }
        icono = tipo_icono.get(ong_cercana.get('type', ''), tipo_icono['default'])
        
        folium.Marker(
            location=(ong_cercana['lat'], ong_cercana['lon']),
            popup=folium.Popup(
                f"""
                <div style='font-size:14px; max-width:280px;'>
                    <div style='background:#27ae60; color:white; padding:8px; border-radius:5px 5px 0 0; margin:-10px -10px 10px -10px;'>
                        <b>🏠 ONG Destino</b>
                    </div>
                    <p><b>📌 Nombre:</b> {ong_cercana['name']}</p>
                    <p><b>🎯 Tipo:</b> {ong_cercana['type']}</p>
                    <p><b>🏙️ Municipio:</b> {municipio_ong}</p>
                    <p><b>📏 Distancia:</b> {ong_cercana['distancia']:.1f} km</p>
                    <div style='background:#f8f9fa; padding:5px; border-radius:3px; margin:5px 0;'>
                        <small>📍 {ong_cercana['lat']:.4f}, {ong_cercana['lon']:.4f}</small>
                    </div>
                </div>
                """,
                max_width=320
            ),
            tooltip=f"Destino: {ong_cercana['name']}",
            icon=folium.Icon(color="green", icon=icono, prefix="fa")
        ).add_to(m)
    
    # --- MARCADOR DE LA SIGUIENTE RECOMENDACIÓN ---\
    if siguiente_recomendacion:
        folium.Marker(
            location=(siguiente_recomendacion['lat'], siguiente_recomendacion['lon']),
            popup=folium.Popup(
                f"""
                <div style='font-size:14px; max-width:280px;'>
                    <div style='background:#FF9800; color:white; padding:8px; border-radius:5px 5px 0 0; margin:-10px -10px 10px -10px;'>
                        <b>⭐ Próxima Recomendación</b>
                    </div>
                    <p><b>📌 Nombre:</b> {siguiente_recomendacion['name']}</p>
                    <p><b>🎯 Tipo:</b> {siguiente_recomendacion['type']}</p>
                    <p><b>🏙️ Municipio:</b> {siguiente_recomendacion.get('municipio', 'Desconocido')}</p>
                    <p><b>📏 Distancia:</b> {siguiente_recomendacion['distancia']:.1f} km</p>
                    <div style='background:#fff3e0; padding:5px; border-radius:3px; margin:5px 0;'>
                        <small>💡 Recomendación del sistema</small>
                    </div>
                </div>
                """,
                max_width=320
            ),
            tooltip=f"⭐ Recomendación: {siguiente_recomendacion['name']}",
            icon=folium.Icon(color="orange", icon="star", prefix="fa")
        ).add_to(m)
    
    # --- OTRAS ONGs CON FICHAS INFORMATIVAS COMPLETAS ---
    ongs_marcadas = 0
    for ong in waypoints:
        # Evitar marcar dos veces el destino y la recomendación
        is_dest = ong_cercana and ong['name'] == ong_cercana.get('name', '')
        is_rec = siguiente_recomendacion and ong['name'] == siguiente_recomendacion.get('name', '')
        
        if not is_dest and not is_rec:
            municipio_ong = ong.get('municipio', 'Desconocido')
            
            # Determinar color según tipo
            color_ong = {
                'Albergue': 'lightblue',
                'Comedor': 'orange',
                'Frontera': 'red',
                'default': 'gray'
            }
            color = color_ong.get(ong.get('type', ''), color_ong['default'])
            
            folium.CircleMarker(
                location=(ong['lat'], ong['lon']),
                radius=8,
                popup=folium.Popup(
                    f"""
                    <div style='font-size:13px; max-width:260px;'>
                        <div style='background:{color}; color:white; padding:6px; border-radius:5px 5px 0 0; margin:-10px -10px 8px -10px;'>
                            <b>🏠 Punto de Ayuda</b>
                        </div>
                        <p><b>📌 Nombre:</b> {ong['name']}</p>
                        <p><b>🎯 Tipo:</b> {ong['type']}</p>
                        <p><b>🏙️ Municipio:</b> {municipio_ong}</p>
                        <div style='background:#f8f9fa; padding:3px; border-radius:3px; margin:3px 0;'>
                            <small>📍 {ong['lat']:.4f}, {ong['lon']:.4f}</small>
                        </div>
                    </div>
                    """,
                    max_width=300
                ),
                tooltip=f"{ong['type']}: {ong['name']}",
                color=color,
                fillColor=color,
                weight=2,
                fillOpacity=0.7
            ).add_to(m)
            ongs_marcadas += 1
    
    print(f"📍 Marcadas {ongs_marcadas} ONGs adicionales con fichas informativas")
    
    # --- LEYENDA MEJORADA CON RECOMENDACIONES ---
    legend_html = '''
    <div style="
        position: fixed; 
        bottom: 20px; 
        left: 10px; 
        width: 220px; 
        height: auto;
        background-color: white; 
        border: 2px solid #4A00E0; 
        z-index: 9999; 
        font-size: 11px;
        padding: 10px;
        border-radius: 5px;
        box-shadow: 0 0 10px rgba(0,0,0,0.3);
    ">
        <h4 style="margin:0 0 8px 0; color:#4A00E0; font-size:12px;">🗺️ Leyenda del Mapa</h4>
        
        <div style="margin:5px 0;">
            <p style="margin:2px 0; font-weight:bold;">🎯 Niveles de Riesgo:</p>
            <p style="margin:2px 0;"><span style="color:red; font-weight:bold;">●</span> Alto</p>
            <p style="margin:2px 0;"><span style="color:orange; font-weight:bold;">●</span> Medio</p>
            <p style="margin:2px 0;"><span style="color:green; font-weight:bold;">●</span> Bajo</p>
            <p style="margin:2px 0;"><span style="color:gray; font-weight:bold;">●</span> Desconocido</p>
        </div>
        
        <div style="margin:5px 0;">
            <p style="margin:2px 0; font-weight:bold;">📍 Marcadores:</p>
            <p style="margin:2px 0;"><span style="color:orange;">⭐</span> Recomendación</p>
            <p style="margin:2px 0;"><span style="color:lightblue;">●</span> Albergue</p>
            <p style="margin:2px 0;"><span style="color:orange;">●</span> Comedor</p>
            <p style="margin:2px 0;"><span style="color:red;">●</span> Frontera</p>
        </div>
    </div>
    '''
    m.get_root().html.add_child(folium.Element(legend_html))
    
    # --- PANEL CON PESTAÑAS INCLUYENDO RECOMENDACIÓN ---
    destino_nombre = ong_cercana.get('name', 'No disponible') if ong_cercana else 'No disponible'
    destino_distancia = ong_cercana.get('distancia', 0) if ong_cercana else 0
    destino_tipo = ong_cercana.get('type', 'No disponible') if ong_cercana else 'No disponible'
    destino_municipio = ong_cercana.get('municipio', 'Desconocido') if ong_cercana else 'Desconocido'
    
    # Calcular estadísticas de riesgo de la ruta
    total_segmentos = len(segmentos_ruta)
    riesgo_alto = sum(1 for s in segmentos_ruta if s['grado_riesgo'] == 'Alto')
    riesgo_medio = sum(1 for s in segmentos_ruta if s['grado_riesgo'] == 'Medio')
    riesgo_bajo = sum(1 for s in segmentos_ruta if s['grado_riesgo'] == 'Bajo')
    
    # Preparar datos de recomendación
    if siguiente_recomendacion:
        rec_nombre = siguiente_recomendacion['name']
        rec_distancia = siguiente_recomendacion['distancia']
        rec_tipo = siguiente_recomendacion['type']
        rec_municipio = siguiente_recomendacion.get('municipio', 'Desconocido')
    else:
        rec_nombre = "No disponible"
        rec_distancia = 0
        rec_tipo = "No disponible"
        rec_municipio = "Desconocido"
    
    # Crear HTML para otras ONGs cercanas
    otras_ongs_html = ""
    # Evitar mostrar el destino actual y la recomendación
    ongs_filtradas = [o for o in ongs_cercanas if o['name'] != destino_nombre and o['name'] != rec_nombre]
    
    if ongs_filtradas:
        for ong in ongs_filtradas[:5]:
            otras_ongs_html += f"""
            <div style="font-size:10px; margin:4px 0; padding:5px; background:#f8f9fa; border-radius:4px; border-left: 3px solid #4A00E0;">
                <div style="font-weight:bold;">{ong['name']}</div>
                <div style="color:#666; font-size:9px;">{ong['type']} - {ong.get('municipio', 'Desconocido')} - {ong['distancia']:.1f} km</div>
            </div>
            """
    else:
        otras_ongs_html = '<div style="font-size:10px; color:#666; text-align:center;">No hay más ONGs cercanas</div>'
    
    # Crear HTML para municipios en ruta
    municipios_html = ""
    for segmento in segmentos_ruta:
        color = colores_riesgo.get(segmento['grado_riesgo'], 'gray')
        municipios_html += f'<div style="font-size:10px; margin:3px 0; padding:3px; border-left: 3px solid {color}; background: #f8f9fa;">{segmento["municipio"]} <span style="float:right; color:{color};">{segmento["grado_riesgo"]}</span></div>'
    
    info_html = f'''
<div style="
    position: fixed; 
    top: 10px; 
    right: 10px; 
    z-index: 9999; 
    font-family: Arial, sans-serif;
">
    <div id="info-toggle" style="
        background: #4A00E0; 
        color: white; 
        padding: 8px 15px; 
        border-radius: 20px; 
        cursor: pointer; 
        font-size: 12px; 
        font-weight: bold;
        box-shadow: 0 2px 8px rgba(0,0,0,0.3);
        text-align: center;
        margin-bottom: 5px;
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 5px;
    " onclick="toggleInfo()">
        <span>📋</span>
        <span>Información de Ruta</span>
        <span id="toggle-arrow">▼</span>
    </div>

    <div id="info-panel" style="
        background: white; 
        border: 2px solid #4A00E0; 
        border-radius: 10px; 
        width: 320px; 
        max-height: 500px; 
        overflow: hidden;
        box-shadow: 0 4px 15px rgba(0,0,0,0.2);
        display: none;
    ">
        <div style="display: flex; border-bottom: 1px solid #ddd; background: #f8f9fa;">
            <div class="tab-button active" onclick="switchTab('destino')" style="flex:1; padding:8px; text-align:center; cursor:pointer; border-bottom: 2px solid #4A00E0; font-size:11px;">🎯 Destino</div>
            <div class="tab-button" onclick="switchTab('riesgo')" style="flex:1; padding:8px; text-align:center; cursor:pointer; font-size:11px;">📊 Riesgo</div>
            <div class="tab-button" onclick="switchTab('recomendacion')" style="flex:1; padding:8px; text-align:center; cursor:pointer; font-size:11px;">⭐ Recomendación</div>
            <div class="tab-button" onclick="switchTab('ruta')" style="flex:1; padding:8px; text-align:center; cursor:pointer; font-size:11px;">🗺️ Ruta</div>
        </div>

        <div style="padding: 12px; max-height: 400px; overflow-y: auto;">
            
            <div id="tab-destino" class="tab-content">
                <div style="margin-bottom: 15px;">
                    <h4 style="margin:0 0 8px 0; color:#4A00E0; font-size:13px;">🏠 ONG Destino Actual</h4>
                    <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 10px; border-radius: 6px;">
                        <p style="margin:0 0 5px 0; font-size:12px; font-weight:bold;">{destino_nombre}</p>
                        <p style="margin:0; font-size:10px; opacity:0.9;">{destino_tipo} - {destino_municipio}</p>
                    </div>
                </div>

                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 10px;">
                    <div style="background: #e3f2fd; padding: 6px; border-radius: 4px; text-align: center;">
                        <div style="font-size:10px; color:#1976d2;">📏 Distancia</div>
                        <div style="font-size:12px; font-weight:bold; color:#1976d2;">{destino_distancia:.1f} km</div>
                    </div>
                    <div style="background: #e8f5e8; padding: 6px; border-radius: 4px; text-align: center;">
                        <div style="font-size:10px; color:#388e3c;">👤 Usuario</div>
                        <div style="font-size:12px; font-weight:bold; color:#388e3c;">ID {id_usuario}</div>
                    </div>
                </div>
            </div>

            <div id="tab-riesgo" class="tab-content" style="display: none;">
                <h4 style="margin:0 0 10px 0; color:#4A00E0; font-size:13px;">📊 Análisis de Riesgo</h4>
                
                <div style="margin-bottom: 15px;">
                    <div style="display: flex; justify-content: space-between; margin-bottom: 5px;">
                        <span style="font-size:11px; font-weight:bold;">Resumen de Segmentos:</span>
                        <span style="font-size:11px; font-weight:bold;">{total_segmentos} total</span>
                    </div>
                    
                    <div style="background: #f0f0f0; border-radius: 10px; height: 20px; margin-bottom: 10px; overflow: hidden;">
                        <div style="background: green; width: {(riesgo_bajo/total_segmentos)*100 if total_segmentos else 0}%; height: 100%; float: left;" title="Bajo: {riesgo_bajo}"></div>
                        <div style="background: orange; width: {(riesgo_medio/total_segmentos)*100 if total_segmentos else 0}%; height: 100%; float: left;" title="Medio: {riesgo_medio}"></div>
                        <div style="background: red; width: {(riesgo_alto/total_segmentos)*100 if total_segmentos else 0}%; height: 100%; float: left;" title="Alto: {riesgo_alto}"></div>
                    </div>
                    
                    <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 5px; text-align: center;">
                        <div>
                            <div style="color: green; font-size:12px;">● {riesgo_bajo}</div>
                            <div style="font-size:9px; color:#666;">Bajo</div>
                        </div>
                        <div>
                            <div style="color: orange; font-size:12px;">● {riesgo_medio}</div>
                            <div style="font-size:9px; color:#666;">Medio</div>
                        </div>
                        <div>
                            <div style="color: red; font-size:12px;">● {riesgo_alto}</div>
                            <div style="font-size:9px; color:#666;">Alto</div>
                        </div>
                    </div>
                </div>
            </div>

            <div id="tab-recomendacion" class="tab-content" style="display: none;">
                <h4 style="margin:0 0 10px 0; color:#4A00E0; font-size:13px;">⭐ Próxima Recomendación</h4>
                
                <div style="margin-bottom: 15px;">
                    <div style="background: linear-gradient(135deg, #FF9800 0%, #F57C00 100%); color: white; padding: 10px; border-radius: 6px; margin-bottom: 10px;">
                        <p style="margin:0 0 5px 0; font-size:12px; font-weight:bold;">{rec_nombre}</p>
                        <p style="margin:0; font-size:10px; opacity:0.9;">{rec_tipo} - {rec_municipio}</p>
                    </div>

                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 10px;">
                        <div style="background: #fff3e0; padding: 6px; border-radius: 4px; text-align: center;">
                            <div style="font-size:10px; color:#EF6C00;">📏 Distancia</div>
                            <div style="font-size:12px; font-weight:bold; color:#EF6C00;">{rec_distancia:.1f} km</div>
                        </div>
                        <div style="background: #e8f5e8; padding: 6px; border-radius: 4px; text-align: center;">
                            <div style="font-size:10px; color:#388e3c;">🎯 Tipo</div>
                            <div style="font-size:12px; font-weight:bold; color:#388e3c;">{rec_tipo}</div>
                        </div>
                    </div>

                    <div style="background: #e3f2fd; padding: 8px; border-radius: 5px; margin-bottom: 10px;">
                        <p style="margin:0; font-size:10px; color:#1976d2; font-weight:bold;">💡 Recomendación del Sistema</p>
                        <p style="margin:5px 0 0 0; font-size:9px; color:#1976d2;">Esta ONG ha sido seleccionada como tu próxima parada recomendada basada en proximidad y disponibilidad.</p>
                    </div>
                </div>

                <div style="border-top: 1px solid #eee; padding-top: 10px;">
                    <h5 style="margin:0 0 8px 0; color:#4A00E0; font-size:12px;">📍 Otras ONGs Cercanas</h5>
                    <div style="max-height: 150px; overflow-y: auto;">
                        {otras_ongs_html}
                    </div>
                </div>
            </div>

            <div id="tab-ruta" class="tab-content" style="display: none;">
                <h4 style="margin:0 0 10px 0; color:#4A00E0; font-size:13px;">🗺️ Detalles de Ruta</h4>
                
                <div style="margin-bottom: 10px;">
                    <div style="font-size:11px; margin-bottom: 5px;"><b>ONGs disponibles:</b> {len(waypoints)}</div>
                    <div style="font-size:11px; margin-bottom: 5px;"><b>Segmentos calculados:</b> {total_segmentos}</div>
                </div>

                <div style="max-height: 200px; overflow-y: auto; border: 1px solid #eee; border-radius: 5px; padding: 8px;">
                    <div style="font-size:11px; font-weight:bold; margin-bottom: 5px;">Municipios en ruta:</div>
                    {municipios_html}
                </div>
            </div>

        </div>
    </div>
</div>

<style>
.tab-button {{
    transition: all 0.3s ease;
}}
.tab-button:hover {{
    background: #e3f2fd;
}}
.tab-button.active {{
    background: #4A00E0;
    color: white;
}}
.tab-content {{
    animation: fadeIn 0.3s ease;
}}
@keyframes fadeIn {{
    from {{ opacity: 0; }}
    to {{ opacity: 1; }}
}}
</style>

<script>
function toggleInfo() {{
    var panel = document.getElementById('info-panel');
    var arrow = document.getElementById('toggle-arrow');
    if (panel.style.display === 'none' || panel.style.display === '') {{
        panel.style.display = 'block';
        arrow.innerHTML = '▲';
    }} else {{
        panel.style.display = 'none';
        arrow.innerHTML = '▼';
    }}
}}

function switchTab(tabName) {{
    // Ocultar todos los contenidos
    var contents = document.getElementsByClassName('tab-content');
    for (var i = 0; i < contents.length; i++) {{
        contents[i].style.display = 'none';
    }}
    
    // Remover clase active de todos los botones
    var buttons = document.getElementsByClassName('tab-button');
    for (var i = 0; i < buttons.length; i++) {{
        buttons[i].classList.remove('active');
    }}
    
    // Mostrar contenido seleccionado y activar botón
    document.getElementById('tab-' + tabName).style.display = 'block';
    event.target.classList.add('active');
}}

// Cerrar al hacer clic fuera
document.addEventListener('click', function(event) {{
    var panel = document.getElementById('info-panel');
    var button = document.getElementById('info-toggle');
    if (!panel.contains(event.target) && !button.contains(event.target)) {{
        panel.style.display = 'none';
        document.getElementById('toggle-arrow').innerHTML = '▼';
    }}
}});

// Inicializar con primera pestaña activa
document.addEventListener('DOMContentLoaded', function() {{
    switchTab('destino');
}});
</script>
'''
    m.get_root().html.add_child(folium.Element(info_html))
    
    # Guardar con meta tags para móvil
    archivo_html = f"ruta_movil_{id_usuario}.html"
    html_content = m.get_root().render()
    
    # Meta tags optimizados para móvil
    html_content = html_content.replace('<head>', '''
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <style>
            body { 
                margin: 0; 
                padding: 0; 
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            }
            #map { 
                position: absolute; 
                top: 0; 
                bottom: 0; 
                width: 100%; 
            }
            .leaflet-popup-content { 
                font-size: 14px; 
                line-height: 1.4;
            }
            .leaflet-control-zoom {
                margin-top: 180px !important;
            }
            .leaflet-popup-content-wrapper {
                border-radius: 8px;
                box-shadow: 0 3px 10px rgba(0,0,0,0.2);
            }
        </style>
    ''')

    return html_content

@app.route('/')
def index():
    # Página de bienvenida simple
    return "Servidor de Mapas Activo. Usa la app móvil."

@app.route('/health')
def health():
    return "OK"

@app.route('/mapa')
def serve_map():
    """
    Ruta principal que genera el mapa dinámicamente.
    """
    try:
        # 1. Obtener parámetros de la URL enviados desde la app móvil
        lat = request.args.get('lat', type=float)
        lon = request.args.get('lon', type=float)
        id_usuario = request.args.get('id_usuario', default=1, type=int)
        
        # Validación básica de coordenadas
        if lat is None or lon is None:
            return "Error: Faltan parámetros 'lat' o 'lon' en la URL.", 400
            
        start_point = (lat, lon)
        
        print(f"🚀 Petición recibida para Usuario: {id_usuario}, Ubicación: {start_point}")

        # 2. Cargar datos (igual que en el notebook)
        waypoints = cargar_waypoints_ongs()
        riesgo_por_municipio_nombre = cargar_datos_riesgo()
        colores_riesgo = {'Alto': 'red', 'Medio': 'orange', 'Bajo': 'green', 'Desconocido': 'gray'}

        # Si no hay waypoints, no podemos calcular nada
        if not waypoints:
            print("❌ No hay waypoints disponibles. Comprueba la conexión a la base de datos.")
            return "Error: No hay datos de ONGs disponibles. Comprueba la BD.", 500
        
        # 3. Calcular ONG más cercana y recomendación
        ong_cercana = ong_mas_cercana(start_point, waypoints)
        ongs_ordenadas = find_sorted_ongs(start_point, waypoints)
        
        siguiente_recomendacion = None
        if len(ongs_ordenadas) > 1:
            siguiente_recomendacion = ongs_ordenadas[1]
        elif len(ongs_ordenadas) == 1:
            siguiente_recomendacion = ongs_ordenadas[0]
            
        # Si el destino es la única ONG, la recomendación no tiene sentido, la filtramos para el panel
        if siguiente_recomendacion and ong_cercana and siguiente_recomendacion['name'] == ong_cercana['name']:
            ongs_cercanas = ongs_ordenadas[:1] # Solo el destino
        else:
            ongs_cercanas = ongs_ordenadas[:5]

        # 4. Calcular la ruta (¡Esta es la parte lenta!)
        segmentos_ruta = []
        if ong_cercana:
            try:
                dest_point = (ong_cercana['lat'], ong_cercana['lon'])
                
                # 1. Definir el bounding box (rectángulo)
                north = max(lat, dest_point[0])
                south = min(lat, dest_point[0])
                east = max(lon, dest_point[1])
                west = min(lon, dest_point[1])
                
                # 2. Añadir un pequeño margen (padding) de aprox. 1.1km
                padding = 0.01 
                
                print(f"🗺️ Descargando grafo OSMnx desde Bounding Box...")
                
                # 3. Descargar solo ese rectángulo (mucho más rápido)
                G = ox.graph_from_bbox(
                    north + padding, 
                    south - padding, 
                    east + padding, 
                    west - padding, 
                    network_type="drive"
                )
                print("✅ Grafo descargado")
                
                orig_node = ox.distance.nearest_nodes(G, lon, lat)
                dest_node = ox.distance.nearest_nodes(G, dest_point[1], dest_point[0])
                
                print("🧠 Calculando ruta A*...")
                route = nx.astar_path(
                    G, orig_node, dest_node,
                    heuristic=lambda u, v: haversine_heuristic(u, v, G),
                    weight='length'
                )
                
                route_coords = [(G.nodes[n]['y'], G.nodes[n]['x']) for n in route]
                
                # --- INICIO DE LA LÓGICA DE SEGMENTACIÓN COMPLETA ---
                
                segmento_actual = []
                municipio_actual = None
                riesgo_actual = 'Desconocido' # Inicializar
        
                print("📊 Segmentando ruta por municipios y nivel de riesgo...")
                for i, coord in enumerate(route_coords):
                    lat_coord, lon_coord = coord # Usamos variables locales para la coordenada
                    
                    # Esta es la función de aproximación que usaste
                    municipio = obtener_municipio_por_proximidad(lat_coord, lon_coord, waypoints) 
                    
                    # Obtener riesgo para este municipio
                    riesgo = riesgo_por_municipio_nombre.get(municipio, 'Desconocido')
                    
                    if municipio_actual is None:
                        municipio_actual = municipio
                        segmento_actual.append(coord)
                        riesgo_actual = riesgo
                    elif municipio == municipio_actual:
                        segmento_actual.append(coord)
                    else:
                        # Cambio de municipio - guardar segmento anterior y empezar nuevo
                        if segmento_actual:
                            segmentos_ruta.append({
                                'coords': segmento_actual.copy(),
                                'municipio': municipio_actual,
                                'grado_riesgo': riesgo_actual
                            })
                        
                        municipio_actual = municipio
                        segmento_actual = [coord] # Empezar nuevo segmento
                        riesgo_actual = riesgo
            
                # Añadir el último segmento
                if segmento_actual:
                    segmentos_ruta.append({
                        'coords': segmento_actual,
                        'municipio': municipio_actual,
                        'grado_riesgo': riesgo_actual
                    })
            
                print(f"📊 Ruta segmentada en {len(segmentos_ruta)} tramos por nivel de riesgo")
                
                # --- FIN DE LA LÓGICA DE SEGMENTACIÓN ---

            except NetworkXNoPath:
                print(f"❌ NO SE ENCONTRÓ RUTA. Es posible que los puntos no estén conectados por calles.")
                segmentos_ruta = [] 
            except Exception as e:
                print(f"❌ Error al calcular la ruta: {e}")
                segmentos_ruta = []
        
        # 5. Generar y devolver el HTML del mapa
        print("🎨 Generando mapa Folium...")
        map_html = generar_mapa_movil_con_recomendaciones(
            start_point, 
            ong_cercana, 
            segmentos_ruta, 
            waypoints, 
            id_usuario,
            colores_riesgo,
            ongs_cercanas,
            siguiente_recomendacion
        )
        
        print("✅ Mapa generado. Enviando HTML a la app.")
        return map_html

    except Exception as e:
        print(f"💥 Error fatal en /mapa: {e}")
        return f"<h1>Error al generar el mapa:</h1><p>{e}</p>", 500


if __name__ == '__main__':
    # Esta parte solo se usa para pruebas locales, Railway usa el 'Procfile'
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
