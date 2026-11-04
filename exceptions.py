import logging

from django.db import IntegrityError
from django.db.models import ProtectedError, RestrictedError
from rest_framework import status
from rest_framework.exceptions import Throttled
from rest_framework.response import Response
from rest_framework.views import exception_handler

log = logging.getLogger("sga.errores")


def manejador_excepciones(exc, context):
    """Bloqueos de integridad referencial -> 409 (en lugar de 500), sin enviar al navegador detalles de la base."""
    if isinstance(exc, (ProtectedError, RestrictedError)):
        relacionados = sorted({o._meta.verbose_name_plural for o in exc.args[1]})
        return Response(
            {"detail": "No se puede eliminar: tiene registros asociados.", "relacionados": relacionados},
            status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, IntegrityError):
        # El texto de PostgreSQL (tablas, restricciones, valores) queda solo en el registro del servidor
        vista = context.get("view")
        log.warning("Restricción de la base en %s: %s", type(vista).__name__ if vista else "?",
                    (str(exc).splitlines() or [""])[0][:300])
        return Response({"detail": "La operación viola una restricción de la base de datos (dato repetido o "
                                   "relacionado con otro registro)."}, status=status.HTTP_409_CONFLICT)
    if isinstance(exc, Throttled):
        espera = f" Intente de nuevo en {int(exc.wait)} segundos." if exc.wait else ""
        respuesta = exception_handler(exc, context)
        respuesta.data = {"detail": "Demasiadas solicitudes seguidas." + espera}
        return respuesta
    return exception_handler(exc, context)
