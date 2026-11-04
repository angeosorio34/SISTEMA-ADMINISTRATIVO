"""
Copias de seguridad desde la consola (lo mismo que Configuración -> Copias de seguridad).

  python manage.py respaldo crear                  copia manual ahora
  python manage.py respaldo listar                 copias guardadas
  python manage.py respaldo restaurar <copia.zip>  restaura (también en una computadora nueva sin datos)
  python manage.py respaldo automaticas            hace las copias automáticas pendientes (para cron)
  python manage.py respaldo esquema [--escribir]   compara la base con core/esquema_referencia.txt
                                                   (--escribir la regenera tras un cambio de estructura)
"""
import shutil
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core import respaldos as r


class Command(BaseCommand):
    help = "Copias de seguridad de SGA-GALLEGO: crear, listar, restaurar, automaticas, esquema."

    def add_arguments(self, parser):
        parser.add_argument("accion", choices=["crear", "listar", "restaurar", "automaticas", "esquema"])
        parser.add_argument("archivo", nargs="?", help="Copia .zip a restaurar")
        parser.add_argument("--si", action="store_true", help="Restaurar sin pedir confirmación")
        parser.add_argument("--escribir", action="store_true", help="Regenerar esquema_referencia.txt")

    def handle(self, *args, accion, archivo=None, si=False, escribir=False, **opciones):
        try:
            getattr(self, f"_{accion}")(archivo, si, escribir)
        except r.RespaldoError as e:
            raise CommandError(str(e))

    def _crear(self, *_):
        copia = r.crear("manual", "consola")
        self.stdout.write(self.style.SUCCESS(f"Copia creada: {r.carpeta() / copia['nombre']}"))

    def _listar(self, *_):
        self.stdout.write(f"Carpeta: {r.carpeta()}")
        for c in r.listar():
            res = c["resumen"]
            self.stdout.write(f"  {c['nombre']}  {c['bytes'] / 1e6:6.1f} MB  "
                              + (f"{res.get('socios', 0)} socios, {res.get('recibos', 0)} recibos" if c["valida"]
                                 else f"NO VALIDA: {c['error']}"))

    def _automaticas(self, *_):
        hechas = r.ejecutar_pendientes()
        self.stdout.write(f"Copias automáticas hechas: {', '.join(hechas) or 'ninguna pendiente'}")

    def _restaurar(self, archivo, si, _):
        if not archivo:
            raise CommandError("Indique el archivo .zip de la copia.")
        ruta = Path(archivo).resolve()
        if not ruta.is_file():
            raise CommandError(f"No existe {ruta}")
        estado = r.estado_base()
        if estado == "lista" and not si:
            respuesta = input("Se reemplazarán TODOS los datos actuales por los de la copia (antes se guarda una "
                              "copia previa). Escriba RESTAURAR para continuar: ")
            if respuesta.strip() != "RESTAURAR":
                raise CommandError("Cancelado.")
        # La copia queda también en la carpeta de copias (como "subida") antes de restaurarla
        r.carpeta().mkdir(parents=True, exist_ok=True)
        temporal = r.carpeta() / f".subida-consola-{ruta.stem[:40]}.zip"
        shutil.copyfile(ruta, temporal)
        try:
            copia, _ = r.registrar_subida(temporal)
        finally:
            temporal.unlink(missing_ok=True)
        resultado = r.restaurar(r.ruta_copia(copia["nombre"]), "consola", primera_vez=estado != "lista")
        res = resultado["resumen"]
        self.stdout.write(self.style.SUCCESS(
            f"Restaurada {copia['nombre']}: {res['socios']} socios, {res['recibos']} recibos, {res['usuarios']} "
            f"usuarios, {resultado['fotos']} fotos." + (f" Copia previa: {resultado['previa']}" if resultado["previa"] else "")))

    def _esquema(self, _archivo, _si, escribir):
        with r.conectar() as c:
            firma = r.firma_esquema(c)
        if escribir:
            # La versión que se reemplaza pasa a «anterior»: sus copias se podrán restaurar (y se actualizarán solas)
            if r.REFERENCIA.is_file():
                vieja = r.huella(r.leer_referencia())
                if vieja != r.huella(firma) and vieja not in r.huellas_anteriores():
                    from datetime import date
                    with open(r.ANTERIORES, "a", encoding="utf-8") as f:
                        f.write(f"{vieja}  # hasta el {date.today():%d/%m/%Y}\n")
                    self.stdout.write(f"Versión anterior guardada en {r.ANTERIORES.name}: {vieja[:12]}")
            r.REFERENCIA.write_text("\n".join(firma) + "\n", encoding="utf-8")
            self.stdout.write(self.style.SUCCESS(f"{r.REFERENCIA.name}: {len(firma)} líneas (huella {r.huella(firma)[:12]})"))
            return
        referencia = r.leer_referencia()
        if firma == referencia:
            self.stdout.write(self.style.SUCCESS(f"La base coincide con {r.REFERENCIA.name} ({len(firma)} objetos)."))
            return
        for linea in sorted(set(referencia) - set(firma))[:20]:
            self.stdout.write(f"  falta en la base: {linea}")
        for linea in sorted(set(firma) - set(referencia))[:20]:
            self.stdout.write(f"  sobra en la base: {linea}")
        raise CommandError("La estructura de la base no coincide con esquema_referencia.txt.")
