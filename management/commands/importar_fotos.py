"""Importa las fotos del sistema anterior (carpeta "FOTOS SICGA" de Access) a la carpeta de fotos (MEDIA_ROOT).

Uso:
    python manage.py importar_fotos "D:\\FOTOS SICGA" --simular     (solo informa)
    python manage.py importar_fotos "D:\\FOTOS SICGA"
    python manage.py importar_fotos "D:\\FOTOS SICGA.zip"           (también un .zip de la carpeta)

Es lo mismo que Configuración -> Copias de seguridad -> «Fotos del sistema anterior» (core/fotos.py):
cada foto se busca por el código exacto del socio o beneficiario (P043.jpg, P423-4.jpg), se revisa que sea una
imagen real y se guarda sin metadatos. No toca a quien ya tenga una foto cargada en el sistema nuevo (salvo
--reemplazar).
"""
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core import fotos


class Command(BaseCommand):
    help = "Importa las fotos del sistema anterior (carpeta FOTOS SICGA o un .zip de ella) a la carpeta de fotos."

    def add_arguments(self, parser):
        parser.add_argument("carpeta", help="Copia de la carpeta FOTOS SICGA de la computadora de Access, o un .zip")
        parser.add_argument("--simular", action="store_true", help="Solo informa; no copia ni modifica nada.")
        parser.add_argument("--reemplazar", action="store_true",
                            help="Importa también para quien ya tiene una foto cargada en el sistema nuevo.")

    def handle(self, carpeta, simular=False, reemplazar=False, **_):
        origen = Path(carpeta)
        try:
            if origen.is_file() and origen.suffix.lower() == ".zip":
                archivos = fotos.leer_zip(origen)
            elif origen.is_dir():
                archivos = fotos.leer_carpeta(origen)
            else:
                raise CommandError(f"No existe la carpeta o el .zip {origen}")
        except fotos.FotoInvalida as e:
            raise CommandError(str(e))
        r = fotos.importar(archivos, reemplazar=reemplazar, simular=simular)
        verbo = "se importarían" if simular else "importadas"
        self.stdout.write(f"{r['imagenes']} imágenes en {origen}")
        for carpeta_destino in ("socios", "beneficiarios"):
            d = r[carpeta_destino]
            self.stdout.write(f"{carpeta_destino}: {d['importadas']} fotos {verbo}; {d['ya_tenian']} ya tenían foto en el "
                              f"sistema nuevo; {len(d['invalidas'])} no son imágenes válidas")
            for linea in d["invalidas"][:10]:
                self.stdout.write(f"   {linea}")
        if r["sin_dueno"]:
            self.stdout.write(f"{r['sin_dueno']} imágenes no corresponden a ningún socio ni beneficiario "
                              f"(ej.: {', '.join(r['ejemplos_sin_dueno'][:8])})")
        if simular:
            self.stdout.write("Simulación: no se copió ni modificó nada.")
