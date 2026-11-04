"""Inicio de sesión con el nombre de usuario (sin distinguir mayúsculas) o con el email del usuario.

El login dice "Nombre de Usuario o Email": si el texto tiene "@" se busca por email; si no, por usuario.
Si el email lo comparten dos usuarios no se adivina cuál es: hay que entrar con el nombre de usuario.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.utils import timezone
from rest_framework import exceptions
from rest_framework.authentication import TokenAuthentication

log = logging.getLogger("sga.seguridad")


class UsuarioOEmailBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        if not username or password is None:
            return None
        Usuario = get_user_model()
        texto = username.strip()
        campo = "email__iexact" if "@" in texto else "username__iexact"
        candidatos = list(Usuario._default_manager.filter(**{campo: texto})[:2])
        if len(candidatos) != 1:
            Usuario().set_password(password)      # mismo tiempo de respuesta exista o no el usuario
            return None
        usuario = candidatos[0]
        if usuario.check_password(password) and self.user_can_authenticate(usuario):
            return usuario
        return None


# -----------------------------------------------------------------------------
# Sesión de la API (cookie HttpOnly del navegador o «Authorization: Token» de programas); ver core/seguridad.py
# -----------------------------------------------------------------------------

COOKIE_SESION = "sga_sesion"
CABECERA_CSRF = "HTTP_X_SGA"
METODOS_SEGUROS = ("GET", "HEAD", "OPTIONS")


def horas_sesion() -> int:
    return int(getattr(settings, "SGA_SESION_HORAS", 12))


def ip_de(request) -> str:
    """IP del cliente (detrás del proxy de HTTPS, la que el proxy informa)."""
    if getattr(settings, "DETRAS_DE_PROXY", False):
        reenviada = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if reenviada:
            return reenviada.split(",")[-1].strip()[:45]
    return (request.META.get("REMOTE_ADDR") or "?")[:45]


# -----------------------------------------------------------------------------
# Autenticación
# -----------------------------------------------------------------------------

class TokenConVencimiento(TokenAuthentication):
    """Authorization: Token <clave>, con el mismo vencimiento que la sesión del navegador."""

    def authenticate_credentials(self, key):
        usuario, token = super().authenticate_credentials(key)
        if timezone.now() - token.created > timedelta(hours=horas_sesion()):
            token.delete()
            raise exceptions.AuthenticationFailed("Su sesión venció. Entre de nuevo.")
        return usuario, token


class CookieSesion(TokenConVencimiento):
    """La sesión del navegador: cookie HttpOnly + cabecera X-SGA en las escrituras (anti-CSRF)."""

    def authenticate(self, request):
        clave = request.COOKIES.get(COOKIE_SESION)
        if not clave or request.META.get("HTTP_AUTHORIZATION"):
            return None                 # credenciales explícitas (programas): las atiende TokenConVencimiento
        if request.method not in METODOS_SEGUROS:
            if request.META.get(CABECERA_CSRF) != "1" or not origen_permitido(request):
                log.warning("Escritura rechazada por protección CSRF: %s %s desde %s", request.method,
                            request.path, ip_de(request))
                raise exceptions.PermissionDenied("Solicitud bloqueada por seguridad: recargue la página e intente de nuevo.")
        return self.authenticate_credentials(clave)


def origen_permitido(request) -> bool:
    """Si el navegador dice de qué sitio viene la solicitud (Origin), debe ser este mismo."""
    origen = request.META.get("HTTP_ORIGIN")
    if not origen:
        return True
    propio = f"{'https' if request.is_secure() else 'http'}://{request.get_host()}"
    return origen == propio or origen in getattr(settings, "CSRF_TRUSTED_ORIGINS", []) \
        or origen in getattr(settings, "CORS_ALLOWED_ORIGINS", [])
