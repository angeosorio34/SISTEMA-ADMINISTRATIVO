from rest_framework.permissions import SAFE_METHODS, BasePermission

# Grupos que crea el comando `importar_usuarios` a partir de nivel_acceso de Access
GRUPO_ACCESO_TOTAL = "acceso_total"   # nivel 0
GRUPO_CONSULTA = "consulta"           # nivel 1


def tiene_acceso_total(user) -> bool:
    return bool(user and user.is_authenticated and (
        user.is_superuser or user.groups.filter(name=GRUPO_ACCESO_TOTAL).exists()
    ))


class AccesoTotalOSoloLectura(BasePermission):
    """Lectura para cualquier usuario autenticado; escritura solo para nivel 0 (acceso total)."""

    message = "Su nivel de acceso es de consulta: no puede modificar datos."

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        return request.method in SAFE_METHODS or tiene_acceso_total(request.user)
