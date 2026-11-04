"""
PDF de retenciones de IVA: el comprobante que se entrega al proveedor y la relación del período.

Formato habitual del comprobante en Venezuela (Ley del IVA, art. 11; Providencia SNAT/2015/0049): número de 14
dígitos, fecha de emisión, período fiscal, datos del agente y del sujeto retenido, y por cada factura: fecha,
números de factura y de control, total con IVA, compras sin derecho a crédito (exento), base imponible, alícuota,
impuesto y monto retenido.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from xml.sax.saxutils import escape

from .. import club
from .motor import fuente

MESES = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre",
         "Noviembre", "Diciembre"]
GRIS = colors.HexColor("#d9e2e6")
LINEA = colors.HexColor("#5b6b70")


def bs(valor) -> str:
    """1234567.8 -> '1.234.567,80'."""
    texto = f"{Decimal(valor or 0):,.2f}"
    return texto.replace(",", "_").replace(".", ",").replace("_", ".")


def porcentaje(valor) -> str:
    """Decimal('16.00') -> '16'; Decimal('8.50') -> '8,5'."""
    return f"{Decimal(valor):.2f}".rstrip("0").rstrip(".").replace(".", ",")


def _fecha(d: date | None) -> str:
    return d.strftime("%d/%m/%Y") if d else ""


def _estilos():
    normal, negrita = fuente("arial", False, False), fuente("arial", True, False)
    return {
        "titulo": ParagraphStyle("titulo", fontName=negrita, fontSize=12.5, leading=15, alignment=1),
        "sub": ParagraphStyle("sub", fontName=normal, fontSize=7.5, leading=9.5, alignment=1, textColor=colors.HexColor("#333333")),
        "et": ParagraphStyle("et", fontName=negrita, fontSize=7.5, leading=9),
        "dato": ParagraphStyle("dato", fontName=normal, fontSize=9, leading=11),
        "celda": ParagraphStyle("celda", fontName=normal, fontSize=7.8, leading=9.5, alignment=1),
        "cab": ParagraphStyle("cab", fontName=negrita, fontSize=6.8, leading=8.2, alignment=1),
        "nota": ParagraphStyle("nota", fontName=normal, fontSize=7, leading=9, textColor=colors.HexColor("#333333")),
        "normal": normal, "negrita": negrita,
    }


def _caja(filas, anchos, e) -> Table:
    """Recuadros de datos: etiqueta pequeña arriba y el dato abajo."""
    celdas = [[[Paragraph(escape(et), e["et"]), Paragraph(escape(str(v)), e["dato"])] for et, v in fila] for fila in filas]
    t = Table(celdas, colWidths=anchos)
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, LINEA), ("INNERGRID", (0, 0), (-1, -1), 0.4, LINEA),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 3),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    return t


def _anulado(canvas, doc):
    canvas.saveState()
    canvas.setFont(fuente("arial", True, False), 70)
    canvas.setFillColor(colors.Color(0.8, 0.1, 0.1, alpha=0.18))
    canvas.translate(doc.pagesize[0] / 2, doc.pagesize[1] / 2)
    canvas.rotate(20)
    canvas.drawCentredString(0, 0, "ANULADO")
    canvas.restoreState()


def comprobante(r) -> bytes:
    e = _estilos()
    salida = BytesIO()
    doc = SimpleDocTemplate(salida, pagesize=landscape(letter), leftMargin=12 * mm, rightMargin=12 * mm,
                            topMargin=11 * mm, bottomMargin=11 * mm,
                            title=f"Comprobante de retención de IVA {r.numero_comprobante}", author=club.RAZON_SOCIAL)
    ancho = doc.width
    partes = [
        Paragraph("COMPROBANTE DE RETENCIÓN DEL IMPUESTO AL VALOR AGREGADO", e["titulo"]),
        Paragraph("Ley del IVA, artículo 11: la Administración Tributaria podrá designar como responsables del pago del "
                  "impuesto, en calidad de agentes de retención, a quienes por sus funciones públicas o por razón de sus "
                  "actividades privadas intervengan en operaciones gravadas con el impuesto. "
                  "Providencia Administrativa SNAT/2015/0049.", e["sub"]),
        Spacer(1, 5 * mm),
        _caja([[("N.º DE COMPROBANTE", r.numero_comprobante), ("FECHA DE EMISIÓN", _fecha(r.fecha_comprobante)),
                ("PERÍODO FISCAL", f"Año {r.periodo[:4]} · Mes {r.periodo[4:]} ({MESES[int(r.periodo[4:]) - 1]})"),
                ("QUINCENA", "1.ª (del 1 al 15)" if r.quincena == 1 else "2.ª (del 16 al fin de mes)")]],
              [ancho * 0.22, ancho * 0.18, ancho * 0.36, ancho * 0.24], e),
        Spacer(1, 3 * mm),
        _caja([[("NOMBRE O RAZÓN SOCIAL DEL AGENTE DE RETENCIÓN", club.RAZON_SOCIAL),
                 ("RIF DEL AGENTE DE RETENCIÓN", club.RIF)],
                [("DIRECCIÓN FISCAL DEL AGENTE DE RETENCIÓN", club.DIRECCION), ("TELÉFONO", club.TELEFONO)]],
              [ancho * 0.7, ancho * 0.3], e),
        Spacer(1, 3 * mm),
        _caja([[("NOMBRE O RAZÓN SOCIAL DEL SUJETO RETENIDO", r.proveedor_nombre),
                 ("RIF DEL SUJETO RETENIDO", r.proveedor_rif)]], [ancho * 0.7, ancho * 0.3], e),
        Spacer(1, 5 * mm),
    ]
    cab = ["Oper. N.º", "Fecha de la factura", "N.º de factura", "N.º de control", "Tipo de transacción",
           "Total compras incluyendo el IVA", "Compras sin derecho a crédito IVA", "Base imponible", "% alícuota",
           "Impuesto IVA", "% retención", "IVA retenido"]
    fila = ["1", _fecha(r.fecha_factura), r.numero_factura, r.numero_control, "01-Reg", bs(r.total_factura),
            bs(r.monto_exento), bs(r.base_imponible), porcentaje(r.alicuota), bs(r.monto_iva), f"{r.porcentaje_retencion}",
            bs(r.monto_retenido)]
    total = ["", "", "", "", "TOTALES", bs(r.total_factura), bs(r.monto_exento), bs(r.base_imponible), "",
             bs(r.monto_iva), "", bs(r.monto_retenido)]
    proporciones = [0.05, 0.08, 0.09, 0.1, 0.07, 0.11, 0.1, 0.1, 0.06, 0.09, 0.06, 0.09]
    t = Table([[Paragraph(c, e["cab"]) for c in cab], [Paragraph(escape(str(c)), e["celda"]) for c in fila],
               [Paragraph(f"<b>{escape(c)}</b>", e["celda"]) for c in total]],
              colWidths=[ancho * p for p in proporciones], repeatRows=1)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, LINEA), ("BACKGROUND", (0, 0), (-1, 0), GRIS),
                           ("BACKGROUND", (0, 2), (-1, 2), colors.HexColor("#eef2f4")),
                           ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 1), (-1, -1), 4),
                           ("BOTTOMPADDING", (0, 1), (-1, -1), 4)]))
    partes += [t, Spacer(1, 3 * mm)]
    nota = (f"Retención del {r.porcentaje_retencion} % del IVA."
            + (f" Motivo: {escape(r.motivo_100)}." if r.porcentaje_retencion == 100 and r.motivo_100 else "")
            + f" Monto pagado al proveedor: Bs {bs(r.monto_pagar)} (total de la factura menos el IVA retenido)."
            + (f" <b>ANULADO el {_fecha(r.fecha_anulacion)}: {escape(r.motivo_anulacion or '')}</b>" if r.anulada else ""))
    partes += [Paragraph(nota, e["nota"]), Spacer(1, 16 * mm)]
    firmas = Table([["______________________________________", "______________________________________"],
                    ["Agente de retención (sello y firma)", "Sujeto retenido: recibido por (nombre, C.I. y fecha)"]],
                   colWidths=[ancho / 2, ancho / 2])
    firmas.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER"), ("FONTNAME", (0, 0), (-1, -1), e["normal"]),
                                ("FONTSIZE", (0, 0), (-1, -1), 8.5)]))
    partes.append(firmas)
    alpie = _anulado if r.anulada else (lambda c, d: None)
    doc.build(partes, onFirstPage=alpie, onLaterPages=alpie)
    return salida.getvalue()


def relacion(retenciones: list, anio: int, mes: int, quincena: int | None) -> bytes:
    """Todos los comprobantes del período con la suma de lo retenido (lo que se declara y entera al SENIAT)."""
    e = _estilos()
    salida = BytesIO()
    periodo = f"{MESES[mes - 1]} {anio}" + (f", {'1.ª' if quincena == 1 else '2.ª'} quincena" if quincena else "")
    doc = SimpleDocTemplate(salida, pagesize=landscape(letter), leftMargin=10 * mm, rightMargin=10 * mm,
                            topMargin=10 * mm, bottomMargin=10 * mm, title=f"Relación de retenciones de IVA {periodo}",
                            author=club.RAZON_SOCIAL)
    ancho = doc.width
    partes = [Paragraph(f"{club.RAZON_SOCIAL} · RIF {club.RIF}", e["sub"]),
              Paragraph(f"RELACIÓN DE RETENCIONES DE IVA · {periodo.upper()}", e["titulo"]), Spacer(1, 4 * mm)]
    cab = ["N.º comprobante", "Fecha", "Proveedor", "RIF", "Factura", "Control", "Total factura", "Base imponible",
           "Exento", "IVA", "%", "IVA retenido", "Pagado"]
    filas = [[Paragraph(c, e["cab"]) for c in cab]]
    sumas = {k: Decimal(0) for k in ("total_factura", "base_imponible", "monto_exento", "monto_iva", "monto_retenido",
                                     "monto_pagar")}
    for r in retenciones:
        filas.append([Paragraph(escape(str(c)), e["celda"]) for c in (
            r.numero_comprobante, _fecha(r.fecha_comprobante), r.proveedor_nombre, r.proveedor_rif, r.numero_factura,
            r.numero_control, *((bs(getattr(r, k)) for k in ("total_factura", "base_imponible", "monto_exento",
                                                             "monto_iva")) if not r.anulada else ("ANULADO", "", "", "")),
            "" if r.anulada else str(r.porcentaje_retencion),
            "" if r.anulada else bs(r.monto_retenido), "" if r.anulada else bs(r.monto_pagar))])
        if not r.anulada:
            for k in sumas:
                sumas[k] += getattr(r, k)
    filas.append([Paragraph(f"<b>{escape(c)}</b>", e["celda"]) for c in (
        "", "", f"{sum(1 for r in retenciones if not r.anulada)} comprobante(s)", "", "", "TOTALES",
        bs(sumas["total_factura"]), bs(sumas["base_imponible"]), bs(sumas["monto_exento"]), bs(sumas["monto_iva"]), "",
        bs(sumas["monto_retenido"]), bs(sumas["monto_pagar"]))])
    proporciones = [0.1, 0.06, 0.16, 0.08, 0.06, 0.07, 0.08, 0.08, 0.06, 0.07, 0.03, 0.08, 0.07]
    t = Table(filas, colWidths=[ancho * p for p in proporciones], repeatRows=1)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, LINEA), ("BACKGROUND", (0, 0), (-1, 0), GRIS),
                           ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eef2f4")),
                           ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    partes += [t, Spacer(1, 4 * mm),
               Paragraph(f"<b>Monto total retenido del período: Bs {bs(sumas['monto_retenido'])}</b> "
                         "(suma de las retenciones; los comprobantes anulados no se suman).", e["dato"])]
    if not retenciones:
        partes.append(Paragraph("No hay comprobantes en este período.", e["dato"]))
    doc.build(partes)
    return salida.getvalue()
