"""
Cargar desde Access (SICGA .accdb) solo lo que falta (Configuración -> Copias de seguridad).

Mientras todavía se use Access, se sube su archivo .accdb y el sistema trae lo que se hizo allá (recibos, pagos,
mensualidades, socios, egresos...), sin duplicar lo que ya está y sin pisar lo que se cambió aquí.

Cómo decide, registro por registro y campo por campo (comparación a tres bandas):
  BASE     Access tal como se cargó la última vez: el esquema legacy, la copia 1:1 de las tablas de Access que
           dejó la migración y que se renueva en cada carga
  ACCESS   el archivo que se sube ahora
  SISTEMA  lo que hay hoy en el sistema
Los tres pasan por la MISMA transformación de la migración (migracion/02_etl_access_to_pg.py) y se comparan por
claves naturales (código de acción, número de recibo, socio + año + mes...), nunca por ids internos:
  - nuevo en Access: se agrega, salvo que el sistema ya lo tenga igual ("ya estaba") o con otros datos (conflicto:
    no se toca y se informa);
  - cambiado en Access: cada campo cambiado se copia si en el sistema sigue como en BASE; si aquí también se
    cambió, ese campo queda como conflicto;
  - borrado en Access: aquí no se borra, solo se informa;
  - lo hecho solo en el sistema nuevo se respeta.
Antes de guardar se hace una copia de seguridad ("Antes de cargar Access"); todo va en una sola transacción y al
final BASE pasa a ser este Access: la próxima carga solo trae lo posterior.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import re
import secrets
import shutil
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import psycopg
from django.conf import settings
from django.utils import timezone
from psycopg import sql

from . import respaldos

log = logging.getLogger("sga.access")

MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
# Columnas que apuntan a otro registro: se comparan por su dato natural, no por el id interno
FK = {"socio_id": "socio", "provincia_id": "provincia", "tipo_pago_id": "tipo_pago", "banco_id": "banco",
      "concesion_id": "concesion"}
# Contadores de Configuración: nunca bajan (se queda el mayor de los dos sistemas)
CONTADORES = ("ultima_factura", "ultimo_numero_control", "ultimo_voucher")
# Tablas de trabajo de Access (las crea y borra SICGA; la migración no las usa): pueden faltar
TEMPORALES = {"TempDIVISAS", "TempMONTOSAA", "TempTotalSOCIOS"}
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
MAX_DETALLE = 40


class ImportacionError(Exception):
    """Mensaje para mostrar tal cual al usuario."""


@dataclass(frozen=True)
class Spec:
    tabla: str
    titulo: str
    clave: tuple[str, ...]           # identifica el registro (en documentos, la parte que no cambia al editarlo)
    documentos: bool = False         # filas repetibles (se comparan como multiconjunto)
    ignorar: tuple[str, ...] = ()    # se recalculan al guardar (enlaces a recibos)
    id_natural: bool = False         # el id es parte del dato (nivel, cuota 1..12, fila única de parámetros)
    unicas: tuple[str, ...] = ()     # columnas únicas en el sistema: si ya existe el valor, conflicto


# En orden de carga (primero lo que otros registros necesitan)
SPECS = (
    Spec("nivel_acceso", "Niveles de acceso", ("id",), id_natural=True),
    Spec("usuario_sistema", "Usuarios de Access", ("username",)),
    Spec("provincia", "Provincias", ("nombre",)),
    Spec("tipo_pago", "Tipos de pago", ("codigo",)),
    Spec("banco", "Bancos y cuentas", ("nombre",)),
    Spec("configuracion", "Parámetros (último recibo, montos...)", ("id",), id_natural=True),
    Spec("calendario_cobro", "Calendario de cobro", ("mes",)),
    Spec("tarifa_mensual", "Tarifas mensuales", ("anio", "mes")),
    Spec("cuota_extra", "Cuotas extraordinarias", ("id",), id_natural=True),
    Spec("socio", "Socios", ("codigo",)),
    Spec("beneficiario", "Beneficiarios", ("codigo",)),
    Spec("socio_mensualidad", "Mensualidades de socios", ("socio_id", "anio", "mes")),
    Spec("socio_cuota_extra", "Cuotas extraordinarias de socios", ("socio_id", "cuota_extra_id")),
    Spec("concesion", "Concesionarios", ("tipo_pago_id",)),
    Spec("concesion_mensualidad", "Mensualidades de concesionarios", ("concesion_id", "anio", "mes")),
    Spec("factura", "Recibos", ("numero", "socio_id", "codigo_socio_legado", "fecha_emision"), documentos=True),
    Spec("abono", "Abonos y notas de crédito",
         ("numero_documento", "socio_id", "fecha_emision", "anio_deuda", "numero_factura_relacionada"),
         documentos=True, ignorar=("factura_relacionada_id",)),
    Spec("cheque_recibido", "Cheques recibidos", ("numero_cheque", "numero_cuenta", "numero_factura"),
         documentos=True, ignorar=("factura_id",)),
    Spec("deposito", "Depósitos", ("numero_deposito", "numero_factura", "fecha_pago"), documentos=True,
         ignorar=("factura_id",)),
    Spec("cheque_emitido", "Cheques emitidos (egresos)", ("numero_cheque", "fecha", "beneficiario"), documentos=True,
         unicas=("numero_cheque",)),
    Spec("libro_diario", "Libro diario", ("numero_factura", "socio_id", "fecha_emision"), documentos=True,
         ignorar=("factura_id",)),
    Spec("proveedor", "Proveedores", ("nombre_empresa",), documentos=True, unicas=("codigo",)),
    Spec("activo_fijo", "Activos fijos", ("codigo", "descripcion"), documentos=True, unicas=("codigo",)),
    Spec("uso_salon", "Uso del salón", ("socio_id", "fecha_uso"), documentos=True),
)
POR_TABLA = {s.tabla: s for s in SPECS}

ETIQUETAS = {
    "pagado": "pagada", "pagada": "pagada", "numero_factura": "recibo", "anulada": "anulado", "anulado": "anulado",
    "fecha_anulacion": "fecha de anulación", "fecha_cancelacion": "fecha de cobro", "estado": "estado",
    "telefono_habitacion": "teléfono", "direccion_habitacion": "dirección", "monto_tarifa": "tarifa",
    "cliente_nombre": "nombre", "cliente_rif": "C.I./RIF", "descripcion": "concepto", "monto_efectivo": "efectivo",
    "monto_deposito": "depósito", "monto_cheque": "cheque", "monto_tarjeta_debito": "tarjeta de débito",
    "monto_tarjeta_credito": "tarjeta de crédito", "ultima_factura": "último recibo",
    "ultimo_numero_control": "último número de control", "ultimo_voucher": "último voucher",
}


def _etiqueta(columna: str) -> str:
    return ETIQUETAS.get(columna, columna.removesuffix("_id").replace("_", " "))


def _bs(v) -> str:
    if v is None:
        return "—"
    return f"{Decimal(v):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _fecha_texto(v) -> str:
    return v.strftime("%d/%m/%Y") if isinstance(v, date) else "—"


def _valor_texto(v) -> str:
    if isinstance(v, bool):
        return "sí" if v else "no"
    if isinstance(v, Decimal):
        return _bs(v)
    if isinstance(v, date):
        return _fecha_texto(v)
    return "vacío" if v in (None, "") else str(v)


def describir(spec: Spec, f: dict) -> str:
    """Una línea legible para el informe."""
    t = spec.tabla
    quien = f.get("socio_id") or f.get("codigo_socio_legado") or ""
    if t == "factura":
        persona = " ".join(x for x in (quien, f.get("cliente_nombre") or "") if x) or "—"
        return (f"Recibo {f.get('numero') or 's/n'} · {_fecha_texto(f.get('fecha_emision'))} · {persona}"
                f" · Bs {_bs(f.get('monto'))}" + (" · ANULADO" if f.get("anulada") else ""))
    if t in ("socio_mensualidad", "concesion_mensualidad"):
        dueno = f.get("socio_id") or f.get("concesion_id")
        estado = "pagada" if f.get("pagado") else "pendiente"
        recibo = f" (recibo {f['numero_factura']})" if f.get("numero_factura") else ""
        return f"{dueno} {MESES[f['mes'] - 1]}-{f['anio']}: {estado}{recibo}"
    if t == "socio_cuota_extra":
        return f"{f.get('socio_id')} cuota extra {f.get('cuota_extra_id')}: {'pagada' if f.get('pagada') else 'pendiente'}"
    if t == "socio":
        return f"{f.get('codigo')} {f.get('apellidos') or ''}, {f.get('nombres') or ''}".strip(" ,")
    if t == "beneficiario":
        return f"{f.get('codigo')} {f.get('nombre_completo') or ''} ({f.get('socio_id')})"
    if t == "abono":
        return (f"Abono {f.get('numero_documento')} · {_fecha_texto(f.get('fecha_emision'))} · {quien}"
                f" · Bs {_bs(f.get('monto'))}")
    if t == "cheque_recibido":
        return (f"Cheque {f.get('numero_cheque') or 's/n'} · {f.get('banco_emisor') or ''} · Bs {_bs(f.get('monto'))}"
                f" · recibo {f.get('numero_factura') or '—'}")
    if t == "deposito":
        return (f"Depósito {f.get('numero_deposito') or 's/n'} · {_fecha_texto(f.get('fecha_pago'))}"
                f" · Bs {_bs(f.get('monto'))} · recibo {f.get('numero_factura') or '—'}")
    if t == "cheque_emitido":
        return (f"Cheque {f.get('numero_cheque') or 's/n'} · {_fecha_texto(f.get('fecha'))} · "
                f"{f.get('beneficiario') or ''} · Bs {_bs(f.get('monto'))}")
    if t == "libro_diario":
        return f"Recibo {f.get('numero_factura')} · {_fecha_texto(f.get('fecha_emision'))} · Bs {_bs(f.get('monto'))}"
    if t == "tarifa_mensual":
        return f"{MESES[f['mes'] - 1]}-{f['anio']}: Bs {_bs(f.get('monto'))}"
    if t == "cuota_extra":
        return f"Cuota extra {f.get('numero')}: {f.get('descripcion') or ''} Bs {_bs(f.get('monto'))}"
    if t == "configuracion":
        return "Parámetros generales"
    if t == "uso_salon":
        return f"{f.get('socio_id')} · {_fecha_texto(f.get('fecha_uso'))} · {f.get('observacion') or ''}"
    clave = " · ".join(_valor_texto(f.get(c)) for c in POR_TABLA[t].clave)
    return f"{clave}"


# -----------------------------------------------------------------------------
# Disponibilidad, transformación de la migración y lectura de los tres lados
# -----------------------------------------------------------------------------

def _ruta_etl() -> Path:
    return Path(getattr(settings, "ETL_ACCESS", "") or Path(settings.BASE_DIR).parent / "migracion" / "02_etl_access_to_pg.py")


def disponibilidad() -> tuple[bool, str]:
    try:
        import pyodbc
    except ImportError:
        return False, "Cargar desde Access solo funciona en la computadora con Windows del sistema (falta pyodbc)."
    if not any("Access Driver" in d for d in pyodbc.drivers()):
        return False, ("Falta el controlador de Microsoft Access en esta computadora (Microsoft Access Database "
                       "Engine).")
    if not _ruta_etl().is_file():
        return False, f"No se encontró el programa de la migración ({_ruta_etl()})."
    return True, ""


def _etl():
    """Carga una instancia nueva del programa de la migración: su transformación es la que se usa en las tres
    versiones, así un registro que no cambió da exactamente los mismos valores."""
    disponible, motivo = disponibilidad()
    if not disponible:
        raise ImportacionError(motivo)
    nombre = f"etl_sicga_{secrets.token_hex(4)}"
    spec = importlib.util.spec_from_file_location(nombre, _ruta_etl())
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[nombre] = modulo          # dataclasses lo busca ahí mientras se define el módulo
    try:
        spec.loader.exec_module(modulo)
    finally:
        sys.modules.pop(nombre, None)
    return modulo


def _transformar(etl, datos: dict) -> tuple[dict, list]:
    """Corre la transformación de la migración en memoria: tabla -> (columnas, filas), e incidencias."""
    recogido: dict[str, tuple[list, list]] = {}

    def recoger(_pg, tabla, columnas, filas):
        recogido[tabla] = (list(columnas), list(filas))
        return len(filas)

    etl.copiar = recoger
    ctx = etl.Ctx()
    for paso in (etl.t_catalogos, etl.t_parametros, etl.t_socios, etl.t_beneficiarios, etl.t_concesiones,
                 etl.t_facturas, etl.t_cobros, etl.t_otros):
        paso(None, datos, ctx)
    return recogido, ctx.incidencias


def _leer_base(conexion: psycopg.Connection, etl) -> tuple[dict, dict]:
    """El Access de la última carga (esquema legacy). Devuelve los datos y, por tabla de Access, (tabla legacy,
    columnas legacy, nombres originales)."""
    tablas = conexion.execute("""
        SELECT c.relname, obj_description(c.oid, 'pg_class'),
               array_agg(a.attname::text ORDER BY a.attnum),
               array_agg(coalesce(col_description(c.oid, a.attnum), a.attname::text) ORDER BY a.attnum)
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
          JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped AND a.attname <> '_fila_origen'
         WHERE n.nspname = 'legacy' AND c.relkind = 'r'
         GROUP BY c.oid, c.relname""").fetchall()
    datos, mapa = {}, {}
    for relname, comentario, columnas, originales in tablas:
        coincide = re.search(r"\[(.+)\]\s*$", comentario or "")
        if not coincide:
            continue
        nombre = coincide.group(1)
        consulta = sql.SQL("SELECT {} FROM {} ORDER BY _fila_origen").format(
            sql.SQL(", ").join(map(sql.Identifier, columnas)), sql.Identifier("legacy", relname))
        filas = [tuple(f) for f in conexion.execute(consulta).fetchall()]
        datos[nombre] = etl.TablaAccess(nombre, list(originales), [None] * len(columnas), filas)
        mapa[nombre] = (relname, list(columnas), list(originales))
    if not datos:
        raise ImportacionError("La base no tiene la copia de Access de la migración (esquema legacy).")
    return datos, mapa


def _leer_access(etl, ruta: Path, clave: str, mapa_base: dict) -> dict:
    """Las tablas de SICGA del archivo subido (solo lectura). La estructura debe ser la misma de la migración."""
    import pyodbc

    # El controlador de Access toma las llaves {} como parte de la clave: va tal cual, y ';' no lo admite
    if ";" in (clave or ""):
        raise ImportacionError("La clave de Access tiene un punto y coma (;), que el controlador de Access no admite.")
    cadena = f"DRIVER={{Microsoft Access Driver (*.mdb, *.accdb)}};DBQ={ruta};PWD={clave or ''};"
    try:
        conexion = pyodbc.connect(cadena, readonly=True, autocommit=True, timeout=60)
    except pyodbc.Error as e:
        texto = str(e)
        if re.search(r"password|contrase", texto, re.I):
            raise ImportacionError("La clave de la base de Access no es correcta.")
        if re.search(r"Unrecognized database format|no reconoc|not a valid|no es v.lid|-1028|da.ado|corrupt", texto, re.I):
            raise ImportacionError("El archivo no es una base de datos de Access válida (o está dañado).")
        if re.search(r"already in use|exclusiv|en uso", texto, re.I):
            raise ImportacionError("El archivo de Access está abierto en modo exclusivo; ciérrelo y vuelva a copiarlo.")
        raise ImportacionError("No se pudo abrir el archivo de Access: " + texto.split("]")[-1].strip()[:300])
    datos = {}
    with conexion:
        cursor = conexion.cursor()
        existentes = {t.table_name for t in cursor.tables(tableType="TABLE")}
        faltan = [t for t in mapa_base if t not in existentes and t not in TEMPORALES]
        if faltan:
            raise ImportacionError("El archivo no es el Access de SICGA: le faltan las tablas " + ", ".join(sorted(faltan)[:6]) + ".")
        for nombre, (_relname, _columnas, originales) in mapa_base.items():
            if nombre not in existentes:
                continue                     # tabla temporal que Access ya borró: se deja la copia anterior
            cursor.execute(f"SELECT * FROM [{nombre.replace(']', ']]')}]")
            columnas = [d[0] for d in cursor.description]
            if sorted(columnas) != sorted(originales):
                if nombre in TEMPORALES:
                    continue
                distintas = sorted(set(columnas) ^ set(originales))
                raise ImportacionError(f"La tabla {nombre} del archivo de Access tiene otras columnas "
                                       f"({', '.join(distintas[:5])}): no es la misma versión de SICGA.")
            datos[nombre] = etl.TablaAccess(nombre, columnas, [d[1] for d in cursor.description],
                                            [tuple(f) for f in cursor.fetchall()])
    return datos


def _tipos_sistema(conexion: psycopg.Connection) -> dict[str, dict[str, str]]:
    tipos: dict[str, dict[str, str]] = defaultdict(dict)
    for tabla, columna, tipo in conexion.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_schema = 'public'"):
        tipos[tabla][columna] = tipo
    return tipos


def _coaccionar(v, tipo: str | None):
    """El valor de la transformación con el tipo de la columna del sistema (para comparar igual con igual)."""
    if v is None or tipo is None:
        return v
    if isinstance(v, float):
        v = Decimal(repr(v))
    if tipo in ("integer", "smallint", "bigint"):
        if isinstance(v, str):
            return int(v) if v.strip().lstrip("-").isdigit() else v
        return int(v)
    if tipo == "numeric":
        return v if isinstance(v, Decimal) else Decimal(str(v))
    if tipo == "boolean":
        return bool(v)
    if tipo == "date":
        return v.date() if isinstance(v, datetime) else v
    if tipo in ("text", "character varying", "character"):
        return str(v)
    return v


def _columnas(spec: Spec, columnas_etl: list[str]) -> list[str]:
    return [c for c in columnas_etl if (c != "id" or spec.id_natural) and c not in spec.ignorar]


def _naturales(recogido: dict, tipos: dict) -> dict[str, list[dict]]:
    """Filas de la transformación con los enlaces cambiados por su dato natural (P001, M001, BANESCO...)."""
    def mapa(tabla, clave, valor):
        columnas, filas = recogido.get(tabla, ([], []))
        if not filas:
            return {}
        i, j = columnas.index(clave), columnas.index(valor)
        return {f[i]: f[j] for f in filas}

    mapas = {"socio": mapa("socio", "id", "codigo"), "provincia": mapa("provincia", "id", "nombre"),
             "tipo_pago": mapa("tipo_pago", "id", "codigo"), "banco": mapa("banco", "id", "nombre")}
    mapas["concesion"] = {cid: mapas["tipo_pago"].get(tid) for cid, tid in mapa("concesion", "id", "tipo_pago_id").items()}
    salida = {}
    for spec in SPECS:
        columnas_etl, filas = recogido.get(spec.tabla, ([], []))
        columnas = _columnas(spec, columnas_etl)
        posiciones = [columnas_etl.index(c) for c in columnas]
        lista = []
        for fila in filas:
            d = {}
            for c, p in zip(columnas, posiciones):
                v = fila[p]
                d[c] = (mapas[FK[c]].get(v) if v is not None else None) if c in FK else _coaccionar(v, tipos[spec.tabla].get(c))
            lista.append(d)
        salida[spec.tabla] = lista
    return salida


def _mapas_sistema(conexion: psycopg.Connection, inverso: bool = False) -> dict[str, dict]:
    mapas = {
        "socio": dict(conexion.execute("SELECT id, codigo FROM public.socio").fetchall()),
        "provincia": dict(conexion.execute("SELECT id, nombre FROM public.provincia").fetchall()),
        "tipo_pago": dict(conexion.execute("SELECT id, codigo FROM public.tipo_pago").fetchall()),
        "banco": dict(conexion.execute("SELECT id, nombre FROM public.banco").fetchall()),
        "concesion": dict(conexion.execute(
            "SELECT c.id, t.codigo FROM public.concesion c JOIN public.tipo_pago t ON t.id = c.tipo_pago_id").fetchall()),
    }
    return {k: {v: i for i, v in m.items()} for k, m in mapas.items()} if inverso else mapas


def _leer_sistema(conexion: psycopg.Connection, columnas: dict[str, list[str]]) -> tuple[dict, dict]:
    """Filas naturales del sistema y su id (o su clave, en el calendario de cobro que no tiene id)."""
    mapas = _mapas_sistema(conexion)
    filas, ids = {}, {}
    for spec in SPECS:
        cols = columnas[spec.tabla]
        tiene_id = spec.tabla != "calendario_cobro"
        seleccion = (["id"] if tiene_id and "id" not in cols else []) + cols
        consulta = sql.SQL("SELECT {} FROM {}").format(sql.SQL(", ").join(map(sql.Identifier, seleccion)),
                                                       sql.Identifier("public", spec.tabla))
        lista, lista_ids = [], []
        for fila in conexion.execute(consulta):
            valores = dict(zip(seleccion, fila))
            d = {c: (mapas[FK[c]].get(valores[c]) if valores[c] is not None else None) if c in FK else valores[c]
                 for c in cols}
            lista.append(d)
            lista_ids.append(valores["id"] if tiene_id else valores["mes"])
        filas[spec.tabla], ids[spec.tabla] = lista, lista_ids
    return filas, ids


# -----------------------------------------------------------------------------
# Comparación a tres bandas
# -----------------------------------------------------------------------------

@dataclass
class Cambio:
    accion: str                       # nuevo | actualizar | ya_estaba | conflicto | borrado
    fila: dict                        # el registro de Access (o el borrado)
    campos: dict = field(default_factory=dict)      # actualizar: columna -> valor nuevo
    choques: dict = field(default_factory=dict)     # conflicto: columna -> (aquí, en Access)
    pg_id: object = None
    motivo: str = ""


def _congelar(d: dict) -> tuple:
    return tuple(sorted(d.items()))


def _fusionar_campos(spec: Spec, base: dict | None, nuevo: dict, sistema: dict) -> tuple[dict, dict]:
    campos, choques = {}, {}
    for c, v in nuevo.items():
        if spec.tabla == "configuracion" and c in CONTADORES:
            if v is not None and (sistema.get(c) is None or v > sistema[c]):
                campos[c] = v              # el contador más alto de los dos
            continue
        if base is None:                   # existe en los dos lados sin base común: solo se completa lo vacío
            if sistema.get(c) is None and v is not None:
                campos[c] = v
            elif v != sistema.get(c) and v is not None:
                choques[c] = (sistema.get(c), v)
            continue
        if v == base.get(c):
            continue                       # Access no lo cambió
        if sistema.get(c) == base.get(c):
            campos[c] = v                  # solo cambió en Access
        elif sistema.get(c) != v:
            choques[c] = (sistema.get(c), v)   # cambió en los dos lados, distinto
    return campos, choques


def _comparar_clave(spec: Spec, B: list, N: list, P: list, P_ids: list) -> list[Cambio]:
    def k(d):
        return tuple(d.get(c) for c in spec.clave)
    base, nuevo = {k(d): d for d in B}, {k(d): d for d in N}
    sistema = {k(d): (d, i) for d, i in zip(P, P_ids)}
    cambios = []
    for clave in sorted(set(base) | set(nuevo), key=lambda x: tuple(str(v) for v in x)):
        b, n = base.get(clave), nuevo.get(clave)
        if b == n:
            continue
        if n is None:
            cambios.append(Cambio("borrado", b))
            continue
        p, pid = sistema.get(clave, (None, None))
        if p is None:
            cambios.append(Cambio("nuevo", n) if b is None else
                           Cambio("conflicto", n, motivo="cambió en Access pero aquí ya no existe"))
            continue
        if p == n:
            cambios.append(Cambio("ya_estaba", n, pg_id=pid))
            continue
        campos, choques = _fusionar_campos(spec, b, n, p)
        if campos:
            cambios.append(Cambio("actualizar", n, campos=campos, pg_id=pid))
        if choques:
            cambios.append(Cambio("conflicto", n, choques=choques, pg_id=pid))
        if not campos and not choques:
            cambios.append(Cambio("ya_estaba", n, pg_id=pid))
    return cambios


def _comparar_documentos(spec: Spec, B: list, N: list, P: list, P_ids: list) -> list[Cambio]:
    def ident(d):
        return tuple(d.get(c) for c in spec.clave)
    cb, cn, cp = Counter(map(_congelar, B)), Counter(map(_congelar, N)), Counter(map(_congelar, P))
    ids_por_fila: dict[tuple, list] = defaultdict(list)
    por_ident: dict[tuple, list] = defaultdict(list)
    for d, i in zip(P, P_ids):
        ids_por_fila[_congelar(d)].append(i)
        por_ident[ident(d)].append((d, i))
    unicos = {u: {d.get(u) for d in P if d.get(u) is not None} for u in spec.unicas}
    usados: set = set()
    consumidas: Counter = Counter()
    agregadas, quitadas = cn - cb, cb - cn
    nuevos_por_ident: dict[tuple, list] = defaultdict(list)
    quitados_por_ident: dict[tuple, list] = defaultdict(list)
    for t, n in sorted(agregadas.items(), key=lambda x: str(x[0])):
        nuevos_por_ident[ident(dict(t))] += [dict(t)] * n
    for t, n in sorted(quitadas.items(), key=lambda x: str(x[0])):
        quitados_por_ident[ident(dict(t))] += [dict(t)] * n

    def ya_estaba(fila) -> bool:
        t = _congelar(fila)
        if cp[t] - cb[t] - consumidas[t] > 0:   # hay más copias aquí que en la base: alguien ya la cargó
            consumidas[t] += 1
            return True
        return False

    cambios = []
    for clave in sorted(set(nuevos_por_ident) | set(quitados_por_ident), key=lambda x: tuple(str(v) for v in x)):
        agregados, quitados = nuevos_por_ident.get(clave, []), quitados_por_ident.get(clave, [])
        if len(agregados) == 1 and len(quitados) == 1:      # el mismo documento, modificado en Access
            viejo, nuevo = quitados[0], agregados[0]
            if ya_estaba(nuevo):
                cambios.append(Cambio("ya_estaba", nuevo))
                continue
            libres = [i for i in ids_por_fila.get(_congelar(viejo), []) if i not in usados]
            if libres:
                usados.add(libres[0])
                campos = {c: v for c, v in nuevo.items() if v != viejo.get(c)}
                cambios.append(Cambio("actualizar", nuevo, campos=campos, pg_id=libres[0]))
                continue
            candidatos = [(d, i) for d, i in por_ident.get(clave, []) if i not in usados]
            if len(candidatos) == 1:
                d, i = candidatos[0]
                usados.add(i)
                campos, choques = _fusionar_campos(spec, viejo, nuevo, d)
                if campos:
                    cambios.append(Cambio("actualizar", nuevo, campos=campos, pg_id=i))
                if choques:
                    cambios.append(Cambio("conflicto", nuevo, choques=choques, pg_id=i))
                if not campos and not choques:
                    cambios.append(Cambio("ya_estaba", nuevo, pg_id=i))
            else:
                cambios.append(Cambio("conflicto", nuevo, motivo="cambió en Access pero aquí no está o hay varios iguales"))
            continue
        for fila in quitados:
            cambios.append(Cambio("borrado", fila))
        for fila in agregados:
            if ya_estaba(fila):
                cambios.append(Cambio("ya_estaba", fila))
                continue
            propios = [d for d, _ in por_ident.get(clave, []) if _congelar(d) not in cb]
            choque_unico = next((u for u in spec.unicas if fila.get(u) is not None and fila[u] in unicos[u]), None)
            if propios:
                cambios.append(Cambio("conflicto", fila, motivo="aquí ya hay uno con el mismo número y otros datos"))
            elif choque_unico:
                cambios.append(Cambio("conflicto", fila, motivo=f"aquí ya existe otro con ese {_etiqueta(choque_unico)}"))
            else:
                for u in spec.unicas:
                    if fila.get(u) is not None:
                        unicos[u].add(fila[u])
                cambios.append(Cambio("nuevo", fila))
    return cambios


def _clave_incidencia(t) -> tuple:
    return (t[0], re.sub(r" \(fila \d+\)$", "", t[1] or ""), t[2], t[3])


@dataclass
class Plan:
    cambios: dict[str, list[Cambio]]
    incidencias: list[tuple]
    rechazo: str
    access: dict
    datos_access: dict
    mapa_base: dict

    @property
    def total(self) -> int:
        return (sum(1 for lista in self.cambios.values() for c in lista if c.accion in ("nuevo", "actualizar"))
                + len(self.incidencias))


def _numero_recibo(fila) -> int:
    numero = fila.get("numero") or ""
    return int(numero) if str(numero).isdigit() else 0


def _planificar(conexion: psycopg.Connection, etl, datos_access: dict, mapa_base: dict, datos_base: dict) -> Plan:
    tipos = _tipos_sistema(conexion)
    rec_base, inc_base = _transformar(etl, datos_base)
    rec_access, inc_access = _transformar(etl, datos_access)
    B, N = _naturales(rec_base, tipos), _naturales(rec_access, tipos)
    columnas = {s.tabla: _columnas(s, rec_access.get(s.tabla, rec_base.get(s.tabla, ([], [])))[0]) for s in SPECS}
    P, P_ids = _leer_sistema(conexion, columnas)
    cambios = {}
    for spec in SPECS:
        comparar = _comparar_documentos if spec.documentos else _comparar_clave
        cambios[spec.tabla] = comparar(spec, B[spec.tabla], N[spec.tabla], P[spec.tabla], P_ids[spec.tabla])

    vistas = {_clave_incidencia(t) for t in inc_base} | {
        _clave_incidencia(t) for t in conexion.execute(
            "SELECT tabla_origen, clave_origen, tipo, detalle FROM public.migracion_incidencia").fetchall()}
    incidencias, repetidas = [], set()
    for t in inc_access:
        clave = _clave_incidencia(t)
        if clave not in vistas and clave not in repetidas:
            repetidas.add(clave)
            incidencias.append(t)

    # Un archivo más antiguo que el último cargado, o de otra base, se rechaza: si no, desharía datos
    recibos_base, recibos_access = B["factura"], N["factura"]
    max_base = max(map(_numero_recibo, recibos_base), default=0)
    max_access = max(map(_numero_recibo, recibos_access), default=0)
    borrados_recibos = sum(1 for c in cambios["factura"] if c.accion == "borrado")
    borrados_docs = sum(1 for s in SPECS if s.documentos for c in cambios[s.tabla] if c.accion == "borrado")
    rechazo = ""
    if max_access < max_base:
        rechazo = (f"Este archivo de Access es más antiguo que el último cargado: su último recibo es el {max_access} y "
                   f"el sistema ya tiene hasta el {max_base}. No se cargó nada.")
    elif borrados_recibos > 20 or borrados_docs > 200:
        rechazo = (f"Este archivo no parece el Access del club o es más antiguo: le faltan {borrados_recibos} recibos y "
                   f"{borrados_docs} documentos que sí tenía el último cargado. No se cargó nada.")
    ultimo = max(recibos_access, key=_numero_recibo, default=None)
    access = {"recibos": len(recibos_access), "socios": len(N["socio"]),
              "ultimo_recibo": ultimo.get("numero") if ultimo else None,
              "fecha_ultimo_recibo": ultimo["fecha_emision"].isoformat() if ultimo and ultimo.get("fecha_emision") else None}
    return Plan(cambios, incidencias, rechazo, access, datos_access, mapa_base)


def _resumen(plan: Plan) -> dict:
    categorias = []
    for spec in SPECS:
        lista = plan.cambios[spec.tabla]
        if not lista:
            continue
        cuenta = Counter(c.accion for c in lista)
        detalle = []
        for accion in ("nuevo", "actualizar", "conflicto", "borrado", "ya_estaba"):
            elegidos = [c for c in lista if c.accion == accion]
            for c in elegidos[:MAX_DETALLE]:
                texto = describir(spec, c.fila)
                if accion == "actualizar":
                    texto += ": " + ", ".join(f"{_etiqueta(k)} → {_valor_texto(v)}" for k, v in c.campos.items())
                elif accion == "conflicto":
                    texto += ": " + (c.motivo or "; ".join(
                        f"{_etiqueta(k)} aquí {_valor_texto(a)} y en Access {_valor_texto(b)}" for k, (a, b) in c.choques.items()))
                detalle.append({"accion": accion, "texto": texto})
            if len(elegidos) > MAX_DETALLE:
                detalle.append({"accion": accion, "texto": f"... y {len(elegidos) - MAX_DETALLE} más"})
        categorias.append({"tabla": spec.tabla, "titulo": spec.titulo, "nuevos": cuenta["nuevo"],
                           "actualizados": cuenta["actualizar"], "ya_estaban": cuenta["ya_estaba"],
                           "conflictos": cuenta["conflicto"], "borrados": cuenta["borrado"], "detalle": detalle})
    if plan.incidencias:
        categorias.append({"tabla": "migracion_incidencia", "titulo": "Incidencias para revisar", "nuevos": len(plan.incidencias),
                           "actualizados": 0, "ya_estaban": 0, "conflictos": 0, "borrados": 0,
                           "detalle": [{"accion": "nuevo", "texto": f"{t[0]} {t[1]}: {t[3]}"} for t in plan.incidencias[:MAX_DETALLE]]})
    return {"rechazo": plan.rechazo, "access": plan.access, "total_cambios": 0 if plan.rechazo else plan.total,
            "conflictos": sum(c["conflictos"] for c in categorias), "categorias": categorias}


# -----------------------------------------------------------------------------
# Guardar
# -----------------------------------------------------------------------------

def _resolver_factura(conexion, numero, socio_id, monto, fecha) -> int | None:
    """Como en la migración: si hay varios recibos con ese número, desempata por socio, monto y fecha."""
    if not numero:
        return None
    candidatos = conexion.execute("SELECT id, socio_id, monto, fecha_emision FROM public.factura WHERE numero = %s",
                                  [numero]).fetchall()
    for filtro in (lambda c: c[1] == socio_id, lambda c: monto is not None and abs(c[2]) == abs(monto),
                   lambda c: fecha is not None and c[3] == fecha):
        if len(candidatos) <= 1:
            break
        candidatos = [c for c in candidatos if filtro(c)] or candidatos
    return candidatos[0][0] if len(candidatos) == 1 else None


def _a_sistema(valores: dict, mapas_inv: dict) -> dict:
    return {c: (mapas_inv[FK[c]].get(v) if v is not None else None) if c in FK else v for c, v in valores.items()}


def _enlaces(conexion, spec: Spec, fila_sistema: dict, natural: dict) -> dict:
    """Enlace al recibo de los pagos (como en la migración)."""
    if spec.tabla in ("cheque_recibido", "deposito", "libro_diario"):
        fecha = natural.get("fecha_pago") or natural.get("fecha_emision")
        return {"factura_id": _resolver_factura(conexion, natural.get("numero_factura"), fila_sistema.get("socio_id"),
                                                natural.get("monto"), fecha)}
    if spec.tabla == "abono":
        return {"factura_relacionada_id": _resolver_factura(conexion, natural.get("numero_factura_relacionada"),
                                                            fila_sistema.get("socio_id"), natural.get("monto"), None)}
    return {}


def _aplicar(conexion: psycopg.Connection, plan: Plan) -> Counter:
    hechos: Counter = Counter()
    mapas_inv = _mapas_sistema(conexion, inverso=True)
    for spec in SPECS:
        for c in plan.cambios[spec.tabla]:
            if c.accion == "nuevo":
                valores = _a_sistema(c.fila, mapas_inv)
                valores.update(_enlaces(conexion, spec, valores, c.fila))
                columnas = list(valores)
                con_id = spec.tabla != "calendario_cobro"      # el calendario se identifica por el mes
                cursor = conexion.execute(
                    sql.SQL("INSERT INTO {} ({}) VALUES ({})" + (" RETURNING id" if con_id else "")).format(
                        sql.Identifier("public", spec.tabla), sql.SQL(", ").join(map(sql.Identifier, columnas)),
                        sql.SQL(", ").join(sql.Placeholder() * len(columnas))),
                    [valores[k] for k in columnas])
                nuevo_id = cursor.fetchone()[0] if con_id else None
                # Los registros nuevos que otros necesitan (socio, banco, tipo de pago...) quedan a mano
                natural = {"socio": "codigo", "provincia": "nombre", "tipo_pago": "codigo", "banco": "nombre",
                           "concesion": "tipo_pago_id"}
                if spec.tabla in natural:
                    mapas_inv[spec.tabla][c.fila[natural[spec.tabla]]] = nuevo_id
                hechos[spec.tabla, "nuevo"] += 1
            elif c.accion == "actualizar" and c.campos:
                valores = _a_sistema(c.campos, mapas_inv)
                if "numero_factura" in c.campos or "numero_factura_relacionada" in c.campos or "socio_id" in c.campos:
                    actual = dict(c.fila)
                    valores.update(_enlaces(conexion, spec, _a_sistema(actual, mapas_inv), actual))
                donde = "mes" if spec.tabla == "calendario_cobro" else "id"
                conexion.execute(
                    sql.SQL("UPDATE {} SET {} WHERE {} = %s").format(
                        sql.Identifier("public", spec.tabla),
                        sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(k)) for k in valores),
                        sql.Identifier(donde)),
                    [*valores.values(), c.pg_id])
                hechos[spec.tabla, "actualizar"] += 1
    if plan.incidencias:
        with conexion.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO public.migracion_incidencia (tabla_origen, clave_origen, tipo, detalle, datos, resuelta, "
                "nota_resolucion, resuelta_por, resuelta_en) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                plan.incidencias)
        hechos["migracion_incidencia", "nuevo"] += len(plan.incidencias)
    return hechos


def _renovar_base(conexion: psycopg.Connection, plan: Plan, registro: dict) -> None:
    """La copia legacy pasa a ser este Access (sin las claves en texto plano de USUARIOS)."""
    for nombre, (relname, columnas, originales) in plan.mapa_base.items():
        tabla = plan.datos_access.get(nombre)
        if tabla is None:
            continue                         # tabla temporal ausente en este Access: queda la anterior
        posicion = {c: i for i, c in enumerate(tabla.columnas)}
        conexion.execute(sql.SQL("TRUNCATE {}").format(sql.Identifier("legacy", relname)))
        lista = sql.SQL(", ").join(map(sql.Identifier, ["_fila_origen", *columnas]))
        with conexion.cursor().copy(sql.SQL("COPY {} ({}) FROM STDIN").format(sql.Identifier("legacy", relname), lista)) as cp:
            for i, fila in enumerate(tabla.filas, 1):
                cp.write_row((i, *(None if (nombre == "USUARIOS" and original == "Clave") else fila[posicion[original]]
                                   for original in originales)))
    conexion.execute(sql.SQL("COMMENT ON SCHEMA legacy IS {}").format(sql.Literal(json.dumps(registro, ensure_ascii=False))))


# -----------------------------------------------------------------------------
# Lo que usa la API
# -----------------------------------------------------------------------------

def _carpeta_subida(token: str) -> Path:
    if not _TOKEN.match(token or ""):
        raise ImportacionError("La carga no existe o ya terminó; vuelva a subir el archivo.")
    carpeta = respaldos.carpeta() / f".access-{token}"
    if not carpeta.is_dir():
        raise ImportacionError("La carga no existe o ya terminó; vuelva a subir el archivo.")
    return carpeta


def guardar_subida(archivo, nombre: str) -> str:
    """Deja el .accdb subido en una carpeta temporal propia y devuelve su token."""
    extension = Path(nombre or "").suffix.lower()
    if extension not in (".accdb", ".mdb"):
        raise ImportacionError("Elija el archivo de la base de Access (.accdb o .mdb).")
    token = secrets.token_urlsafe(24)
    carpeta = respaldos.carpeta() / f".access-{token}"
    carpeta.mkdir(parents=True)
    with open(carpeta / f"sicga{extension}", "wb") as destino:
        for trozo in archivo.chunks():
            destino.write(trozo)
    return token


def descartar(token: str) -> None:
    try:
        shutil.rmtree(_carpeta_subida(token), ignore_errors=True)
    except ImportacionError:
        pass


def _archivo(token: str) -> Path:
    carpeta = _carpeta_subida(token)
    archivos = [p for p in carpeta.iterdir() if p.suffix.lower() in (".accdb", ".mdb")]
    if not archivos:
        raise ImportacionError("La carga no existe o ya terminó; vuelva a subir el archivo.")
    return archivos[0]


def ultima_carga() -> dict | None:
    try:
        with respaldos.conectar() as c:
            comentario = c.execute("SELECT obj_description(oid, 'pg_namespace') FROM pg_namespace "
                                   "WHERE nspname = 'legacy'").fetchone()
    except psycopg.Error:
        return None
    try:
        datos = json.loads(comentario[0]) if comentario and comentario[0] else None
    except ValueError:
        return None
    return datos if isinstance(datos, dict) else None


def analizar(token: str, clave: str) -> dict:
    """Compara sin guardar nada."""
    ruta = _archivo(token)
    etl = _etl()
    with respaldos.candado(), respaldos.conectar(autocommit=False) as conexion:
        conexion.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        conexion.read_only = True
        datos_base, mapa = _leer_base(conexion, etl)
        datos_access = _leer_access(etl, ruta, clave, mapa)
        plan = _planificar(conexion, etl, datos_access, mapa, datos_base)
        conexion.rollback()
    return {**_resumen(plan), "token": token, "ultima_carga": ultima_carga()}


def cargar(token: str, clave: str, usuario: str) -> dict:
    """Vuelve a comparar (por si algo cambió) y guarda todo en una sola transacción, con copia previa."""
    ruta = _archivo(token)
    etl = _etl()
    with respaldos.candado():
        if respaldos.estado_base() != "lista":
            raise ImportacionError("El sistema no tiene datos: primero restaure una copia de seguridad.")
        with respaldos.conectar(autocommit=False) as conexion:
            conexion.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            try:
                datos_base, mapa = _leer_base(conexion, etl)
                datos_access = _leer_access(etl, ruta, clave, mapa)
                plan = _planificar(conexion, etl, datos_access, mapa, datos_base)
                if plan.rechazo:
                    raise ImportacionError(plan.rechazo)
                resumen = _resumen(plan)
                # Copia de los datos actuales antes de tocar nada (pg_dump, en otra sesión)
                previa = respaldos._crear("importacion", usuario)
                hechos = _aplicar(conexion, plan)
                registro = {"fecha": timezone.localtime().isoformat(timespec="seconds"), "usuario": usuario,
                            "cambios": sum(hechos.values()), "conflictos": resumen["conflictos"],
                            "ultimo_recibo": plan.access["ultimo_recibo"], "copia_previa": previa["nombre"]}
                _renovar_base(conexion, plan, registro)
                conexion.commit()
            except psycopg.errors.SerializationFailure:
                conexion.rollback()
                raise ImportacionError("Alguien modificó datos mientras se cargaba: no se guardó nada. Intente de nuevo.")
            except Exception:
                conexion.rollback()
                raise
    descartar(token)
    log.info("Carga desde Access: %s cambios (copia previa %s)", registro["cambios"], previa["nombre"])
    return {**resumen, "hechos": registro["cambios"], "copia_previa": previa["nombre"], "ultima_carga": registro}
