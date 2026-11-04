"""
Servidor de SGA-GALLEGO para Windows (waitress), el que usan deploy/windows/iniciar_sga.ps1 y el inicio automático.

  python manage.py servir                 en esta computadora: http://localhost:8000
  (con SGA_RED_LOCAL=1 en backend/.env)   también para las demás computadoras de la red del club

A diferencia de «runserver» (solo para programar), waitress atiende a varias computadoras a la vez y no muestra
detalles internos. En Linux (servidor en internet) se usa gunicorn: ver deploy/instalar.sh.
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.utils import OperationalError


class Command(BaseCommand):
    help = "Inicia SGA-GALLEGO con el servidor waitress (Windows): local o, con SGA_RED_LOCAL=1, en la red del club."

    def add_arguments(self, parser):
        parser.add_argument("--puerto", type=int, default=settings.PUERTO)
        parser.add_argument("--hilos", type=int, default=8, help="Solicitudes atendidas a la vez (por defecto 8)")

    def handle(self, puerto, hilos, **_):
        try:
            from waitress import serve
        except ImportError:
            raise CommandError("Falta waitress: .venv\\Scripts\\python.exe -m pip install -r requirements.txt")
        try:
            connection.ensure_connection()
        except OperationalError as e:
            raise CommandError("No hay conexión con la base de datos (PostgreSQL). Iníciela con "
                               f"deploy\\windows\\iniciar_sga.ps1. Detalle: {str(e).strip().splitlines()[0]}")
        finally:
            connection.close()
        self._actualizar_base()

        from django.core.wsgi import get_wsgi_application

        host = "0.0.0.0" if settings.RED_LOCAL else "127.0.0.1"
        self.stdout.write(f"SGA-GALLEGO en http://localhost:{puerto}/")
        if settings.RED_LOCAL:
            import socket

            from sicga.settings import _direcciones_locales

            otras = [socket.gethostname(), *_direcciones_locales()]
            self.stdout.write("Red local activa. Desde las otras computadoras: "
                              + "  ".join(f"http://{h}:{puerto}/" for h in otras))
        else:
            self.stdout.write("Solo esta computadora (SGA_RED_LOCAL=0).")
        self.stdout.flush()                  # al registro (servidor.log) enseguida, no al cerrar
        # Detrás de un túnel o proxy de HTTPS en ESTA computadora (enlace para mostrar el sistema por internet): solo
        # a él se le creen las cabeceras X-Forwarded-* (que la conexión original era HTTPS y la IP del visitante).
        # Sin esto, waitress las borra, Django cree que llegó por HTTP y redirige a HTTPS sin fin.
        proxy = ({"trusted_proxy": "127.0.0.1", "trusted_proxy_count": 1,
                  "trusted_proxy_headers": {"x-forwarded-proto", "x-forwarded-for"}}
                 if settings.DETRAS_DE_PROXY else {})
        if proxy:
            self.stdout.write("Detrás de un túnel/proxy de HTTPS en esta computadora (127.0.0.1).")
        # Las copias automáticas y la tasa del BCV ya arrancaron en core.apps (este proceso es el servidor)
        serve(get_wsgi_application(), host=host, port=puerto, threads=hilos, ident="SGA-GALLEGO",
              # restauraciones y cargas de Access pueden tardar varios minutos
              channel_timeout=900,
              # copias de seguridad subidas (hasta SGA_RESPALDOS_MAX_MB)
              max_request_body_size=(settings.RESPALDOS_MAX_SUBIDA_MB + 16) * 1024 * 1024,
              clear_untrusted_proxy_headers=True, **proxy)

    def _actualizar_base(self):
        """Tras actualizar el programa, crea en la base lo nuevo (por ejemplo, las tablas de un módulo nuevo)."""
        from django.core.management import call_command
        from django.db.migrations.executor import MigrationExecutor

        ejecutor = MigrationExecutor(connection)
        pendientes = ejecutor.migration_plan(ejecutor.loader.graph.leaf_nodes())
        connection.close()
        if pendientes:
            self.stdout.write(f"Actualizando la base de datos ({len(pendientes)} cambio(s))...")
            call_command("migrate", interactive=False, verbosity=0)
