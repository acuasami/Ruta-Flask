from flask import Flask, send_file, render_template_string
import os

app = Flask(__name__)

# HTML básico para mostrar el mapa
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Mapa de Rutas Migratorias</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body { margin: 0; padding: 0; }
        #map { width: 100%; height: 100vh; }
    </style>
</head>
<body>
    <div id="map"></div>
    <script>
        // Redireccionar al archivo HTML del mapa
        window.location.href = '/mapa';
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/mapa')
def serve_map():
    try:
        return send_file('ruta_movil_1.html')
    except FileNotFoundError:
        return "Mapa no generado aún. Ejecuta el notebook primero.", 404

@app.route('/health')
def health():
    return "OK"

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)