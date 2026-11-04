"""
Crea los usuarios de Django a partir de usuario_sistema (migrado de Access).

    python manage.py importar_usuarios                       # sin clave: el admin la asigna luego
    python manage.py importar_usuarios --clave-temporal X    # misma clave temporal para todos

Nivel 0 (acceso total) -> grupo "acceso_total"; nivel 1 (consulta) -> grupo "consulta".
Las claves de Access NO se migran (estaban en texto plano).
"""
from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import UsuarioSistema
from core.permissions import GRUPO_ACCESO_TOTAL, GRUPO_CONSULTA


class Command(BaseCommand):
    help = "Crea/actualiza usuarios de Django desde la tabla usuario_sistema"

    def add_arguments(self, parser):
        parser.add_argument("--clave-temporal", help="clave inicial para los usuarios nuevos")

    @transaction.atomic
    def handle(self, *args, clave_temporal=None, **opts):
        total, _ = Group.objects.get_or_create(name=GRUPO_ACCESO_TOTAL)
        consulta, _ = Group.objects.get_or_create(name=GRUPO_CONSULTA)

        for u in UsuarioSistema.objects.all():
            username = u.username.lower()
            nombre, _, apellido = (u.nombre or "").partition(" ")
            user, creado = User.objects.get_or_create(
                username=username, defaults={"first_name": nombre[:150], "last_name": apellido[:150]})
            user.is_active = u.activo
            if creado:
                if clave_temporal:
                    user.set_password(clave_temporal)
                else:
                    user.set_unusable_password()
            user.save()
            user.groups.remove(total, consulta)
            user.groups.add(total if u.nivel_acceso_id == 0 else consulta)
            estado = "creado" if creado else "actualizado"
            self.stdout.write(f"  {username:<12} {estado:<11} nivel {u.nivel_acceso_id}")
        self.stdout.write(self.style.SUCCESS("Usuarios sincronizados."))
