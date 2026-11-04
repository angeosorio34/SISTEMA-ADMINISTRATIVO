#!/usr/bin/env python
"""
ETL SICGA: Microsoft Access (.accdb) -> PostgreSQL.

Fases
  1. EXTRACT  Lee las 26 tablas del .accdb (solo lectura).
  2. LEGACY   Copia 1:1 cada tabla a legacy.<tabla> (snake_case, sin transformar).
  3. CORE     Limpia, normaliza y carga el modelo public.* definido en 01_schema.sql,
              resolviendo claves foraneas. Lo que no encaja va a public.migracion_incidencia.
  4. VALIDAR  Compara conteos y sumas de importes origen vs destino.

Todo ocurre en UNA transaccion: si algo falla, PostgreSQL queda como estaba.
El script es idempotente: se puede ejecutar las veces que haga falta.

Uso
  python 02_etl_access_to_pg.py --crear-esquema     # primera vez (ejecuta 01_schema.sql)
  python 02_etl_access_to_pg.py                     # recarga datos
  python 02_etl_access_to_pg.py --dry-run           # ejecuta todo y hace ROLLBACK

Configuracion (variables de entorno o archivo .env junto al script)
  ACCESS_DB_PATH  ruta al .accdb
  ACCESS_PWD      contrasena de la base de datos Access
  PG_DSN          postgresql://usuario:clave@host:5432/base
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import psycopg
import pyodbc
from psycopg import sql

log = logging.getLogger("etl_sicga")
BASE_DIR = Path(__file__).resolve().parent

MESES = ["ENE", "FEB", "MAR", "ABR", "MAY", "JUN", "JUL", "AGO", "SEP", "OCT", "NOV", "DIC"]
MESES_CAP = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
# Columnas de MONTOS con el dia de corte ('31/01'); septiembre se llama "Sept"
MESES_CORTE = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sept", "Oct", "Nov", "Dic"]

Q4 = Decimal("0.0001")


# =============================================================================
# Configuracion
# =============================================================================

def cargar_dotenv(ruta: Path) -> None:
    """Carga KEY=VALUE de un .env sin depender de python-dotenv (no pisa el entorno)."""
    if not ruta.exists():
        return
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        os.environ.setdefault(clave.strip(), valor.strip().strip('"').strip("'"))


def env_obligatoria(nombre: str) -> str:
    valor = os.environ.get(nombre)
    if not valor:
        sys.exit(f"Falta la variable de entorno {nombre} (ver .env.example)")
    return valor


# =============================================================================
# Limpieza de valores
# =============================================================================

def txt(v, max_len: int | None = None, alnum: bool = True) -> str | None:
    """Texto recortado; '' -> None. Con alnum=True descarta basura sin letras ni digitos ('º', '-')."""
    if v is None:
        return None
    s = str(v).strip()
    if not s or (alnum and not re.search(r"[0-9A-Za-zÀ-ÖØ-öø-ÿ]", s)):
        return None
    return s[:max_len] if max_len else s


def mayus(v, max_len: int | None = None) -> str | None:
    s = txt(v)
    return s.upper()[:max_len] if s and max_len else (s.upper() if s else None)


def dinero(v) -> Decimal | None:
    if v is None:
        return None
    d = v if isinstance(v, Decimal) else Decimal(repr(float(v)))
    return d.quantize(Q4, rounding=ROUND_HALF_UP)


def dinero0(v) -> Decimal:
    return dinero(v) or Decimal(0)


def fecha(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    return v if isinstance(v, date) else None


def bit(v) -> bool:
    return bool(v)


def entero(v) -> int | None:
    s = txt(v)
    return int(s) if s and s.isdigit() else None


def cedula(v) -> str | None:
    s = mayus(v)
    return re.sub(r"[.,\s\-]", "", s)[:15] if s else None


ESTADO_CIVIL = {
    "C": "C", "CASADO": "C", "CASADA": "C", "CASADIO": "C", "CADADA": "C",
    "S": "S", "SOLTERO": "S", "SOLTERA": "S",
    "D": "D", "DIVORCIADO": "D", "DIVORCIADA": "D",
    "V": "V", "VIUDO": "V", "VIUDA": "V",
    # Union estable de hecho (reconocida en Venezuela); en Access venia truncada a 10 caracteres
    "U": "U", "UNION DE H": "U", "UNION DE HECHO": "U", "UNION ESTABLE": "U", "RELACION D": "U",
    "CONCUBINO": "U", "CONCUBINA": "U",
    "D C/P": "D",
}

# Beneficiarios sin socio en Access, asignados tras revisar los datos:
#   344-2 PIERRE RAMOS LUCENA (hijo): el unico socio 344 es P344 LUCENA DE RAMOS.
#   3     ELSA NAVARRO DE HERNANDEZ (conyuge): P353 HERNANDEZ ROJAS tiene registrados
#         P353-2 y P353-3 (hijos HERNANDEZ NAVARRO) y le falta justamente el P353-1.
CORRECCION_BENEFICIARIO = {"344-2": ("P344", "P344-2"), "3": ("P353", "P353-1")}

# Incidencias revisadas una a una con los datos de Access (10/10/2026). Al migrar quedan marcadas como
# resueltas con esta explicacion; las que no esten aqui quedan pendientes de revisar.
REVISADO_POR = "revisión de la migración"
REVISADO_EN = datetime(2026, 10, 10, 12, 0, tzinfo=timezone(timedelta(hours=-4)))
REVISADAS = {
    ("BANCOS", "MI CASA EAP"): "Correcto: MI CASA EAP figura como banco en los cheques emitidos (CHEQUES CG); se agregó al catálogo.",
    ("RELSOLV", "P225/2023"): "Correcto: la fila estaba repetida idéntica en Access; se conservó una sola.",
    ("MOROSOS", "P225/2023"): "Correcto: la fila estaba repetida idéntica en Access; se conservó una sola.",
    ("BENEFICIARIOS", "3"): "Correcto: ELSA NAVARRO DE HERNÁNDEZ (cónyuge) es de P353 HERNÁNDEZ ROJAS: sus hijos son "
                            "P353-2 y P353-3 y su foto en Access se llamaba P353-1.jpg.",
    ("BENEFICIARIOS", "344-2"): "Correcto: PIERRE RAMOS LUCENA (hijo) es de P344 LUCENA DE RAMOS; su foto en Access "
                                "se llamaba P344-2.jpg.",
    ("FACTURAS", "00230"): "Correcto: en 2010 la acción P225 era de VERILDO D'AMICO BIANCHI (las facturas 00098 a "
                           "00473 de P225 están a su nombre).",
    ("FACTURAS", "00277"): "Correcto: GABRIEL GARCIA SERRANO es el titular de P479.",
    ("FACTURAS", "00252"): "Resuelta: en 2010 la acción P328 era de EDUARDO JOSÉ MARTÍNEZ ROJAS (C.I. 12.575.499): "
                           "las facturas 00093 a 01819 de P328 están a su nombre. '328' es P328 sin la letra. La "
                           "acción pasó después a ADRIANA VIETTRI FERNÁNDEZ.",
    ("DEPOSITOS", "52300810629"): "Resuelta: es el depósito de la factura 00252 (Cena Día de las Madres 2010) de "
                                  "EDUARDO MARTÍNEZ, entonces titular de P328.",
    ("CHEQUES", "82685074"): "Resuelta: el '1' es un error de digitación. El cheque pagó la factura 04119 (concesión "
                             "de abril de 2014) del concesionario TASCA RESTAURANT EL HORREO, C.A.; los demás cheques "
                             "de esa cuenta no llevan socio.",
    ("FACTURAS", "01180"): "Correcto: T001 era el número de socio temporal (sin acción) de RICHARD LÓPEZ SABURIDO, "
                           "C.I. 6.284.136 (factura 01032: «NOVIEMBRE A MARZO SOCIO TEMPORAL»). Queda como ingreso "
                           "de un no socio.",
    ("CHEQUES", "34004088"): "Correcto: cheque de la factura 01180 del socio temporal RICHARD LÓPEZ SABURIDO "
                             "(T001, sin acción).",
    ("FACTURAS", "17314"): "Correcto: ingreso de la escuela de natación cobrado por el personal; no es de un socio.",
    ("FACTURAS", "17869"): "Correcto: ingreso del día cobrado por el personal (LEXANGI SUNIAGA); no es de un socio.",
    ("FACTURAS", "18073"): "Correcto: ingreso de la oficina deportiva cobrado por el personal; no es de un socio.",
    ("FACTURAS", "18460"): "Correcto: ingreso de la oficina deportiva cobrado por el personal; no es de un socio.",
    ("DEPOSITOS", "0728"): "Revisada: depósito real (Banesco, 22/09/2022, Bs 1.756,65) de un abono de servicios "
                           "(Tachitec, Corpoelec, Hidrocaribe) del concesionario TASCA RESTAURANT EL HORREO, que es "
                           "quien pagaba esos servicios. Su recibo 16798 se borró en Access (también 16792 y 16801) "
                           "y no se volvió a emitir. Se conserva el depósito, sin recibo, para la conciliación bancaria.",
    ("DEPOSITOS", "00706531"): "Revisada: depósito real (Banesco, 18/01/2023, Bs 972,60) de la escuela de natación "
                               "(enero + inscripción). Su recibo 17206 se borró en Access junto con 17204 y 17205 y "
                               "no se volvió a emitir. Se conserva el depósito, sin recibo, para la conciliación bancaria.",
}


def palabras(texto) -> set:
    """"Isgro de D'Amico" -> {'ISGRO', 'AMICO'} (sin tildes, sin palabras cortas)."""
    t = unicodedata.normalize("NFKD", (texto or "").upper()).encode("ascii", "ignore").decode()
    return {w for w in re.split(r"[^A-Z]+", t) if len(w) >= 4}

PARENTESCO = {"CONYUGE": "CÓNYUGE", "MAMA": "MADRE", "MAMÁ": "MADRE", "PAPA": "PADRE", "PAPÁ": "PADRE"}

BANCO_ALIAS = {"DELSUR": "DEL SUR", "BAMESCO": "BANESCO"}


def snake(nombre: str) -> str:
    """'N°Fact' -> 'n_fact', 'AñoDeuda' -> 'anio_deuda', 'CHEQUES CG' -> 'cheques_cg'."""
    s = nombre.replace("ñ", "ni").replace("Ñ", "NI").replace("°", "_").replace("º", "_")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    return re.sub(r"_+", "_", s) or "col"


# =============================================================================
# Contexto: mapas de claves e incidencias
# =============================================================================

@dataclass
class Ctx:
    periodo_actual: int = 0
    nivel_ids: set = field(default_factory=set)
    provincia_id: dict = field(default_factory=dict)       # NOMBRE -> id
    tipo_pago_id: dict = field(default_factory=dict)       # M001 -> id
    banco_id: dict = field(default_factory=dict)           # NOMBRE -> id
    socio_id: dict = field(default_factory=dict)           # P001 -> id
    socio_apellidos: dict = field(default_factory=dict)    # id -> {'GARCIA', 'SERRANO'}
    concesion_id: dict = field(default_factory=dict)       # A002 -> id
    facturas_por_numero: dict = field(default_factory=lambda: defaultdict(list))  # numero -> [(id, socio_id)]
    socio_de_factura: dict = field(default_factory=dict)   # factura id -> socio id
    pagadores: dict = field(default_factory=lambda: defaultdict(set))  # P328 -> palabras de quienes pagaban
    incidencias: list = field(default_factory=list)

    def incidencia(self, tabla, clave, tipo, detalle, datos=None):
        if datos is not None:
            datos = json.dumps(datos, default=str, ensure_ascii=False)
        clave = None if clave is None else str(clave)[:60]
        nota = REVISADAS.get((tabla, (clave or "").split(" (fila")[0]))
        self.incidencias.append((tabla, clave, tipo, detalle, datos, nota is not None, nota,
                                 REVISADO_POR if nota else None, REVISADO_EN if nota else None))

    def resolver_socio(self, codigo, tabla, clave, nombre=None):
        """-> (socio_id, codigo_legado). Si el codigo no existe se conserva como texto."""
        codigo = mayus(codigo, 10)   # en FACTURAS/DEPOSITOS hay codigos en minuscula ('p214')
        if not codigo:
            return None, None
        sid = self.socio_id.get(codigo)
        if sid:
            return sid, None
        if codigo.isdigit():
            # '225' sin letra: se acepta P225/U225 solo si su apellido aparece en el nombre del documento
            candidatos = [c for c in (f"P{codigo.zfill(3)}", f"U{codigo.zfill(3)}") if c in self.socio_id]
            validos = [c for c in candidatos
                       if palabras(nombre) & self.socio_apellidos.get(self.socio_id[c], set())]
            if len(validos) == 1:
                self.incidencia(tabla, clave, "CORREGIDA",
                                f"Codigo '{codigo}' asociado a {validos[0]} (coincide el apellido con '{nombre}')")
                return self.socio_id[validos[0]], None
            # ... o si el nombre es el de quien pagaba las demas facturas de esa accion (titular de entonces)
            validos = [c for c in candidatos if len(palabras(nombre) & self.pagadores.get(c, set())) >= 2]
            if len(validos) == 1:
                self.incidencia(tabla, clave, "CORREGIDA",
                                f"Codigo '{codigo}' asociado a {validos[0]} ('{nombre}' es quien pagaba las demás "
                                f"facturas de esa acción en esa época)")
                return self.socio_id[validos[0]], None
        if not codigo.isdigit() and not re.fullmatch(r"[PU]\d+", codigo):
            # 'PP', 'PPP', 'T001': codigos que no siguen el formato de accion (P/U + numero)
            self.incidencia(tabla, clave, "CORREGIDA", f"Codigo '{codigo}' no es de socio: se registra como cliente no socio")
            return None, codigo
        self.incidencia(tabla, clave, "REF_NO_RESUELTA",
                        f"Socio '{codigo}' no existe y no se pudo verificar por el nombre")
        return None, codigo

    def resolver_pago(self, codigo, numero_factura, tabla, clave, monto, fecha_doc):
        """Cheques y depositos -> (socio_id, codigo_legado, factura_id). Si el codigo de socio del pago no es
        una accion ('1', '328', 'T001') y el pago corresponde a una factura, el socio es el de esa factura:
        en Access el codigo del pago se tecleaba a mano y el de la factura salia del formulario de cobro."""
        cod = mayus(codigo, 10)
        fid = self.resolver_factura(numero_factura, self.socio_id.get(cod or ""), tabla, clave,
                                    monto=monto, fecha_doc=fecha_doc)
        if cod and cod not in self.socio_id and fid is not None:
            sid = self.socio_de_factura.get(fid)
            accion = next((c for c, i in self.socio_id.items() if i == sid), None) if sid else None
            self.incidencia(tabla, clave, "CORREGIDA",
                            f"Codigo '{cod}' no es una acción: se toma el socio de la factura {numero_factura} "
                            f"({accion or 'sin socio'})")
            return sid, (None if sid else cod), fid
        sid, legado = self.resolver_socio(codigo, tabla, clave)
        return sid, legado, fid

    def resolver_tipo_pago(self, codigo, tabla, clave):
        codigo = txt(codigo, 4)
        if not codigo:
            return None
        tid = self.tipo_pago_id.get(codigo.upper())
        if not tid:
            self.incidencia(tabla, clave, "REF_NO_RESUELTA", f"Tipo de pago '{codigo}' no existe en TIPOSPAGOS")
        return tid

    def resolver_banco(self, nombre):
        n = mayus(nombre)
        if not n:
            return None
        return self.banco_id.get(BANCO_ALIAS.get(n, n))

    def resolver_factura(self, numero, socio_id, tabla, clave, registrar=True, monto=None, fecha_doc=None):
        """Los numeros de factura no son unicos en Access: si hay varias, desempata por
        socio, despues por monto y por ultimo por fecha."""
        numero = txt(numero, 10)
        if not numero:
            return None
        candidatas = self.facturas_por_numero.get(numero, [])
        filtros = (
            lambda c: c[1] == socio_id,
            lambda c: monto is not None and abs(c[2]) == abs(monto),
            lambda c: fecha_doc is not None and c[3] == fecha_doc,
        )
        for filtro in filtros:
            if len(candidatas) <= 1:
                break
            candidatas = [c for c in candidatas if filtro(c)] or candidatas
        if len(candidatas) == 1:
            return candidatas[0][0]
        if registrar:
            if candidatas:
                motivo = "ambigua: varias facturas con ese numero y mismo socio/monto/fecha"
            else:
                motivo = ("no existe en FACTURAS (eliminada en Access); el pago se conserva "
                          "con el numero como texto")
            self.incidencia(tabla, clave, "REF_NO_RESUELTA", f"Factura '{numero}' {motivo}")
        return None


# =============================================================================
# 1. EXTRACT
# =============================================================================

@dataclass
class TablaAccess:
    nombre: str
    columnas: list           # nombres originales
    tipos: list              # tipo python de cursor.description
    filas: list              # tuplas

    def dicts(self):
        for fila in self.filas:
            yield dict(zip(self.columnas, fila))


def extraer(ruta_accdb: str, pwd: str) -> dict[str, TablaAccess]:
    cadena = f"DRIVER={{Microsoft Access Driver (*.mdb, *.accdb)}};DBQ={ruta_accdb};PWD={pwd};"
    with pyodbc.connect(cadena, readonly=True, autocommit=True) as cn:
        cur = cn.cursor()
        nombres = [t.table_name for t in cur.tables(tableType="TABLE")]
        datos = {}
        for nombre in nombres:
            cur.execute(f"SELECT * FROM [{nombre}]")
            columnas = [d[0] for d in cur.description]
            tipos = [d[1] for d in cur.description]
            filas = [tuple(f) for f in cur.fetchall()]
            datos[nombre] = TablaAccess(nombre, columnas, tipos, filas)
            log.info("  extraida %-18s %6d filas", nombre, len(filas))
    return datos


# =============================================================================
# 2. LEGACY (copia 1:1)
# =============================================================================

TIPO_PG = {
    str: "text", bool: "boolean", int: "integer", float: "double precision",
    Decimal: "numeric(19,4)", datetime: "timestamp", date: "date",
    bytearray: "bytea", bytes: "bytea",
}


def cargar_legacy(pg: psycopg.Connection, datos: dict[str, TablaAccess]) -> None:
    with pg.cursor() as cur:
        for t in datos.values():
            tabla = snake(t.nombre)
            cols, usados = [], {"_fila_origen"}
            for c in t.columnas:
                n, i = snake(c), 2
                while n in usados:          # evita colisiones tras pasar a snake_case
                    n, i = f"{snake(c)}_{i}", i + 1
                usados.add(n)
                cols.append(n)
            ddl_cols = ", ".join(
                f'"{n}" {TIPO_PG.get(tp, "text")}' for n, tp in zip(cols, t.tipos)
            )
            cur.execute(f'DROP TABLE IF EXISTS legacy."{tabla}"')
            cur.execute(f'CREATE TABLE legacy."{tabla}" (_fila_origen integer PRIMARY KEY, {ddl_cols})')
            comentario = f"Copia 1:1 de la tabla Access [{t.nombre}]"
            cur.execute(sql.SQL("COMMENT ON TABLE legacy.{} IS {}").format(
                sql.Identifier(tabla), sql.Literal(comentario)))
            for n, original in zip(cols, t.columnas):
                cur.execute(sql.SQL("COMMENT ON COLUMN legacy.{}.{} IS {}").format(
                    sql.Identifier(tabla), sql.Identifier(n), sql.Literal(original)))
            lista = ", ".join(["_fila_origen"] + [f'"{n}"' for n in cols])
            with cur.copy(f'COPY legacy."{tabla}" ({lista}) FROM STDIN') as cp:
                for i, fila in enumerate(t.filas, 1):
                    cp.write_row((i, *fila))
            log.info("  legacy.%-20s %6d filas", tabla, len(t.filas))


# =============================================================================
# 3. CORE (transformaciones)
# =============================================================================

TABLAS_CORE = [
    "nivel_acceso", "usuario_sistema", "provincia", "tipo_pago", "banco",
    "configuracion", "calendario_cobro", "tarifa_mensual", "cuota_extra",
    "socio", "socio_mensualidad", "socio_cuota_extra", "beneficiario",
    "concesion", "concesion_mensualidad",
    "factura", "abono", "cheque_recibido", "deposito", "cheque_emitido", "libro_diario",
    "proveedor", "activo_fijo", "uso_salon", "migracion_incidencia",
]


def copiar(pg, tabla: str, columnas: list[str], filas: list[tuple]) -> int:
    if not filas:
        return 0
    with pg.cursor() as cur, cur.copy(
        f"COPY public.{tabla} ({', '.join(columnas)}) FROM STDIN"
    ) as cp:
        for f in filas:
            cp.write_row(f)
    log.info("  public.%-22s %6d filas", tabla, len(filas))
    return len(filas)


def t_catalogos(pg, d, ctx):
    # nivel_acceso
    filas = []
    for r in d["NIVEL"].dicts():
        if r["Nivel"] is None:
            continue
        ctx.nivel_ids.add(int(r["Nivel"]))
        filas.append((int(r["Nivel"]), txt(r["DescN"], 100) or f"Nivel {r['Nivel']}"))
    copiar(pg, "nivel_acceso", ["id", "descripcion"], filas)

    # usuario_sistema (sin la clave en texto plano)
    filas, vistos = [], set()
    for i, r in enumerate(d["USUARIOS"].dicts(), 1):
        usuario = txt(r["Usuario"], 30, alnum=False)
        if not usuario:
            ctx.incidencia("USUARIOS", f"fila {i}", "RECHAZADA", "Usuario vacio")
            continue
        if usuario.lower() in vistos:
            ctx.incidencia("USUARIOS", usuario, "DUPLICADA", "Usuario repetido")
            continue
        vistos.add(usuario.lower())
        nivel = r["Nivel"]
        if nivel not in ctx.nivel_ids:
            ctx.incidencia("USUARIOS", usuario, "CORREGIDA", f"Nivel {nivel} invalido; se asigna 1 (consulta)")
            nivel = 1
        filas.append((len(filas) + 1, usuario, txt(r["Nombre"], 150), cedula(r["CI"]), nivel))
    copiar(pg, "usuario_sistema", ["id", "username", "nombre", "cedula", "nivel_acceso_id"], filas)

    # provincia
    filas = []
    for r in d["PROVINCIAS"].dicts():
        nombre = txt(r["Provincia"], 50)
        if nombre and nombre.upper() not in ctx.provincia_id:
            ctx.provincia_id[nombre.upper()] = len(filas) + 1
            filas.append((len(filas) + 1, nombre))
    copiar(pg, "provincia", ["id", "nombre"], filas)

    # tipo_pago
    filas = []
    for r in d["TIPOSPAGOS"].dicts():
        codigo = mayus(r["COD"], 4)
        if not codigo or codigo in ctx.tipo_pago_id:
            ctx.incidencia("TIPOSPAGOS", codigo, "RECHAZADA", "Codigo vacio o repetido", r)
            continue
        ctx.tipo_pago_id[codigo] = len(filas) + 1
        filas.append((len(filas) + 1, codigo, txt(r["Descrip"], 100) or codigo))
    copiar(pg, "tipo_pago", ["id", "codigo", "descripcion"], filas)

    # banco (FLAG '2' = efectivo / cargo en cuenta)
    filas = []
    for r in d["BANCOS"].dicts():
        nombre = mayus(r["NomBanco"], 30)
        if not nombre or nombre in ctx.banco_id:
            continue
        ctx.banco_id[nombre] = len(filas) + 1
        filas.append((len(filas) + 1, nombre, txt(r["NumCuenta"], 30), dinero0(r["Saldo"]),
                      txt(r["FLAG"]) != "2"))
    for r in d["CHEQUES CG"].dicts():
        nombre = mayus(r["BANCO"], 30)
        if nombre and BANCO_ALIAS.get(nombre, nombre) not in ctx.banco_id:
            ctx.banco_id[nombre] = len(filas) + 1
            filas.append((len(filas) + 1, nombre, None, Decimal(0), True))
            ctx.incidencia("BANCOS", nombre, "CORREGIDA", "Banco usado en CHEQUES CG agregado al catalogo")
    copiar(pg, "banco", ["id", "nombre", "numero_cuenta", "saldo", "es_cuenta_bancaria"], filas)


def t_parametros(pg, d, ctx):
    montos = next(d["MONTOS"].dicts())
    ctx.periodo_actual = entero(montos["Periodo"]) or date.today().year
    copiar(pg, "configuracion",
           ["id", "periodo_actual", "mensualidad", "mensualidad_uso", "ultima_factura",
            "ultimo_numero_control", "ultimo_voucher", "total_acciones_propietario",
            "total_acciones_uso", "divisas_inicio", "fecha_inicio_divisas"],
           [(1, ctx.periodo_actual, dinero(montos["Mensual"]), dinero(montos["MensualUso"]),
             montos["UltFac"], montos["UltCont"], montos["UltV"], montos["TAcP"], montos["TAcU"],
             montos["DivInicio"], fecha(montos["FechaIniDiv"]))])

    # calendario_cobro: '31/01' -> dia 31
    filas = []
    for mes, col in enumerate(MESES_CORTE, 1):
        dia = entero((txt(montos[col]) or "").split("/")[0])
        if dia and 1 <= dia <= 31:
            filas.append((mes, dia))
    copiar(pg, "calendario_cobro", ["mes", "dia_corte"], filas)

    # tarifa_mensual: MONTOS (periodo actual, manda) + MONTOSAA (historico)
    tarifas = {}
    for mes, m in enumerate(MESES_CAP, 1):
        if montos[f"Mens{m}"] is not None:
            tarifas[(ctx.periodo_actual, mes)] = dinero(montos[f"Mens{m}"])
    for r in d["MONTOSAA"].dicts():
        anio = entero(r["Periodo"])
        if not anio:
            ctx.incidencia("MONTOSAA", r["Periodo"], "RECHAZADA", "Periodo invalido", r)
            continue
        for mes, m in enumerate(MESES_CAP, 1):
            if r[f"Mens{m}"] is not None:
                tarifas.setdefault((anio, mes), dinero(r[f"Mens{m}"]))
    copiar(pg, "tarifa_mensual", ["anio", "mes", "monto"],
           [(a, m, v) for (a, m), v in sorted(tarifas.items())])

    # cuota_extra 1..12 (id = numero)
    filas = [(n, n, txt(montos[f"Desc{n}"], 80), dinero0(montos[f"CuotaExtra{n}"]), fecha(montos[f"Fecha{n}"]))
             for n in range(1, 13)]
    copiar(pg, "cuota_extra", ["id", "numero", "descripcion", "monto", "fecha_vencimiento"], filas)


def t_socios(pg, d, ctx):
    filas, mensualidad, cuotas = [], {}, []
    for i, r in enumerate(d["SOCIOS"].dicts(), 1):
        codigo = mayus(r["IDSocio"], 4)
        if not codigo:
            ctx.incidencia("SOCIOS", f"fila {i}", "RECHAZADA", "IDSocio vacio", r)
            continue
        if codigo in ctx.socio_id:
            ctx.incidencia("SOCIOS", codigo, "DUPLICADA", "IDSocio repetido; se conserva el primero", r)
            continue
        sid = len(filas) + 1
        ctx.socio_id[codigo] = sid
        ctx.socio_apellidos[sid] = palabras(r["Apellidos"])

        tipo = "P" if r["TipoSocio"] else "U"
        if codigo[0] in "PU" and codigo[0] != tipo:
            ctx.incidencia("SOCIOS", codigo, "CORREGIDA", f"TipoSocio no coincide con el prefijo; se usa '{tipo}'")

        provincia_id = None
        if prov := txt(r["Provincia"]):
            provincia_id = ctx.provincia_id.get(prov.upper())
            if not provincia_id:
                ctx.incidencia("SOCIOS", codigo, "REF_NO_RESUELTA", f"Provincia '{prov}' desconocida")

        estado_civil = None
        if ec := mayus(r["EdoCivil"]):
            estado_civil = ESTADO_CIVIL.get(ec)
            if not estado_civil:
                ctx.incidencia("SOCIOS", codigo, "CORREGIDA", f"Estado civil '{ec}' no reconocido; queda NULL")

        observaciones = "\n".join(filter(None, (txt(r["Obs1"]), txt(r["Obs2"]), txt(r["Obs3"])))) or None

        filas.append((
            sid, codigo, tipo, bit(r["AcAdic"]), txt(r["Apellidos"], 150), txt(r["Nombres"], 150),
            cedula(r["CI"]), mayus(r["RH"], 10), fecha(r["FechaNac"]), txt(r["LugNac"], 255),
            mayus(r["Nac"], 30), provincia_id, bit(r["Gallego"]), bit(r["AscendGallego"]), estado_civil,
            txt(r["Profesion"], 30), txt(r["DirHab"], 255), txt(r["TelHab"], 15), txt(r["Cel"], 15),
            txt(r["email"], 60), txt(r["email2"], 60), txt(r["NomEmpr"], 80), txt(r["DirTrab"], 255),
            txt(r["DirWebTrab"], 30), txt(r["TelTrab"], 15), txt(r["Fax"], 15), txt(r["MarcaVeh"], 20),
            txt(r["PlacaVeh"], 10), txt(r["Aficiones"], 255), txt(r["TmEnCiud"], 20), txt(r["DomCob"], 255),
            fecha(r["FechIng"]), mayus(r["Ref1"], 10), mayus(r["Ref2"], 10),
            (mayus(r["Ex"]) or "").startswith("EXONER"), bit(r["ExT"]), observaciones,
            txt(r["RutaSocio"], 255), bit(r["Act"]), bit(r["Band"]),
        ))

        # Estado vivo del periodo actual: ENE..DIC + FENE..FDIC
        for mes, m in enumerate(MESES, 1):
            mensualidad[(sid, ctx.periodo_actual, mes)] = [bit(r[m]), txt(r[f"F{m}"], 10), None, "SOCIOS"]
        # Cuotas extraordinarias: Cuota1..12 + FC1..12
        for n in range(1, 13):
            pagada, factura = bit(r[f"Cuota{n}"]), txt(r[f"FC{n}"], 10)
            if pagada or factura:
                cuotas.append((sid, n, pagada, factura))

    copiar(pg, "socio",
           ["id", "codigo", "tipo_socio", "accion_adicional", "apellidos", "nombres", "cedula",
            "grupo_sanguineo", "fecha_nacimiento", "lugar_nacimiento", "nacionalidad", "provincia_id",
            "es_gallego", "ascendencia_gallega", "estado_civil", "profesion", "direccion_habitacion",
            "telefono_habitacion", "celular", "email", "email_alterno", "empresa", "direccion_trabajo",
            "web_trabajo", "telefono_trabajo", "fax", "marca_vehiculo", "placa_vehiculo", "aficiones",
            "tiempo_en_ciudad", "domicilio_cobro", "fecha_ingreso", "referencia_1", "referencia_2",
            "exonerado", "exonerado_total", "observaciones", "ruta_foto", "act_legado", "band_legado"],
           filas)
    copiar(pg, "socio_cuota_extra", ["socio_id", "cuota_extra_id", "pagada", "numero_factura"], cuotas)

    # Historico. Prioridad: SOCIOS (vivo) > RELSOLV > MOROSOS
    for tabla, col_anio in (("RELSOLV", "AÑO"), ("MOROSOS", "AñoDeuda")):
        vistos = {}
        for i, r in enumerate(d[tabla].dicts(), 1):
            codigo, anio = mayus(r["IDSocio"], 4), entero(r[col_anio])
            sid = ctx.socio_id.get(codigo)
            clave = f"{codigo}/{r[col_anio]}"
            if not sid or not anio:
                ctx.incidencia(tabla, clave, "RECHAZADA", "Socio inexistente o anio invalido", r)
                continue
            if (sid, anio) in vistos:
                if vistos[(sid, anio)] == r:
                    ctx.incidencia(tabla, clave, "CORREGIDA", "Fila duplicada identica descartada")
                else:
                    ctx.incidencia(tabla, clave, "DUPLICADA",
                                   "Socio/anio repetido con datos distintos; se conserva el primero", r)
                continue
            vistos[(sid, anio)] = r
            for mes, m in enumerate(MESES, 1):
                k = (sid, anio, mes)
                tarifa = dinero(r[f"Mens{MESES_CAP[mes - 1]}"]) if tabla == "MOROSOS" else None
                if k not in mensualidad:
                    mensualidad[k] = [bit(r[m]), txt(r[f"F{m}"], 10), tarifa, tabla]
                elif tarifa is not None and mensualidad[k][2] is None:
                    mensualidad[k][2] = tarifa

    copiar(pg, "socio_mensualidad",
           ["socio_id", "anio", "mes", "pagado", "numero_factura", "monto_tarifa", "origen"],
           [(s, a, m, *v) for (s, a, m), v in sorted(mensualidad.items())])


def t_beneficiarios(pg, d, ctx):
    filas, vistos = [], set()
    for i, r in enumerate(d["BENEFICIARIOS"].dicts(), 1):
        codigo = mayus(r["IdBen"], 7) or txt(r["IdBen"], 7, alnum=False)
        if codigo in CORRECCION_BENEFICIARIO and not mayus(r["IdSocio"]):
            socio_codigo, nuevo = CORRECCION_BENEFICIARIO[codigo]
            ctx.incidencia("BENEFICIARIOS", codigo, "CORREGIDA",
                           f"Sin socio en Access; asignado a {socio_codigo} con codigo {nuevo}")
            r = {**r, "IdSocio": socio_codigo}
            codigo = nuevo
        if not codigo or codigo in vistos:
            ctx.incidencia("BENEFICIARIOS", codigo or f"fila {i}", "RECHAZADA", "IdBen vacio o repetido", r)
            continue
        sid = ctx.socio_id.get(mayus(r["IdSocio"]) or "")
        if not sid:  # plan B: el prefijo de IdBen es el codigo del socio (P001-1 -> P001)
            prefijo = codigo.split("-")[0]
            sid = ctx.socio_id.get(prefijo)
            if sid:
                ctx.incidencia("BENEFICIARIOS", codigo, "CORREGIDA",
                               f"IdSocio '{r['IdSocio']}' inexistente; se asocia a {prefijo} por el prefijo de IdBen")
        if not sid:
            ctx.incidencia("BENEFICIARIOS", codigo, "RECHAZADA", f"Socio '{r['IdSocio']}' inexistente", r)
            continue
        vistos.add(codigo)
        parentesco = mayus(r["Parent"], 20)
        filas.append((codigo, sid, txt(r["NombyApel"], 255), cedula(r["CI"]),
                      PARENTESCO.get(parentesco, parentesco), fecha(r["FecNac"]), mayus(r["RH"], 10),
                      txt(r["Telf"], 15), txt(r["email"], 60), txt(r["MarcaVeh"]), txt(r["PlacVeh"], 10),
                      bit(r["Act"]), txt(r["RutaBen"], 255)))
    copiar(pg, "beneficiario",
           ["codigo", "socio_id", "nombre_completo", "cedula", "parentesco", "fecha_nacimiento",
            "grupo_sanguineo", "telefono", "email", "marca_vehiculo", "placa_vehiculo", "activo", "ruta_foto"],
           filas)


def t_concesiones(pg, d, ctx):
    filas = []
    for r in d["CONCESION"].dicts():
        codigo = mayus(r["CONC"], 4)
        tpid = ctx.tipo_pago_id.get(codigo or "")
        if not tpid:
            ctx.incidencia("CONCESION", codigo, "RECHAZADA", "CONC no existe en TIPOSPAGOS", r)
            continue
        cid = len(filas) + 1
        ctx.concesion_id[codigo] = cid
        filas.append((cid, tpid, txt(r["NOMCONC"], 255) or codigo, txt(r["RIF"], 20),
                      Decimal(repr(r["PORAP"] or 0)).quantize(Q4)))
    copiar(pg, "concesion", ["id", "tipo_pago_id", "nombre", "rif", "porcentaje_aporte"], filas)

    # Prioridad: CONCESION (periodo actual) > RELSOLVCONC > MORCONC
    mens = {}
    fuentes = (("CONCESION", None), ("RELSOLVCONC", "AÑO"), ("MORCONC", "AñoDeuda"))
    for tabla, col_anio in fuentes:
        for r in d[tabla].dicts():
            cid = ctx.concesion_id.get(mayus(r["CONC"]) or "")
            anio = ctx.periodo_actual if col_anio is None else entero(r[col_anio])
            if not cid or not anio:
                ctx.incidencia(tabla, r["CONC"], "RECHAZADA", "Concesion inexistente o anio invalido", r)
                continue
            for mes, m in enumerate(MESES, 1):
                mens.setdefault((cid, anio, mes), (bit(r[f"C{m}"]), txt(r[f"FC{m}"], 10), tabla))
    copiar(pg, "concesion_mensualidad",
           ["concesion_id", "anio", "mes", "pagado", "numero_factura", "origen"],
           [(c, a, m, *v) for (c, a, m), v in sorted(mens.items())])


def t_facturas(pg, d, ctx):
    # Quienes pagaban cada accion (sus titulares de entonces): para los codigos sin letra ('328')
    for r in d["FACTURAS"].dicts():
        codigo = mayus(r["IdSocio"], 10)
        if codigo in ctx.socio_id:
            ctx.pagadores[codigo] |= palabras(r["NomyApel"])
    filas = []
    for i, r in enumerate(d["FACTURAS"].dicts(), 1):
        fid = len(filas) + 1
        numero = txt(r["N°Fact"], 10)
        clave = f"{numero or '?'} (fila {i})"
        sid, legado = ctx.resolver_socio(r["IdSocio"], "FACTURAS", clave, nombre=r["NomyApel"])
        if numero:
            ctx.facturas_por_numero[numero].append((fid, sid, dinero0(r["Monto"]), fecha(r["FecGenFact"])))
        ctx.socio_de_factura[fid] = sid
        filas.append((
            fid, numero, txt(r["N°Cont"], 12), sid, legado, txt(r["NomyApel"], 150), mayus(r["CIoRIF"], 20),
            ctx.resolver_tipo_pago(r["TipPago"], "FACTURAS", clave), txt(r["DescPago"], 150),
            fecha(r["FecGenFact"]), fecha(r["FecTOPFact"]), fecha(r["FecCanc"]),
            dinero0(r["Monto"]), dinero0(r["MonCh"]), dinero0(r["MonDep"]), dinero0(r["MonTarDeb"]),
            dinero0(r["MonTarCred"]), dinero0(r["MonEf"]), entero(r["NumCuo"]),
            bit(r["Estado"]), bit(r["Ap"]), bit(r["Nula"]), fecha(r["FecNula"]),
            bit(r["PagoDiv"]), dinero(r["MontoDiv"]),
        ))
    copiar(pg, "factura",
           ["id", "numero", "numero_control", "socio_id", "codigo_socio_legado", "cliente_nombre",
            "cliente_rif", "tipo_pago_id", "descripcion", "fecha_emision", "fecha_tope",
            "fecha_cancelacion", "monto", "monto_cheque", "monto_deposito", "monto_tarjeta_debito",
            "monto_tarjeta_credito", "monto_efectivo", "numero_cuotas", "estado", "es_aporte",
            "anulada", "fecha_anulacion", "pago_divisas", "monto_divisas"],
           filas)


def t_cobros(pg, d, ctx):
    # abono
    filas = []
    for i, r in enumerate(d["ABONOS"].dicts(), 1):
        numero = txt(r["N°Fact"], 10)
        clave = f"{numero or '?'} (fila {i})"
        sid = ctx.socio_id.get(mayus(r["IdSocio"]) or "")
        if not sid or not numero:
            ctx.incidencia("ABONOS", clave, "RECHAZADA", "Socio inexistente o documento vacio", r)
            continue
        fact_c = txt(r["FactC"], 10)
        # FactC numerico -> factura; "NCmmaa" -> otra nota de credito (no es FK)
        factura_id = ctx.resolver_factura(fact_c, sid, "ABONOS", clave, registrar=bool(fact_c and fact_c.isdigit()),
                                          monto=dinero0(r["MontoAbo"]))
        filas.append((
            numero, sid, txt(r["NomyApel"], 150), mayus(r["CIoRIF"], 20),
            ctx.resolver_tipo_pago(r["TipPago"], "ABONOS", clave), txt(r["DescPago"], 150),
            fecha(r["FecGenFact"]), fecha(r["FecCanc"]), dinero0(r["MontoAbo"]), dinero0(r["MonCh"]),
            dinero0(r["MonDep"]), dinero0(r["MonTarDeb"]), dinero0(r["MonTarCred"]), dinero0(r["MonEf"]),
            bit(r["Estado"]), bit(r["Nula"]), fecha(r["FecNula"]), fact_c, factura_id, entero(r["AñoDeuda"]),
        ))
    copiar(pg, "abono",
           ["numero_documento", "socio_id", "cliente_nombre", "cliente_rif", "tipo_pago_id", "descripcion",
            "fecha_emision", "fecha_cancelacion", "monto", "monto_cheque", "monto_deposito",
            "monto_tarjeta_debito", "monto_tarjeta_credito", "monto_efectivo", "estado", "anulado",
            "fecha_anulacion", "numero_factura_relacionada", "factura_relacionada_id", "anio_deuda"],
           filas)

    # cheque_recibido
    filas = []
    for i, r in enumerate(d["CHEQUES"].dicts(), 1):
        clave = f"{txt(r['N°Cheque']) or '?'} (fila {i})"
        numero_factura = txt(r["N°Fact"], 10)
        sid, legado, fid = ctx.resolver_pago(r["IDSocio"], numero_factura, "CHEQUES", clave,
                                             dinero0(r["Monto"]), fecha(r["FechaPago"]))
        filas.append((sid, legado, txt(r["N°Cheque"], 15), txt(r["N°Cuenta"], 20), mayus(r["NombreBanco"], 255),
                      fecha(r["FechaPago"]), txt(r["Descripcion"], 150), dinero0(r["Monto"]), numero_factura, fid))
    copiar(pg, "cheque_recibido",
           ["socio_id", "codigo_socio_legado", "numero_cheque", "numero_cuenta", "banco_emisor",
            "fecha_pago", "descripcion", "monto", "numero_factura", "factura_id"],
           filas)

    # deposito
    filas = []
    for i, r in enumerate(d["DEPOSITOS"].dicts(), 1):
        clave = f"{txt(r['N°Dep']) or '?'} (fila {i})"
        numero_factura = txt(r["N°Fact"], 10)
        sid, legado, fid = ctx.resolver_pago(r["IDSocio"], numero_factura, "DEPOSITOS", clave,
                                             dinero0(r["Monto"]), fecha(r["FechaPago"]))
        filas.append((sid, legado, txt(r["N°Dep"], 20), ctx.resolver_banco(r["NombreBanco"]),
                      txt(r["NombreBanco"], 255), fecha(r["FechaPago"]), txt(r["Descripcion"], 150),
                      dinero0(r["Monto"]), numero_factura, fid))
    copiar(pg, "deposito",
           ["socio_id", "codigo_socio_legado", "numero_deposito", "banco_id", "banco_nombre", "fecha_pago",
            "descripcion", "monto", "numero_factura", "factura_id"],
           filas)

    # cheque_emitido (CHEQUES CG)
    filas, cheques = [], set()
    for i, r in enumerate(d["CHEQUES CG"].dicts(), 1):
        numero = txt(r["NumeroCheque"], 15)
        if numero and numero in cheques:
            ctx.incidencia("CHEQUES CG", numero, "DUPLICADA", "NumeroCheque repetido; se carga sin numero", r)
            numero = None
        if numero:
            cheques.add(numero)
        banco_nombre = mayus(r["BANCO"], 30)
        banco_id = ctx.resolver_banco(banco_nombre)
        if banco_nombre and not banco_id:
            ctx.incidencia("CHEQUES CG", numero or f"fila {i}", "REF_NO_RESUELTA",
                           f"Banco '{banco_nombre}' no esta en BANCOS")
        lugar = (txt(r["LUGAR"]) or "").rstrip(", ")[:20] or None   # 'Lechería, ' -> 'Lechería'
        filas.append((fecha(r["FechaHoy"]), numero, txt(r["NumeroCuenta"], 20), banco_id,
                      banco_nombre, txt(r["BENEFICIARIO"], 50), dinero0(r["MONTO"]), txt(r["ConceptoPago"], 250),
                      txt(r["MONTO EN LETRAS"], 95, alnum=False), txt(r["MONTO EN LETRAS 2"], 30, alnum=False),
                      txt(r["NÚMERO DE VAUCHER"], 10), lugar,
                      mayus(r["Preparado"], 10), bit(r["Nulidad"]), bit(r["PagoDiv"]), dinero(r["MontoDiv"])))
    copiar(pg, "cheque_emitido",
           ["fecha", "numero_cheque", "numero_cuenta", "banco_id", "banco_nombre", "beneficiario", "monto",
            "concepto", "monto_en_letras", "monto_en_letras_2", "numero_voucher", "lugar", "preparado_por", "anulado",
            "pago_divisas", "monto_divisas"],
           filas)

    # libro_diario (LIBDIA)
    filas = []
    for i, r in enumerate(d["LIBDIA"].dicts(), 1):
        numero = txt(r["N°Fact"], 10)
        clave = f"{numero or '?'} (fila {i})"
        sid, legado = ctx.resolver_socio(r["IdSocio"], "LIBDIA", clave, nombre=r["NomyApel"])
        filas.append((
            numero, ctx.resolver_factura(numero, sid, "LIBDIA", clave, monto=dinero0(r["Monto"])), sid, legado,
            txt(r["NomyApel"], 150), mayus(r["CIoRIF"], 20), ctx.resolver_tipo_pago(r["TipPago"], "LIBDIA", clave),
            txt(r["DescPago"], 150), fecha(r["FecGenFact"]), fecha(r["FecTOPFact"]), fecha(r["FecCanc"]),
            dinero0(r["Monto"]), dinero0(r["MonCh"]), dinero0(r["MonDep"]), dinero0(r["MonTarDeb"]),
            dinero0(r["MonTarCred"]), dinero0(r["MonEf"]), entero(r["NumCuo"]), bit(r["Estado"]),
            bit(r["Nula"]), fecha(r["FecNula"]), txt(r["N°Vauch"], 10), dinero(r["PagCh"]), dinero(r["PagEf"]),
            dinero(r["PagCC"]), mayus(r["BANCO"], 25),
        ))
    copiar(pg, "libro_diario",
           ["numero_factura", "factura_id", "socio_id", "codigo_socio_legado", "cliente_nombre", "cliente_rif",
            "tipo_pago_id", "descripcion", "fecha_emision", "fecha_tope", "fecha_cancelacion", "monto",
            "monto_cheque", "monto_deposito", "monto_tarjeta_debito", "monto_tarjeta_credito", "monto_efectivo",
            "numero_cuotas", "estado", "anulada", "fecha_anulacion", "numero_voucher", "pago_cheque",
            "pago_efectivo", "pago_cargo_cuenta", "banco_nombre"],
           filas)


def t_otros(pg, d, ctx):
    filas, codigos = [], set()
    for i, r in enumerate(d["PROVEEDOR"].dicts(), 1):
        nombre = txt(r["NombEmp"], 100)
        if not any(txt(v) for v in r.values()):
            continue  # fila completamente vacia en Access: no es un dato
        if not nombre:
            ctx.incidencia("PROVEEDOR", f"fila {i}", "RECHAZADA", "Proveedor sin nombre", r)
            continue
        codigo = txt(r["Cod"], 5)
        if codigo in codigos:
            ctx.incidencia("PROVEEDOR", codigo, "DUPLICADA", "Codigo repetido; se carga sin codigo", r)
            codigo = None
        codigos.add(codigo)
        filas.append((codigo, nombre, txt(r["Suministro"], 255), txt(r["Direc"], 150), txt(r["Telf"], 15),
                      txt(r["Repres"], 100), txt(r["Cel"], 15)))
    copiar(pg, "proveedor",
           ["codigo", "nombre_empresa", "suministro", "direccion", "telefono", "representante", "celular"], filas)

    filas = []
    for i, r in enumerate(d["ACTIVOS"].dicts(), 1):
        descripcion = txt(r["Desc"], 255)
        if not descripcion:
            ctx.incidencia("ACTIVOS", f"fila {i}", "RECHAZADA", "Activo sin descripcion", r)
            continue
        filas.append((txt(r["Cod"], 5), descripcion, r["Cant"] if r["Cant"] is not None else 1,
                      txt(r["Ubic"], 150), fecha(r["FechaIng"]), fecha(r["FechaSal"])))
    copiar(pg, "activo_fijo",
           ["codigo", "descripcion", "cantidad", "ubicacion", "fecha_ingreso", "fecha_salida"], filas)

    filas = []
    for i, r in enumerate(d["USOSALON"].dicts(), 1):
        sid = ctx.socio_id.get(mayus(r["IDSocio"]) or "")
        if not sid or not fecha(r["FechaUso"]):
            ctx.incidencia("USOSALON", f"fila {i}", "RECHAZADA", "Socio inexistente o fecha vacia", r)
            continue
        filas.append((sid, fecha(r["FechaUso"]), txt(r["Obs"], 150)))
    copiar(pg, "uso_salon", ["socio_id", "fecha_uso", "observacion"], filas)


def cargar_core(pg, d: dict[str, TablaAccess]) -> Ctx:
    ctx = Ctx()
    with pg.cursor() as cur:
        cur.execute("TRUNCATE " + ", ".join(f"public.{t}" for t in TABLAS_CORE) + " RESTART IDENTITY CASCADE")
    t_catalogos(pg, d, ctx)
    t_parametros(pg, d, ctx)
    t_socios(pg, d, ctx)
    t_beneficiarios(pg, d, ctx)
    t_concesiones(pg, d, ctx)
    t_facturas(pg, d, ctx)
    t_cobros(pg, d, ctx)
    t_otros(pg, d, ctx)
    copiar(pg, "migracion_incidencia", ["tabla_origen", "clave_origen", "tipo", "detalle", "datos", "resuelta",
                                         "nota_resolucion", "resuelta_por", "resuelta_en"], ctx.incidencias)

    # Los ids se asignaron desde Python: alinear las secuencias IDENTITY para que Django siga numerando
    with pg.cursor() as cur:
        cur.execute("""SELECT table_name FROM information_schema.columns
                       WHERE table_schema = 'public' AND column_name = 'id' AND is_identity = 'YES'""")
        for (t,) in cur.fetchall():
            cur.execute(f"SELECT setval(pg_get_serial_sequence('public.{t}', 'id'), "
                        f"COALESCE((SELECT MAX(id) FROM public.{t}), 0) + 1, false)")
    return ctx


# =============================================================================
# 4. VALIDACION
# =============================================================================

def validar(pg, d: dict[str, TablaAccess]) -> bool:
    def suma_origen(tabla, col):
        t = d[tabla]
        i = t.columnas.index(col)
        return sum((dinero0(f[i]) for f in t.filas), Decimal(0))

    controles = [  # (tabla Access, columna importe, tabla destino, columna destino)
        ("FACTURAS", "Monto", "factura", "monto"),
        ("ABONOS", "MontoAbo", "abono", "monto"),
        ("CHEQUES", "Monto", "cheque_recibido", "monto"),
        ("DEPOSITOS", "Monto", "deposito", "monto"),
        ("CHEQUES CG", "MONTO", "cheque_emitido", "monto"),
        ("LIBDIA", "Monto", "libro_diario", "monto"),
    ]
    ok = True
    log.info("")
    log.info("%-12s %8s %8s %8s %22s %22s", "ORIGEN", "FILAS", "DESTINO", "RECHAZ.", "SUMA ORIGEN", "SUMA DESTINO")
    with pg.cursor() as cur:
        for tabla, col, destino, col_dest in controles:
            cur.execute(f"SELECT COUNT(*), COALESCE(SUM({col_dest}), 0) FROM public.{destino}")
            n_dest, suma_dest = cur.fetchone()
            cur.execute("SELECT COUNT(*) FROM public.migracion_incidencia WHERE tabla_origen = %s AND tipo = 'RECHAZADA'",
                        (tabla,))
            rechazadas = cur.fetchone()[0]
            n_orig, suma_orig = len(d[tabla].filas), suma_origen(tabla, col)
            cuadra = n_orig == n_dest + rechazadas and (rechazadas or suma_orig == suma_dest)
            ok &= cuadra
            log.info("%-12s %8d %8d %8d %22s %22s %s", tabla, n_orig, n_dest, rechazadas,
                     f"{suma_orig:,.2f}", f"{suma_dest:,.2f}", "OK" if cuadra else "<-- REVISAR")

        log.info("")
        cur.execute("""SELECT tabla_origen, tipo, COUNT(*), COUNT(*) FILTER (WHERE NOT resuelta)
                         FROM public.migracion_incidencia GROUP BY 1, 2 ORDER BY 1, 2""")
        for tabla, tipo, n, pendientes in cur.fetchall():
            log.info("  incidencias %-14s %-16s %6d  (sin revisar: %d)", tabla, tipo, n, pendientes)
    return ok


# =============================================================================
# main
# =============================================================================

def main() -> int:
    ap = argparse.ArgumentParser(description="Migra SICGA de Access a PostgreSQL")
    ap.add_argument("--crear-esquema", action="store_true", help="ejecuta 01_schema.sql antes de cargar")
    ap.add_argument("--sin-legacy", action="store_true", help="no recrea el esquema legacy")
    ap.add_argument("--dry-run", action="store_true", help="ejecuta todo y hace ROLLBACK al final")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    cargar_dotenv(BASE_DIR / ".env")
    ruta_accdb = env_obligatoria("ACCESS_DB_PATH")
    pwd = env_obligatoria("ACCESS_PWD")
    dsn = env_obligatoria("PG_DSN")

    log.info("1/4 Extrayendo %s", ruta_accdb)
    datos = extraer(ruta_accdb, pwd)

    with psycopg.connect(dsn, autocommit=False) as pg:
        try:
            if args.crear_esquema:
                log.info("    Creando esquema (01_schema.sql)")
                sql = (BASE_DIR / "01_schema.sql").read_text(encoding="utf-8")
                sql = sql.replace("BEGIN;", "").replace("COMMIT;", "")  # usamos la transaccion del ETL
                pg.execute(sql)
            if not args.sin_legacy:
                log.info("2/4 Cargando esquema legacy")
                cargar_legacy(pg, datos)
            log.info("3/4 Transformando y cargando esquema public")
            ctx = cargar_core(pg, datos)
            log.info("4/4 Validando (%d incidencias registradas)", len(ctx.incidencias))
            ok = validar(pg, datos)
        except Exception:
            pg.rollback()
            log.exception("Error: se hizo ROLLBACK, la base de datos no cambio")
            return 1

        if args.dry_run:
            pg.rollback()
            log.info("--dry-run: ROLLBACK ejecutado, no se guardo nada")
        else:
            pg.commit()
            log.info("COMMIT realizado")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
