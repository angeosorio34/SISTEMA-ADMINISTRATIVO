"""
Retenciones de IVA a proveedores (módulo «Retenciones IVA»).

El club, como agente de retención, retiene parte del IVA de las facturas de compra y paga al proveedor el resto:
  Monto de la factura (A) = base imponible + IVA + exento
  IVA retenido            = IVA x 75 %   (o 100 % en los casos de la norma)
  Monto a pagar           = A - IVA retenido  -> con 75 %: base + 25 % del IVA + exento; con 100 %: base + exento
Lo retenido se declara y se entera al SENIAT por quincena: «Monto total retenido = suma de las retenciones».

Comprobante de retención: número de 14 dígitos, AAAAMM (año y mes de emisión) + 8 dígitos secuenciales que no se
reinician cada mes. Referencias: Ley del IVA, art. 11; Providencia Administrativa SNAT/2015/0049. El contador del
club debe confirmar los porcentajes y el formato del archivo para el portal del SENIAT.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

DOS = Decimal("0.01")
PORCENTAJES = (75, 100)
ALICUOTAS = (Decimal("16"), Decimal("8"), Decimal("31"))     # general, reducida, general + lujo
MOTIVOS_100 = (
    "El proveedor no está inscrito en el RIF o su RIF no coincide",
    "La factura no discrimina el IVA o no cumple los requisitos",
    "El proveedor no ha presentado sus declaraciones de IVA",
    "Otro caso de la norma (ver observación)",
)
TOLERANCIA_IVA = Decimal("1.00")     # diferencia aceptada por redondeos de la factura (Bs)


def redondear(valor: Decimal) -> Decimal:
    return Decimal(valor).quantize(DOS, rounding=ROUND_HALF_UP)


# -----------------------------------------------------------------------------
# RIF venezolano: letra + 8 dígitos + dígito verificador
# -----------------------------------------------------------------------------

_VALOR_LETRA = {"V": 1, "E": 2, "J": 3, "P": 4, "G": 5}
_PESOS = (4, 3, 2, 7, 6, 5, 4, 3, 2)


def normalizar_rif(texto: str) -> str:
    """'j080144252', 'J-08014425-2', 'J 08014425 2' -> 'J-08014425-2'. ValueError si no tiene la forma de un RIF."""
    limpio = re.sub(r"[\s.\-]", "", (texto or "").upper())
    if not re.fullmatch(r"[VEJPGC]\d{9}", limpio):
        raise ValueError("El RIF debe ser una letra (V, E, J, P, G o C) y 9 dígitos, por ejemplo J-12345678-9.")
    return f"{limpio[0]}-{limpio[1:9]}-{limpio[9]}"


def digito_rif(rif: str) -> int | None:
    """Dígito verificador que corresponde a un RIF (None si la letra no tiene cálculo conocido)."""
    letra, numero = rif[0], rif[2:10]
    if letra not in _VALOR_LETRA:
        return None
    suma = sum(p * d for p, d in zip(_PESOS, [_VALOR_LETRA[letra], *map(int, numero)]))
    resto = 11 - suma % 11
    return 0 if resto >= 10 else resto


def rif_valido(rif: str) -> bool:
    esperado = digito_rif(rif)
    return esperado is None or esperado == int(rif[-1])


# -----------------------------------------------------------------------------
# Cálculo
# -----------------------------------------------------------------------------

def iva_esperado(base: Decimal, alicuota: Decimal) -> Decimal:
    return redondear(Decimal(base) * Decimal(alicuota) / 100)


def calcular(base: Decimal, iva: Decimal, exento: Decimal, porcentaje: int) -> dict:
    """Montos de la retención: los mismos del comprobante y del pago."""
    total = redondear(Decimal(base) + Decimal(iva) + Decimal(exento))
    retenido = redondear(Decimal(iva) * porcentaje / 100)
    return {"total_factura": total, "monto_retenido": retenido, "monto_pagar": total - retenido}


def periodo_y_quincena(fecha: date) -> tuple[str, int]:
    return f"{fecha:%Y%m}", 1 if fecha.day <= 15 else 2


def siguiente_numero(fecha_comprobante: date, ultimo: str | None) -> str:
    """AAAAMM de la fecha de emisión + secuencial de 8 dígitos (sigue del último comprobante emitido)."""
    secuencial = int(ultimo[6:]) + 1 if ultimo else 1
    if secuencial > 99_999_999:
        secuencial = 1          # la norma reinicia el secuencial solo al superar los 8 dígitos
    return f"{fecha_comprobante:%Y%m}{secuencial:08d}"


# -----------------------------------------------------------------------------
# Archivo para el portal del SENIAT (retenciones de IVA, texto separado por tabuladores)
# -----------------------------------------------------------------------------

def _monto(valor: Decimal) -> str:
    return f"{redondear(valor):.2f}"


def archivo_txt(retenciones, rif_agente: str) -> str:
    """Una línea por comprobante (sin los anulados), con los 16 campos del formato estándar:
    RIF agente, período, fecha factura, tipo de operación (C), tipo de documento, RIF proveedor, n.º factura,
    n.º control, total de la factura, base imponible, IVA retenido, documento afectado, n.º comprobante,
    monto exento, alícuota y n.º de expediente."""
    agente = rif_agente.replace("-", "")
    lineas = []
    for r in retenciones:
        if r.anulada:
            continue
        lineas.append("\t".join([
            agente, r.periodo, f"{r.fecha_factura:%Y-%m-%d}", "C", r.tipo_documento, r.proveedor_rif.replace("-", ""),
            r.numero_factura, r.numero_control, _monto(r.total_factura), _monto(r.base_imponible),
            _monto(r.monto_retenido), "0", r.numero_comprobante, _monto(r.monto_exento), _monto(r.alicuota), "0",
        ]))
    return "\r\n".join(lineas) + ("\r\n" if lineas else "")
