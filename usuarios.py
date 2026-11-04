"""
Modulo de usuarios (ConsUsr / IngUsr / ModUsr / AccIniUsr de Access).

Nivel 0 = acceso total (grupo "acceso_total"); nivel 1 = consulta (grupo "consulta").
Las claves se guardan con hash (en Access estaban en texto plano en USUARIOS.Clave).
"""
import logging

from django.contrib.auth.models import Group, User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import BasePermission
from rest_framework.response import Response

from rest_framework.authtoken.models import Token

from .permissions import GRUPO_ACCESO_TOTAL, GRUPO_CONSULTA, tiene_acceso_total
from .seguridad import desbloquear, ip_de

log = logging.getLogger("sga.seguridad")


def _cerrar_sesiones(usuario):
    """Anula las sesiones abiertas del usuario (cambio de clave, desactivación, eliminación)."""
    Token.objects.filter(user=usuario).delete()


class SoloAccesoTotal(BasePermission):
    message = "Solo un usuario con acceso total puede administrar usuarios."

    def has_permission(self, request, view):
        return tiene_acceso_total(request.user)


def _validar_clave(clave: str, usuario=None):
    try:
        validate_password(clave, usuario)
    except DjangoValidationError as e:
        raise ValidationError({"clave": list(e.messages)})


class UsuarioSerializer(serializers.ModelSerializer):
    nombre = serializers.CharField(source="first_name", required=False, allow_blank=True, max_length=150)
    nivel = serializers.ChoiceField(choices=[(0, "Acceso total"), (1, "Consulta")], write_only=True, required=False)
    nivel_acceso = serializers.SerializerMethodField()
    activo = serializers.BooleanField(source="is_active", required=False)
    ultimo_acceso = serializers.DateTimeField(source="last_login", read_only=True)
    clave = serializers.CharField(write_only=True, required=False, min_length=10)

    class Meta:
        model = User
        fields = ["id", "username", "nombre", "email", "nivel", "nivel_acceso", "activo", "ultimo_acceso", "clave",
                  "is_superuser"]
        read_only_fields = ["is_superuser"]

    def get_nivel_acceso(self, u):
        return 0 if u.is_superuser or u.groups.filter(name=GRUPO_ACCESO_TOTAL).exists() else 1

    def validate_username(self, valor):
        valor = valor.strip().lower()
        consulta = User.objects.filter(username=valor)
        if self.instance:
            consulta = consulta.exclude(pk=self.instance.pk)
        if consulta.exists():
            raise ValidationError("Ya existe un usuario con ese nombre.")
        return valor

    def validate_email(self, valor):
        """Opcional; con el se puede iniciar sesion, asi que no puede repetirse."""
        valor = (valor or "").strip().lower()
        if valor:
            consulta = User.objects.filter(email__iexact=valor)
            if self.instance:
                consulta = consulta.exclude(pk=self.instance.pk)
            if consulta.exists():
                raise ValidationError("Otro usuario ya tiene ese email.")
        return valor

    def _asignar_nivel(self, usuario, nivel):
        if nivel is None:
            return
        total, _ = Group.objects.get_or_create(name=GRUPO_ACCESO_TOTAL)
        consulta, _ = Group.objects.get_or_create(name=GRUPO_CONSULTA)
        usuario.groups.remove(total, consulta)
        usuario.groups.add(total if int(nivel) == 0 else consulta)

    def create(self, datos):
        clave = datos.pop("clave", None)
        nivel = datos.pop("nivel", 1)
        if not clave:
            raise ValidationError({"clave": "Indique una clave inicial."})
        usuario = User(**datos)
        _validar_clave(clave, usuario)
        usuario.set_password(clave)
        usuario.save()
        self._asignar_nivel(usuario, nivel)
        return usuario

    def update(self, usuario, datos):
        datos.pop("clave", None)        # la clave se cambia con /reiniciar-clave/
        nivel = datos.pop("nivel", None)
        for k, v in datos.items():
            setattr(usuario, k, v)
        usuario.save()
        self._asignar_nivel(usuario, nivel)
        return usuario


class UsuarioViewSet(viewsets.ModelViewSet):
    queryset = User.objects.order_by("username").prefetch_related("groups")
    serializer_class = UsuarioSerializer
    permission_classes = [SoloAccesoTotal]
    pagination_class = None

    def _proteger(self, usuario):
        if usuario == self.request.user:
            raise PermissionDenied("No puede quitarse a sí mismo el acceso ni eliminarse.")
        self._solo_superusuario(usuario)

    def _solo_superusuario(self, usuario):
        if usuario.is_superuser and not self.request.user.is_superuser:
            raise PermissionDenied("Solo un superusuario puede modificar a otro superusuario.")

    def _registrar(self, accion, usuario):
        log.info("Usuarios: %s %s (por %s desde %s)", accion, usuario.username, self.request.user.username,
                 ip_de(self.request))

    def perform_create(self, serializer):
        self._registrar("alta de", serializer.save())

    def perform_update(self, serializer):
        cambios = serializer.validated_data
        self._solo_superusuario(serializer.instance)        # nombre, email... de un superusuario: solo otro superusuario
        if "is_active" in cambios or "nivel" in cambios:
            self._proteger(serializer.instance)
        usuario = serializer.save()
        if not usuario.is_active:
            _cerrar_sesiones(usuario)
        self._registrar("modificación de " + ", ".join(sorted(cambios)) + " de", usuario)

    def perform_destroy(self, usuario):
        self._proteger(usuario)
        self._registrar("eliminación de", usuario)
        usuario.delete()

    @action(detail=True, methods=["post"], url_path="reiniciar-clave")
    def reiniciar_clave(self, request, pk=None):
        """ModUsr "Reiniciar Clave": asigna una clave nueva (el usuario puede cambiarla luego)."""
        usuario = self.get_object()
        if usuario.is_superuser and not request.user.is_superuser:
            raise PermissionDenied("Solo un superusuario puede cambiar la clave de otro superusuario.")
        clave = request.data.get("clave") or ""
        if len(clave) < 10:
            raise ValidationError({"clave": "Mínimo 10 caracteres."})
        _validar_clave(clave, usuario)
        usuario.set_password(clave)
        usuario.save(update_fields=["password"])
        _cerrar_sesiones(usuario)               # quien tuviera la clave anterior queda fuera
        desbloquear(usuario.username)
        if usuario.email:
            desbloquear(usuario.email)
        self._registrar("reinicio de clave de", usuario)
        return Response({"detail": "Clave actualizada."})
