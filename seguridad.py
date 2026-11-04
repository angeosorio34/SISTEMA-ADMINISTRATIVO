"""
Seguridad de SGA-GALLEGO: sesión, intentos de entrada, límites de uso, cabeceras y registro de seguridad.

Sesión
  Al entrar, el servidor deja la clave de la sesión en una cookie HttpOnly (el código de la página no puede
  leerla, así que un script inyectado no puede robarla), SameSite=Strict y Secure cuando la conexión es HTTPS.
  La sesión vence a las SGA_SESION_HORAS horas (12 por defecto) y «Cerrar sesión» la anula en el servidor.
  Las escrituras con la cookie exigen la cabecera X-SGA: 1, que otro sitio no puede enviar sin permiso del
  servidor (CORS): así se bloquea la falsificación de solicitudes (CSRF).
  Los programas y pruebas pueden usar «Authorization: Token <clave>»; esa clave nunca llega al navegador.
  Las clases que leen la sesión están en core/autenticacion.py (DRF las carga desde la configuración).

Entrada
  Más de SGA_INTENTOS_MAX intentos fallidos con el mismo usuario lo bloquean SGA_BLOQUEO_MINUTOS minutos
  (exista o no, para no revelar qué usuarios existen); además hay un límite de intentos por dirección IP y un
  campo trampa invisible que solo rellenan los robots.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth.models import update_last_login
from django.core.cache import cache
from django.http import HttpResponse
from django.middleware.gzip import GZipMiddleware
from django.utils import timezone
from rest_framework import exceptions
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle

from .autenticacion import CABECERA_CSRF, COOKIE_SESION, CookieSesion, horas_sesion, ip_de, origen_permitido
from .permissions import tiene_acceso_total

log = logging.getLogger("sga.seguridad")


# -----------------------------------------------------------------------------
# Límites de uso (DRF throttling)
# -----------------------------------------------------------------------------

class IntentosEntrada(AnonRateThrottle):
    scope = "entrada"

    def get_ident(self, request):
        return ip_de(request)


class OperacionesPesadas(UserRateThrottle):
    """Copias, restauraciones y cargas desde Access: pocas por hora y por usuario."""
    scope = "pesado"


class OperacionesPesadasSinSesion(AnonRateThrottle):
    scope = "pesado"

    def get_ident(self, request):
        return ip_de(request)


# -----------------------------------------------------------------------------
# Bloqueo por intentos fallidos
# -----------------------------------------------------------------------------

def _clave_bloqueo(usuario: str) -> str:
    return "sga-fallos-" + hashlib.sha256(usuario.strip().lower().encode("utf-8")).hexdigest()[:32]


def intentos_fallidos(usuario: str) -> int:
    return cache.get(_clave_bloqueo(usuario), 0)


def registrar_fallo(usuario: str) -> int:
    clave = _clave_bloqueo(usuario)
    total = cache.get(clave, 0) + 1
    cache.set(clave, total, settings.SGA_BLOQUEO_MINUTOS * 60)
    return total


def desbloquear(usuario: str) -> None:
    cache.delete(_clave_bloqueo(usuario))


def _nombre_para_registro(texto: str) -> str:
    """En el registro solo va el nombre si es un usuario real (si alguien escribe su clave en el campo
    usuario, la clave no queda escrita en el registro)."""
    from django.contrib.auth import get_user_model

    texto = (texto or "").strip()
    campo = "email__iexact" if "@" in texto else "username__iexact"
    usuario = get_user_model().objects.filter(**{campo: texto}).values_list("username", flat=True).first()
    return usuario or "(usuario inexistente)"


# -----------------------------------------------------------------------------
# Entrar y salir
# -----------------------------------------------------------------------------

def datos_usuario(u) -> dict:
    return {"username": u.username, "nombre": u.get_full_name() or u.username, "acceso_total": tiene_acceso_total(u),
            "grupos": list(u.groups.values_list("name", flat=True))}


def _token_vigente(usuario) -> Token:
    token = Token.objects.filter(user=usuario).first()
    if token and timezone.now() - token.created > timedelta(hours=horas_sesion()):
        token.delete()
        token = None
    return token or Token.objects.create(user=usuario)


def _poner_cookie(request, respuesta, token: Token) -> None:
    restante = token.created + timedelta(hours=horas_sesion()) - timezone.now()
    respuesta.set_cookie(COOKIE_SESION, token.key, max_age=max(int(restante.total_seconds()), 0), path="/",
                         secure=request.is_secure(), httponly=True, samesite="Strict")


def _entrar(request, devolver_clave: bool) -> Response:
    texto = str(request.data.get("username") or "")[:150]
    clave = str(request.data.get("password") or "")
    if not texto.strip() or not clave:
        return Response({"detail": "Escriba su usuario y su contraseña."}, status=400)
    if request.data.get("sitio_web"):                     # campo trampa: solo lo rellena un robot
        log.warning("Entrada rechazada (campo trampa relleno) desde %s", ip_de(request))
        return Response({"detail": "Usuario o contraseña incorrectos."}, status=400)
    if intentos_fallidos(texto) >= settings.SGA_INTENTOS_MAX:
        log.warning("Entrada bloqueada para %s desde %s (demasiados intentos)", _nombre_para_registro(texto), ip_de(request))
        return Response({"detail": f"Demasiados intentos fallidos. Espere {settings.SGA_BLOQUEO_MINUTOS} minutos "
                                   "o pida a un administrador que le reinicie la clave."}, status=429)
    usuario = authenticate(request, username=texto, password=clave)
    if usuario is None:
        total = registrar_fallo(texto)
        log.warning("Entrada fallida (%d) para %s desde %s", total, _nombre_para_registro(texto), ip_de(request))
        return Response({"detail": "Usuario o contraseña incorrectos."}, status=400)
    desbloquear(texto)
    update_last_login(None, usuario)
    token = _token_vigente(usuario)
    log.info("Entrada de %s desde %s", usuario.username, ip_de(request))
    cuerpo = {"usuario": datos_usuario(usuario)}
    if devolver_clave:
        cuerpo["token"] = token.key
    respuesta = Response(cuerpo)
    _poner_cookie(request, respuesta, token)
    return respuesta


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([IntentosEntrada])
def entrar(request):
    """POST /api/auth/entrar/ {username, password} -> datos del usuario + cookie de sesión HttpOnly."""
    return _entrar(request, devolver_clave=False)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([IntentosEntrada])
def token_para_programas(request):
    """POST /api/auth/token/: igual que entrar, y además devuelve la clave para «Authorization: Token» (programas
    y pruebas; la página nunca la usa)."""
    return _entrar(request, devolver_clave=True)


@api_view(["GET"])
@authentication_classes([])       # la cookie se revisa aquí: una vencida no debe dar 401 antes de llegar
@permission_classes([AllowAny])
def sesion(request):
    """GET /api/auth/sesion/ -> {"usuario": {...}} o {"usuario": null}: lo que pregunta la página al abrirse
    (sin sesión responde 200, no 401, para no llenar la consola del navegador de errores)."""
    try:
        resultado = CookieSesion().authenticate(request)
    except exceptions.APIException:
        resultado = None
    if not resultado:
        respuesta = Response({"usuario": None})
        if COOKIE_SESION in request.COOKIES:          # cookie vencida o anulada: se borra
            respuesta.delete_cookie(COOKIE_SESION, path="/", samesite="Strict")
        return respuesta
    return Response({"usuario": datos_usuario(resultado[0])})


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def salir(request):
    """POST /api/auth/salir/: anula la sesión en el servidor y borra la cookie."""
    clave = request.COOKIES.get(COOKIE_SESION)
    if clave and request.META.get(CABECERA_CSRF) == "1" and origen_permitido(request):
        token = Token.objects.filter(key=clave).select_related("user").first()
        if token:
            log.info("Salida de %s desde %s", token.user.username, ip_de(request))
            token.delete()
    respuesta = Response(status=204)
    respuesta.delete_cookie(COOKIE_SESION, path="/", samesite="Strict")
    return respuesta


# -----------------------------------------------------------------------------
# Cabeceras de seguridad y robots
# -----------------------------------------------------------------------------

CSP = "; ".join((
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",          # atributos style de React y de la ventana del PDF
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    "connect-src 'self'",
    "object-src 'self' blob:",                    # visor de PDF del navegador
    "frame-src 'self' blob:",
    "worker-src 'self' blob:",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
))
PERMISOS = "camera=(), microphone=(), geolocation=(), payment=(), usb=(), serial=(), bluetooth=(), interest-cohort=()"


class CabecerasSeguridad:
    """Content-Security-Policy, Permissions-Policy, no indexar, y no guardar datos personales en caché."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        respuesta = self.get_response(request)
        if not request.path.startswith("/admin/"):     # el admin de Django (solo en desarrollo) usa sus scripts
            csp = CSP + ("; upgrade-insecure-requests" if request.is_secure() else "")
            respuesta.setdefault("Content-Security-Policy", csp)
        respuesta.setdefault("Permissions-Policy", PERMISOS)
        respuesta.setdefault("X-Robots-Tag", "noindex, nofollow, noarchive")
        respuesta.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        if request.path.startswith("/api/"):
            respuesta["Cache-Control"] = "no-store"
            respuesta.setdefault("Pragma", "no-cache")
        return respuesta


class ArchivosDelDiseno(GZipMiddleware):
    """/assets/ (programa, estilos, fuentes e imágenes de la página): comprimidos y en caché por un año.
    Sus nombres llevan una huella que cambia con cada versión, así que nunca se sirve uno viejo. Por un enlace de
    internet desde esta computadora la primera carga baja de ~15 s a ~4 s. Solo /assets/: los datos (/api/) no se
    comprimen (así no se expone a ataques de tipo BREACH) y siguen sin guardarse en caché."""

    def process_response(self, request, response):
        if not request.path.startswith("/assets/") or response.status_code != 200:
            return response
        response["Cache-Control"] = "public, max-age=31536000, immutable"
        return super().process_response(request, response)


def robots(request):
    return HttpResponse("User-agent: *\nDisallow: /\n", content_type="text/plain; charset=utf-8")
