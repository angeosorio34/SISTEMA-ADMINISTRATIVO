"""
Catalogo de informes impresos de SICGA (listados de Access).

Cada informe usa su plantilla original (plantillas/<Informe>.txt) y una funcion que entrega
las filas con los nombres de campo que esperaba Access. Lo que en Access calculaba el VBA de
cada informe (morosidad, totales, mostrar/ocultar controles) se calcula aqui, una sola vez:

  * Access sumaba los totales en los eventos Print/Format de cada seccion. Esos eventos se
    disparan varias veces cuando Access recoloca paginas, por lo que los totales podian salir
    duplicados. Aqui se calculan sobre los datos del grupo (`pies`).
  * Los listados de morosos no excluian a los socios al dia ni a los exonerados; aqui si.

Uso: generar("morosos", {"anio": "2026", "hasta_mes": "10"}) -> bytes del PDF.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from decimal import Decimal, InvalidOperation
from typing import Callable

from django.db import connection

from .motor import InformeAccess, MESES

MES_COL = ["ENE", "FEB", "MAR", "ABR", "MAY", "JUN", "JUL", "AGO", "SEP", "OCT", "NOV", "DIC"]
MES_CAP = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
# Nombres de mes que usaba el VBA de los informes de morosidad
MES_VBA = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "septiembre",
           "octubre", "noviembre", "diciembre"]


@dataclass
class Parametro:
    nombre: str
    etiqueta: str
    tipo: str                      # fecha | anio | mes | entero | dinero | socio | concesion
    obligatorio: bool = True


@dataclass
class Listado:
    clave: str
    titulo: str
    grupo: str
    informe: str
    datos: Callable[[dict], "Resultado"]
    parametros: list[Parametro] = field(default_factory=list)
    descripcion: str = ""


@dataclass
class Resultado:
    filas: list[dict]
    formularios: dict = field(default_factory=dict)          # [Forms]![...]![X] -> valor
    pies: dict = field(default_factory=dict)                 # control sin origen -> f(filas)
    visibilidad: dict = field(default_factory=dict)          # control -> bool | f(campos)
    subinformes: dict = field(default_factory=dict)          # control subinforme -> {campo: valor}


# =============================================================================
# Utilidades
# =============================================================================

def _sql(consulta: str, params: dict | None = None) -> list[dict]:
    with connection.cursor() as cur:
        cur.execute(consulta, params or {})
        columnas = [c[0] for c in cur.description]
        return [dict(zip(columnas, fila)) for fila in cur.fetchall()]


def _suma(campo: str, condicion: Callable[[dict], bool] = lambda f: True):
    return lambda filas: sum((Decimal(f.get(campo) or 0) for f in filas if condicion(f)), Decimal(0))


def _cuenta(condicion: Callable[[dict], bool] = lambda f: True):
    return lambda filas: sum(1 for f in filas if condicion(f))


def _rango(p: dict) -> dict:
    """Fecha1/Fecha2 (MenuContabilidad) y FechIni/FechaFin (MenuCheques) como en Access."""
    return {"Fecha1": p["desde"], "Fecha2": p["hasta"], "FechIni": p["desde"], "FechaFin": p["hasta"]}


def _vis_rango(p: dict, dia: str = "Dia", rango: str = "EntreFechas", ocultar_si_dia=()) -> dict:
    """Report_Load de Access: un solo dia -> muestra 'Dia'; si no, 'EntreFechas'."""
    un_dia = p["desde"] == p["hasta"]
    # "el:" acompana a la fecha de un solo dia; el VBA de Access lo hacia al reves y con un rango
    # imprimia "el: entre el: ... y el: ..."
    vis = {dia: un_dia, rango: not un_dia, "Etiqueta52": un_dia}
    for nombre in ocultar_si_dia:
        vis[nombre] = not un_dia
    return vis


SOCIO_BASE = """
    SELECT s.id, s.tipo_socio = 'P' AS "TipoSocio", left(s.codigo, 1) AS "TS", s.codigo AS "IDSocio",
           s.apellidos AS "Apellidos", s.nombres AS "Nombres", s.cedula AS "CI",
           s.telefono_habitacion AS "TelHab", s.celular AS "Cel", s.email AS "email", s.email_alterno AS "email2",
           s.telefono_trabajo AS "TelTrab", s.fecha_ingreso AS "FechIng", s.accion_adicional AS "AcAdic",
           s.es_gallego AS "Gallego", s.ascendencia_gallega AS "AscendGallego", p.nombre AS "Provincia",
           s.lugar_nacimiento AS "LugNac", s.fecha_nacimiento AS "FechaNac",
           CASE WHEN s.exonerado THEN 'EXONERADA' END AS "Ex", s.exonerado_total AS "ExT",
           s.band_legado AS "Band"
      FROM socio s LEFT JOIN provincia p ON p.id = s.provincia_id
"""


# =============================================================================
# Socios
# =============================================================================

def d_socios(p):
    return Resultado(_sql(SOCIO_BASE + " WHERE s.apellidos IS NOT NULL ORDER BY s.codigo"))


def d_beneficiarios(p):
    return Resultado(_sql("""
        SELECT s.codigo AS "IDSocio", b.codigo AS "IdBen", b.nombre_completo AS "NombyApel", b.cedula AS "CI",
               b.parentesco AS "Parent", b.fecha_nacimiento AS "FecNac", b.grupo_sanguineo AS "RH",
               b.telefono AS "Telf", b.marca_vehiculo AS "MarcaVeh", b.placa_vehiculo AS "PlacVeh", b.activo AS "Act"
          FROM beneficiario b JOIN socio s ON s.id = b.socio_id
         WHERE b.activo ORDER BY s.codigo, b.codigo"""))


def d_acciones_libres(p):
    return Resultado(_sql(SOCIO_BASE + " WHERE s.nombres IS NULL ORDER BY s.codigo"))


def d_socios_provincia(p):
    return Resultado(_sql(SOCIO_BASE + " WHERE s.apellidos IS NOT NULL AND s.provincia_id IS NOT NULL ORDER BY p.nombre, s.codigo"))


def d_socios_gallegos(p):
    filas = _sql(SOCIO_BASE + """ WHERE NOT s.accion_adicional AND s.apellidos IS NOT NULL
                                    AND (s.es_gallego OR s.ascendencia_gallega) ORDER BY s.codigo""")
    return Resultado(filas, pies={"TotG": _cuenta(lambda f: f["Gallego"]), "TotAG": _cuenta(lambda f: f["AscendGallego"])})


def d_cumpleanos(p):
    filas = _sql(SOCIO_BASE + """ WHERE s.apellidos IS NOT NULL AND extract(month FROM s.fecha_nacimiento) = %(mes)s
                                    ORDER BY extract(day FROM s.fecha_nacimiento), s.codigo""", {"mes": p["mes"]})
    for f in filas:
        f["Mes"] = f"{p['mes']:02d}"
        f["Día"] = f["FechaNac"].strftime("%d/%m") if f["FechaNac"] else ""
    return Resultado(filas, formularios={"MES": f"{p['mes']:02d}"})


def d_socios_fecha_ingreso(p):
    return Resultado(_sql(SOCIO_BASE + """ WHERE NOT s.accion_adicional AND s.apellidos IS NOT NULL
                                             AND s.nombres IS NOT NULL ORDER BY s.fecha_ingreso, s.codigo"""))


def d_acciones_exoneradas(p):
    filas = _sql(SOCIO_BASE + " WHERE s.exonerado ORDER BY s.codigo")
    return Resultado(filas, visibilidad={"TEMP": lambda c: bool(c.get("ExT"))})


def d_gallegos_y_beneficiarios(p):
    """TempTotalSOCIOS de Access: titulares + beneficiarios, con su condicion de gallego."""
    filas = _sql("""
        SELECT s.codigo AS "IDSocio", s.accion_adicional AS "AcAdic", NULL AS "IDBen", s.apellidos AS "Apellidos",
               s.nombres AS "Nombres", s.cedula AS "CI", s.fecha_ingreso AS "FechIng", 'TITULAR' AS "Titularidad",
               p.nombre AS "Provincia", s.lugar_nacimiento AS "LugNac", s.es_gallego AS "Gallego",
               s.ascendencia_gallega AS "AscendGallego", NULL AS "Parent", 0 AS orden
          FROM socio s LEFT JOIN provincia p ON p.id = s.provincia_id WHERE s.apellidos IS NOT NULL
        UNION ALL
        SELECT s.codigo, s.accion_adicional, b.codigo, b.nombre_completo, '(B)', b.cedula, NULL, NULL, NULL, NULL,
               false, false, b.parentesco, 1
          FROM beneficiario b JOIN socio s ON s.id = b.socio_id WHERE b.activo
         ORDER BY 1, 14, 3""")
    for f in filas:
        f["Galicidad"] = "DESCENDIENTE" if f["AscendGallego"] else "GALLEGO" if f["Gallego"] else " "
    return Resultado(filas, pies={"TotG": _cuenta(lambda f: f["Gallego"]), "TotAG": _cuenta(lambda f: f["AscendGallego"])})


def d_uso_salon(p):
    filas = _sql("""
        SELECT s.codigo AS "IDSocio", s.apellidos AS "Apellidos", s.nombres AS "Nombres", s.cedula AS "CI",
               s.telefono_habitacion AS "TelHab", s.celular AS "Cel", u.fecha_uso AS "FechaUso", u.observacion AS "Obs"
          FROM uso_salon u JOIN socio s ON s.id = u.socio_id
         WHERE extract(year FROM u.fecha_uso) = %(anio)s ORDER BY s.codigo, u.fecha_uso""", {"anio": p["anio"]})
    return Resultado(filas, formularios={"AÑO": str(p["anio"])})


# =============================================================================
# Morosidad
# =============================================================================

def _estado_meses(anio: int) -> list[dict]:
    """Socios con sus 12 meses del anio: pagado y monto de la tarifa (o el del reporte de morosos)."""
    return _sql(SOCIO_BASE.replace("FROM socio s", """,
               coalesce(json_agg(m.mes ORDER BY m.mes) FILTER (WHERE m.pagado), '[]') AS pagados,
               json_agg(m.mes ORDER BY m.mes) AS registrados,
               json_object_agg(m.mes, coalesce(m.monto_tarifa, t.monto, 0)) AS montos
          FROM socio s JOIN socio_mensualidad m ON m.socio_id = s.id AND m.anio = %(anio)s
          LEFT JOIN tarifa_mensual t ON t.anio = m.anio AND t.mes = m.mes""") + """
         GROUP BY s.id, p.nombre ORDER BY s.codigo""", {"anio": anio})


def _fila_morosidad(f: dict, anio: int, hasta_mes: int) -> dict | None:
    pagados = set(f.pop("pagados") or [])
    registrados = set(f.pop("registrados") or [])
    montos = {int(k): Decimal(str(v)) for k, v in (f.pop("montos") or {}).items()}
    pendientes = [m for m in range(1, hasta_mes + 1) if m in registrados and m not in pagados]
    for i, (col, vba) in enumerate(zip(MES_COL, MES_VBA), 1):
        f[col] = f[vba] = i in pagados
        f[f"Mens{MES_CAP[i - 1]}"] = montos.get(i)
    # Corrige Access: ni exonerados ni acciones libres (sin titular) cuentan como morosos. Como en
    # Access, tampoco las que no pagan cuota de mantenimiento (Band = No, p. ej. acciones adicionales)
    if not pendientes or f["Ex"] or f["ExT"] or not f["Nombres"] or not f["Band"]:
        return None
    f["CANTMESES"] = len(pendientes)
    f["MesesDeuda"] = ", ".join(MES_COL[m - 1] for m in pendientes)
    f["TotalMorosidad"] = sum((montos.get(m, Decimal(0)) for m in pendientes), Decimal(0))
    f["TotDeuda"] = f["TotalMorosidad"]
    f["AñoDeuda"] = str(anio)
    f["TotSoc"] = 1
    return f


def _tarifas(anio: int) -> dict:
    return {f"Mens{MES_CAP[m - 1]}": monto for m, monto in
            _sql_valores("SELECT mes, monto FROM tarifa_mensual WHERE anio = %(anio)s", {"anio": anio})}


def _sql_valores(consulta, params):
    with connection.cursor() as cur:
        cur.execute(consulta, params)
        return cur.fetchall()


def d_morosos(p):
    anio, hasta = p["anio"], p["hasta_mes"]
    filas = [r for f in _estado_meses(anio) if (r := _fila_morosidad(f, anio, hasta))]
    return Resultado(filas, formularios={**_tarifas(anio), "Marco55": hasta, "Periodo": str(anio)},
                     pies={"TOTAL": _suma("TotalMorosidad")})


def d_morosos_anios_anteriores(p):
    """ListaSociosMorAA: deuda de los anios anteriores al periodo (todos los meses del anio)."""
    filas = []
    for (anio,) in _sql_valores("SELECT DISTINCT anio FROM socio_mensualidad WHERE anio < %(p)s ORDER BY anio",
                                {"p": p["anio"]}):
        filas += [r for f in _estado_meses(anio) if (r := _fila_morosidad(f, anio, 12))]
    filas.sort(key=lambda f: (f["TS"], f["IDSocio"], f["AñoDeuda"]))
    return Resultado(filas, formularios={"Marco55": 12}, pies={"TOTAL": _suma("TotalMorosidad")})


def d_morosos_cuota_extra(p):
    hasta = p["hasta_cuota"]
    cuotas = {n: (m, d) for n, m, d in _sql_valores("SELECT numero, monto, descripcion FROM cuota_extra", {})}
    filas = _sql(SOCIO_BASE.replace("FROM socio s", """,
               coalesce(json_agg(ce.numero) FILTER (WHERE sce.pagada), '[]') AS pagadas
          FROM socio s LEFT JOIN socio_cuota_extra sce ON sce.socio_id = s.id
          LEFT JOIN cuota_extra ce ON ce.id = sce.cuota_extra_id""") + """
         WHERE s.apellidos IS NOT NULL GROUP BY s.id, p.nombre ORDER BY s.codigo""")
    resultado = []
    for f in filas:
        pagadas = set(f.pop("pagadas") or [])
        pendientes = [n for n in range(1, hasta + 1) if n not in pagadas and cuotas.get(n, (0,))[0]]
        for n in range(1, 13):
            f[f"Cuota{n}"] = n in pagadas
        if not pendientes or f["Ex"] or f["ExT"] or not f["Nombres"]:
            continue
        f["CANTCUOT"] = len(pendientes)
        f["TotalMorosidad"] = sum((Decimal(cuotas[n][0]) for n in pendientes), Decimal(0))
        f["TotSoc"] = 1
        resultado.append(f)
    formularios = {"Marco113": hasta}
    for n, (monto, desc) in cuotas.items():
        formularios[f"CuotaExtra{n}"], formularios[f"Desc{n}"] = monto, desc
    return Resultado(resultado, formularios=formularios, pies={"TOTAL": _suma("TotalMorosidad")})


def _relacion_por_accion(p, solo_morosos_6=False):
    filas = _sql("""
        SELECT s.codigo AS "IDSocio", s.codigo AS "IdSocio", s.apellidos AS "Apellidos", s.nombres AS "Nombres",
               m.anio::text AS "AÑO", CASE WHEN s.exonerado THEN 'EXONERADA' END AS "Ex",
               coalesce(json_agg(m.mes ORDER BY m.mes) FILTER (WHERE m.pagado), '[]') AS pagados,
               json_object_agg(m.mes, coalesce(m.numero_factura, '')) AS facturas
          FROM socio_mensualidad m JOIN socio s ON s.id = m.socio_id
         WHERE (%(anio)s = 0 OR m.anio = %(anio)s)
         GROUP BY s.id, m.anio ORDER BY s.codigo, m.anio""", {"anio": p.get("anio") or 0})
    periodo, hasta = p.get("periodo"), p.get("hasta_mes", 12)
    for f in filas:
        pagados = set(f.pop("pagados") or [])
        facturas = f.pop("facturas") or {}
        for i, col in enumerate(MES_COL, 1):
            f[col] = i in pagados
            f[f"F{col}"] = facturas.get(str(i)) or None
        limite = hasta if periodo and int(f["AÑO"]) == periodo else 12
        f["CantMes"] = sum(1 for i in range(1, limite + 1) if i not in pagados)
    if solo_morosos_6:
        total = {}
        for f in filas:
            if not f["Ex"]:
                total[f["IDSocio"]] = total.get(f["IDSocio"], 0) + f["CantMes"]
        filas = [f for f in filas if not f["Ex"] and f["Nombres"] and total.get(f["IDSocio"], 0) >= 6 and f["CantMes"] > 0]
    return filas


def d_socios_solventes(p):
    """En Access este informe no tenia filtro: listaba a todos los socios. Aqui solo los que estan
    al dia hasta el mes indicado (los exonerados cuentan como solventes)."""
    anio, hasta = p["anio"], p["hasta_mes"]
    filas = []
    for f in _estado_meses(anio):
        pagados = set(f.pop("pagados") or [])
        registrados = set(f.pop("registrados") or [])
        f.pop("montos", None)
        if not f["Nombres"] or not f["Band"]:      # ConsultaListaSolventes: [Band] = -1
            continue
        pendientes = [m for m in range(1, hasta + 1) if m in registrados and m not in pagados]
        if pendientes and not (f["Ex"] or f["ExT"]):
            continue
        for i, col in enumerate(MES_COL, 1):
            f[col] = i in pagados
        f["TotSoc"] = 1
        filas.append(f)
    return Resultado(filas, formularios={"Marco55": hasta})


def d_solventes_cuota_extra(p):
    hasta = p["hasta_cuota"]
    cuotas = {n: (m, d) for n, m, d in _sql_valores("SELECT numero, monto, descripcion FROM cuota_extra", {})}
    filas = _sql(SOCIO_BASE.replace("FROM socio s", """,
               coalesce(json_agg(ce.numero) FILTER (WHERE sce.pagada), '[]') AS pagadas
          FROM socio s LEFT JOIN socio_cuota_extra sce ON sce.socio_id = s.id
          LEFT JOIN cuota_extra ce ON ce.id = sce.cuota_extra_id""") + """
         WHERE s.nombres IS NOT NULL GROUP BY s.id, p.nombre ORDER BY s.codigo""")
    resultado = []
    for f in filas:
        pagadas = set(f.pop("pagadas") or [])
        pendientes = [n for n in range(1, hasta + 1) if n not in pagadas and cuotas.get(n, (0,))[0]]
        if pendientes and not (f["Ex"] or f["ExT"]):
            continue
        for n in range(1, 13):
            f[f"Cuota{n}"] = n in pagadas
        f["TotSoc"] = 1
        resultado.append(f)
    formularios = {"Marco113": hasta, **{f"Desc{n}": d for n, (_, d) in cuotas.items()}}
    return Resultado(resultado, formularios=formularios)


def d_acciones_adicionales(p):
    return Resultado(_sql(SOCIO_BASE + " WHERE s.accion_adicional ORDER BY s.codigo"))


ESTADO_CIVIL_TEXTO = {"C": "CASADO(A)", "S": "SOLTERO(A)", "D": "DIVORCIADO(A)", "V": "VIUDO(A)",
                      "U": "UNION ESTABLE"}


def d_planilla_ingreso(p):
    filas = _sql("""
        SELECT s.codigo AS "IDSocio", s.ruta_foto AS "RutaSocio", s.apellidos AS "Apellidos", s.nombres AS "Nombres",
               s.cedula AS "CI", s.grupo_sanguineo AS "RH", s.fecha_nacimiento AS "FechaNac",
               s.lugar_nacimiento AS "LugNac", s.nacionalidad AS "Nac", p.nombre AS "Provincia",
               s.es_gallego AS "Gallego", s.ascendencia_gallega AS "AscendGallego", s.estado_civil AS "EdoCivil",
               s.profesion AS "Profesion", s.direccion_habitacion AS "DirHab", s.telefono_habitacion AS "TelHab",
               s.celular AS "Cel", s.email AS "email", s.email_alterno AS "email2", s.empresa AS "NomEmpr",
               s.direccion_trabajo AS "DirTrab", s.web_trabajo AS "DirWebTrab", s.telefono_trabajo AS "TelTrab",
               s.fax AS "Fax", s.marca_vehiculo AS "MarcaVeh", s.placa_vehiculo AS "PlacaVeh",
               s.aficiones AS "Aficiones", s.tiempo_en_ciudad AS "TmEnCiud", s.domicilio_cobro AS "DomCob",
               s.fecha_ingreso AS "FechIng", s.referencia_1 AS "Ref1", s.referencia_2 AS "Ref2",
               s.observaciones AS "Obs3"
          FROM socio s LEFT JOIN provincia p ON p.id = s.provincia_id
         WHERE s.codigo = %(socio)s""", p)
    if not filas:
        raise LookupError(f"No existe el socio {p['socio']}.")
    for f in filas:
        f["EdoCivil"] = ESTADO_CIVIL_TEXTO.get(f["EdoCivil"], f["EdoCivil"])
    return Resultado(filas)


def _concesiones(p):
    filas = _sql("""
        SELECT tp.codigo AS "CONC", tp.descripcion AS "Descrip", m.anio::text AS "AÑO",
               coalesce(json_agg(m.mes ORDER BY m.mes) FILTER (WHERE m.pagado), '[]') AS pagados,
               json_object_agg(m.mes, coalesce(m.numero_factura, '')) AS facturas
          FROM concesion_mensualidad m JOIN concesion c ON c.id = m.concesion_id
          JOIN tipo_pago tp ON tp.id = c.tipo_pago_id
         WHERE (%(anio)s = 0 OR m.anio = %(anio)s)
         GROUP BY tp.codigo, tp.descripcion, m.anio ORDER BY tp.codigo, m.anio""", {"anio": p.get("anio") or 0})
    for f in filas:
        pagados = set(f.pop("pagados") or [])
        facturas = f.pop("facturas") or {}
        for i, col in enumerate(MES_COL, 1):
            f[f"C{col}"] = i in pagados
            f[f"FC{col}"] = facturas.get(str(i)) or None
    return filas


def d_solvencia_concesionarios(p):
    return Resultado(_concesiones(p))


def d_recibos_concesionarios(p):
    return Resultado(_concesiones(p))


def d_relacion_pagos(p):
    return Resultado(_relacion_por_accion(p), visibilidad={"Ex": lambda c: c.get("Ex") == "EXONERADA"})


def d_relacion_facturas(p):
    return Resultado(_relacion_por_accion(p), visibilidad={"Ex": lambda c: c.get("Ex") == "EXONERADA"})


def d_morosos_6_meses(p):
    p = {**p, "anio": 0}
    filas = _relacion_por_accion(p, solo_morosos_6=True)
    return Resultado(filas, formularios={"Periodo": str(p["periodo"]), "Marco55": p["hasta_mes"]})


# =============================================================================
# Contabilidad (rango de fechas)
# =============================================================================

FACTURA_BASE = """
    SELECT f.numero AS "N°Fact", coalesce(s.codigo, f.codigo_socio_legado) AS "IdSocio",
           f.cliente_nombre AS "NomyApel", f.cliente_rif AS "CIoRIF",
           -- Access unia con TIPOSPAGOS (INNER JOIN) y omitia sin aviso las facturas sin tipo de pago
           coalesce(tp.codigo, 'S/T') AS "TipPago", coalesce(tp.descripcion, 'Sin tipo de pago') AS "Descrip",
           f.descripcion AS "DescPago", f.fecha_emision AS "FecGenFact",
           f.fecha_cancelacion AS "FecCanc", f.monto AS "Monto", f.estado AS "Estado", f.anulada AS "Nula",
           f.fecha_anulacion AS "FecNula", f.monto_cheque AS "MonCh", f.monto_deposito AS "MonDep",
           f.monto_tarjeta_debito AS "MonTarDeb", f.monto_tarjeta_credito AS "MonTarCred",
           f.monto_efectivo AS "MonEf", f.numero_cuotas AS "NumCuo"
      FROM factura f LEFT JOIN socio s ON s.id = f.socio_id LEFT JOIN tipo_pago tp ON tp.id = f.tipo_pago_id
"""


def _facturas(p, condicion, orden):
    filas = _sql(FACTURA_BASE + f" WHERE {condicion} ORDER BY {orden}", p)
    for f in filas:
        f["TotCancel"] = Decimal(0) if f["Nula"] else f["Monto"]
    return filas


def d_facturas_emitidas(p):
    return Resultado(_facturas(p, "f.fecha_emision BETWEEN %(desde)s AND %(hasta)s AND NOT f.anulada",
                               "f.fecha_emision, f.numero"),
                     formularios=_rango(p), visibilidad=_vis_rango(p, ocultar_si_dia=("EtiquetaFecha", "FecGenFact")),
                     pies={"Total": _suma("Monto"), "TotFac": _suma("Monto")})


def d_facturas_cobradas(p):
    return Resultado(_facturas(p, "f.fecha_cancelacion BETWEEN %(desde)s AND %(hasta)s AND NOT f.anulada",
                               "f.fecha_cancelacion, f.numero"),
                     formularios=_rango(p), visibilidad=_vis_rango(p, ocultar_si_dia=("EtiquetaFecha", "FecCanc")),
                     pies={"Total": _suma("Monto"), "TotFac": _suma("Monto")})


def d_facturas_anuladas(p):
    return Resultado(_facturas(p, "f.anulada AND f.fecha_anulacion BETWEEN %(desde)s AND %(hasta)s",
                               "f.fecha_anulacion, f.numero"),
                     formularios=_rango(p), visibilidad=_vis_rango(p, ocultar_si_dia=("EtiquetaFecha", "FecNula")),
                     pies={"Total": _suma("Monto")})


def d_ingresos_detalle(p):
    filas = _facturas(p, "f.fecha_cancelacion BETWEEN %(desde)s AND %(hasta)s AND f.estado",
                      "coalesce(tp.codigo, 'S/T'), f.numero")
    return Resultado(filas, formularios=_rango(p), visibilidad=_vis_rango(p),
                     pies={"Tot1": _suma("TotCancel"), "SubTotal": _suma("TotCancel")})


def d_ingresos_resumen(p):
    filas = _facturas(p, "f.fecha_cancelacion BETWEEN %(desde)s AND %(hasta)s AND f.estado",
                      "coalesce(tp.codigo, 'S/T'), f.numero")
    return Resultado(filas, formularios=_rango(p), visibilidad=_vis_rango(p))


def _totales_periodo(p) -> dict:
    """Lo que en Access calculaban los subinformes del Estado de resultados."""
    (abonos, egresos, efectivo), = _sql_valores("""
        SELECT (SELECT coalesce(sum(monto), 0) FROM abono WHERE fecha_cancelacion BETWEEN %(desde)s AND %(hasta)s AND NOT anulado),
               (SELECT coalesce(sum(monto), 0) FROM cheque_emitido WHERE fecha BETWEEN %(desde)s AND %(hasta)s AND NOT anulado),
               (SELECT coalesce(sum(monto), 0) FROM cheque_emitido WHERE fecha BETWEEN %(desde)s AND %(hasta)s AND NOT anulado
                                                                     AND banco_nombre = 'EFECTIVO')""", p)
    return {"abonos": abonos, "egresos": egresos, "efectivo": efectivo}


def d_estado_resultados(p):
    filas = _facturas(p, "f.fecha_cancelacion BETWEEN %(desde)s AND %(hasta)s AND f.estado",
                      "coalesce(tp.codigo, 'S/T'), f.numero")
    t = _totales_periodo(p)
    hay_cuota_extra = any(f["TipPago"] == "Z002" for f in filas)
    total = _suma("TotCancel")
    return Resultado(
        filas, formularios=_rango(p), visibilidad=_vis_rango(p),
        # Access sumaba Total en PieDelGrupo0_Print (se duplicaba al repaginar); aqui se calcula una vez
        pies={"TotTipo": _suma("TotCancel"),
              "Total": lambda filas_: total(filas_) + (t["abonos"] if hay_cuota_extra else 0)},
        subinformes={"Secundario27": {"TotAbo": t["abonos"]}, "Secundario29": {"TotAbo": t["abonos"]},
                     "Secundario68": {"TotChqCG": t["egresos"]}, "Secundario62": {"TotEf": t["efectivo"]}})


def d_cheques_recibidos(p):
    return Resultado(_sql("""
        SELECT c.banco_emisor AS "NombreBanco", c.numero_cheque AS "N°Cheque", c.monto AS "Monto",
               c.numero_cuenta AS "N°Cuenta", c.fecha_pago AS "FechaPago",
               coalesce(s.codigo, f.codigo_socio_legado) AS "IdSocio", f.numero AS "N°Fact",
               f.cliente_nombre AS "NomyApel", f.cliente_rif AS "CIoRIF", f.descripcion AS "DescPago"
          FROM cheque_recibido c JOIN factura f ON f.id = c.factura_id LEFT JOIN socio s ON s.id = f.socio_id
         WHERE c.fecha_pago BETWEEN %(desde)s AND %(hasta)s
         ORDER BY c.banco_emisor, c.numero_cuenta, c.numero_cheque""", p), formularios=_rango(p))


def d_abonos(p):
    return Resultado(_sql("""
        SELECT a.numero_documento AS "N°Fact", s.codigo AS "IdSocio", a.cliente_nombre AS "NomyApel",
               tp.codigo AS "TipPago", a.descripcion AS "DescPago", a.fecha_cancelacion AS "FecCanc",
               a.monto AS "MontoAbo", a.estado AS "Estado", a.fecha_anulacion AS "FecNula", a.anulado AS "Nula",
               a.monto_cheque AS "MonCh", a.monto_deposito AS "MonDep", a.monto_tarjeta_credito AS "MonTarCred",
               a.monto_tarjeta_debito AS "MonTarDeb", a.monto_efectivo AS "MonEf"
          FROM abono a JOIN socio s ON s.id = a.socio_id LEFT JOIN tipo_pago tp ON tp.id = a.tipo_pago_id
         WHERE a.fecha_cancelacion BETWEEN %(desde)s AND %(hasta)s ORDER BY a.fecha_cancelacion, a.numero_documento""", p),
        formularios=_rango(p))


def d_socios_con_abonos(p):
    return Resultado(_sql("""
        SELECT s.codigo AS "IdSocio", a.cliente_nombre AS "NomyApel", a.cliente_rif AS "CIoRIF",
               a.numero_documento AS "N°Fact", a.fecha_cancelacion AS "FecCanc", a.monto AS "MontoAbo",
               a.descripcion AS "DescPago", a.estado AS "Estado", a.anulado AS "Nula", a.fecha_anulacion AS "FecNula",
               a.anio_deuda::text AS "AñoDeuda"
          FROM abono a JOIN socio s ON s.id = a.socio_id
         WHERE NOT a.estado AND NOT a.anulado ORDER BY s.codigo, a.numero_documento"""))


def _existe(tabla_sql: str, valor: str, mensaje: str):
    if not _sql_valores(tabla_sql, {"v": valor}):
        raise LookupError(mensaje)


def d_recibos_socio(p):
    _existe("SELECT 1 FROM socio WHERE codigo = %(v)s", p["socio"], f"No existe el socio {p['socio']}.")
    filas = _sql("""
        SELECT s.apellidos AS "Apellidos", s.nombres AS "Nombres", s.tipo_socio = 'P' AS "TipoSocio",
               s.codigo AS "IDSocio", f.numero AS "N°Fact", f.fecha_emision AS "FecGenFact",
               f.fecha_cancelacion AS "FecCanc", f.descripcion AS "DescPago", f.monto AS "Monto",
               f.cliente_nombre AS "NomyApel", f.anulada AS "Nula"
          FROM factura f JOIN socio s ON s.id = f.socio_id
         WHERE s.codigo = %(socio)s ORDER BY f.fecha_emision, f.numero""", p)
    return Resultado(filas, formularios={"IDSocio": p["socio"]}, pies={"TotFac": _suma("Monto", lambda f: not f["Nula"])})


def d_recibos_concesionario(p):
    _existe("SELECT 1 FROM concesion c JOIN tipo_pago tp ON tp.id = c.tipo_pago_id WHERE tp.codigo = %(v)s",
            p["concesion"], f"No existe el concesionario {p['concesion']}.")
    filas = _sql("""
        SELECT f.numero AS "N°Fact", f.fecha_emision AS "FecGenFact", f.fecha_cancelacion AS "FecCanc",
               f.descripcion AS "DescPago", f.monto AS "Monto", f.cliente_nombre AS "NomyApel", f.anulada AS "Nula",
               tp.codigo AS "CONC", c.nombre AS "NOMCONC", c.rif AS "RIF"
          FROM factura f JOIN concesion c ON c.tipo_pago_id = f.tipo_pago_id JOIN tipo_pago tp ON tp.id = c.tipo_pago_id
         WHERE tp.codigo = %(concesion)s ORDER BY f.fecha_emision, f.numero""", p)
    rif = filas[0]["RIF"] if filas else None
    return Resultado(filas, formularios={"RIF": rif}, pies={"TotFac": _suma("Monto", lambda f: not f["Nula"])})


# =============================================================================
# Egresos
# =============================================================================

EGRESO_BASE = """
    SELECT c.fecha AS "FechaHoy", c.numero_cheque AS "NumeroCheque", c.numero_cuenta AS "NumeroCuenta",
           c.banco_nombre AS "BANCO", c.beneficiario AS "BENEFICIARIO", c.monto AS "MONTO",
           c.numero_voucher AS "NÚMERO DE VAUCHER", c.concepto AS "ConceptoPago", c.preparado_por AS "Preparado",
           c.anulado AS "Nulidad"
      FROM cheque_emitido c
"""


def _egresos(p, anulados: bool, efectivo: bool | None = None):
    filtro = "" if efectivo is None else ("AND c.banco_nombre = 'EFECTIVO'" if efectivo
                                          else "AND coalesce(c.banco_nombre, '') <> 'EFECTIVO'")
    filas = _sql(EGRESO_BASE + f""" WHERE c.fecha BETWEEN %(desde)s AND %(hasta)s AND c.anulado = %(anulados)s {filtro}
                                    ORDER BY c.banco_nombre, c.fecha, c.numero_voucher""", {**p, "anulados": anulados})
    for f in filas:
        d = f["FechaHoy"]
        f["FECHA (DIA Y MES)"] = f"{d.day}/{d.month}/" if d else ""
        f["AÑO"] = str(d.year) if d else ""
    return filas


def d_cheques_emitidos(p):
    # Access usaba el mismo informe para cheques y para efectivo y dejaba los dos titulos encimados
    return Resultado(_egresos(p, False, efectivo=False), formularios=_rango(p), pies={"MontoParcial": _suma("MONTO")},
                     visibilidad={"EtiquetaCheq": True, "EtiquetaEfec": False})


def d_pagos_efectivo(p):
    return Resultado(_egresos(p, False, efectivo=True), formularios=_rango(p), pies={"MontoParcial": _suma("MONTO")},
                     visibilidad={"EtiquetaCheq": False, "EtiquetaEfec": True})


def d_cheques_anulados(p):
    return Resultado(_egresos(p, True), formularios=_rango(p))


# =============================================================================
# Proveedores y activos
# =============================================================================

def d_proveedores(p):
    return Resultado(_sql("""SELECT codigo AS "Cod", nombre_empresa AS "NombEmp", suministro AS "Suministro",
                                    direccion AS "Direc", telefono AS "Telf", representante AS "Repres", celular AS "Cel"
                               FROM proveedor ORDER BY codigo NULLS LAST, nombre_empresa"""))


def _activos(baja: bool):
    return _sql(f"""SELECT codigo AS "Cod", descripcion AS "Desc", cantidad AS "Cant", ubicacion AS "Ubic",
                           fecha_ingreso AS "FechaIng", fecha_salida AS "FechaSal"
                      FROM activo_fijo WHERE fecha_salida IS {'NOT ' if baja else ''}NULL ORDER BY codigo, descripcion""")


def d_activos(p):
    return Resultado(_activos(False))


def d_activos_baja(p):
    return Resultado(_activos(True))


# =============================================================================
# Catalogo
# =============================================================================

F_DESDE = Parametro("desde", "Desde", "fecha")
F_HASTA = Parametro("hasta", "Hasta", "fecha")
ANIO = Parametro("anio", "Año", "anio")
HASTA_MES = Parametro("hasta_mes", "Hasta el mes", "mes")

LISTADOS: dict[str, Listado] = {l.clave: l for l in [
    # Socios
    Listado("socios", "Listado de socios (propietarios y de uso)", "Socios", "ListaSocios", d_socios),
    Listado("beneficiarios", "Beneficiarios activos", "Socios", "ListaBeneficiarios", d_beneficiarios),
    Listado("acciones-libres", "Acciones disponibles para la venta (libres)", "Socios", "ListaAcciones",
            d_acciones_libres),
    Listado("socios-provincia", "Socios por provincia de origen", "Socios", "ListaSociosxRegion", d_socios_provincia),
    Listado("socios-gallegos", "Socios gallegos y descendientes", "Socios", "ListaSociosGallegos", d_socios_gallegos),
    Listado("gallegos-beneficiarios", "Gallegos y descendientes con beneficiarios", "Socios", "ListaSocGallDescBen",
            d_gallegos_y_beneficiarios),
    Listado("socios-fecha-ingreso", "Socios por fecha de ingreso", "Socios", "ListaSociosxFI", d_socios_fecha_ingreso),
    Listado("cumpleanos", "Cumpleaños del mes", "Socios", "ListaSociosCumpleaños", d_cumpleanos,
            [Parametro("mes", "Mes", "mes")]),
    Listado("acciones-adicionales", "Acciones adicionales", "Socios", "ListaAccionAdic", d_acciones_adicionales),
    Listado("planilla-ingreso", "Planilla de ingreso del socio", "Socios", "PlanillaIngreso", d_planilla_ingreso,
            [Parametro("socio", "Código de socio", "socio")]),
    Listado("acciones-exoneradas", "Acciones exoneradas", "Socios", "ListaAccionExon", d_acciones_exoneradas),
    Listado("uso-salon", "Uso del salón por socios", "Socios", "ListaSociosxUsoSalon", d_uso_salon, [ANIO]),
    # Morosidad
    Listado("morosos", "Morosos por cuota de mantenimiento", "Morosidad", "ListaSociosMorosos", d_morosos,
            [ANIO, HASTA_MES], "Solo socios con meses pendientes hasta el mes indicado; excluye exonerados."),
    Listado("morosos-cuota-extra", "Morosos por cuota extraordinaria", "Morosidad", "ListaCExtraMorosos",
            d_morosos_cuota_extra, [Parametro("hasta_cuota", "Hasta la cuota", "entero")]),
    Listado("morosos-anios-anteriores", "Morosos de años anteriores", "Morosidad", "ListaSociosMorAA",
            d_morosos_anios_anteriores, [ANIO]),
    Listado("morosos-6-meses", "Socios con 6 o más meses de deuda", "Morosidad", "RelacionMor6mes",
            d_morosos_6_meses, [HASTA_MES]),
    Listado("socios-solventes", "Socios solventes (mantenimiento)", "Morosidad", "ListaSociosSolventes",
            d_socios_solventes, [ANIO, HASTA_MES], "Solo los socios al día hasta el mes indicado."),
    Listado("solventes-cuota-extra", "Socios solventes (cuota extraordinaria)", "Morosidad", "ListaCExtraSolventes",
            d_solventes_cuota_extra, [Parametro("hasta_cuota", "Hasta la cuota", "entero")]),
    Listado("relacion-pagos", "Relación de pagos por acción", "Morosidad", "RelacionPagxAccion", d_relacion_pagos,
            [ANIO]),
    Listado("relacion-facturas", "Relación de recibos por acción", "Morosidad", "RelacionFactxAccion",
            d_relacion_facturas, [ANIO]),
    # Contabilidad
    Listado("facturas-emitidas", "Recibos emitidos", "Contabilidad", "ListaFacEmit", d_facturas_emitidas, [F_DESDE, F_HASTA]),
    Listado("facturas-cobradas", "Recibos cobrados", "Contabilidad", "ListaFacCob", d_facturas_cobradas, [F_DESDE, F_HASTA]),
    Listado("facturas-anuladas", "Recibos anulados", "Contabilidad", "ListaFacAnul", d_facturas_anuladas, [F_DESDE, F_HASTA]),
    Listado("ingresos-detalle", "Detalle de ingresos", "Contabilidad", "IngresosDetalle", d_ingresos_detalle, [F_DESDE, F_HASTA]),
    Listado("ingresos-resumen", "Resumen de movimientos", "Contabilidad", "IngresosResumen", d_ingresos_resumen, [F_DESDE, F_HASTA]),
    Listado("estado-resultados", "Estado de resultados", "Contabilidad", "EstadoResultados", d_estado_resultados, [F_DESDE, F_HASTA]),
    Listado("cheques-recibidos", "Cheques recibidos", "Contabilidad", "ListaCHEQUESRecib", d_cheques_recibidos, [F_DESDE, F_HASTA]),
    Listado("abonos", "Abonos del período", "Contabilidad", "AbonosDia", d_abonos, [F_DESDE, F_HASTA]),
    Listado("socios-con-abonos", "Socios con abonos pendientes", "Contabilidad", "InfSociosConAbono", d_socios_con_abonos),
    Listado("recibos-socio", "Recibos emitidos a un socio", "Contabilidad", "ListaRecibosEmitidosxSocios", d_recibos_socio,
            [Parametro("socio", "Código de socio", "socio")]),
    Listado("solvencia-concesionarios", "Solvencia de concesionarios", "Concesionarios", "RelacionPagxConc",
            d_solvencia_concesionarios, [ANIO]),
    Listado("relacion-recibos-concesionarios", "Relación de recibos por concesionario", "Concesionarios",
            "RelacionFactxConc", d_recibos_concesionarios, [ANIO]),
    Listado("recibos-concesionario", "Recibos de un concesionario", "Concesionarios", "ListaRecibosEmitidosxConc",
            d_recibos_concesionario, [Parametro("concesion", "Concesionario", "concesion")]),
    # Egresos
    Listado("cheques-emitidos", "Cheques y pagos emitidos", "Egresos", "ListaCheqEmit", d_cheques_emitidos, [F_DESDE, F_HASTA]),
    Listado("pagos-efectivo", "Pagos en efectivo", "Egresos", "ListaCheqEmit", d_pagos_efectivo, [F_DESDE, F_HASTA]),
    Listado("cheques-anulados", "Pagos anulados", "Egresos", "ListaCheqAnul", d_cheques_anulados, [F_DESDE, F_HASTA]),
    # Administracion
    Listado("proveedores", "Listado de proveedores", "Administración", "ListaProveedores", d_proveedores),
    Listado("activos", "Activos en uso", "Administración", "ListaActivos", d_activos),
    Listado("activos-baja", "Activos de baja", "Administración", "ListaActivosBaja", d_activos_baja),
]}


# =============================================================================
# Agregados de la revision completa (botones de Access que faltaban)
# =============================================================================

from django.conf import settings as _settings


def _ruta_foto(relativa):
    """Foto del socio en MEDIA_ROOT; las rutas de Access (otra computadora) no se abren."""
    from core.fotos import ruta_media
    ruta = ruta_media(relativa)
    return str(ruta) if ruta else None


def _beneficiarios_de(codigo: str) -> list[dict]:
    return _sql("""
        SELECT s.codigo AS "IdSocio", b.codigo AS "IdBen", b.nombre_completo AS "NombyApel", b.cedula AS "CI",
               b.parentesco AS "Parent", b.fecha_nacimiento AS "FecNac", b.grupo_sanguineo AS "RH",
               b.telefono AS "Telf", b.marca_vehiculo AS "MarcaVeh", b.placa_vehiculo AS "PlacVeh", b.activo AS "Act"
          FROM beneficiario b JOIN socio s ON s.id = b.socio_id
         WHERE s.codigo = %(c)s AND b.activo ORDER BY b.codigo""", {"c": codigo})


def _socio_breve(codigo) -> list[dict]:
    if not codigo:
        return []
    return _sql("""SELECT codigo AS "IDSocio", apellidos AS "Apellidos", nombres AS "Nombres", cedula AS "CI"
                     FROM socio WHERE codigo = %(c)s""", {"c": codigo})


def d_planilla_ingreso_completa(p):
    """Planilla con la foto, los familiares (SubInfBenef) y los socios que lo proponen (Ref1/Ref2)."""
    r = d_planilla_ingreso(p)
    for f in r.filas:
        f["RutaSocio"] = _ruta_foto(f.get("RutaSocio"))
        f["__sub__"] = {"Secundario116": _beneficiarios_de(f["IDSocio"]),
                        "Secundario45": _socio_breve(f.get("Ref1")),
                        "Subinforme SOCIOS": _socio_breve(f.get("Ref2"))}
    return r


def d_planilla_vacia(p):
    return Resultado([{}])


def _socios_tipo(tipo: str | None):
    def datos(p):
        filtro = f" AND s.tipo_socio = '{tipo}'" if tipo in ("P", "U") else ""
        return Resultado(_sql(SOCIO_BASE + f" WHERE s.apellidos IS NOT NULL{filtro} ORDER BY s.codigo"))
    return datos


def d_acciones(p):
    """"De Acciones" (menu de socios): todas las acciones con su titular; las libres figuran como
    disponibles para la venta. Pie con los totales."""
    filas = _sql(SOCIO_BASE + " ORDER BY s.codigo")
    for f in filas:
        f["Titular"] = (" ".join(filter(None, [f["Apellidos"], f["Nombres"]])) if f["Nombres"]
                        else "DISPONIBLE PARA LA VENTA")
    return Resultado(filas, pies={"TotOcupadas": _cuenta(lambda f: bool(f["Nombres"])),
                                  "TotLibres": _cuenta(lambda f: not f["Nombres"])})


def d_socios_y_beneficiarios(p):
    filas = _sql(SOCIO_BASE + " WHERE s.nombres IS NOT NULL ORDER BY s.codigo")
    for f in filas:
        f["__sub__"] = {"Beneficiarios": _beneficiarios_de(f["IDSocio"])}
    return Resultado(filas)


BANCOS_LIBRO = {"Ban": "BANESCO", "Ven": "VENEZUELA", "DS": "DEL SUR", "PV": "PROVINCIAL"}


def d_libro_diario(p):
    """Libro diario y conciliacion por banco. En Access, los depositos/cheques/cargos por banco se
    leian de otros informes abiertos ([Reports]![SubinfDEPxBANC]...) y si no lo estaban salian en 0."""
    filas = _sql("""
        SELECT f.numero AS "N°Fact", coalesce(s.codigo, f.codigo_socio_legado) AS "IdSocio", NULL AS "N°Vauch",
               f.cliente_nombre AS "NomyApel", tp.codigo AS "TipPago", f.descripcion AS "DescPago",
               f.fecha_cancelacion AS "FecCanc", f.monto AS "Monto", f.monto_cheque AS "MonCh",
               f.monto_deposito AS "MonDep", f.monto_tarjeta_debito AS "MonTarDeb",
               f.monto_tarjeta_credito AS "MonTarCred", f.monto_efectivo AS "MonEf",
               0 AS "PagCh", 0 AS "PagEf", 0 AS "PagCC", NULL AS "BANCO", 0 AS orden
          FROM factura f LEFT JOIN socio s ON s.id = f.socio_id LEFT JOIN tipo_pago tp ON tp.id = f.tipo_pago_id
         WHERE f.fecha_cancelacion BETWEEN %(desde)s AND %(hasta)s AND f.estado AND NOT f.anulada
        UNION ALL
        SELECT NULL, NULL, c.numero_voucher, c.beneficiario, NULL, c.concepto, c.fecha, c.monto, 0, 0, 0, 0, 0,
               CASE WHEN c.banco_nombre NOT IN ('EFECTIVO', 'CARGO EN CUENTA') THEN c.monto ELSE 0 END,
               CASE WHEN c.banco_nombre = 'EFECTIVO' THEN c.monto ELSE 0 END,
               CASE WHEN c.banco_nombre = 'CARGO EN CUENTA' THEN c.monto ELSE 0 END,
               coalesce(NULLIF(c.banco_nombre, 'CARGO EN CUENTA'), c.numero_cuenta), 1
          FROM cheque_emitido c WHERE c.fecha BETWEEN %(desde)s AND %(hasta)s AND NOT c.anulado
         ORDER BY 7, 18, 1, 3""", p)
    formularios = {**_rango(p)}
    for clave in ("Saldo", "Tsi", "ga", "ChAn", "ChDev"):
        for sufijo in ("Ban", "Ven", "DS", "PV"):
            formularios[f"{clave}{sufijo.upper() if clave in ('Tsi', 'ga') else sufijo}"] = p.get(f"{clave.lower()}_{sufijo.lower()}") or 0
    for sufijo, banco in BANCOS_LIBRO.items():
        (dep,), = _sql_valores("SELECT coalesce(sum(monto), 0) FROM deposito d JOIN banco b ON b.id = d.banco_id "
                               "WHERE d.fecha_pago BETWEEN %(desde)s AND %(hasta)s AND b.nombre = %(b)s", {**p, "b": banco})
        (che,), = _sql_valores("SELECT coalesce(sum(monto), 0) FROM cheque_emitido WHERE fecha BETWEEN %(desde)s AND %(hasta)s "
                               "AND NOT anulado AND banco_nombre = %(b)s", {**p, "b": banco})
        (cc,), = _sql_valores("SELECT coalesce(sum(monto), 0) FROM cheque_emitido WHERE fecha BETWEEN %(desde)s AND %(hasta)s "
                              "AND NOT anulado AND banco_nombre = 'CARGO EN CUENTA' AND numero_cuenta ILIKE %(b)s",
                              {**p, "b": f"%{banco}%"})
        s = {"Ban": "BAN", "Ven": "VEN", "DS": "DS", "PV": "PV"}[sufijo]
        formularios[f"SubinfDEPxBANC!MonTot{s}"] = dep
        formularios[f"SubinfCHEMxBANC!MonTot{s}"] = che
        formularios[f"SubinfCHEMxBANC!MonTotCC{sufijo}"] = cc
    return Resultado(filas, formularios=formularios, visibilidad=_vis_rango(p))


def d_movimiento_divisas(p):
    """IngEgrDiv: ingresos (recibos pagados en divisas) y egresos (pagos en divisas) por dia."""
    filas = _sql("""
        SELECT f.fecha_emision AS "Fecha", 'Ingreso' AS "TipoMov", f.numero AS "N°Fact", NULL AS "NumVaucher",
               coalesce(s.codigo, f.codigo_socio_legado) AS "IdSocio", f.cliente_nombre AS "NomyApel",
               tp.codigo AS "TipPago", f.descripcion AS "DescPago", f.monto_divisas AS "MontoDivIng", 0 AS "MontoDivEg"
          FROM factura f LEFT JOIN socio s ON s.id = f.socio_id LEFT JOIN tipo_pago tp ON tp.id = f.tipo_pago_id
         WHERE f.pago_divisas AND NOT f.anulada AND f.fecha_emision BETWEEN %(desde)s AND %(hasta)s
        UNION ALL
        SELECT c.fecha, 'Egreso', NULL, c.numero_voucher, NULL, c.beneficiario, NULL, c.concepto, 0, c.monto_divisas
          FROM cheque_emitido c WHERE c.pago_divisas AND NOT c.anulado AND c.fecha BETWEEN %(desde)s AND %(hasta)s
         ORDER BY 1, 2, 3""", p)
    dias = (p["hasta"] - p["desde"]).days
    return Resultado(filas, formularios={**_rango(p), "Dias": dias},
                     visibilidad={**_vis_rango(p),
                                  "NumVaucher": lambda c: c.get("TipoMov") == "Egreso",
                                  "N°Fact": lambda c: c.get("TipoMov") != "Egreso",
                                  "Etiqueta59": lambda c: c.get("TipoMov") == "Egreso",
                                  "Etiqueta14": lambda c: c.get("TipoMov") != "Egreso",
                                  "Etiqueta15": lambda c: c.get("TipoMov") != "Egreso"})


def caja_divisas() -> dict:
    """"Saldo en caja" (divisas). En Access el formulario leia [Ingreso]/[Egreso] de consultas cuyas
    columnas se llamaban SumaDeMontoDivIng/SumaDeMontoDivEg: daba error y siempre mostraba solo el
    saldo inicial. Aqui: saldo inicial + ingresos - egresos en divisas desde la fecha de inicio."""
    (inicio, desde), = _sql_valores("SELECT coalesce(divisas_inicio, 0), fecha_inicio_divisas FROM configuracion", {})
    desde = desde or date(1900, 1, 1)
    (ing,), = _sql_valores("SELECT coalesce(sum(monto_divisas), 0) FROM factura WHERE pago_divisas AND NOT anulada "
                           "AND fecha_emision >= %(d)s", {"d": desde})
    (egr,), = _sql_valores("SELECT coalesce(sum(monto_divisas), 0) FROM cheque_emitido WHERE pago_divisas AND NOT anulado "
                           "AND fecha >= %(d)s", {"d": desde})
    return {"saldo_inicial": inicio, "desde": desde, "ingresos": ing, "egresos": egr,
            "saldo": Decimal(inicio) + Decimal(ing) - Decimal(egr)}


_PAR_LIBRO = [Parametro(f"{c}_{b}", f"{e} {n}", "dinero", obligatorio=False)
              for c, e in (("saldo", "Saldo inicial"), ("tsi", "Transf. sin identificar"), ("ga", "Gastos bancarios"),
                           ("chan", "Cheques anulados"), ("chdev", "Cheques devueltos"))
              for b, n in (("ban", "Banesco"), ("ven", "Venezuela"), ("ds", "Del Sur"), ("pv", "Provincial"))]

for _l in [
    Listado("socios-propietarios", "Listado de socios propietarios", "Socios", "ListaSocios", _socios_tipo("P")),
    Listado("socios-uso", "Listado de socios de uso", "Socios", "ListaSocios", _socios_tipo("U")),
    Listado("acciones", "Listado de acciones (todas, con su titular)", "Socios", "ListaAccionesTodas", d_acciones),
    Listado("socios-beneficiarios", "Socios y sus beneficiarios", "Socios", "SociosBeneficiarios", d_socios_y_beneficiarios),
    Listado("planilla-vacia", "Planilla de ingreso (vacía)", "Socios", "PlanillaIngresoVacia", d_planilla_vacia),
    Listado("libro-diario", "Libro diario y conciliación bancaria", "Contabilidad", "LibroDiario", d_libro_diario,
            [F_DESDE, F_HASTA, *_PAR_LIBRO],
            "Los depósitos, cheques y cargos por banco se calculan solos (en Access salían en 0)."),
    Listado("movimiento-divisas", "Movimiento de divisas", "Contabilidad", "IngEgrDiv", d_movimiento_divisas,
            [F_DESDE, F_HASTA]),
]:
    LISTADOS[_l.clave] = _l
LISTADOS["planilla-ingreso"].datos = d_planilla_ingreso_completa



def _importe(valor: str) -> Decimal:
    """Importe escrito por el usuario: "1.234,56", "1234,56", "1.500" (punto de miles) o "1234.56".
    (Antes se quitaban todos los puntos: "1234.56" se leia como 123456, y un texto daba error 500.)"""
    t = valor.replace(" ", "")
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", t):
        t = t.replace(".", "")
    try:
        importe = Decimal(t)
    except InvalidOperation:
        raise ValueError(valor) from None
    if not importe.is_finite():
        raise ValueError(valor)
    return importe


def leer_parametros(listado: Listado, consulta: dict, periodo: int) -> dict:
    """Valida y completa los parametros (con valores por defecto razonables)."""
    hoy = date.today()
    p = {"periodo": periodo}
    errores = {}
    for par in listado.parametros:
        valor = (consulta.get(par.nombre) or "").strip()
        try:
            if par.tipo == "fecha":
                p[par.nombre] = date.fromisoformat(valor) if valor else (hoy.replace(day=1) if par.nombre == "desde" else hoy)
            elif par.tipo == "anio":
                p[par.nombre] = int(valor) if valor else periodo
            elif par.tipo == "mes":
                p[par.nombre] = int(valor) if valor else (hoy.month if periodo == hoy.year else 12)
                if not 1 <= p[par.nombre] <= 12:
                    raise ValueError
            elif par.tipo == "entero":
                p[par.nombre] = int(valor) if valor else 12
            elif par.tipo == "dinero":
                p[par.nombre] = _importe(valor) if valor else Decimal(0)
            else:
                if not valor and par.obligatorio:
                    errores[par.nombre] = "Obligatorio."
                p[par.nombre] = valor.upper()
        except ValueError:
            errores[par.nombre] = "Valor no válido."
    if "desde" in p and "hasta" in p and p["desde"] > p["hasta"]:
        errores["hasta"] = "Debe ser posterior a la fecha inicial."
    if errores:
        raise ValueError(errores)
    return p


def generar(clave: str, parametros: dict) -> bytes:
    listado = LISTADOS[clave]
    r = listado.datos(parametros)
    formularios = {**r.formularios, "_visibilidad": r.visibilidad}
    return InformeAccess.cargar(listado.informe).render_listado(
        r.filas, formularios, titulo=listado.titulo, pies=r.pies, subinformes=r.subinformes)


def catalogo() -> list[dict]:
    return [{"clave": l.clave, "titulo": l.titulo, "grupo": l.grupo, "descripcion": l.descripcion,
             "parametros": [{"nombre": p.nombre, "etiqueta": p.etiqueta, "tipo": p.tipo} for p in l.parametros]}
            for l in LISTADOS.values()]


__all__ = ["LISTADOS", "catalogo", "generar", "leer_parametros", "MESES"]
