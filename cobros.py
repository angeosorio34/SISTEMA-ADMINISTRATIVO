"""
Logica de cobro de SICGA, equivalente a los formularios PagoMensualidad, PagoMensualidadAA,
GeneraFac, FormAnulaFac, AgregAcc y Socios ("Liberar accion") de Access.

  * Al emitir un recibo: marca los meses (de cualquier anio), las cuotas extraordinarias y los
    meses del concesionario con el numero del recibo; registra el cheque y el deposito recibidos
    (rechazando los repetidos, como Access) y, si se pide, deja el recibo como abono a cuenta.
  * "Pago con abono": paga meses/cuotas con el saldo a favor del socio, creando la nota de
    credito "NCmmaa" con monto negativo; si el saldo queda en 0 cierra los abonos (CierrAbono).
  * Al anular un recibo: devuelve a pendiente todo lo que ese recibo habia marcado y reabre los
    abonos que habia cerrado (AnulAbono/AnulAbonoC). En Access esto ultimo se hacia en dos
    pasos manuales y, si se olvidaba el segundo, los meses quedaban "pagados" con un recibo nulo.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import Max, Q, Sum

from . import models as m

MESES = ["ENE", "FEB", "MAR", "ABR", "MAY", "JUN", "JUL", "AGO", "SEP", "OCT", "NOV", "DIC"]

# Tipos de pago que solo se cobran a socios en la Caja (PagoMensualidad / cuotas extra). Todos los
# demas son "otros ingresos" (GeneraFac de Access: alquiler del salon, concesionarios, eventos,
# carnet, comite de damas, otros), igual que el filtro COD<>"M001" And COD<>"Z002" de ese formulario.
TIPOS_SOLO_SOCIOS = ("M001", "Z002")


class ErrorCobro(Exception):
    """Error de validacion con campo: ErrorCobro({"meses_pagados": "..."})."""


# ---------------------------------------------------------------- numeracion

def siguiente(conf: m.Configuracion, campo: str, ancho: int) -> str:
    """Siguiente numero correlativo (ultima_factura, ultimo_numero_control, ultimo_voucher)."""
    valor = (getattr(conf, campo) or 0) + 1
    setattr(conf, campo, valor)
    conf.save(update_fields=[campo])
    return str(valor).zfill(ancho)


# ---------------------------------------------------------------- validaciones previas

def validar_cobro(socio, anio: int, meses: list[int], cuotas: list[int], concesion, meses_concesion: list[int],
                  anio_concesion: int, cheque: dict | None, deposito: dict | None) -> None:
    errores = {}
    if (meses or cuotas) and socio is None:
        errores["socio"] = "Indique el socio al que se aplican los meses o cuotas."
    if meses and socio is not None:
        filas = {r.mes: r for r in m.SocioMensualidad.objects.filter(socio=socio, anio=anio, mes__in=meses)}
        faltan = [MESES[x - 1] for x in meses if x not in filas]
        pagados = [MESES[x - 1] for x in meses if x in filas and filas[x].pagado]
        if faltan:
            errores["meses_pagados"] = f"{', '.join(faltan)} {anio}: el socio no tiene esos meses registrados."
        elif pagados:
            errores["meses_pagados"] = f"{', '.join(pagados)} {anio} ya estaban pagados."
    if cuotas and socio is not None:
        definidas = set(m.CuotaExtra.objects.filter(numero__in=cuotas).values_list("numero", flat=True))
        pagadas = set(m.SocioCuotaExtra.objects.filter(socio=socio, pagada=True, cuota_extra__numero__in=cuotas)
                      .values_list("cuota_extra__numero", flat=True))
        if set(cuotas) - definidas:
            errores["cuotas_pagadas"] = "Hay cuotas extraordinarias que no existen."
        elif pagadas:
            errores["cuotas_pagadas"] = f"Las cuotas {', '.join(map(str, sorted(pagadas)))} ya estaban pagadas."
    if meses_concesion:
        if concesion is None:
            errores["meses_concesion"] = "El tipo de pago elegido no corresponde a un concesionario."
        else:
            pagados = m.ConcesionMensualidad.objects.filter(concesion=concesion, anio=anio_concesion,
                                                            mes__in=meses_concesion, pagado=True)
            if pagados.exists():
                errores["meses_concesion"] = (f"{', '.join(MESES[x.mes - 1] for x in pagados)} {anio_concesion} "
                                              "ya estaban pagados por el concesionario.")
    # Access: "Ya existe ese cheque / deposito en la base de datos"
    if cheque and cheque.get("numero"):
        if m.ChequeRecibido.objects.filter(numero_cheque=cheque["numero"].strip(),
                                           numero_cuenta=(cheque.get("cuenta") or "").strip() or None).exists():
            errores["cheque"] = f"Ya existe el cheque {cheque['numero']} de esa cuenta."
    if deposito and deposito.get("numero"):
        filtro = Q(numero_deposito=deposito["numero"].strip())
        if deposito.get("banco"):
            filtro &= Q(banco_id=deposito["banco"])
        if m.Deposito.objects.filter(filtro).exists():
            errores["deposito"] = f"Ya existe el depósito {deposito['numero']} en ese banco."
    if errores:
        raise ErrorCobro(errores)


# ---------------------------------------------------------------- aplicar un recibo

def aplicar_recibo(factura: m.Factura, anio: int, meses: list[int], cuotas: list[int], concesion,
                   meses_concesion: list[int], anio_concesion: int, cheque: dict | None, deposito: dict | None,
                   abono: bool) -> None:
    numero = factura.numero
    if meses:
        m.SocioMensualidad.objects.filter(socio=factura.socio, anio=anio, mes__in=meses).update(
            pagado=True, numero_factura=numero)
    for n in cuotas:
        cuota = m.CuotaExtra.objects.get(numero=n)
        m.SocioCuotaExtra.objects.update_or_create(socio=factura.socio, cuota_extra=cuota,
                                                   defaults={"pagada": True, "numero_factura": numero})
    if meses_concesion and concesion is not None:
        for mes in meses_concesion:
            m.ConcesionMensualidad.objects.update_or_create(
                concesion=concesion, anio=anio_concesion, mes=mes,
                defaults={"pagado": True, "numero_factura": numero, "origen": "CONCESION"})
    if cheque and cheque.get("numero"):
        m.ChequeRecibido.objects.create(
            socio=factura.socio, numero_cheque=cheque["numero"].strip(),
            numero_cuenta=(cheque.get("cuenta") or "").strip() or None,
            banco_emisor=(cheque.get("banco") or "").strip().upper() or None, fecha_pago=factura.fecha_emision,
            descripcion=factura.descripcion, monto=factura.monto_cheque, numero_factura=numero, factura=factura)
    if deposito and deposito.get("numero"):
        banco = m.Banco.objects.filter(pk=deposito.get("banco")).first() if deposito.get("banco") else None
        m.Deposito.objects.create(
            socio=factura.socio, numero_deposito=deposito["numero"].strip(), banco=banco,
            banco_nombre=banco.nombre if banco else (deposito.get("banco_nombre") or None),
            fecha_pago=factura.fecha_emision, descripcion=factura.descripcion, monto=factura.monto_deposito,
            numero_factura=numero, factura=factura)
    if abono:
        # Abono a cuenta: queda pendiente de aplicar (Estado = 0 en ABONOS), suma al saldo a favor
        m.Abono.objects.create(
            numero_documento=numero, socio=factura.socio, cliente_nombre=factura.cliente_nombre,
            cliente_rif=factura.cliente_rif, tipo_pago=factura.tipo_pago, descripcion=factura.descripcion,
            fecha_cancelacion=factura.fecha_emision, monto=factura.monto, monto_cheque=factura.monto_cheque,
            monto_deposito=factura.monto_deposito, monto_tarjeta_debito=factura.monto_tarjeta_debito,
            monto_tarjeta_credito=factura.monto_tarjeta_credito, monto_efectivo=factura.monto_efectivo,
            estado=False, anio_deuda=m.Configuracion.actual().periodo_actual)


# ---------------------------------------------------------------- anular un recibo

def revertir_recibo(factura: m.Factura) -> dict:
    """Deshace lo que marco el recibo. Devuelve cuantos registros se revirtieron."""
    numero, socio = factura.numero, factura.socio
    resultado = {"meses": 0, "cuotas": 0, "meses_concesion": 0, "abonos_reabiertos": 0, "abonos_anulados": 0}
    if not numero:
        return resultado
    if socio is not None:
        resultado["meses"] = m.SocioMensualidad.objects.filter(socio=socio, numero_factura=numero, pagado=True) \
            .update(pagado=False, numero_factura=None)
        resultado["cuotas"] = m.SocioCuotaExtra.objects.filter(socio=socio, numero_factura=numero).delete()[0]
        # AnulAbonoC: los abonos que este recibo habia cerrado vuelven a estar pendientes
        resultado["abonos_reabiertos"] = m.Abono.objects.filter(
            socio=socio, numero_factura_relacionada=numero, estado=True, anulado=False
        ).update(estado=False, numero_factura_relacionada=None, factura_relacionada=None)
        # AnulAbono: si el recibo era un abono a cuenta, el abono queda anulado
        resultado["abonos_anulados"] = m.Abono.objects.filter(socio=socio, numero_documento=numero, anulado=False) \
            .update(anulado=True, fecha_anulacion=factura.fecha_anulacion or date.today())
    concesion = m.Concesion.objects.filter(tipo_pago_id=factura.tipo_pago_id).first() if factura.tipo_pago_id else None
    if concesion is not None:
        resultado["meses_concesion"] = m.ConcesionMensualidad.objects.filter(
            concesion=concesion, numero_factura=numero, pagado=True).update(pagado=False, numero_factura=None)
    return resultado


# ---------------------------------------------------------------- abonos

def saldo_a_favor(socio: m.Socio) -> Decimal:
    """AboAnt de Access: suma de los abonos pendientes (positivos) y notas de credito (negativas)."""
    return socio.abonos.filter(estado=False, anulado=False).aggregate(t=Sum("monto"))["t"] or Decimal(0)


def monto_de(socio: m.Socio, anio: int, meses: list[int], cuotas: list[int]) -> Decimal:
    tarifas = dict(m.TarifaMensual.objects.filter(anio=anio).values_list("mes", "monto"))
    total = Decimal(0)
    for fila in m.SocioMensualidad.objects.filter(socio=socio, anio=anio, mes__in=meses):
        total += fila.monto_tarifa if fila.monto_tarifa is not None else tarifas.get(fila.mes, Decimal(0))
    total += m.CuotaExtra.objects.filter(numero__in=cuotas).aggregate(t=Sum("monto"))["t"] or Decimal(0)
    return total


@transaction.atomic
def pagar_con_abono(socio: m.Socio, anio: int, meses: list[int], cuotas: list[int]) -> m.Abono:
    """"Pago con Abono" de PagoMensualidad: sin dinero nuevo, con el saldo a favor."""
    if not meses and not cuotas:
        raise ErrorCobro({"meses_pagados": "Seleccione los meses o cuotas que se pagan con el abono."})
    validar_cobro(socio, anio, meses, cuotas, None, [], anio, None, None)
    monto = monto_de(socio, anio, meses, cuotas)
    saldo = saldo_a_favor(socio)
    if monto <= 0:
        raise ErrorCobro({"meses_pagados": "Los meses seleccionados no tienen tarifa definida."})
    if monto > saldo:
        raise ErrorCobro({"monto": f"El saldo a favor (Bs {saldo:,.2f}) no alcanza para cubrir Bs {monto:,.2f}."})
    # Numero de la nota de credito como en Access: "NC" + mes (13..24 = cuota extra) + anio de 2 digitos
    codigo_mes = (max(cuotas) + 12) if cuotas else max(meses)
    numero = f"NC{codigo_mes:02d}{anio % 100:02d}"
    descripcion = ", ".join([MESES[x - 1] for x in sorted(meses)] + [f"CUOTA {c}" for c in sorted(cuotas)]) + f" {anio}"
    nota = m.Abono.objects.create(
        numero_documento=numero, socio=socio, cliente_nombre=socio.nombre_completo, cliente_rif=socio.cedula,
        tipo_pago=m.TipoPago.objects.filter(codigo="Z002" if cuotas and not meses else "M001").first(),
        descripcion=descripcion, fecha_cancelacion=date.today(), monto=-monto, estado=False, anio_deuda=anio)
    m.SocioMensualidad.objects.filter(socio=socio, anio=anio, mes__in=meses).update(pagado=True, numero_factura=numero)
    for n in cuotas:
        m.SocioCuotaExtra.objects.update_or_create(socio=socio, cuota_extra=m.CuotaExtra.objects.get(numero=n),
                                                   defaults={"pagada": True, "numero_factura": numero})
    if saldo - monto == 0:
        # CierrAbono: el saldo se consumio entero -> los abonos quedan cerrados contra esta nota
        socio.abonos.filter(estado=False, anulado=False).update(estado=True, numero_factura_relacionada=numero)
    return nota


@transaction.atomic
def anular_abono(abono: m.Abono) -> dict:
    """"Anular Solvencias x Abonos" (FormPagxAbono): anula un abono o nota de credito.
    Si es una nota de credito (pago con abono), los meses/cuotas que pago vuelven a pendiente y los
    abonos que habia cerrado se reabren, de modo que el saldo a favor vuelve a estar disponible."""
    if abono.anulado:
        raise ErrorCobro({"abono": "El abono ya estaba anulado."})
    numero, socio = abono.numero_documento, abono.socio
    meses = cuotas = reabiertos = 0
    if abono.monto < 0:
        meses = m.SocioMensualidad.objects.filter(socio=socio, numero_factura=numero, pagado=True) \
            .update(pagado=False, numero_factura=None)
        cuotas = m.SocioCuotaExtra.objects.filter(socio=socio, numero_factura=numero).delete()[0]
        reabiertos = m.Abono.objects.filter(socio=socio, numero_factura_relacionada=numero, anulado=False) \
            .exclude(pk=abono.pk).update(estado=False, numero_factura_relacionada=None)
    abono.anulado, abono.fecha_anulacion = True, date.today()
    abono.save(update_fields=["anulado", "fecha_anulacion"])
    return {"meses": meses, "cuotas": cuotas, "abonos_reabiertos": reabiertos, "saldo_a_favor": saldo_a_favor(socio)}


# ---------------------------------------------------------------- acciones

@transaction.atomic
def agregar_acciones(tipo: str, cantidad: int) -> list[m.Socio]:
    """AgregAcc: crea acciones nuevas (libres, sin titular) con el siguiente numero P### o U###."""
    if tipo not in ("P", "U") or not 1 <= cantidad <= 100:
        raise ErrorCobro({"cantidad": "Indique el tipo (P o U) y una cantidad entre 1 y 100."})
    numeros = [int(c[1:]) for c in m.Socio.objects.filter(codigo__startswith=tipo).values_list("codigo", flat=True)
               if c[1:].isdigit()]
    siguiente_num = max(numeros or [0]) + 1
    conf = m.Configuracion.objects.select_for_update().get(pk=1)
    nuevas = []
    for i in range(cantidad):
        socio = m.Socio.objects.create(codigo=f"{tipo}{siguiente_num + i:03d}", tipo_socio=tipo)
        m.SocioMensualidad.objects.bulk_create([m.SocioMensualidad(socio=socio, anio=conf.periodo_actual, mes=mes,
                                                                   pagado=False, origen="SOCIOS") for mes in range(1, 13)])
        nuevas.append(socio)
    campo = "total_acciones_propietario" if tipo == "P" else "total_acciones_uso"
    setattr(conf, campo, (getattr(conf, campo) or 0) + cantidad)
    conf.save(update_fields=[campo])
    return nuevas


def deuda_de(socio: m.Socio) -> dict:
    """Caja (PagoMensualidad / PagoMensualidadAA de Access): los meses del periodo y de cada año con
    deuda, con su monto y el recibo con que se pagaron; las cuotas extraordinarias; el saldo a favor
    y lo vencido. No cuentan como deuda la accion libre, la exonerada ni la que no paga mantenimiento."""
    periodo = m.Configuracion.actual().periodo_actual
    hoy = date.today()
    mes_actual = 12 if periodo < hoy.year else hoy.month if periodo == hoy.year else 0
    tarifas = {(t.anio, t.mes): t.monto for t in m.TarifaMensual.objects.all()}
    filas = sorted(socio.mensualidades.all(), key=lambda f: (f.anio, f.mes))

    def monto(f):
        return f.monto_tarifa if f.monto_tarifa is not None else tarifas.get((f.anio, f.mes), Decimal(0))

    anios = sorted({f.anio for f in filas if not f.pagado} | {periodo}, reverse=True)
    exento = socio.nombres is None or socio.exonerado or socio.exonerado_total or not socio.band_legado
    # Tarifa del periodo muy por debajo de las demas (p. ej. enero 2026 = Bs 12 en Access): se avisa en la caja
    del_periodo = sorted(v for (a, _), v in tarifas.items() if a == periodo and v)
    mediana = del_periodo[len(del_periodo) // 2] if del_periodo else Decimal(0)

    def dudoso(f):
        return f.anio == periodo and mediana > 0 and monto(f) < mediana / 100

    def resumen(condicion):
        elegidas = [] if exento else [f for f in filas if not f.pagado and condicion(f)]
        return {"meses": len(elegidas), "monto": sum((monto(f) for f in elegidas), Decimal(0))}

    marcadas = {c.cuota_extra_id: c for c in socio.cuotas_extra.all()}
    cuotas = []
    for c in m.CuotaExtra.objects.order_by("numero"):
        propia = marcadas.get(c.id)
        if (c.monto or 0) <= 0 and not propia:
            continue
        cuotas.append({"numero": c.numero, "descripcion": c.descripcion, "monto": c.monto or Decimal(0),
                       "pagada": bool(propia and propia.pagada),
                       "numero_factura": propia.numero_factura if propia else None})
    pendientes = [] if socio.nombres is None else [c for c in cuotas if not c["pagada"]]
    return {
        "periodo": periodo, "mes_actual": mes_actual,
        "libre": socio.nombres is None, "exonerado": socio.exonerado or socio.exonerado_total,
        "paga_mantenimiento": socio.band_legado,
        "saldo_a_favor": saldo_a_favor(socio),
        "anios": [{"anio": a, "meses": [{"mes": f.mes, "pagado": f.pagado, "numero_factura": f.numero_factura,
                                         "monto": monto(f), "dudoso": dudoso(f)} for f in filas if f.anio == a]}
                  for a in anios],
        "cuotas": cuotas,
        "vencido_periodo": resumen(lambda f: f.anio == periodo and f.mes <= mes_actual),
        "anios_anteriores": resumen(lambda f: f.anio < periodo),
        "cuotas_pendientes": {"cuotas": len(pendientes), "monto": sum((c["monto"] for c in pendientes), Decimal(0))},
    }


@transaction.atomic
def alta_accion(socio: m.Socio) -> None:
    """Accion nueva creada ya con su titular ("Nuevo socio"): como AgregAcc, crea las 12 mensualidades
    del periodo (sin ellas no se le podria cobrar ni figuraria como moroso) y la suma al total de su tipo."""
    conf = m.Configuracion.objects.select_for_update().get(pk=1)
    m.SocioMensualidad.objects.bulk_create(
        [m.SocioMensualidad(socio=socio, anio=conf.periodo_actual, mes=mes, pagado=False, origen="SOCIOS")
         for mes in range(1, 13)], ignore_conflicts=True)
    campo = "total_acciones_propietario" if socio.tipo_socio == "P" else "total_acciones_uso"
    setattr(conf, campo, (getattr(conf, campo) or 0) + 1)
    conf.save(update_fields=[campo])


def marcar_meses_no_cobrados(socio: m.Socio, meses: list[int]) -> int:
    """Casillas "Señal de mes facturado" del formulario IngSocios de Access: al vender una accion libre
    (o dar de alta un socio a mitad de año) los meses del periodo que el nuevo titular no debe quedan
    pagados sin recibo, igual que al tildarlos a mano."""
    if not meses:
        return 0
    periodo = m.Configuracion.actual().periodo_actual
    return m.SocioMensualidad.objects.filter(socio=socio, anio=periodo, mes__in=meses, pagado=False) \
        .update(pagado=True)


# Datos del titular que se borran al liberar la accion. "observaciones" no: es el historial de la
# accion ("Traspaso de...", Obs3 en Access), que LimpiaAcc tambien conservaba.
DATOS_PERSONALES = ["apellidos", "nombres", "cedula", "grupo_sanguineo", "fecha_nacimiento", "lugar_nacimiento",
                    "nacionalidad", "provincia", "estado_civil", "profesion", "direccion_habitacion",
                    "telefono_habitacion", "celular", "email", "email_alterno", "empresa", "direccion_trabajo",
                    "web_trabajo", "telefono_trabajo", "fax", "marca_vehiculo", "placa_vehiculo", "aficiones",
                    "tiempo_en_ciudad", "domicilio_cobro", "fecha_ingreso", "referencia_1", "referencia_2",
                    "ruta_foto"]


@transaction.atomic
def liberar_accion(socio: m.Socio) -> dict:
    """"Liberar Accion" (ElimSocBen + LimpiaAcc): da de baja al titular y a sus beneficiarios.
    La accion (codigo, tipo) y todo su historial de recibos y mensualidades se conservan."""
    beneficiarios = socio.beneficiarios.count()
    socio.beneficiarios.all().delete()
    for campo in DATOS_PERSONALES:
        setattr(socio, campo, None)
    socio.accion_adicional = socio.es_gallego = socio.ascendencia_gallega = False
    socio.exonerado = socio.exonerado_total = False
    socio.band_legado = False          # Band = 0 en LimpiaAcc: la accion libre no paga mantenimiento
    socio.save()
    return {"codigo": socio.codigo, "beneficiarios_eliminados": beneficiarios}
