"""
Documentos impresos de SICGA con el mismo formato que el sistema Access.

Cada funcion traduce los modelos nuevos a los nombres de campo que esperaban los
informes originales (los de la tabla o los de los formularios [Forms]![...]) y
devuelve el PDF.

    Recibo de pago (original) -> informe FacturaCxC   (al emitir la factura)
    Recibo de pago (copia)    -> informe FacturaCons  ("COPIA FIEL AL ORIGINAL")
    Solvencia                 -> informe Solvencia
    Comprobante de egreso     -> informe InfVaucher
    Cheque                    -> informe InfCheque
"""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from .motor import InformeAccess, Registro, formatear

# =============================================================================
# Monto en letras, con el mismo estilo que escribia Access en CHEQUES CG:
#   191984.71 -> "CIENTO NOVENTA Y UN MIL NOVECIENTOS OCHENTA Y CUATRO CON 71 CTS"
#   78300     -> "SETENTA Y OCHO MIL TRESCIENTOS SIN CTS"
# =============================================================================

_UNIDADES = ["", "UNO", "DOS", "TRES", "CUATRO", "CINCO", "SEIS", "SIETE", "OCHO", "NUEVE", "DIEZ",
             "ONCE", "DOCE", "TRECE", "CATORCE", "QUINCE", "DIECISEIS", "DIECISIETE", "DIECIOCHO",
             "DIECINUEVE", "VEINTE", "VEINTIUNO", "VEINTIDOS", "VEINTITRES", "VEINTICUATRO",
             "VEINTICINCO", "VEINTISEIS", "VEINTISIETE", "VEINTIOCHO", "VEINTINUEVE"]
_DECENAS = ["", "", "", "TREINTA", "CUARENTA", "CINCUENTA", "SESENTA", "SETENTA", "OCHENTA", "NOVENTA"]
_CENTENAS = ["", "CIENTO", "DOSCIENTOS", "TRESCIENTOS", "CUATROCIENTOS", "QUINIENTOS", "SEISCIENTOS",
             "SETECIENTOS", "OCHOCIENTOS", "NOVECIENTOS"]


def _hasta_999(n: int, apocope: bool) -> str:
    """apocope=True: 'UN' en lugar de 'UNO' (antes de MIL / MILLONES)."""
    if n == 0:
        return ""
    if n == 100:
        return "CIEN"
    c, resto = divmod(n, 100)
    partes = [_CENTENAS[c]] if c else []
    if resto < 30:
        palabra = _UNIDADES[resto]
        if apocope and palabra.endswith("UNO"):
            palabra = palabra[:-1]
        if palabra:
            partes.append(palabra)
    else:
        d, u = divmod(resto, 10)
        unidad = _UNIDADES[u]
        if apocope and unidad == "UNO":
            unidad = "UN"
        partes.append(_DECENAS[d] + (f" Y {unidad}" if u else ""))
    return " ".join(partes)


def entero_a_letras(n: int) -> str:
    if n == 0:
        return "CERO"
    millones, resto = divmod(n, 1_000_000)
    miles, unidades = divmod(resto, 1000)
    partes = []
    if millones:
        partes.append("UN MILLON" if millones == 1 else f"{entero_a_letras(millones).replace('UNO', 'UN')} MILLONES"
                      if millones >= 1000 else f"{_hasta_999(millones, True)} MILLONES")
    if miles:
        partes.append("UN MIL" if miles == 1 else f"{_hasta_999(miles, True)} MIL")   # estilo actual de caja
    if unidades:
        partes.append(_hasta_999(unidades, False))
    return " ".join(partes)


def monto_en_letras(monto: Decimal) -> str:
    monto = Decimal(monto).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    entero = int(monto)
    centimos = int((monto - entero) * 100)
    sufijo = f"CON {centimos:02d} CTS" if centimos else "SIN CTS"
    return f"{entero_a_letras(entero)} {sufijo}"


def partir_letras(texto: str, ancho: int = 95) -> tuple[str, str]:
    """Access repartia el monto en letras en dos lineas (95 + resto)."""
    if len(texto) <= ancho:
        return texto, ""
    corte = texto.rfind(" ", 0, ancho + 1)
    corte = corte if corte > 0 else ancho
    return texto[:corte], texto[corte:].strip()


# =============================================================================
# Recibo de pago (factura)
# =============================================================================

def factura_pdf(factura, original: bool = False) -> bytes:
    cheque = factura.cheques.order_by("id").first()
    deposito = factura.depositos.order_by("id").first()
    socio = factura.socio
    tarjeta = (factura.monto_tarjeta_credito or 0) + (factura.monto_tarjeta_debito or 0)
    campos = {
        # Campos de la tabla FACTURAS (informe FacturaCons)
        "N°Fact": factura.numero, "N°Cont": factura.numero_control,
        "IdSocio": socio.codigo if socio else factura.codigo_socio_legado,
        "NomyApel": factura.cliente_nombre, "CIoRIF": factura.cliente_rif,
        "FecGenFact": factura.fecha_emision, "Monto": factura.monto, "DescPago": factura.descripcion,
        "MonDep": factura.monto_deposito, "MonCh": factura.monto_cheque, "MonEf": factura.monto_efectivo,
        "MonTarCred": factura.monto_tarjeta_credito, "MonTarDeb": factura.monto_tarjeta_debito,
        "NumCuo": factura.numero_cuotas,
        # Campos del formulario FormFACTURAS / Mensualidad (informe FacturaCxC)
        "FacTxt": factura.numero or "", "ContTxt": factura.numero_control or "",
        "TotDeuda": Decimal(0), "CantMesDeuda": factura.numero_cuotas,
        "BancCh": cheque.banco_emisor if cheque else None, "ChqNum": cheque.numero_cheque if cheque else None,
        "BancDep": deposito.banco_nombre if deposito else None,
        "DepNum": deposito.numero_deposito if deposito else None,
        "Efect": bool(factura.monto_efectivo), "Tar": bool(tarjeta),
    }
    subinformes = {
        "Secundario55": {"NombreBanco": campos["BancCh"], "N°Cheque": campos["ChqNum"]},
        "Secundario57": {"NombreBanco": campos["BancDep"], "N°Dep": campos["DepNum"]},
    }
    informe = InformeAccess.cargar("FacturaCxC" if original else "FacturaCons")
    return informe.render([Registro(campos, subinformes)], titulo=f"Recibo de pago {factura.numero or ''}")


# =============================================================================
# Solvencia del socio para un anio
# =============================================================================

_MESES_SOLVENCIA = ["Enero", "Febrero", "marzo", "Abril", "Mayo", "junio", "Julio", "Agosto",
                    "septiembre", "octubre", "noviembre", "diciembre"]


def solvencia_pdf(socio, anio: int, saldo_a_favor: Decimal = Decimal(0)) -> bytes:
    pagados = set(socio.mensualidades.filter(anio=anio, pagado=True).values_list("mes", flat=True))
    campos = {
        "IDSocio": socio.codigo,
        "ApelSocio": " ".join(filter(None, [socio.apellidos, socio.nombres])),
        "CI": socio.cedula, "AñoDeuda": str(anio), "Resto": saldo_a_favor,
        **{nombre: (i + 1) in pagados for i, nombre in enumerate(_MESES_SOLVENCIA)},
    }
    return InformeAccess.cargar("Solvencia").render([Registro(campos)], titulo=f"Solvencia {socio.codigo} {anio}")


# =============================================================================
# Egresos: comprobante (vaucher) y cheque
# =============================================================================

def letras_de(cheque) -> tuple[str, str]:
    """El texto guardado (el que se imprimio en su dia) o, si no hay, el generado."""
    if cheque.monto_en_letras:
        return cheque.monto_en_letras, cheque.monto_en_letras_2 or ""
    return partir_letras(monto_en_letras(cheque.monto or 0))


def _campos_egreso(cheque) -> dict:
    letras1, letras2 = letras_de(cheque)
    f = cheque.fecha or date.today()
    return {
        "MONTOTEXTO": formatear(cheque.monto or 0, "Standard"),
        "MONTO": cheque.monto, "BENEFICIARIO": cheque.beneficiario,
        "MONTO EN LETRAS": letras1, "Monto2": letras2,
        "FECHA (DIA Y MES)": f"{f.day}/{f.month}/", "Fecha(año)": str(f.year),
        "Lugar": f"{cheque.lugar or 'Lechería'}, ",
        "ConceptoPago": cheque.concepto, "NumeroCheque": cheque.numero_cheque,
        "BANCO": cheque.banco_nombre, "NumeroCuenta": cheque.numero_cuenta,
        "Preparado": cheque.preparado_por, "NÚMERO DE VAUCHER": cheque.numero_voucher,
    }


def comprobante_egreso_pdf(cheque) -> bytes:
    return InformeAccess.cargar("InfVaucher").render(
        [Registro(_campos_egreso(cheque))], titulo=f"Comprobante de egreso {cheque.numero_voucher or ''}")


def cheque_pdf(cheque) -> bytes:
    return InformeAccess.cargar("InfCheque").render(
        [Registro(_campos_egreso(cheque))], titulo=f"Cheque {cheque.numero_cheque or ''}")
