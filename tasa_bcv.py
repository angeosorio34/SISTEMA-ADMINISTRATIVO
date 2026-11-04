"""
Tasa de cambio oficial del dólar publicada por el Banco Central de Venezuela (menú lateral y Configuración).

El servidor la consulta solo en la portada de https://www.bcv.org.ve/ (bloque «USD» y su «Fecha Valor»), siempre por
HTTPS con el certificado verificado. Se guarda en DATOS_DIR/tasa_bcv.json con su historial; no es parte de la base
de datos porque es un dato público que se puede volver a consultar.

El BCV publica por la tarde la tasa que rige desde el siguiente día hábil («Fecha Valor»): la tasa vigente es la
última con fecha valor de hoy o anterior; la que rige mañana se muestra aparte como «próxima».
Si el BCV no responde, un usuario con acceso total puede escribirla a mano (queda marcada «manual»).
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.utils import timezone

log = logging.getLogger("sga.tasa_bcv")

URL = "https://www.bcv.org.ve/"
MAX_BYTES = 3 * 1024 * 1024
MAX_HISTORIAL = 500
CADA_HORAS = 2              # consulta periódica (de 7:00 a 22:00)
REINTENTO_MINUTOS = 30      # tras un fallo
_DOLAR = re.compile(r'id="dolar".{0,1500}?<strong[^>]*>\s*([\d.,]+)\s*</strong>', re.S)
_FECHA = re.compile(r'Fecha\s+Valor:\s*<span[^>]*content="(\d{4}-\d{2}-\d{2})', re.S)
_cerrojo = threading.Lock()   # lecturas y escrituras del archivo dentro del proceso (Windows no deja reemplazar un
                              # archivo que otro hilo está leyendo)


class TasaError(Exception):
    """Mensaje para mostrar tal cual al usuario."""


def _archivo() -> Path:
    return Path(settings.DATOS_DIR) / "tasa_bcv.json"


def _leer() -> dict:
    with _cerrojo:
        try:
            datos = json.loads(_archivo().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"tasas": []}
        except (OSError, ValueError):
            log.warning("tasa_bcv.json dañado: se empieza de nuevo")
            return {"tasas": []}
    return datos if isinstance(datos, dict) and isinstance(datos.get("tasas"), list) else {"tasas": []}


def _guardar(datos: dict) -> None:
    with _cerrojo:
        _archivo().parent.mkdir(parents=True, exist_ok=True)
        temporal = _archivo().with_name(f".tasa_bcv.{secrets.token_hex(4)}.tmp")
        temporal.write_text(json.dumps(datos, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temporal, _archivo())


def a_decimal(texto: str) -> Decimal:
    """'881,06640000' (formato del BCV) o '881.0664' -> Decimal."""
    texto = (texto or "").strip().replace(" ", "")
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    try:
        valor = Decimal(texto)
    except InvalidOperation:
        raise TasaError("La tasa no es un número válido.")
    if not valor.is_finite() or valor <= 0 or valor >= Decimal("1e12"):
        raise TasaError("La tasa debe ser un número mayor que cero.")
    return valor


def leer_portada(html: str) -> tuple[Decimal, date]:
    """Extrae (tasa del dólar, fecha valor) de la portada del BCV."""
    dolar, fecha = _DOLAR.search(html), _FECHA.search(html)
    if not dolar or not fecha:
        raise TasaError("La página del BCV cambió de formato: no se encontró la tasa del dólar.")
    return a_decimal(dolar.group(1)), date.fromisoformat(fecha.group(1))


def descargar() -> tuple[Decimal, date]:
    """Consulta el BCV (HTTPS con el certificado verificado; nunca se desactiva la verificación)."""
    pedido = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0 (SGA-GALLEGO; tasa oficial)",
                                                  "Accept": "text/html"})
    try:
        with urllib.request.urlopen(pedido, timeout=30, context=ssl.create_default_context()) as respuesta:
            html = respuesta.read(MAX_BYTES + 1)
    except ssl.SSLError as e:
        raise TasaError(f"No se pudo verificar la conexión segura con el BCV ({e.reason or 'certificado'}).")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        motivo = getattr(e, "reason", None) or e
        raise TasaError(f"No hay conexión con la página del BCV ({str(motivo)[:120]}).")
    if len(html) > MAX_BYTES:
        raise TasaError("La respuesta del BCV es demasiado grande.")
    tasa, fecha_valor = leer_portada(html.decode("utf-8", "replace"))
    hoy = timezone.localdate()
    if not hoy - timedelta(days=30) <= fecha_valor <= hoy + timedelta(days=10):
        raise TasaError(f"La fecha valor publicada por el BCV ({fecha_valor:%d/%m/%Y}) no es razonable.")
    return tasa, fecha_valor


def registrar(tasa: Decimal, fecha_valor: date, fuente: str, usuario: str = "") -> dict:
    """Guarda la tasa de esa fecha valor. La del BCV reemplaza a una escrita a mano; una manual no pisa la del BCV."""
    datos = _leer()
    ahora = timezone.now().isoformat(timespec="seconds")
    previa = next((t for t in datos["tasas"] if t.get("fecha_valor") == fecha_valor.isoformat()), None)
    if previa and previa.get("fuente") == "bcv" and fuente == "manual":
        raise TasaError(f"Para el {fecha_valor:%d/%m/%Y} ya está la tasa oficial publicada por el BCV.")
    nueva = {"fecha_valor": fecha_valor.isoformat(), "tasa": str(tasa), "fuente": fuente, "registrada": ahora,
             "usuario": usuario[:150]}
    datos["tasas"] = sorted([t for t in datos["tasas"] if t is not previa] + [nueva], key=lambda t: t["fecha_valor"])
    datos["tasas"] = datos["tasas"][-MAX_HISTORIAL:]
    if fuente == "bcv":
        datos.update(ultimo_exito=ahora, ultimo_intento=ahora, error=None)
    _guardar(datos)
    if not previa or previa.get("tasa") != nueva["tasa"]:
        log.info("Tasa BCV %s para el %s (%s%s)", tasa, fecha_valor, fuente, f", {usuario}" if usuario else "")
    return estado()


def actualizar() -> dict:
    """Consulta el BCV ahora. Si falla, guarda el motivo (se muestra en el menú) y lanza TasaError."""
    try:
        tasa, fecha_valor = descargar()
    except TasaError as e:
        datos = _leer()
        datos.update(ultimo_intento=timezone.now().isoformat(timespec="seconds"), error=str(e))
        _guardar(datos)
        log.warning("Tasa BCV: %s", e)
        raise
    return registrar(tasa, fecha_valor, "bcv")


def _vigente_y_proxima(tasas: list, hoy: date) -> tuple[dict | None, dict | None]:
    pasadas = [t for t in tasas if t["fecha_valor"] <= hoy.isoformat()]
    futuras = [t for t in tasas if t["fecha_valor"] > hoy.isoformat()]
    if pasadas:
        return pasadas[-1], (futuras[0] if futuras else None)
    # Solo se conoce la que rige desde un próximo día hábil (p. ej. un domingo, con la del lunes): se muestra esa
    return (futuras[0] if futuras else None), (futuras[1] if len(futuras) > 1 else None)


def _fecha(texto):
    try:
        return datetime.fromisoformat(texto) if texto else None
    except ValueError:
        return None


def estado(historial: int = 0) -> dict:
    datos = _leer()
    hoy = timezone.localdate()
    vigente, proxima = _vigente_y_proxima(datos["tasas"], hoy)
    exito = _fecha(datos.get("ultimo_exito"))
    # Aviso si no hay tasa o si el último intento falló y la última consulta buena tiene más de un día
    vieja = exito is None or timezone.now() - exito > timedelta(hours=26)
    resultado = {
        "tasa": vigente["tasa"] if vigente else None,
        "fecha_valor": vigente["fecha_valor"] if vigente else None,
        "fuente": vigente["fuente"] if vigente else None,
        "proxima": {k: proxima[k] for k in ("tasa", "fecha_valor", "fuente")} if proxima else None,
        "consultada": datos.get("ultimo_exito"),
        "ultimo_intento": datos.get("ultimo_intento"),
        "error": datos.get("error"),
        "aviso": vigente is None or (bool(datos.get("error")) and vieja),
        "automatica": settings.TASA_BCV_AUTOMATICA,
    }
    if historial:
        resultado["historial"] = list(reversed(datos["tasas"]))[:historial]
    return resultado


# -----------------------------------------------------------------------------
# Consulta automática (un hilo del servidor, como las copias de seguridad)
# -----------------------------------------------------------------------------

_hilo: threading.Thread | None = None


def toca_consultar(ahora: datetime, datos: dict) -> bool:
    local = timezone.localtime(ahora)
    intento, exito = _fecha(datos.get("ultimo_intento")), _fecha(datos.get("ultimo_exito"))
    if not datos.get("tasas") or exito is None:
        return intento is None or ahora - intento >= timedelta(minutes=REINTENTO_MINUTOS)
    if not 7 <= local.hour < 22:
        return False
    if datos.get("error") and intento and ahora - intento < timedelta(minutes=REINTENTO_MINUTOS):
        return False
    return ahora - exito >= timedelta(hours=CADA_HORAS)


def _bucle() -> None:
    time.sleep(10)
    while True:
        try:
            if toca_consultar(timezone.now(), _leer()):
                actualizar()
        except TasaError:
            pass                      # ya quedó anotado en el archivo y en el registro
        except Exception:
            log.exception("Consulta automática de la tasa BCV")
        time.sleep(300)


def iniciar_consulta_automatica() -> None:
    from .respaldos import _es_proceso_servidor

    global _hilo
    if _hilo is not None or not settings.TASA_BCV_AUTOMATICA or not _es_proceso_servidor():
        return
    _hilo = threading.Thread(target=_bucle, name="sga-tasa-bcv", daemon=True)
    _hilo.start()
    log.info("Tasa BCV: consulta automática activa (%s)", URL)
