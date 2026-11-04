"""
Copias de seguridad de SGA-GALLEGO (Configuración -> Copias de seguridad).

Una copia es un .zip con todo lo necesario para dejar el sistema como estaba:
  manifiesto.json  fecha, equipo, filas de cada tabla y huella sha256 de la base (un archivo
                   dañado o alterado no se acepta)
  base.dump        la base completa (pg_dump en formato custom: esquemas public y legacy)
  media/...        fotos de socios y beneficiarios (MEDIA_ROOT)

Restaurar no toca la base en uso hasta haber comprobado la copia:
  1. se revisa el .zip: manifiesto, huella y lista de objetos (solo tablas, secuencias, índices,
     restricciones, dominios y comentarios; nada de funciones ni disparadores);
  2. se carga en una base aparte (<base>_restauracion) con un usuario de PostgreSQL sin
     privilegios: un archivo manipulado no puede ejecutar nada fuera de esa base;
  3. se compara su estructura con la del sistema y las filas de cada tabla con el manifiesto;
  4. se guarda una copia "previa" de los datos actuales (para poder deshacer);
  5. se intercambian los nombres: <base> -> <base>_anterior y <base>_restauracion -> <base>,
     y lo mismo con la carpeta de fotos.

Las copias automáticas (diaria, semanal, anual) las hace un hilo del propio servidor: si la
computadora estaba apagada a la hora programada, la copia se hace en cuanto se abre el sistema.
"""
from __future__ import annotations

import calendar
import contextlib
import hashlib
import json
import logging
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from datetime import date, datetime, timedelta
from datetime import time as hora_del_dia
from pathlib import Path, PurePosixPath

import psycopg
from django.conf import settings
from django.db import connections
from django.utils import timezone
from psycopg import sql

log = logging.getLogger("sga.respaldos")

APLICACION = "SGA-GALLEGO"
FORMATO = 1
TIPOS_AUTOMATICOS = ("diaria", "semanal", "anual")
TIPOS = TIPOS_AUTOMATICOS + ("manual", "previa", "subida", "importacion")
_NOMBRE = re.compile(r"^SGA-GALLEGO_(\d{4}-\d{2}-\d{2})_(\d{6})_(diaria|semanal|anual|manual|previa|subida|importacion)(?:_\d{1,3})?\.zip$")
_CANDADO = 0x5347_4147_4C4C            # una sola copia o restauración a la vez, en todos los procesos
ROL_RESTAURACION = "sga_restauracion"   # usuario sin privilegios con el que se carga una copia
REFERENCIA = Path(__file__).with_name("esquema_referencia.txt")
# Huellas de la estructura de versiones anteriores del sistema: sus copias se restauran y se actualizan solas
# (migraciones de Django). Al cambiar la estructura, la huella vieja se agrega aquí (manage.py respaldo esquema --escribir).
ANTERIORES = Path(__file__).with_name("esquemas_anteriores.txt")
MAX_ENTRADAS_ZIP = 200_000
MAX_DESCOMPRIMIDO = 20 * 1024**3
_YA_COMPRIMIDOS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".zip", ".pdf"}
_RESERVADOS = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_PARTE = re.compile(r"^\w[\w .()\-]{0,150}$")
# Objetos que trae el pg_dump de este sistema (pg_restore --list); cualquier otro se rechaza
_TOC_PERMITIDOS = ("TABLE DATA", "SEQUENCE SET", "SEQUENCE OWNED BY", "FK CONSTRAINT", "CHECK CONSTRAINT",
                   "CONSTRAINT", "SEQUENCE", "DEFAULT", "DOMAIN", "INDEX", "TABLE", "SCHEMA", "COMMENT", "ACL")
_TOC_RECHAZADOS = ("DEFAULT ACL", "TABLE ATTACH", "INDEX ATTACH")
_TOC_LINEA = re.compile(r"^\d+;\s+\d+\s+\d+\s+(.+)$")
# Lo que nunca tiene una base de SGA-GALLEGO: código que se ejecutaría al usar los datos
_PROHIBIDOS = ("FUNCTION ", "TRIGGER ", "RULE ", "POLICY ", "EXTENSION ", "EVENT TRIGGER ", "OPERATOR ",
               "CAST ", "LARGE OBJECTS ", "STATISTICS ", "COLLATION ", "CONVERSION ", "PUBLICATION ",
               "FOREIGN SERVER ", "FDW ", "TEXT SEARCH ", "OPCLASS ", "OPFAMILY ", "ACCESS METHOD ",
               "LANGUAGE ", "TRANSFORM ", "DEFAULT ACL ")

ARCHIVO_PROGRAMACION = "programacion.json"
ARCHIVO_EJECUCION = "ejecucion.json"
PROGRAMACION_INICIAL = {
    "diaria": {"activa": True, "hora": "18:00", "conservar": 7},
    "semanal": {"activa": True, "hora": "18:00", "dia_semana": 4, "conservar": 5},   # 0 = lunes ... 4 = viernes
    "anual": {"activa": True, "hora": "18:00", "dia": 31, "mes": 12, "conservar": 10},
}


class RespaldoError(Exception):
    """Error con un mensaje para mostrar tal cual al usuario."""


class Ocupado(RespaldoError):
    pass


class NoEncontrada(RespaldoError):
    pass


# -----------------------------------------------------------------------------
# Carpeta, nombres y rutas
# -----------------------------------------------------------------------------

def carpeta() -> Path:
    return Path(settings.RESPALDOS_DIR)


def _rutas() -> list[Path]:
    """Copias de la carpeta, la más nueva primero (la fecha y hora van en el nombre)."""
    if not carpeta().is_dir():
        return []
    rutas = [r for r in carpeta().iterdir() if _NOMBRE.match(r.name) and r.is_file()]
    return sorted(rutas, key=lambda r: (*_NOMBRE.match(r.name).group(1, 2), r.name), reverse=True)


def ruta_copia(nombre: str) -> Path:
    """Copia de la carpeta a partir de su nombre: solo nombres generados por el sistema (sin rutas)."""
    if not _NOMBRE.match(nombre or ""):
        raise NoEncontrada("No existe esa copia de seguridad.")
    ruta = carpeta() / nombre
    if not ruta.is_file():
        raise NoEncontrada("La copia ya no existe en la carpeta de copias.")
    return ruta


def _nombre_libre(momento: datetime, tipo: str) -> str:
    base = f"SGA-GALLEGO_{timezone.localtime(momento):%Y-%m-%d_%H%M%S}_{tipo}"
    nombre, n = f"{base}.zip", 1
    while (carpeta() / nombre).exists():
        n += 1
        nombre = f"{base}_{n}.zip"
    return nombre


def _ruta_segura(relativa: str) -> bool:
    """Ruta relativa de una foto ("fotos/socios/P041.jpg"): sin unidades, raíces, ".." ni nombres reservados."""
    if not relativa or "\\" in relativa or ":" in relativa or relativa.startswith("/"):
        return False
    partes = PurePosixPath(relativa).parts
    return 0 < len(partes) <= 8 and all(
        _PARTE.match(p) and not p.endswith((".", " ")) and p.split(".")[0].upper() not in _RESERVADOS for p in partes)


def _fecha(texto) -> datetime | None:
    if not texto:
        return None
    try:
        valor = datetime.fromisoformat(str(texto))
    except ValueError:
        return None
    return valor if timezone.is_aware(valor) else timezone.make_aware(valor)


def _primera_linea(e: Exception) -> str:
    return (str(e).strip().splitlines() or [type(e).__name__])[0]


# -----------------------------------------------------------------------------
# Conexiones y herramientas de PostgreSQL
# -----------------------------------------------------------------------------

def _bd() -> dict:
    return settings.DATABASES["default"]


def _parametros(base: str | None = None, usuario: str | None = None, clave: str | None = None) -> dict:
    bd = _bd()
    p = {"host": bd.get("HOST") or "localhost", "port": int(bd.get("PORT") or 5432), "dbname": base or bd["NAME"],
         "user": usuario or bd["USER"], "application_name": "sga-respaldos", "connect_timeout": 15}
    clave = clave if usuario else bd.get("PASSWORD")
    if clave:
        p["password"] = clave
    opciones = bd.get("OPTIONS") or {}
    p.update({k: opciones[k] for k in ("sslmode", "sslrootcert", "channel_binding") if k in opciones})
    return p


def conectar(base: str | None = None, usuario: str | None = None, clave: str | None = None,
             autocommit: bool = True) -> psycopg.Connection:
    return psycopg.connect(**_parametros(base, usuario, clave), autocommit=autocommit)


def _mantenimiento() -> psycopg.Connection:
    """Conexión a la base de mantenimiento ("postgres"): desde ahí se crean y renombran bases."""
    return conectar(base=settings.RESPALDOS_BD_MANTENIMIENTO)


def _entorno(base: str | None = None, usuario: str | None = None, clave: str | None = None) -> dict:
    """Variables para pg_dump / pg_restore: la clave nunca va en la línea de comandos."""
    p = _parametros(base, usuario, clave)
    entorno = {k: v for k, v in os.environ.items() if not k.upper().startswith("PG")}
    entorno.update(PGHOST=str(p["host"]), PGPORT=str(p["port"]), PGDATABASE=p["dbname"], PGUSER=p["user"],
                   PGAPPNAME="sga-respaldos", PGCLIENTENCODING="UTF8", PGCONNECT_TIMEOUT="15")
    if "password" in p:
        entorno["PGPASSWORD"] = p["password"]
    for clave_libpq, variable in (("sslmode", "PGSSLMODE"), ("sslrootcert", "PGSSLROOTCERT"),
                                  ("channel_binding", "PGCHANNELBINDING")):
        if clave_libpq in p:
            entorno[variable] = str(p[clave_libpq])
    return entorno


def _numero_version(texto: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", texto)[:2]) or (0,)


def binario(nombre: str) -> Path:
    """pg_dump / pg_restore: SGA_PG_BIN, luego la instalación más nueva de PostgreSQL, luego el PATH."""
    exe = nombre + (".exe" if os.name == "nt" else "")
    candidatos: list[Path] = []
    if settings.PG_BIN_DIR:
        candidatos.append(Path(settings.PG_BIN_DIR) / exe)
    if os.name == "nt":
        for raiz in dict.fromkeys([os.environ.get("ProgramFiles") or r"C:\Program Files", r"C:\Program Files"]):
            candidatos += sorted(Path(raiz, "PostgreSQL").glob(f"*/bin/{exe}"),
                                 key=lambda r: _numero_version(r.parent.parent.name), reverse=True)
    if encontrado := shutil.which(nombre):      # Ubuntu: /usr/bin/pg_dump elige la versión del servidor
        candidatos.append(Path(encontrado))
    candidatos += sorted(Path("/usr/lib/postgresql").glob(f"*/bin/{nombre}"),
                         key=lambda r: _numero_version(r.parent.parent.name), reverse=True)
    for candidato in candidatos:
        if candidato.is_file():
            return candidato
    raise RespaldoError(f"No se encontró {nombre} de PostgreSQL en este equipo. Instale PostgreSQL o indique "
                        "su carpeta bin en la variable SGA_PG_BIN.")


def disponibilidad() -> tuple[bool, str]:
    try:
        binario("pg_dump")
        binario("pg_restore")
    except RespaldoError as e:
        return False, str(e)
    return True, ""


def _texto(datos: bytes | None) -> str:
    if not datos:
        return ""
    try:
        return datos.decode("utf-8")
    except UnicodeDecodeError:
        return datos.decode("cp1252" if os.name == "nt" else "latin-1", "replace")


def _ejecutar(argumentos: list, entorno: dict, tiempo: int = 1800) -> str:
    """Ejecuta pg_dump / pg_restore y devuelve su salida; si falla, un RespaldoError legible."""
    nombre = Path(str(argumentos[0])).stem
    try:
        r = subprocess.run([str(a) for a in argumentos], env=entorno, capture_output=True, timeout=tiempo,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise RespaldoError(f"{nombre} tardó más de {tiempo // 60} minutos y se detuvo.")
    except OSError as e:
        raise RespaldoError(f"No se pudo ejecutar {nombre}: {_primera_linea(e)}")
    if r.returncode != 0:
        error = _texto(r.stderr).strip()
        if re.search(r"unsupported version|versi.n no (soportada|admitida)|no soportada", error, re.I):
            raise RespaldoError("La copia se hizo con una versión más nueva de PostgreSQL: actualice PostgreSQL "
                                "en esta computadora para poder restaurarla.")
        if re.search(r"server version mismatch|no coincide la versi.n del servidor", error, re.I):
            raise RespaldoError(f"{nombre} es más antiguo que el servidor PostgreSQL; instale la misma versión "
                                "de PostgreSQL que el servidor.")
        lineas = [x.strip() for x in error.splitlines() if x.strip()]
        raise RespaldoError(f"{nombre} falló: " + " / ".join(lineas[-3:])[:700])
    return _texto(r.stdout)


def _version(programa: Path) -> str:
    return _ejecutar([programa, "--version"], dict(os.environ), 60).strip().split()[-1]


@contextlib.contextmanager
def candado():
    """Una sola copia o restauración a la vez (bloqueo de PostgreSQL: vale para todos los procesos)."""
    try:
        conexion = _mantenimiento()
    except psycopg.OperationalError as e:
        raise RespaldoError(f"No hay conexión con PostgreSQL: {_primera_linea(e)}")
    try:
        if not conexion.execute("SELECT pg_try_advisory_lock(%s)", [_CANDADO]).fetchone()[0]:
            raise Ocupado("Ya hay una copia de seguridad o una restauración en curso. Intente de nuevo en un momento.")
        yield
    finally:
        conexion.close()        # al cerrar la sesión se libera el bloqueo


def estado_base() -> str:
    """lista | sin_datos (base vacía) | sin_base (aún no existe) | sin_servidor (PostgreSQL no responde)."""
    try:
        with _mantenimiento() as c:
            existe = c.execute("SELECT 1 FROM pg_database WHERE datname = %s", [_bd()["NAME"]]).fetchone()
        if not existe:
            return "sin_base"
        with conectar() as c:
            hay_tablas = c.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relkind = 'r' AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema')").fetchone()[0]
    except psycopg.OperationalError:
        return "sin_servidor"
    return "lista" if hay_tablas else "sin_datos"


# -----------------------------------------------------------------------------
# Estructura (firma) y contenido de una base
# -----------------------------------------------------------------------------

_SQL_FIRMA = """
WITH esq AS (SELECT oid, nspname FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname <> 'information_schema')
SELECT 'SCHEMA ' || nspname FROM esq WHERE nspname <> 'public'
UNION ALL
SELECT format('RELATION %s.%s kind=%s persistence=%s rls=%s/%s', e.nspname, c.relname, c.relkind,
              c.relpersistence, c.relrowsecurity::text, c.relforcerowsecurity::text)
  FROM pg_class c JOIN esq e ON e.oid = c.relnamespace WHERE c.relkind NOT IN ('i', 'I')
UNION ALL
SELECT format('COLUMN %s.%s.%s %s%s%s%s%s%s', e.nspname, c.relname, a.attname, format_type(a.atttypid, a.atttypmod),
              CASE WHEN a.attnotnull THEN ' NOT NULL' ELSE '' END,
              CASE WHEN d.adbin IS NOT NULL THEN ' DEFAULT ' || pg_get_expr(d.adbin, d.adrelid) ELSE '' END,
              CASE WHEN a.attidentity <> '' THEN ' IDENTITY ' || a.attidentity::text ELSE '' END,
              CASE WHEN a.attgenerated <> '' THEN ' GENERATED ' || a.attgenerated::text ELSE '' END,
              CASE WHEN a.attcollation <> 0 AND a.attcollation <> t.typcollation
                   THEN ' COLLATE ' || a.attcollation::regcollation::text ELSE '' END)
  FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN esq e ON e.oid = c.relnamespace
  JOIN pg_type t ON t.oid = a.atttypid
  LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
 WHERE a.attnum > 0 AND NOT a.attisdropped AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'c')
UNION ALL
SELECT format('CONSTRAINT %s.%s %s %s', e.nspname, coalesce(c.relname, t.typname), k.conname, pg_get_constraintdef(k.oid))
  FROM pg_constraint k JOIN esq e ON e.oid = k.connamespace
  LEFT JOIN pg_class c ON c.oid = k.conrelid LEFT JOIN pg_type t ON t.oid = k.contypid
UNION ALL
SELECT 'INDEX ' || pg_get_indexdef(i.indexrelid)
  FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN esq e ON e.oid = c.relnamespace
UNION ALL
SELECT format('SEQUENCE %s.%s %s start=%s increment=%s min=%s max=%s cycle=%s', e.nspname, c.relname,
              format_type(s.seqtypid, NULL), s.seqstart, s.seqincrement, s.seqmin, s.seqmax, s.seqcycle::text)
  FROM pg_sequence s JOIN pg_class c ON c.oid = s.seqrelid JOIN esq e ON e.oid = c.relnamespace
UNION ALL
SELECT format('OWNED %s.%s -> %s.%s.%s', e.nspname, s.relname, tn.nspname, t.relname, a.attname)
  FROM pg_depend dp
  JOIN pg_class s ON s.oid = dp.objid AND s.relkind = 'S' JOIN esq e ON e.oid = s.relnamespace
  JOIN pg_class t ON t.oid = dp.refobjid JOIN pg_namespace tn ON tn.oid = t.relnamespace
  JOIN pg_attribute a ON a.attrelid = dp.refobjid AND a.attnum = dp.refobjsubid
 WHERE dp.classid = 'pg_class'::regclass AND dp.refclassid = 'pg_class'::regclass AND dp.deptype IN ('a', 'i')
UNION ALL
SELECT format('TYPE %s.%s %s%s%s%s', e.nspname, t.typname, t.typtype,
              CASE WHEN t.typtype = 'd' THEN ' ' || format_type(t.typbasetype, t.typtypmod) ELSE '' END,
              CASE WHEN t.typnotnull THEN ' NOT NULL' ELSE '' END,
              CASE WHEN t.typdefault IS NOT NULL THEN ' DEFAULT ' || t.typdefault ELSE '' END)
  FROM pg_type t JOIN esq e ON e.oid = t.typnamespace
 WHERE t.typrelid = 0 AND NOT (t.typcategory = 'A' AND t.typelem <> 0)
UNION ALL
SELECT format('FUNCTION %s.%s(%s)', e.nspname, p.proname, pg_get_function_identity_arguments(p.oid))
  FROM pg_proc p JOIN esq e ON e.oid = p.pronamespace
UNION ALL
SELECT format('TRIGGER %s.%s %s', e.nspname, c.relname, t.tgname)
  FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN esq e ON e.oid = c.relnamespace WHERE NOT t.tgisinternal
UNION ALL
SELECT format('RULE %s.%s %s', e.nspname, c.relname, r.rulename)
  FROM pg_rewrite r JOIN pg_class c ON c.oid = r.ev_class JOIN esq e ON e.oid = c.relnamespace
UNION ALL
SELECT format('POLICY %s.%s %s', e.nspname, c.relname, p.polname)
  FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid JOIN esq e ON e.oid = c.relnamespace
UNION ALL
SELECT 'EXTENSION ' || extname FROM pg_extension WHERE extname <> 'plpgsql'
UNION ALL
SELECT 'EVENT TRIGGER ' || evtname FROM pg_event_trigger
UNION ALL
SELECT format('OPERATOR %s.%s', e.nspname, o.oprname) FROM pg_operator o JOIN esq e ON e.oid = o.oprnamespace
UNION ALL
SELECT 'CAST ' || c.oid FROM pg_cast c WHERE c.oid >= 16384
UNION ALL
SELECT 'LARGE OBJECTS ' || count(*) FROM pg_largeobject_metadata HAVING count(*) > 0
UNION ALL
SELECT format('STATISTICS %s.%s', e.nspname, s.stxname) FROM pg_statistic_ext s JOIN esq e ON e.oid = s.stxnamespace
UNION ALL
SELECT format('COLLATION %s.%s', e.nspname, c.collname) FROM pg_collation c JOIN esq e ON e.oid = c.collnamespace
UNION ALL
SELECT format('CONVERSION %s.%s', e.nspname, c.conname) FROM pg_conversion c JOIN esq e ON e.oid = c.connamespace
UNION ALL
SELECT 'PUBLICATION ' || pubname FROM pg_publication
UNION ALL
SELECT 'FOREIGN SERVER ' || srvname FROM pg_foreign_server
UNION ALL
SELECT 'FDW ' || fdwname FROM pg_foreign_data_wrapper
UNION ALL
SELECT format('TEXT SEARCH %s.%s', e.nspname, c.cfgname) FROM pg_ts_config c JOIN esq e ON e.oid = c.cfgnamespace
UNION ALL
SELECT format('TEXT SEARCH %s.%s', e.nspname, d.dictname) FROM pg_ts_dict d JOIN esq e ON e.oid = d.dictnamespace
UNION ALL
SELECT format('OPCLASS %s.%s', e.nspname, o.opcname) FROM pg_opclass o JOIN esq e ON e.oid = o.opcnamespace
UNION ALL
SELECT format('OPFAMILY %s.%s', e.nspname, o.opfname) FROM pg_opfamily o JOIN esq e ON e.oid = o.opfnamespace
UNION ALL
SELECT 'ACCESS METHOD ' || amname FROM pg_am WHERE oid >= 16384
UNION ALL
SELECT 'LANGUAGE ' || lanname FROM pg_language WHERE oid >= 16384 AND lanname <> 'plpgsql'
UNION ALL
SELECT 'TRANSFORM ' || oid FROM pg_transform
UNION ALL
SELECT 'DEFAULT ACL ' || oid FROM pg_default_acl
"""


def firma_esquema(conexion: psycopg.Connection) -> list[str]:
    """Estructura completa de la base, una línea por objeto, independiente del dueño y del orden."""
    anterior = conexion.execute("SELECT current_setting('search_path')").fetchone()[0]
    dentro_de_transaccion = not conexion.autocommit
    conexion.execute("SELECT set_config('search_path', 'pg_catalog', %s)", [dentro_de_transaccion])
    try:
        return sorted(fila[0] for fila in conexion.execute(_SQL_FIRMA))
    finally:
        conexion.execute("SELECT set_config('search_path', %s, %s)", [anterior, dentro_de_transaccion])


def huella(firma: list[str]) -> str:
    return hashlib.sha256("\n".join(firma).encode("utf-8")).hexdigest()


def objetos_prohibidos(firma: list[str]) -> list[str]:
    malos = [x for x in firma if x.startswith(_PROHIBIDOS)]
    malos += [x for x in firma if x.startswith("RELATION ") and not re.search(r" kind=[rS] .* rls=false/false$", x)]
    malos += [x for x in firma if x.startswith("TYPE ") and x.split()[2] != "d"]
    return malos


def leer_referencia() -> list[str]:
    if not REFERENCIA.is_file():
        raise RespaldoError("Falta core/esquema_referencia.txt (estructura de la base de esta versión del sistema).")
    return [x for x in REFERENCIA.read_text(encoding="utf-8").splitlines() if x.strip()]


def firma_referencia() -> list[str]:
    """Estructura que debe tener una copia: la de la base en uso o, si aún no hay datos, la de esta versión."""
    if estado_base() == "lista":
        with conectar() as c:
            return firma_esquema(c)
    return leer_referencia()


def _tablas(conexion: psycopg.Connection) -> list[tuple[str, str]]:
    return conexion.execute(
        "SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind = 'r' AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' ORDER BY 1, 2").fetchall()


def contar_filas(conexion: psycopg.Connection) -> dict[str, int]:
    return {f"{esquema}.{tabla}": conexion.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(esquema, tabla))).fetchone()[0]
            for esquema, tabla in _tablas(conexion)}


def _resumen(conexion: psycopg.Connection, filas: dict[str, int]) -> dict:
    from django.contrib.auth import get_user_model

    from . import models as m

    def tabla(modelo):
        return f"public.{modelo._meta.db_table}"

    ultimo = conexion.execute(sql.SQL("SELECT max({}) FROM {}").format(
        sql.Identifier(m.Factura._meta.get_field("fecha_emision").column),
        sql.Identifier("public", m.Factura._meta.db_table))).fetchone()[0]
    return {"socios": filas.get(tabla(m.Socio), 0), "recibos": filas.get(tabla(m.Factura), 0),
            "usuarios": filas.get(tabla(get_user_model()), 0), "ultimo_recibo": ultimo.isoformat() if ultimo else None}


def _resumen_sistema() -> dict:
    """Lo mismo, leído con los modelos del sistema: prueba que la aplicación lee bien la base."""
    from django.contrib.auth import get_user_model
    from django.db.models import Max

    from . import models as m

    ultimo = m.Factura.objects.aggregate(u=Max("fecha_emision"))["u"]
    return {"socios": m.Socio.objects.count(), "recibos": m.Factura.objects.count(),
            "usuarios": get_user_model().objects.count(), "ultimo_recibo": ultimo.isoformat() if ultimo else None}


def _lista_objetos(dump: Path) -> list[str]:
    """Tipos de objeto del pg_dump; rechaza la copia si trae algo que este sistema no usa."""
    tipos = []
    for linea in _ejecutar([binario("pg_restore"), "--list", dump], _entorno(), 300).splitlines():
        linea = linea.strip()
        if not linea or linea.startswith(";"):
            continue
        coincide = _TOC_LINEA.match(linea)
        resto = coincide.group(1) if coincide else ""
        tipo = next((t for t in _TOC_PERMITIDOS if resto == t or resto.startswith(t + " ")), None)
        if tipo is None or resto.startswith(_TOC_RECHAZADOS):
            raise RespaldoError(f"La copia contiene objetos que SGA-GALLEGO no usa ({' '.join(resto.split()[:3])}). "
                                "No se puede restaurar.")
        tipos.append(tipo)
    return tipos


def _sha256(ruta: Path) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for trozo in iter(lambda: f.read(1 << 20), b""):
            h.update(trozo)
    return h.hexdigest()


# -----------------------------------------------------------------------------
# Crear y listar copias
# -----------------------------------------------------------------------------

def _archivos_media() -> list[tuple[Path, str]]:
    raiz = Path(settings.MEDIA_ROOT)
    if not raiz.is_dir():
        return []
    archivos = []
    for ruta in sorted(raiz.rglob("*")):
        if ruta.is_file() and not ruta.is_symlink():
            relativa = ruta.relative_to(raiz).as_posix()
            if _ruta_segura(relativa):
                archivos.append((ruta, relativa))
            else:
                log.warning("Archivo omitido en la copia (nombre no admitido): %s", relativa)
    return archivos


def crear(tipo: str = "manual", usuario: str = "") -> dict:
    with candado():
        return _crear(tipo, usuario)


def _crear(tipo: str, usuario: str, ahora: datetime | None = None) -> dict:
    if tipo not in TIPOS:
        raise ValueError(tipo)
    if estado_base() != "lista":
        raise RespaldoError("No hay datos que copiar: la base del sistema no existe o está vacía.")
    momento = timezone.localtime(ahora or timezone.now())
    pg_dump = binario("pg_dump")
    carpeta().mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=carpeta(), prefix=".en-curso-", ignore_cleanup_errors=True) as tmp:
        dump = Path(tmp) / "base.dump"
        # Filas, estructura y pg_dump ven exactamente los mismos datos (misma instantánea)
        with conectar(autocommit=False) as conexion:
            conexion.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            conexion.read_only = True
            instantanea = conexion.execute("SELECT pg_export_snapshot()").fetchone()[0]
            filas = contar_filas(conexion)
            firma = firma_esquema(conexion)
            resumen = _resumen(conexion, filas)
            version, codificacion, collate, ctype = conexion.execute(
                "SELECT current_setting('server_version'), pg_encoding_to_char(encoding), datcollate, datctype "
                "FROM pg_database WHERE datname = current_database()").fetchone()
            _ejecutar([pg_dump, "--format=custom", "--no-password", f"--snapshot={instantanea}", f"--file={dump}"],
                      _entorno())
            conexion.rollback()
        if _lista_objetos(dump).count("TABLE DATA") != len(filas):
            raise RespaldoError("El pg_dump no incluyó todas las tablas; la copia no se guardó.")
        media = _archivos_media()
        manifiesto = {
            "aplicacion": APLICACION, "formato": FORMATO, "tipo": tipo,
            "creado": momento.isoformat(timespec="seconds"), "equipo": platform.node()[:80],
            "creado_por": (usuario or "")[:150],
            "base": {"archivo": "base.dump", "nombre": _bd()["NAME"], "bytes": dump.stat().st_size,
                     "sha256": _sha256(dump), "postgresql": version, "pg_dump": _version(pg_dump),
                     "codificacion": codificacion, "lc_collate": collate, "lc_ctype": ctype,
                     "esquema": huella(firma), "tablas": filas},
            "media": {"archivos": len(media), "bytes": sum(r.stat().st_size for r, _ in media)},
            "resumen": resumen,
        }
        temporal = Path(tmp) / "copia.zip"
        with zipfile.ZipFile(temporal, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            z.writestr("manifiesto.json", json.dumps(manifiesto, ensure_ascii=False, indent=2))
            z.write(dump, "base.dump", compress_type=zipfile.ZIP_STORED)      # pg_dump ya lo comprime
            for ruta, relativa in media:
                z.write(ruta, f"media/{relativa}", compress_type=(
                    zipfile.ZIP_STORED if ruta.suffix.lower() in _YA_COMPRIMIDOS else zipfile.ZIP_DEFLATED))
        with zipfile.ZipFile(temporal) as z:
            if z.testzip() is not None:
                raise RespaldoError("La copia recién creada no pasó la verificación del .zip; no se guardó.")
        nombre = _nombre_libre(momento, tipo)
        os.replace(temporal, carpeta() / nombre)
    log.info("Copia de seguridad creada: %s", nombre)
    return info_copia(carpeta() / nombre)


_info_cache: dict[tuple, dict] = {}


def _validar_manifiesto(m) -> dict:
    if not isinstance(m, dict) or m.get("aplicacion") != APLICACION:
        raise RespaldoError("El archivo no es una copia de seguridad de SGA-GALLEGO.")
    if not isinstance(m.get("formato"), int):
        raise RespaldoError("El manifiesto de la copia está dañado.")
    if m["formato"] > FORMATO:
        raise RespaldoError("La copia se hizo con una versión más nueva de SGA-GALLEGO: actualice el sistema "
                            "en esta computadora antes de restaurarla.")
    base = m.get("base")
    tablas = base.get("tablas") if isinstance(base, dict) else None
    if (not isinstance(base, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(base.get("sha256", "")))
            or not isinstance(tablas, dict) or not tablas
            or not all(isinstance(k, str) and type(v) is int and v >= 0 for k, v in tablas.items())
            or _fecha(m.get("creado")) is None):
        raise RespaldoError("El manifiesto de la copia está incompleto o dañado.")
    return m


def _manifiesto_de(z: zipfile.ZipFile) -> dict:
    try:
        info = z.getinfo("manifiesto.json")
        if info.file_size > 4 * 1024 * 1024:
            raise RespaldoError("El manifiesto de la copia está dañado.")
        return _validar_manifiesto(json.loads(z.read(info).decode("utf-8")))
    except KeyError:
        raise RespaldoError("El archivo no es una copia de seguridad de SGA-GALLEGO (falta el manifiesto).")
    except (ValueError, zipfile.BadZipFile):
        raise RespaldoError("El manifiesto de la copia está dañado.")


def info_copia(ruta: Path) -> dict:
    estado = ruta.stat()
    clave = (ruta.name, estado.st_size, estado.st_mtime_ns)
    if clave in _info_cache:
        return _info_cache[clave]
    datos = {"nombre": ruta.name, "tipo": _NOMBRE.match(ruta.name).group(3), "bytes": estado.st_size,
             "valida": True, "error": "", "creado": None, "equipo": "", "creado_por": "", "resumen": {},
             "fotos": 0, "postgresql": "", "sha256": ""}
    try:
        with zipfile.ZipFile(ruta) as z:
            m = _manifiesto_de(z)
        datos.update(creado=m["creado"], equipo=str(m.get("equipo", ""))[:80],
                     creado_por=str(m.get("creado_por", ""))[:150], resumen=m.get("resumen") or {},
                     fotos=int((m.get("media") or {}).get("archivos", 0)), postgresql=str(m["base"].get("postgresql", "")),
                     sha256=m["base"]["sha256"])
    except (RespaldoError, zipfile.BadZipFile, OSError, TypeError, ValueError) as e:
        datos.update(valida=False, error=str(e) or "No se pudo leer la copia.")
    _info_cache[clave] = datos
    return datos


def listar() -> list[dict]:
    return [info_copia(r) for r in _rutas()]


def eliminar(nombre: str) -> None:
    ruta_copia(nombre).unlink()


def _podar(tipo: str, conservar: int) -> None:
    """Deja solo las `conservar` copias más nuevas de ese tipo (las manuales y subidas no se tocan)."""
    copias = [r for r in _rutas() if _NOMBRE.match(r.name).group(3) == tipo]
    for ruta in copias[max(conservar, 1):]:
        ruta.unlink(missing_ok=True)
        log.info("Copia antigua eliminada: %s", ruta.name)


def _limpiar_restos() -> None:
    """Temporales que hayan quedado de una copia o subida interrumpida (más de un día)."""
    if not carpeta().is_dir():
        return
    limite = time.time() - 86400
    for ruta in carpeta().glob(".*"):
        if ruta.name.startswith((".en-curso-", ".restaurando-", ".revision-", ".subida-", ".access-")) and ruta.stat().st_mtime < limite:
            shutil.rmtree(ruta, ignore_errors=True) if ruta.is_dir() else ruta.unlink(missing_ok=True)


# -----------------------------------------------------------------------------
# Revisar, subir y restaurar
# -----------------------------------------------------------------------------

def inspeccionar(ruta: Path, extraer_en: Path) -> dict:
    """Comprueba un .zip sin tocar ninguna base: estructura, manifiesto, huella y lista de objetos.
    Deja base.dump en `extraer_en` y devuelve el manifiesto."""
    try:
        z = zipfile.ZipFile(ruta)
    except (zipfile.BadZipFile, OSError):
        raise RespaldoError("El archivo no es una copia de seguridad de SGA-GALLEGO (no es un .zip válido).")
    with z:
        entradas = z.infolist()
        if len(entradas) > MAX_ENTRADAS_ZIP or sum(e.file_size for e in entradas) > MAX_DESCOMPRIMIDO:
            raise RespaldoError("La copia es demasiado grande o está dañada.")
        for e in entradas:
            n = e.filename
            if n not in ("manifiesto.json", "base.dump", "media/") and not (
                    n.startswith("media/") and (_ruta_segura(n[6:].rstrip("/")))):
                raise RespaldoError("El archivo contiene partes que no son de una copia de SGA-GALLEGO.")
        m = _manifiesto_de(z)
        if "base.dump" not in z.namelist():
            raise RespaldoError("El archivo no es una copia de seguridad de SGA-GALLEGO (falta la base).")
        h = hashlib.sha256()
        try:
            with z.open("base.dump") as origen, open(extraer_en / "base.dump", "wb") as destino:
                for trozo in iter(lambda: origen.read(1 << 20), b""):
                    h.update(trozo)
                    destino.write(trozo)
        except zipfile.BadZipFile:
            raise RespaldoError("La copia está dañada (el .zip no pasa la verificación).")
    if h.hexdigest() != m["base"]["sha256"]:
        raise RespaldoError("La copia está dañada o fue modificada: la huella de la base no coincide.")
    if _lista_objetos(extraer_en / "base.dump").count("TABLE DATA") != len(m["base"]["tablas"]):
        raise RespaldoError("La copia está incompleta: las tablas no coinciden con su manifiesto.")
    return m


def huellas_anteriores() -> set[str]:
    if not ANTERIORES.is_file():
        return set()
    return {x.split()[0] for x in ANTERIORES.read_text(encoding="utf-8").splitlines()
            if x.strip() and not x.lstrip().startswith("#")}


def _comprobar_version(m: dict) -> None:
    esquema = m["base"].get("esquema")
    if esquema != huella(firma_referencia()) and esquema not in huellas_anteriores():
        raise RespaldoError("Esta copia es de otra versión de SGA-GALLEGO (la estructura de la base es distinta) "
                            "y no se puede restaurar en esta versión del sistema.")


def registrar_subida(temporal: Path) -> tuple[dict, bool]:
    """Revisa un .zip subido desde otra computadora y lo deja en la carpeta. Devuelve (copia, ya_existia)."""
    carpeta().mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=carpeta(), prefix=".revision-", ignore_cleanup_errors=True) as tmp:
        m = inspeccionar(temporal, Path(tmp))
    _comprobar_version(m)
    for ruta in _rutas():
        if info_copia(ruta)["sha256"] == m["base"]["sha256"]:
            return info_copia(ruta), True
    destino = carpeta() / _nombre_libre(_fecha(m["creado"]), "subida")
    shutil.copyfile(temporal, destino)
    log.info("Copia subida: %s", destino.name)
    return info_copia(destino), False


def _rol_para_cargar() -> tuple[str, str | None]:
    """Usuario de PostgreSQL con el que se carga la copia: nunca un superusuario."""
    bd = _bd()
    with _mantenimiento() as adm:
        superusuario, crea_bases = adm.execute(
            "SELECT rolsuper, rolcreatedb FROM pg_roles WHERE rolname = current_user").fetchone()
        if not superusuario:
            if not crea_bases:
                raise RespaldoError(f'El usuario "{bd["USER"]}" de PostgreSQL no tiene permiso para crear bases '
                                    "(CREATEDB); un administrador debe dárselo para poder restaurar.")
            return bd["USER"], bd.get("PASSWORD") or None
        clave = secrets.token_urlsafe(32)
        existe = adm.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [ROL_RESTAURACION]).fetchone()
        adm.execute(sql.SQL("{} ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS "
                            "NOINHERIT PASSWORD {}").format(
            sql.SQL("ALTER" if existe else "CREATE"), sql.Identifier(ROL_RESTAURACION), sql.Literal(clave)))
        return ROL_RESTAURACION, clave


def _borrar_base(adm: psycopg.Connection, nombre: str) -> None:
    adm.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(nombre)))


def _crear_base(adm: psycopg.Connection, nombre: str, propietario: str, base: dict) -> None:
    ultimo = None
    for collate, ctype in ((base.get("lc_collate"), base.get("lc_ctype")), ("C", "C")):
        if not collate or not ctype:
            continue
        try:
            adm.execute(sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0 ENCODING 'UTF8' LOCALE_PROVIDER libc "
                                "LC_COLLATE {} LC_CTYPE {}").format(
                sql.Identifier(nombre), sql.Identifier(propietario), sql.Literal(str(collate)), sql.Literal(str(ctype))))
            return
        except psycopg.Error as e:
            ultimo = e
    raise RespaldoError(f"No se pudo crear la base para restaurar: {_primera_linea(ultimo)}")


def _ajustar_secuencias(conexion: psycopg.Connection) -> None:
    """Cada secuencia queda al menos en el máximo de su columna: los registros nuevos no chocan."""
    pares = conexion.execute("""
        SELECT sn.nspname, s.relname, tn.nspname, t.relname, a.attname
          FROM pg_depend d
          JOIN pg_class s ON s.oid = d.objid AND s.relkind = 'S' JOIN pg_namespace sn ON sn.oid = s.relnamespace
          JOIN pg_class t ON t.oid = d.refobjid JOIN pg_namespace tn ON tn.oid = t.relnamespace
          JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = d.refobjsubid
         WHERE d.classid = 'pg_class'::regclass AND d.refclassid = 'pg_class'::regclass AND d.deptype IN ('a', 'i')
    """).fetchall()
    for esquema_s, secuencia, esquema_t, tabla, columna in pares:
        maximo = conexion.execute(sql.SQL("SELECT max({}) FROM {}").format(
            sql.Identifier(columna), sql.Identifier(esquema_t, tabla))).fetchone()[0]
        if maximo is None:
            continue
        ultimo, llamada = conexion.execute(sql.SQL("SELECT last_value, is_called FROM {}").format(
            sql.Identifier(esquema_s, secuencia))).fetchone()
        if ultimo < maximo or (ultimo == maximo and not llamada):
            conexion.execute("SELECT setval((quote_ident(%s) || '.' || quote_ident(%s))::regclass, %s, true)",
                             [esquema_s, secuencia, maximo])


def _verificar(nueva: str, m: dict, rol: str, clave: str | None = None) -> None:
    """La base cargada debe ser exactamente una base de SGA-GALLEGO y tener todas las filas del manifiesto.
    Si es de una versión anterior del sistema (huella en esquemas_anteriores.txt), antes se actualiza."""
    usuario = _bd()["USER"]
    with conectar(base=nueva) as c:
        firma = firma_esquema(c)
        malos = objetos_prohibidos(firma)
        if malos:
            raise RespaldoError("La copia contiene objetos que no son de SGA-GALLEGO (" + "; ".join(malos[:3])
                                + "). No se restauró nada.")
        # Las filas se comparan tal como se copiaron (las tablas nuevas de una actualización vienen vacías)
        filas = contar_filas(c)
        esperadas = m["base"]["tablas"]
        if filas != esperadas:
            distintas = [t for t in sorted(set(filas) | set(esperadas)) if filas.get(t) != esperadas.get(t)]
            raise RespaldoError("La copia está incompleta: " + "; ".join(
                f"{t} tiene {filas.get(t, 0)} filas y debía tener {esperadas.get(t, 0)}" for t in distintas[:3])
                + ". No se restauró nada.")
    referencia = firma_referencia()
    if firma != referencia and huella(firma) in huellas_anteriores():
        _migrar(nueva, rol, clave)
    with conectar(base=nueva) as c:
        firma = firma_esquema(c)
        if firma != referencia:
            faltan = sorted(set(referencia) - set(firma))
            sobran = sorted(set(firma) - set(referencia))
            detalle = "; ".join([f"falta {x}" for x in faltan[:2]] + [f"sobra {x}" for x in sobran[:2]])
            raise RespaldoError(f"La copia es de otra versión de SGA-GALLEGO ({detalle}). No se restauró nada.")
        _ajustar_secuencias(c)
        if rol != usuario:
            c.execute(sql.SQL("REASSIGN OWNED BY {} TO {}").format(sql.Identifier(rol), sql.Identifier(usuario)))
    if rol != usuario:
        with _mantenimiento() as adm:
            adm.execute(sql.SQL("ALTER DATABASE {} OWNER TO {}").format(sql.Identifier(nueva), sql.Identifier(usuario)))


def _migrar(nueva: str, rol: str, clave: str | None) -> None:
    """Aplica a la base restaurada las migraciones de Django que le faltan (por ejemplo, una tabla nueva)."""
    from django.core.management import call_command

    alias = "sga_restauracion"
    bd = connections.settings["default"]
    connections.settings[alias] = {**bd, "NAME": nueva, "USER": rol,
                                   "PASSWORD": (clave if rol != bd["USER"] else bd.get("PASSWORD")) or ""}
    try:
        call_command("migrate", database=alias, interactive=False, verbosity=0)
        log.info("Copia de una versión anterior: estructura actualizada (migraciones aplicadas)")
    except Exception as e:
        raise RespaldoError(f"No se pudo actualizar la copia a esta versión del sistema: {_primera_linea(e)}")
    finally:
        with contextlib.suppress(Exception):
            connections[alias].close()
        connections.settings.pop(alias, None)
        with contextlib.suppress(AttributeError):
            delattr(connections._connections, alias)


def _mover(origen: Path, destino: Path) -> None:
    """Renombra una carpeta; en Windows reintenta si un archivo está abierto un instante."""
    for intento in range(20):
        try:
            shutil.move(str(origen), str(destino))
            return
        except PermissionError:
            if intento == 19:
                raise RespaldoError(f"No se pudo mover la carpeta {origen.name}: hay un archivo abierto. Cierre las "
                                    "fotos o carpetas abiertas e intente de nuevo.")
            time.sleep(0.3)


def _cambiar_media(nueva: Path) -> Path | None:
    raiz = Path(settings.MEDIA_ROOT)
    anterior = raiz.with_name(raiz.name + "_anterior")
    if anterior.exists():
        shutil.rmtree(anterior)
    if raiz.exists():
        _mover(raiz, anterior)
    else:
        anterior = None
    raiz.parent.mkdir(parents=True, exist_ok=True)
    try:
        _mover(nueva, raiz)
    except Exception:
        if anterior is not None:
            _mover(anterior, raiz)
        raise
    return anterior


def _deshacer_media(anterior: Path | None, nueva: Path) -> None:
    raiz = Path(settings.MEDIA_ROOT)
    if raiz.exists():
        _mover(raiz, nueva)
    if anterior is not None and anterior.exists():
        _mover(anterior, raiz)


def _intercambiar(nombre: str, nueva: str, anterior: str, existe: bool) -> None:
    connections.close_all()          # las conexiones de este hilo a la base en uso
    with _mantenimiento() as adm:
        _borrar_base(adm, anterior)
        if existe:
            for _ in range(40):
                # Se cierran las demás conexiones del sistema (otros hilos o procesos del servidor)
                adm.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s "
                            "AND pid <> pg_backend_pid() AND (usename = current_user "
                            "OR (SELECT rolsuper FROM pg_roles WHERE rolname = current_user))", [nombre])
                try:
                    adm.execute(sql.SQL("ALTER DATABASE {} RENAME TO {}").format(
                        sql.Identifier(nombre), sql.Identifier(anterior)))
                    break
                except psycopg.errors.ObjectInUse:
                    time.sleep(0.25)
            else:
                raise RespaldoError("La base en uso tiene conexiones de otro programa (¿pgAdmin abierto?) que no se "
                                    "pudieron cerrar. Ciérrelo e intente de nuevo; no se restauró nada.")
        try:
            adm.execute(sql.SQL("ALTER DATABASE {} RENAME TO {}").format(sql.Identifier(nueva), sql.Identifier(nombre)))
        except Exception:
            if existe:
                adm.execute(sql.SQL("ALTER DATABASE {} RENAME TO {}").format(
                    sql.Identifier(anterior), sql.Identifier(nombre)))
            raise


def restaurar(ruta: Path, usuario: str = "", primera_vez: bool = False) -> dict:
    with candado():
        return _restaurar(ruta, usuario, primera_vez)


def _restaurar(ruta: Path, usuario: str, primera_vez: bool) -> dict:
    nombre = _bd()["NAME"]
    nueva, anterior = f"{nombre}_restauracion", f"{nombre}_anterior"
    estado = estado_base()
    if estado == "sin_servidor":
        raise RespaldoError("PostgreSQL no responde: no se puede restaurar.")
    if primera_vez and estado == "lista":
        raise RespaldoError("Este sistema ya tiene datos: la copia se restaura desde Configuración → Copias de seguridad.")
    pg_restore = binario("pg_restore")
    carpeta().mkdir(parents=True, exist_ok=True)
    previa = None
    with tempfile.TemporaryDirectory(dir=carpeta(), prefix=".restaurando-", ignore_cleanup_errors=True) as tmp:
        tmp = Path(tmp)
        m = inspeccionar(ruta, tmp)
        _comprobar_version(m)
        fotos = _extraer_media(ruta, tmp / "media")
        rol, clave = _rol_para_cargar()
        try:
            with _mantenimiento() as adm:
                _borrar_base(adm, nueva)
                _crear_base(adm, nueva, rol, m["base"])
            _ejecutar([pg_restore, "--no-owner", "--no-privileges", "--exit-on-error", "--single-transaction",
                       "--no-password", f"--dbname={nueva}", tmp / "base.dump"], _entorno(nueva, rol, clave))
            _verificar(nueva, m, rol, clave)
            if estado == "lista":
                previa = _crear("previa", usuario)
            media_anterior = _cambiar_media(tmp / "media")
            try:
                _intercambiar(nombre, nueva, anterior, existe=estado != "sin_base")
            except Exception:
                _deshacer_media(media_anterior, tmp / "media")
                raise
        except Exception:
            with contextlib.suppress(Exception), _mantenimiento() as adm:
                _borrar_base(adm, nueva)
            raise
        finally:
            if rol == ROL_RESTAURACION:
                with contextlib.suppress(Exception), _mantenimiento() as adm:
                    adm.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(sql.Identifier(ROL_RESTAURACION)))
    resumen = _resumen_sistema()
    registro = {"fecha": timezone.localtime().isoformat(timespec="seconds"), "copia": ruta.name,
                "creada": m["creado"], "usuario": usuario, "previa": previa["nombre"] if previa else None}
    _guardar_json(ARCHIVO_EJECUCION, {**leer_ejecucion(), "ultima_restauracion": registro})
    log.info("Copia restaurada: %s (previa: %s)", ruta.name, registro["previa"])
    return {"copia": info_copia(ruta), "resumen": resumen, "fotos": fotos, "previa": registro["previa"]}


def _extraer_media(ruta: Path, destino: Path) -> int:
    destino.mkdir(parents=True, exist_ok=True)
    cantidad = 0
    with zipfile.ZipFile(ruta) as z:
        for e in z.infolist():
            if not e.filename.startswith("media/") or e.is_dir():
                continue
            relativa = e.filename[6:]
            if not _ruta_segura(relativa):
                raise RespaldoError("La copia contiene una foto con un nombre no permitido.")
            archivo = destino.joinpath(*PurePosixPath(relativa).parts)
            archivo.parent.mkdir(parents=True, exist_ok=True)
            with z.open(e) as origen, open(archivo, "wb") as salida:
                shutil.copyfileobj(origen, salida, 1 << 20)
            cantidad += 1
    return cantidad


# -----------------------------------------------------------------------------
# Programación de las copias automáticas
# -----------------------------------------------------------------------------

def _leer_json(nombre: str) -> dict | None:
    try:
        datos = json.loads((carpeta() / nombre).read_text(encoding="utf-8"))
        return datos if isinstance(datos, dict) else None
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.warning("No se pudo leer %s; se usan los valores iniciales", nombre)
        return None


def _guardar_json(nombre: str, datos: dict) -> None:
    carpeta().mkdir(parents=True, exist_ok=True)
    temporal = carpeta() / f".{nombre}.{secrets.token_hex(4)}.tmp"
    temporal.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporal, carpeta() / nombre)


def leer_programacion() -> dict:
    datos = _leer_json(ARCHIVO_PROGRAMACION)
    if datos is None:
        # Primera vez: activas desde ahora (la primera copia se hace a la próxima hora programada)
        desde = timezone.now().isoformat(timespec="seconds")
        datos = {t: {**c, "activa_desde": desde} for t, c in PROGRAMACION_INICIAL.items()}
        with contextlib.suppress(OSError):
            _guardar_json(ARCHIVO_PROGRAMACION, datos)
    return {t: {**PROGRAMACION_INICIAL[t], **(datos.get(t) if isinstance(datos.get(t), dict) else {})}
            for t in TIPOS_AUTOMATICOS}


def guardar_programacion(nueva: dict) -> dict:
    actual = leer_programacion()
    ahora = timezone.now().isoformat(timespec="seconds")
    datos = {}
    for tipo in TIPOS_AUTOMATICOS:
        cfg = {**actual[tipo], **nueva[tipo]}
        # Al activar una copia que estaba apagada, la primera se hace a la próxima hora programada
        cfg["activa_desde"] = ahora if cfg["activa"] and not actual[tipo]["activa"] else actual[tipo].get("activa_desde", ahora)
        datos[tipo] = cfg
    _guardar_json(ARCHIVO_PROGRAMACION, datos)
    return datos


def leer_ejecucion() -> dict:
    datos = _leer_json(ARCHIVO_EJECUCION) or {}
    vacio = {"ultima": None, "ultimo_intento": None, "error": None}
    resultado = {t: {**vacio, **(datos.get(t) if isinstance(datos.get(t), dict) else {})} for t in TIPOS_AUTOMATICOS}
    resultado["ultima_restauracion"] = datos.get("ultima_restauracion")
    return resultado


def _a_las(dia: date, cfg: dict) -> datetime:
    h, mi = (int(x) for x in cfg["hora"].split(":"))
    return timezone.make_aware(datetime.combine(dia, hora_del_dia(h, mi)))


def _dia_del_anio(anio: int, cfg: dict) -> date:
    return date(anio, cfg["mes"], min(cfg["dia"], calendar.monthrange(anio, cfg["mes"])[1]))   # 29-feb -> 28-feb


def ultimo_turno(tipo: str, cfg: dict, ahora: datetime) -> datetime:
    """Última fecha y hora programada que ya pasó."""
    hoy = timezone.localtime(ahora).date()
    if tipo == "diaria":
        turno = _a_las(hoy, cfg)
        return turno if turno <= ahora else _a_las(hoy - timedelta(days=1), cfg)
    if tipo == "semanal":
        dia = hoy - timedelta(days=(hoy.weekday() - cfg["dia_semana"]) % 7)
        turno = _a_las(dia, cfg)
        return turno if turno <= ahora else _a_las(dia - timedelta(days=7), cfg)
    turno = _a_las(_dia_del_anio(hoy.year, cfg), cfg)
    return turno if turno <= ahora else _a_las(_dia_del_anio(hoy.year - 1, cfg), cfg)


def proximo_turno(tipo: str, cfg: dict, ahora: datetime) -> datetime:
    hoy = timezone.localtime(ahora).date()
    if tipo == "diaria":
        turno = _a_las(hoy, cfg)
        return turno if turno > ahora else _a_las(hoy + timedelta(days=1), cfg)
    if tipo == "semanal":
        dia = hoy + timedelta(days=(cfg["dia_semana"] - hoy.weekday()) % 7)
        turno = _a_las(dia, cfg)
        return turno if turno > ahora else _a_las(dia + timedelta(days=7), cfg)
    turno = _a_las(_dia_del_anio(hoy.year, cfg), cfg)
    return turno if turno > ahora else _a_las(_dia_del_anio(hoy.year + 1, cfg), cfg)


def _pendientes(programacion: dict, ejecucion: dict, ahora: datetime) -> list[str]:
    tipos = []
    for tipo in TIPOS_AUTOMATICOS:
        cfg, hecho = programacion[tipo], ejecucion[tipo]
        if not cfg.get("activa"):
            continue
        turno = ultimo_turno(tipo, cfg, ahora)
        desde = _fecha(cfg.get("activa_desde"))
        ultima = _fecha(hecho.get("ultima"))
        intento = _fecha(hecho.get("ultimo_intento"))
        if (desde and turno < desde) or (ultima and ultima >= turno):
            continue
        if hecho.get("error") and intento and ahora - intento < timedelta(minutes=30):
            continue        # tras un fallo se reintenta cada media hora
        tipos.append(tipo)
    return tipos


def ejecutar_pendientes(ahora: datetime | None = None) -> list[str]:
    """Hace las copias automáticas que tocan (o que no se hicieron porque el equipo estaba apagado)."""
    ahora = ahora or timezone.now()
    programacion = leer_programacion()
    if not _pendientes(programacion, leer_ejecucion(), ahora) or estado_base() != "lista":
        return []
    try:
        with candado():
            ejecucion = leer_ejecucion()        # otro proceso del servidor pudo hacerla mientras tanto
            tipos = _pendientes(programacion, ejecucion, ahora)
            if not tipos:
                return []
            try:
                copia = _crear(tipos[0], "automática", ahora)
                for tipo in tipos[1:]:          # una sola copia sirve para los tipos que coinciden
                    shutil.copyfile(carpeta() / copia["nombre"], carpeta() / _nombre_libre(ahora, tipo))
                for tipo in tipos:
                    ejecucion[tipo] = {"ultima": ahora.isoformat(timespec="seconds"),
                                       "ultimo_intento": ahora.isoformat(timespec="seconds"), "error": None}
                    _podar(tipo, int(programacion[tipo]["conservar"]))
            except Exception as e:
                log.exception("Falló la copia automática (%s)", ", ".join(tipos))
                for tipo in tipos:
                    ejecucion[tipo] = {**ejecucion[tipo], "ultimo_intento": ahora.isoformat(timespec="seconds"),
                                       "error": str(e) if isinstance(e, RespaldoError) else _primera_linea(e)}
            _guardar_json(ARCHIVO_EJECUCION, ejecucion)
            return tipos
    except Ocupado:
        return []


def estado() -> dict:
    """Todo lo que muestra Configuración -> Copias de seguridad."""
    ahora = timezone.now()
    programacion = leer_programacion()
    ejecucion = leer_ejecucion()
    pendientes = _pendientes(programacion, ejecucion, ahora)
    disponible, motivo = disponibilidad()
    return {
        "carpeta": str(carpeta()),
        "disponible": disponible,
        "motivo": motivo,
        "automaticas": settings.RESPALDOS_AUTOMATICOS,
        "programacion": programacion,
        "ejecucion": {t: {**ejecucion[t], "pendiente": t in pendientes,
                          "proxima": proximo_turno(t, programacion[t], ahora).isoformat(timespec="seconds")
                          if programacion[t]["activa"] else None}
                      for t in TIPOS_AUTOMATICOS},
        "ultima_restauracion": ejecucion.get("ultima_restauracion"),
        "copias": listar(),
    }


def resumen_estado() -> dict:
    """Aviso del panel: fecha de la última copia y fallos de las automáticas."""
    rutas = _rutas()
    ultima = info_copia(rutas[0]) if rutas else None
    programacion, ejecucion = leer_programacion(), leer_ejecucion()
    return {
        "ultima": {k: ultima[k] for k in ("nombre", "tipo", "creado")} if ultima else None,
        "errores": {t: ejecucion[t]["error"] for t in TIPOS_AUTOMATICOS
                    if programacion[t]["activa"] and ejecucion[t].get("error")},
        "disponible": disponibilidad()[0],
    }


# -----------------------------------------------------------------------------
# Hilo de las copias automáticas
# -----------------------------------------------------------------------------

_hilo: threading.Thread | None = None


def _es_proceso_servidor() -> bool:
    programa = Path(sys.argv[0]).name.lower() if sys.argv else ""
    if any(s in programa for s in ("gunicorn", "uvicorn", "waitress")):
        return True
    if len(sys.argv) > 1 and sys.argv[1] == "servir":          # waitress en Windows (manage.py servir)
        return True
    if len(sys.argv) > 1 and sys.argv[1] == "runserver":
        # runserver arranca dos procesos: el que vigila el código y el que atiende (RUN_MAIN)
        return os.environ.get("RUN_MAIN") == "true" or "--noreload" in sys.argv
    return False


def _bucle() -> None:
    time.sleep(15)
    while True:
        try:
            _limpiar_restos()
            ejecutar_pendientes()
        except Exception:
            log.exception("Copias automáticas")
        time.sleep(60)


def iniciar_programador() -> None:
    global _hilo
    if _hilo is not None or not settings.RESPALDOS_AUTOMATICOS or not _es_proceso_servidor():
        return
    _hilo = threading.Thread(target=_bucle, name="sga-respaldos", daemon=True)
    _hilo.start()
    log.info("Copias automáticas activas; carpeta: %s", carpeta())
