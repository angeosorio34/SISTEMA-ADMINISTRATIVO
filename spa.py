"""
Sirve el frontend React compilado (frontend/dist) desde el mismo Django, para que
interfaz, API y admin vivan en un solo origen (http://servidor:8000/).

Cualquier ruta que no sea /api/, /admin/, /static/ o /assets/ devuelve index.html y
React Router decide que pantalla mostrar (/socios, /facturas/123, ...).
"""
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, HttpResponse


def index(request, *args, **kwargs):
    archivo = Path(settings.FRONTEND_DIST) / "index.html"
    if not archivo.exists():
        return HttpResponse(
            "<h1>Frontend sin compilar</h1>"
            "<p>Ejecute <code>npm run build</code> en la carpeta <code>frontend/</code>.</p>"
            '<p>Mientras tanto: <a href="/api/">API</a> · <a href="/admin/">Admin</a></p>',
            status=503,
        )
    respuesta = FileResponse(open(archivo, "rb"), content_type="text/html; charset=utf-8")
    respuesta["Cache-Control"] = "no-cache"  # los /assets/ llevan hash; el index siempre fresco
    return respuesta
