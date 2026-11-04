from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve

from core import seguridad, spa

urlpatterns = [
    path("api/", include("core.urls")),
    path("robots.txt", seguridad.robots),
    # Frontend React compilado: archivos con hash de Vite + index.html para el resto de rutas
    re_path(r"^assets/(?P<path>.*)$", serve, {"document_root": settings.FRONTEND_DIST / "assets"}),
]
if settings.DEBUG:
    # Solo en desarrollo: el admin de Django y la API navegable (inicio de sesión propio, sin los límites de intentos)
    urlpatterns += [
        path("admin/", admin.site.urls),
        path("api-auth/", include("rest_framework.urls")),
    ]
urlpatterns.append(re_path(r"^(?!api/|api-auth/|admin/|static/|assets/).*$", spa.index, name="frontend"))
