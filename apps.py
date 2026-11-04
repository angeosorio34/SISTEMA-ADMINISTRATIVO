from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"
    verbose_name = "SICGA"

    def ready(self):
        # Copias de seguridad automáticas (solo en el proceso que atiende: runserver o gunicorn)
        from . import respaldos, tasa_bcv

        respaldos.iniciar_programador()
        tasa_bcv.iniciar_consulta_automatica()      # tasa del dólar del BCV (menú lateral)
