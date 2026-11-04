"""
API de copias de seguridad (Configuración -> Copias de seguridad) y restauración inicial en un equipo nuevo.

Todo exige acceso total, salvo /instalacion/: cuando el sistema aún no tiene datos (computadora nueva)
la pantalla de inicio permite restaurar una copia, solo desde la misma computadora del servidor.
"""
import calendar
import logging
import tempfile
from pathlib import Path

from django.conf import settings
from django.http import FileResponse
from rest_framework import serializers
from rest_framework.decorators import api_view, authentication_classes, parser_classes, permission_classes, throttle_classes
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from . import fotos
from . import importar_access as ia
from . import respaldos as r
from .seguridad import OperacionesPesadas, OperacionesPesadasSinSesion, ip_de
from .usuarios import SoloAccesoTotal

log = logging.getLogger("sga.seguridad")


class SoloAccesoTotalCopias(SoloAccesoTotal):
    message = "Solo un usuario con acceso total puede gestionar las copias de seguridad."


def _error(e: r.RespaldoError) -> Response:
    codigo = 409 if isinstance(e, r.Ocupado) else 404 if isinstance(e, r.NoEncontrada) else 400
    return Response({"detail": str(e)}, status=codigo)


def _guardar_subida(archivo) -> Path:
    """Guarda el .zip subido en la carpeta de copias (nombre temporal que no se lista)."""
    if archivo is None:
        raise ValidationError({"archivo": "Adjunte el archivo .zip de la copia de seguridad."})
    if archivo.size > settings.RESPALDOS_MAX_SUBIDA_MB * 1024 * 1024:
        raise ValidationError({"archivo": f"El archivo supera el máximo de {settings.RESPALDOS_MAX_SUBIDA_MB} MB."})
    r.carpeta().mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=r.carpeta(), prefix=".subida-", suffix=".zip", delete=False) as destino:
        for trozo in archivo.chunks():
            destino.write(trozo)
    return Path(destino.name)


# ------------------------------------------------------------------ programación

class _Turno(serializers.Serializer):
    activa = serializers.BooleanField()
    hora = serializers.RegexField(r"^([01]\d|2[0-3]):[0-5]\d$", error_messages={"invalid": "Hora no válida (HH:MM)."})
    conservar = serializers.IntegerField(min_value=1, max_value=365)


class _Semanal(_Turno):
    dia_semana = serializers.IntegerField(min_value=0, max_value=6)


class _Anual(_Turno):
    dia = serializers.IntegerField(min_value=1, max_value=31)
    mes = serializers.IntegerField(min_value=1, max_value=12)

    def validate(self, datos):
        if datos["dia"] > calendar.monthrange(2024, datos["mes"])[1]:
            raise ValidationError({"dia": "Ese mes no tiene ese día."})
        return datos


class ProgramacionSerializer(serializers.Serializer):
    diaria = _Turno()
    semanal = _Semanal()
    anual = _Anual()


# ------------------------------------------------------------------ copias

@api_view(["GET"])
@permission_classes([SoloAccesoTotalCopias])
def estado(request):
    """GET /api/respaldos/ -> programación, últimas ejecuciones y copias guardadas (?resumen=1: aviso del panel)."""
    if request.query_params.get("resumen"):
        return Response(r.resumen_estado())
    return Response(r.estado())


@api_view(["PUT"])
@permission_classes([SoloAccesoTotalCopias])
def programacion(request):
    datos = ProgramacionSerializer(data=request.data)
    if not datos.is_valid():
        # Un solo mensaje legible: "Diaria: Hora no válida (HH:MM)."
        titulos = {"diaria": "Copia diaria", "semanal": "Copia semanal", "anual": "Copia anual"}
        mensajes = []
        for tipo, errores in datos.errors.items():
            grupos = errores.values() if isinstance(errores, dict) else [errores]
            mensajes += [f"{titulos.get(tipo, tipo)}: {' '.join(map(str, lista))}" for lista in grupos]
        return Response({"detail": " ".join(mensajes)}, status=400)
    r.guardar_programacion(datos.validated_data)
    return Response(r.estado())


@api_view(["POST"])
@permission_classes([SoloAccesoTotalCopias])
@throttle_classes([OperacionesPesadas])
def crear(request):
    try:
        copia = r.crear("manual", request.user.username)
    except r.RespaldoError as e:
        return _error(e)
    return Response({"copia": copia}, status=201)


@api_view(["POST"])
@permission_classes([SoloAccesoTotalCopias])
@throttle_classes([OperacionesPesadas])
@parser_classes([MultiPartParser])
def subir(request):
    """POST /api/respaldos/subir/ (multipart "archivo"): copia traída de otra computadora."""
    temporal = _guardar_subida(request.FILES.get("archivo"))
    try:
        copia, ya_existia = r.registrar_subida(temporal)
    except r.RespaldoError as e:
        return _error(e)
    finally:
        temporal.unlink(missing_ok=True)
    return Response({"copia": copia, "ya_existia": ya_existia}, status=200 if ya_existia else 201)


@api_view(["DELETE"])
@permission_classes([SoloAccesoTotalCopias])
def eliminar(request, nombre):
    try:
        r.eliminar(nombre)
    except r.RespaldoError as e:
        return _error(e)
    return Response(status=204)


@api_view(["GET"])
@permission_classes([SoloAccesoTotalCopias])
def descargar(request, nombre):
    try:
        ruta = r.ruta_copia(nombre)
    except r.RespaldoError as e:
        return _error(e)
    return FileResponse(open(ruta, "rb"), as_attachment=True, filename=ruta.name, content_type="application/zip")


@api_view(["POST"])
@permission_classes([SoloAccesoTotalCopias])
@throttle_classes([OperacionesPesadas])
def restaurar(request, nombre):
    """POST /api/respaldos/<nombre>/restaurar/ {"confirmacion": "RESTAURAR", "clave": "<clave del usuario>"}."""
    if request.data.get("confirmacion") != "RESTAURAR":
        raise ValidationError({"confirmacion": "Escriba RESTAURAR para confirmar."})
    if not request.user.check_password(request.data.get("clave") or ""):
        log.warning("Restauración rechazada (clave incorrecta) para %s desde %s", request.user.username, ip_de(request))
        raise ValidationError({"clave": "Clave incorrecta."})
    log.info("Restauración de %s pedida por %s desde %s", nombre, request.user.username, ip_de(request))
    try:
        resultado = r.restaurar(r.ruta_copia(nombre), request.user.username)
    except r.RespaldoError as e:
        return _error(e)
    return Response(resultado)


# ------------------------------------------------------------------ cargar lo nuevo desde Access

def _error_access(e: Exception) -> Response:
    return Response({"detail": str(e)}, status=409 if isinstance(e, r.Ocupado) else 400)


@api_view(["GET"])
@permission_classes([SoloAccesoTotalCopias])
def access_estado(request):
    disponible, motivo = ia.disponibilidad()
    return Response({"disponible": disponible, "motivo": motivo, "ultima_carga": ia.ultima_carga()})


@api_view(["POST"])
@permission_classes([SoloAccesoTotalCopias])
@throttle_classes([OperacionesPesadas])
@parser_classes([MultiPartParser])
def access_analizar(request):
    """POST multipart "archivo" (.accdb) + "clave": compara sin guardar y devuelve lo que se cargaría."""
    archivo = request.FILES.get("archivo")
    if archivo is None:
        raise ValidationError({"archivo": "Elija el archivo de la base de Access (.accdb)."})
    if archivo.size > settings.RESPALDOS_MAX_SUBIDA_MB * 1024 * 1024:
        raise ValidationError({"archivo": f"El archivo supera el máximo de {settings.RESPALDOS_MAX_SUBIDA_MB} MB."})
    try:
        token = ia.guardar_subida(archivo, archivo.name)
    except ia.ImportacionError as e:
        return _error_access(e)
    try:
        return Response(ia.analizar(token, request.data.get("clave") or ""))
    except (ia.ImportacionError, r.RespaldoError) as e:
        ia.descartar(token)
        return _error_access(e)


@api_view(["POST"])
@permission_classes([SoloAccesoTotalCopias])
@throttle_classes([OperacionesPesadas])
def access_cargar(request):
    """POST {"token", "clave"}: guarda lo analizado (vuelve a comparar dentro de la transacción)."""
    log.info("Carga desde Access pedida por %s desde %s", request.user.username, ip_de(request))
    try:
        return Response(ia.cargar(request.data.get("token") or "", request.data.get("clave") or "",
                                  request.user.username))
    except (ia.ImportacionError, r.RespaldoError) as e:
        return _error_access(e)


@api_view(["POST"])
@permission_classes([SoloAccesoTotalCopias])
def access_descartar(request):
    ia.descartar(request.data.get("token") or "")
    return Response(status=204)


@api_view(["POST"])
@permission_classes([SoloAccesoTotalCopias])
@parser_classes([MultiPartParser])
@throttle_classes([OperacionesPesadas])
def fotos_access(request):
    """POST multipart "archivo" (.zip de la carpeta FOTOS SICGA de Access), "simular" y "reemplazar" ("1"/"0"):
    pone cada foto en su socio o beneficiario por el código exacto (P043.jpg, P423-4.jpg)."""
    simular = request.data.get("simular") == "1"
    reemplazar = request.data.get("reemplazar") == "1"
    temporal = _guardar_subida(request.FILES.get("archivo"))
    try:
        resultado = fotos.importar(fotos.leer_zip(temporal), reemplazar=reemplazar, simular=simular)
    except fotos.FotoInvalida as e:
        return Response({"detail": str(e)}, status=400)
    finally:
        temporal.unlink(missing_ok=True)
    if not simular:
        log.info("Fotos de Access importadas por %s: %d socios, %d beneficiarios", request.user.username,
                 resultado["socios"]["importadas"], resultado["beneficiarios"]["importadas"])
    return Response(resultado)


# ------------------------------------------------------------------ computadora nueva (sin datos)

def _es_local(request) -> bool:
    """Petición hecha en la misma computadora del servidor, sin pasar por un proxy ni un túnel."""
    if settings.DETRAS_DE_PROXY:
        return False
    if any(h in request.META for h in ("HTTP_X_FORWARDED_FOR", "HTTP_FORWARDED", "HTTP_X_REAL_IP",
                                       "HTTP_CF_CONNECTING_IP")):
        return False
    return request.META.get("REMOTE_ADDR") in ("127.0.0.1", "::1")


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def instalacion(request):
    """GET /api/instalacion/ -> {"estado": lista|sin_datos|sin_base|sin_servidor, "puede_restaurar": bool}."""
    estado_actual = r.estado_base()
    return Response({"estado": estado_actual,
                     "puede_restaurar": estado_actual in ("sin_base", "sin_datos") and _es_local(request)})


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([OperacionesPesadasSinSesion])
@parser_classes([MultiPartParser])
def instalacion_restaurar(request):
    """Primera carga en una computadora nueva: solo si no hay datos y desde la misma computadora."""
    # La cabecera obliga al navegador a pedir permiso (CORS) si otra página intentara enviarla
    if not _es_local(request) or request.headers.get("X-SGA-Instalacion") != "1":
        return Response({"detail": "La primera restauración solo puede hacerse desde la computadora donde está "
                                   "instalado el sistema."}, status=403)
    if r.estado_base() not in ("sin_base", "sin_datos"):
        return Response({"detail": "Este sistema ya tiene datos. Entre con su usuario y restaure desde "
                                   "Configuración → Copias de seguridad."}, status=409)
    temporal = _guardar_subida(request.FILES.get("archivo"))
    try:
        copia, _ = r.registrar_subida(temporal)
        resultado = r.restaurar(r.ruta_copia(copia["nombre"]), "instalación", primera_vez=True)
    except r.RespaldoError as e:
        return _error(e)
    finally:
        temporal.unlink(missing_ok=True)
    return Response(resultado)
