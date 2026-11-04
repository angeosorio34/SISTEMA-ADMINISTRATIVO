from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import generics, mixins, viewsets
from rest_framework.decorators import action, api_view, throttle_classes
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from . import filters as f
from . import models as m
from . import serializers as s
from .permissions import GRUPO_ACCESO_TOTAL, tiene_acceso_total
from . import club, cobros, fotos, retenciones
from .reportes import documentos, listados, retenciones_pdf
from .seguridad import OperacionesPesadas


def _pdf(contenido: bytes, nombre: str) -> HttpResponse:
    """PDF para ver en el navegador (inline) con nombre de archivo sugerido."""
    respuesta = HttpResponse(contenido, content_type="application/pdf")
    respuesta["Content-Disposition"] = f'inline; filename="{nombre}.pdf"'
    return respuesta


def _entero(request, nombre, defecto):
    valor = request.query_params.get(nombre)
    if valor in (None, ""):
        return defecto
    try:
        return int(valor)
    except ValueError:
        raise ValidationError({nombre: "Debe ser un numero entero."})


# -----------------------------------------------------------------------------
# Autenticacion
# -----------------------------------------------------------------------------

@api_view(["GET"])
def yo(request):
    """Datos del usuario autenticado (el frontend lo usa para mostrar/ocultar acciones)."""
    u = request.user
    return Response({
        "username": u.username,
        "nombre": u.get_full_name() or u.username,
        "acceso_total": tiene_acceso_total(u),
        "grupos": list(u.groups.values_list("name", flat=True)),
    })


# -----------------------------------------------------------------------------
# Catalogos y parametros
# -----------------------------------------------------------------------------

class ProvinciaViewSet(viewsets.ModelViewSet):
    queryset = m.Provincia.objects.all()
    serializer_class = s.ProvinciaSerializer
    pagination_class = None


class TipoPagoViewSet(viewsets.ModelViewSet):
    queryset = m.TipoPago.objects.all()
    serializer_class = s.TipoPagoSerializer
    pagination_class = None


class BancoViewSet(viewsets.ModelViewSet):
    queryset = m.Banco.objects.all()
    serializer_class = s.BancoSerializer
    pagination_class = None


class NivelAccesoViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = m.NivelAcceso.objects.all()
    serializer_class = s.NivelAccesoSerializer
    pagination_class = None


class TarifaMensualViewSet(viewsets.ModelViewSet):
    queryset = m.TarifaMensual.objects.all()
    serializer_class = s.TarifaMensualSerializer
    filterset_fields = ["anio", "mes"]
    pagination_class = None

    @transaction.atomic
    def perform_update(self, serializer):
        """ActMontoAA de Access: al corregir la tarifa de un mes, la deuda de los meses aun no
        pagados de ese mes se recalcula con el monto nuevo."""
        tarifa = serializer.save()
        m.SocioMensualidad.objects.filter(anio=tarifa.anio, mes=tarifa.mes, pagado=False) \
            .exclude(monto_tarifa=None).update(monto_tarifa=tarifa.monto)


class CuotaExtraViewSet(viewsets.ModelViewSet):
    queryset = m.CuotaExtra.objects.all()
    serializer_class = s.CuotaExtraSerializer
    pagination_class = None


class CalendarioCobroViewSet(viewsets.ModelViewSet):
    queryset = m.CalendarioCobro.objects.all()
    serializer_class = s.CalendarioCobroSerializer
    pagination_class = None


class ConfiguracionView(generics.RetrieveUpdateAPIView):
    """GET / PATCH /api/configuracion/ (fila unica)."""
    serializer_class = s.ConfiguracionSerializer

    def get_object(self):
        obj = m.Configuracion.actual()
        self.check_object_permissions(self.request, obj)
        return obj


@api_view(["POST"])
@transaction.atomic
def nuevo_periodo(request):
    """
    Abre el siguiente periodo de cobro (equivale a "Reinicia Socios / Concesionarios (Nuevo Año)"
    de Access). Body: {"anio": 2027} -> debe ser exactamente periodo_actual + 1, como confirmacion.

    En Access se borraban las marcas ENE..DIC de SOCIOS tras copiar los morosos; aqui el historico
    queda intacto en socio_mensualidad y solo se crean los 12 meses pendientes del anio nuevo,
    se copian las tarifas mensuales y se avanza el periodo vigente.
    """
    if not tiene_acceso_total(request.user):
        return Response({"detail": "Solo un usuario con acceso total puede abrir un periodo."}, status=403)
    conf = m.Configuracion.objects.select_for_update().get(pk=1)
    nuevo = conf.periodo_actual + 1
    if request.data.get("anio") != nuevo:
        raise ValidationError({"anio": f"Para confirmar, envie anio={nuevo} (periodo actual {conf.periodo_actual})."})

    tarifas_previas = dict(m.TarifaMensual.objects.filter(anio=conf.periodo_actual).values_list("mes", "monto"))
    tarifas_nuevas = [m.TarifaMensual(anio=nuevo, mes=mes, monto=tarifas_previas.get(mes, conf.mensualidad or 0))
                      for mes in range(1, 13)
                      if not m.TarifaMensual.objects.filter(anio=nuevo, mes=mes).exists()]
    m.TarifaMensual.objects.bulk_create(tarifas_nuevas)

    m.SocioMensualidad.objects.bulk_create(
        [m.SocioMensualidad(socio_id=sid, anio=nuevo, mes=mes, pagado=False, origen="SOCIOS")
         for sid in m.Socio.objects.values_list("id", flat=True) for mes in range(1, 13)],
        ignore_conflicts=True)
    m.ConcesionMensualidad.objects.bulk_create(
        [m.ConcesionMensualidad(concesion_id=cid, anio=nuevo, mes=mes, pagado=False, origen="CONCESION")
         for cid in m.Concesion.objects.values_list("id", flat=True) for mes in range(1, 13)],
        ignore_conflicts=True)

    conf.periodo_actual = nuevo
    conf.save(update_fields=["periodo_actual"])
    return Response({
        "periodo_actual": nuevo,
        "socios": m.Socio.objects.count(),
        "concesiones": m.Concesion.objects.count(),
        "tarifas_creadas": len(tarifas_nuevas),
    })


class ConcesionViewSet(viewsets.ModelViewSet):
    queryset = m.Concesion.objects.select_related("tipo_pago")
    serializer_class = s.ConcesionSerializer
    pagination_class = None

    @action(detail=True)
    def mensualidades(self, request, pk=None):
        qs = self.get_object().mensualidades.all()
        if anio := request.query_params.get("anio"):
            qs = qs.filter(anio=anio)
        return Response(s.ConcesionMensualidadSerializer(qs, many=True).data)


# -----------------------------------------------------------------------------
# Socios
# -----------------------------------------------------------------------------

class SocioViewSet(viewsets.ModelViewSet):
    filterset_class = f.SocioFilter
    search_fields = ["codigo", "apellidos", "nombres", "cedula", "email", "celular"]
    ordering_fields = ["codigo", "apellidos", "fecha_ingreso"]

    def get_queryset(self):
        qs = m.Socio.objects.select_related("provincia")
        if self.action == "list":
            # Meta.ordering no se aplica a consultas con GROUP BY: ordenar explicitamente
            return qs.annotate(num_beneficiarios=Count("beneficiarios")).order_by("codigo")
        if self.action == "retrieve":
            return qs.prefetch_related("beneficiarios")
        return qs

    def get_serializer_class(self):
        return s.SocioListSerializer if self.action == "list" else s.SocioSerializer

    def perform_create(self, serializer):
        meses = serializer.validated_data.get("meses_no_cobrados") or []
        with transaction.atomic():
            socio = serializer.save()
            cobros.alta_accion(socio)
            cobros.marcar_meses_no_cobrados(socio, meses)

    def perform_update(self, serializer):
        meses = serializer.validated_data.get("meses_no_cobrados") or []
        with transaction.atomic():
            socio = serializer.save()
            cobros.marcar_meses_no_cobrados(socio, meses)

    @action(detail=True)
    def beneficiarios(self, request, pk=None):
        return Response(s.BeneficiarioSerializer(self.get_object().beneficiarios.all(), many=True).data)

    @action(detail=True)
    def mensualidades(self, request, pk=None):
        """?anio=2026 (por defecto, todos los anios)."""
        qs = self.get_object().mensualidades.all()
        if anio := request.query_params.get("anio"):
            qs = qs.filter(anio=anio)
        return Response(s.SocioMensualidadSerializer(qs, many=True).data)

    @action(detail=True, url_path="cuotas-extra", methods=["get", "post"])
    def cuotas_extra(self, request, pk=None):
        """GET: cuotas extraordinarias del socio. POST {cuota_extra, pagada, numero_factura}: marca a mano
        una cuota como pagada o pendiente (equivale a "tildar" en Access)."""
        socio = self.get_object()
        if request.method == "POST":
            if not tiene_acceso_total(request.user):
                return Response({"detail": "Su nivel de acceso es de consulta."}, status=403)
            cuota = generics.get_object_or_404(m.CuotaExtra, pk=request.data.get("cuota_extra"))
            pagada, factura = bool(request.data.get("pagada")), (request.data.get("numero_factura") or None)
            if not pagada and not factura:
                # Pendiente y sin recibo = sin registro (asi estaba antes de marcarla)
                m.SocioCuotaExtra.objects.filter(socio=socio, cuota_extra=cuota).delete()
            else:
                m.SocioCuotaExtra.objects.update_or_create(
                    socio=socio, cuota_extra=cuota, defaults={"pagada": pagada, "numero_factura": factura})
        qs = socio.cuotas_extra.select_related("cuota_extra")
        return Response(s.SocioCuotaExtraSerializer(qs, many=True).data)

    @action(detail=True, url_path="estado-cuenta")
    def estado_cuenta(self, request, pk=None):
        """Resumen financiero del socio: totales y ultimos movimientos."""
        socio = self.get_object()
        facturas = socio.facturas.filter(anulada=False)
        abonos = socio.abonos.filter(anulado=False)
        periodo = m.Configuracion.actual().periodo_actual
        pendientes = socio.mensualidades.filter(anio=periodo, pagado=False).values_list("mes", flat=True)
        return Response({
            "socio": s.SocioListSerializer(m.Socio.objects.annotate(num_beneficiarios=Count("beneficiarios"))
                                           .select_related("provincia").get(pk=socio.pk)).data,
            "periodo": periodo,
            "meses_pendientes_periodo": sorted(pendientes),
            "total_facturado": facturas.aggregate(t=Sum("monto"))["t"] or Decimal(0),
            "total_abonado": abonos.aggregate(t=Sum("monto"))["t"] or Decimal(0),
            "saldo_a_favor": cobros.saldo_a_favor(socio),
            "ultimas_facturas": s.FacturaSerializer(facturas.select_related("tipo_pago")[:10], many=True).data,
            "ultimos_abonos": s.AbonoSerializer(abonos[:10], many=True).data,
        })

    @action(detail=True)
    def deuda(self, request, pk=None):
        """Caja: lo que el socio debe (meses de cada año y cuotas extra), lo ya pagado y su saldo a favor."""
        return Response(cobros.deuda_de(self.get_object()))

    @action(detail=True)
    def solvencia(self, request, pk=None):
        """PDF de solvencia con el formato de Access. ?anio=2026 (por defecto, el periodo actual)."""
        socio = self.get_object()
        anio = _entero(request, "anio", m.Configuracion.actual().periodo_actual)
        return _pdf(documentos.solvencia_pdf(socio, anio), f"solvencia-{socio.codigo}-{anio}")

    def _solo_acceso_total(self, request):
        if not tiene_acceso_total(request.user):
            raise PermissionDenied("Su nivel de acceso es de consulta.")

    @action(detail=True, methods=["post"], url_path="pagar-con-abono")
    def pagar_con_abono(self, request, pk=None):
        """Pago con abono: {"anio": 2026, "meses": [8, 9], "cuotas": []} usando el saldo a favor."""
        self._solo_acceso_total(request)
        socio = self.get_object()
        try:
            nota = cobros.pagar_con_abono(socio, int(request.data.get("anio") or m.Configuracion.actual().periodo_actual),
                                          [int(x) for x in request.data.get("meses") or []],
                                          [int(x) for x in request.data.get("cuotas") or []])
        except cobros.ErrorCobro as err:
            raise ValidationError(err.args[0])
        return Response({"nota_credito": s.AbonoSerializer(nota).data, "saldo_a_favor": cobros.saldo_a_favor(socio)})

    @action(detail=False, methods=["post"], url_path="agregar-acciones")
    def agregar_acciones(self, request):
        """Ingresa acciones nuevas libres: {"tipo": "P" | "U", "cantidad": 1}."""
        self._solo_acceso_total(request)
        try:
            nuevas = cobros.agregar_acciones(str(request.data.get("tipo", "")).upper(), int(request.data.get("cantidad") or 0))
        except (cobros.ErrorCobro, ValueError) as err:
            raise ValidationError(err.args[0] if isinstance(err, cobros.ErrorCobro) else {"cantidad": "Número no válido."})
        return Response({"codigos": [x.codigo for x in nuevas]}, status=201)

    @action(detail=True, methods=["post"])
    def liberar(self, request, pk=None):
        """Liberar accion: baja del titular y sus beneficiarios; la accion y su historial se conservan."""
        self._solo_acceso_total(request)
        return Response(cobros.liberar_accion(self.get_object()))

    @action(detail=True, methods=["get", "post", "delete"])
    def foto(self, request, pk=None):
        return _gestionar_foto(request, self.get_object(), "socios", self)

    @action(detail=False)
    def morosos(self, request):
        """
        Socios con mensualidades sin pagar.
          ?anio=2026      (por defecto, el periodo actual)
          ?hasta_mes=10   (por defecto: mes en curso si es el anio actual, si no 12)
        Excluye socios exonerados, acciones libres y las que no pagan cuota de mantenimiento
        (Band de Access, p. ej. acciones adicionales). El monto estimado usa la tarifa del mes
        (o la registrada en el reporte de morosos de Access cuando existe).
        """
        hoy = date.today()
        anio = _entero(request, "anio", m.Configuracion.actual().periodo_actual)
        hasta_mes = _entero(request, "hasta_mes", hoy.month if anio == hoy.year else 12)
        tarifas = dict(m.TarifaMensual.objects.filter(anio=anio).values_list("mes", "monto"))

        pendientes = (m.SocioMensualidad.objects
                      .filter(anio=anio, mes__lte=hasta_mes, pagado=False,
                              socio__exonerado=False, socio__exonerado_total=False,
                              socio__band_legado=True,        # "paga cuota de mantenimiento" (Band)
                              socio__nombres__isnull=False)   # acciones libres: sin titular, no deben
                      .select_related("socio")
                      .order_by("socio__codigo", "mes"))
        agrupado = defaultdict(lambda: {"meses": [], "monto": Decimal(0), "socio": None})
        for p in pendientes:
            g = agrupado[p.socio_id]
            g["socio"] = p.socio
            g["meses"].append(p.mes)
            g["monto"] += p.monto_tarifa if p.monto_tarifa is not None else tarifas.get(p.mes, Decimal(0))

        resultado = [{
            "socio_id": sid,
            "codigo": g["socio"].codigo,
            "nombre_completo": g["socio"].nombre_completo,
            "tipo_socio": g["socio"].tipo_socio,
            "meses_pendientes": g["meses"],
            "cantidad_meses": len(g["meses"]),
            "monto_estimado": g["monto"],
        } for sid, g in agrupado.items()]

        pagina = self.paginate_queryset(resultado)
        if pagina is not None:
            return self.get_paginated_response(s.MorosoSerializer(pagina, many=True).data)
        return Response(s.MorosoSerializer(resultado, many=True).data)


class ConcesionMensualidadViewSet(viewsets.ModelViewSet):
    """Meses de cada concesionario (pagado / numero de recibo), como el subformulario CONC de Access."""
    queryset = m.ConcesionMensualidad.objects.select_related("concesion")
    serializer_class = s.ConcesionMensualidadSerializer
    filterset_fields = ["concesion", "anio", "pagado"]
    pagination_class = None


class BeneficiarioViewSet(viewsets.ModelViewSet):
    queryset = m.Beneficiario.objects.select_related("socio")
    serializer_class = s.BeneficiarioSerializer
    filterset_fields = ["socio", "activo", "parentesco"]
    search_fields = ["codigo", "nombre_completo", "cedula", "socio__codigo"]

    @action(detail=True, methods=["get", "post", "delete"])
    def foto(self, request, pk=None):
        return _gestionar_foto(request, self.get_object(), "beneficiarios", self)


class SocioMensualidadViewSet(viewsets.ModelViewSet):
    queryset = m.SocioMensualidad.objects.select_related("socio")
    serializer_class = s.SocioMensualidadSerializer
    filterset_fields = ["socio", "anio", "mes", "pagado", "origen"]


# -----------------------------------------------------------------------------
# Facturacion y cobros
# -----------------------------------------------------------------------------

class FacturaViewSet(viewsets.ModelViewSet):
    """Un recibo emitido no se borra ni se reescribe: solo se anula (PATCH anulada + fecha_anulacion)."""
    queryset = m.Factura.objects.select_related("socio", "tipo_pago")
    http_method_names = ["get", "post", "patch", "head", "options"]
    serializer_class = s.FacturaSerializer
    filterset_class = f.FacturaFilter
    search_fields = ["numero", "numero_control", "cliente_nombre", "cliente_rif", "descripcion"]
    ordering_fields = ["fecha_emision", "numero", "monto"]

    @transaction.atomic
    def perform_create(self, serializer):
        """Sin numero explicito, toma el siguiente de configuracion.ultima_factura (bloqueando la
        fila para que dos cajeros simultaneos no obtengan el mismo numero)."""
        conf = m.Configuracion.objects.select_for_update().get(pk=1)
        extra = {}
        if not serializer.validated_data.get("numero"):
            extra["numero"] = cobros.siguiente(conf, "ultima_factura", 5)
        if not serializer.validated_data.get("numero_control"):
            # Access asignaba tambien el numero de control (UltCont) a cada recibo
            extra["numero_control"] = cobros.siguiente(conf, "ultimo_numero_control", 7)
        # Access (ImpFacInd) ponia FecGenFact, FecTOPFact y FecCanc = Date(): el recibo nace cobrado.
        # Sin fecha de cancelacion no aparece en Ingresos ni en el Estado de resultados.
        emision = serializer.validated_data.get("fecha_emision") or date.today()
        extra.setdefault("fecha_emision", emision)
        if not serializer.validated_data.get("fecha_tope"):
            extra["fecha_tope"] = emision
        if not serializer.validated_data.get("fecha_cancelacion"):
            extra["fecha_cancelacion"] = emision
        serializer.save(**extra)

    @transaction.atomic
    def perform_update(self, serializer):
        """Al anular un recibo se devuelve a pendiente todo lo que ese recibo habia marcado."""
        antes = serializer.instance.anulada
        factura = serializer.save()
        if factura.anulada and not antes:
            cobros.revertir_recibo(factura)

    @action(detail=True)
    def pdf(self, request, pk=None):
        """Recibo de pago con el formato de Access.
        ?tipo=original -> informe FacturaCxC (al emitir); por defecto copia (FacturaCons)."""
        factura = self.get_object()
        original = request.query_params.get("tipo") == "original"
        nombre = f"recibo-{factura.numero or factura.pk}{'' if original else '-copia'}"
        return _pdf(documentos.factura_pdf(factura, original=original), nombre)

    @action(detail=True)
    def pagos(self, request, pk=None):
        """Abonos, cheques y depositos asociados a la factura."""
        factura = self.get_object()
        return Response({
            "abonos": s.AbonoSerializer(factura.abonos.all(), many=True).data,
            "cheques": s.ChequeRecibidoSerializer(factura.cheques.all(), many=True).data,
            "depositos": s.DepositoSerializer(factura.depositos.all(), many=True).data,
        })


class AbonoViewSet(viewsets.ReadOnlyModelViewSet):
    """Los abonos y notas de crédito nacen de los cobros (saldo a favor); aquí solo se consultan o anulan."""
    queryset = m.Abono.objects.select_related("socio")
    serializer_class = s.AbonoSerializer
    filterset_class = f.AbonoFilter
    search_fields = ["numero_documento", "numero_factura_relacionada", "cliente_nombre"]
    ordering_fields = ["fecha_cancelacion", "monto"]

    @action(detail=True, methods=["post"])
    def anular(self, request, pk=None):
        """Anula un abono o nota de credito (y revierte lo que pago, si era una nota de credito)."""
        if not tiene_acceso_total(request.user):
            raise PermissionDenied("Su nivel de acceso es de consulta.")
        try:
            return Response(cobros.anular_abono(self.get_object()))
        except cobros.ErrorCobro as err:
            raise ValidationError(err.args[0])


class ChequeRecibidoViewSet(viewsets.ModelViewSet):
    queryset = m.ChequeRecibido.objects.select_related("socio")
    serializer_class = s.ChequeRecibidoSerializer
    filterset_class = f.ChequeRecibidoFilter
    search_fields = ["numero_cheque", "banco_emisor", "descripcion", "numero_factura"]
    ordering_fields = ["fecha_pago", "monto"]


class DepositoViewSet(viewsets.ModelViewSet):
    queryset = m.Deposito.objects.select_related("socio", "banco")
    serializer_class = s.DepositoSerializer
    filterset_class = f.DepositoFilter
    search_fields = ["numero_deposito", "banco_nombre", "descripcion", "numero_factura"]
    ordering_fields = ["fecha_pago", "monto"]


class ChequeEmitidoViewSet(viewsets.ModelViewSet):
    queryset = m.ChequeEmitido.objects.select_related("banco")
    serializer_class = s.ChequeEmitidoSerializer
    filterset_class = f.ChequeEmitidoFilter
    search_fields = ["numero_cheque", "beneficiario", "concepto", "numero_voucher"]
    ordering_fields = ["fecha", "monto"]

    @transaction.atomic
    def perform_create(self, serializer):
        """Sin voucher explicito, toma el siguiente de configuracion.ultimo_voucher (UltV en Access)."""
        if serializer.validated_data.get("numero_voucher"):
            serializer.save()
            self._fijar_letras(serializer.instance)
            return
        conf = m.Configuracion.objects.select_for_update().get(pk=1)
        conf.ultimo_voucher = (conf.ultimo_voucher or 0) + 1
        conf.save(update_fields=["ultimo_voucher"])
        serializer.save(numero_voucher=str(conf.ultimo_voucher).zfill(5))
        self._fijar_letras(serializer.instance)

    @staticmethod
    def _fijar_letras(cheque):
        """El monto en letras queda guardado al emitir: las reimpresiones salen identicas."""
        if not cheque.monto_en_letras:
            cheque.monto_en_letras, cheque.monto_en_letras_2 = documentos.partir_letras(
                documentos.monto_en_letras(cheque.monto or 0))
            cheque.monto_en_letras_2 = cheque.monto_en_letras_2 or None
            cheque.save(update_fields=["monto_en_letras", "monto_en_letras_2"])

    @action(detail=True)
    def comprobante(self, request, pk=None):
        """Comprobante de egreso (vaucher) con el formato de Access."""
        cheque = self.get_object()
        return _pdf(documentos.comprobante_egreso_pdf(cheque), f"comprobante-{cheque.numero_voucher or cheque.pk}")

    @action(detail=True, url_path="imprimir-cheque")
    def imprimir_cheque(self, request, pk=None):
        """Cheque para imprimir sobre la chequera preimpresa (formato de Access)."""
        cheque = self.get_object()
        return _pdf(documentos.cheque_pdf(cheque), f"cheque-{cheque.numero_cheque or cheque.pk}")


class LibroDiarioViewSet(viewsets.ReadOnlyModelViewSet):
    """Libro diario heredado de Access (LIBDIA): solo consulta."""
    queryset = m.LibroDiario.objects.all()
    serializer_class = s.LibroDiarioSerializer
    filterset_fields = ["socio", "factura", "tipo_pago", "anulada"]
    search_fields = ["numero_factura", "cliente_nombre", "numero_voucher"]


# -----------------------------------------------------------------------------
# Otros
# -----------------------------------------------------------------------------

class ProveedorViewSet(viewsets.ModelViewSet):
    queryset = m.Proveedor.objects.all()
    serializer_class = s.ProveedorSerializer
    search_fields = ["codigo", "nombre_empresa", "suministro", "representante"]


class ActivoFijoViewSet(viewsets.ModelViewSet):
    """Activos fijos (MenuActivos de Access): ingreso, consulta / modificacion y bajas."""
    queryset = m.ActivoFijo.objects.order_by(F("codigo").asc(nulls_last=True), "descripcion")
    serializer_class = s.ActivoFijoSerializer
    filterset_class = f.ActivoFilter
    search_fields = ["codigo", "descripcion", "ubicacion"]
    ordering_fields = ["codigo", "descripcion", "ubicacion", "cantidad", "fecha_ingreso", "fecha_salida"]

    @action(detail=False)
    def resumen(self, request):
        """Totales (registros y unidades en uso y de baja), ubicaciones usadas y la siguiente referencia."""
        def totales(qs):
            t = qs.aggregate(registros=Count("id"), unidades=Sum("cantidad"))
            return {"registros": t["registros"], "unidades": t["unidades"] or 0}
        ubicaciones = {u.strip() for u in m.ActivoFijo.objects.exclude(ubicacion__isnull=True)
                       .values_list("ubicacion", flat=True) if u and u.strip()}
        return Response({
            "en_uso": totales(m.ActivoFijo.objects.filter(fecha_salida__isnull=True)),
            "de_baja": totales(m.ActivoFijo.objects.filter(fecha_salida__isnull=False)),
            "ubicaciones": sorted(ubicaciones, key=str.upper),
            "siguiente_referencia": s.siguiente_referencia_activo(),
        })


class UsoSalonViewSet(viewsets.ModelViewSet):
    queryset = m.UsoSalon.objects.select_related("socio")
    serializer_class = s.UsoSalonSerializer
    filterset_class = f.UsoSalonFilter
    search_fields = ["socio__codigo", "socio__apellidos", "socio__nombres", "observacion"]
    ordering_fields = ["fecha_uso"]


class MigracionIncidenciaViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = m.MigracionIncidencia.objects.all()
    serializer_class = s.MigracionIncidenciaSerializer
    filterset_class = f.IncidenciaFilter
    search_fields = ["clave_origen", "detalle"]

    @action(detail=True, methods=["post"])
    def resolver(self, request, pk=None):
        """Marca una incidencia pendiente como resuelta. Body: {"nota": "..."}.
        Con {"reabrir": true} la vuelve a dejar pendiente."""
        incidencia = self.get_object()
        if request.data.get("reabrir"):
            incidencia.resuelta, incidencia.nota_resolucion = False, None
            incidencia.resuelta_por, incidencia.resuelta_en = None, None
        else:
            nota = (request.data.get("nota") or "").strip()
            if not nota:
                raise ValidationError({"nota": "Explique como se resolvio."})
            incidencia.resuelta, incidencia.nota_resolucion = True, nota
            incidencia.resuelta_por, incidencia.resuelta_en = request.user.username, timezone.now()
        incidencia.save(update_fields=["resuelta", "nota_resolucion", "resuelta_por", "resuelta_en"])
        return Response(s.MigracionIncidenciaSerializer(incidencia).data)


# -----------------------------------------------------------------------------
# Panel principal
# -----------------------------------------------------------------------------

def _gestionar_foto(request, obj, carpeta: str, vista):
    """GET devuelve la foto; POST (multipart "foto") la reemplaza; DELETE la quita."""
    from pathlib import Path

    from django.conf import settings
    from django.http import FileResponse, Http404

    if request.method == "GET":
        ruta = fotos.ruta_media(obj.ruta_foto)       # nunca las rutas de Access (red / disco)
        if ruta is None:
            raise Http404("Sin foto.")
        return FileResponse(open(ruta, "rb"))
    if not tiene_acceso_total(request.user):
        raise PermissionDenied("Su nivel de acceso es de consulta.")
    if request.method == "DELETE":
        obj.ruta_foto = None
        obj.save(update_fields=["ruta_foto"])
        return Response(status=204)
    archivo = request.FILES.get("foto")
    if archivo is None:
        raise ValidationError({"foto": "Adjunte una imagen."})
    try:
        contenido, extension = fotos.normalizar(archivo)
    except fotos.FotoInvalida as e:
        raise ValidationError({"foto": str(e)})
    relativa = fotos.relativa_para(carpeta, obj.codigo, extension)
    destino = Path(settings.MEDIA_ROOT) / relativa
    destino.parent.mkdir(parents=True, exist_ok=True)
    for vieja in destino.parent.glob(f"{destino.stem}.*"):      # la foto anterior con otra extensión
        if vieja != destino:
            vieja.unlink(missing_ok=True)
    destino.write_bytes(contenido)
    obj.ruta_foto = relativa
    obj.save(update_fields=["ruta_foto"])
    return Response({"ruta_foto": relativa}, status=201)


@api_view(["GET"])
def caja_divisas(request):
    """Saldo en caja (divisas): saldo inicial + ingresos - egresos en divisas desde la fecha de inicio."""
    return Response(listados.caja_divisas())


@api_view(["GET"])
def reportes_catalogo(request):
    """Informes impresos disponibles (los listados de Access), agrupados por modulo."""
    return Response(listados.catalogo())


@api_view(["GET"])
def reporte_pdf(request, clave):
    """GET /api/reportes/<clave>/?desde=&hasta=&anio=&hasta_mes=&socio=... -> PDF con el formato de Access."""
    listado = listados.LISTADOS.get(clave)
    if listado is None:
        return Response({"detail": f"No existe el informe '{clave}'."}, status=404)
    try:
        parametros = listados.leer_parametros(listado, request.query_params, m.Configuracion.actual().periodo_actual)
    except ValueError as e:
        raise ValidationError(e.args[0])
    try:
        return _pdf(listados.generar(clave, parametros), clave.replace("facturas", "recibos"))
    except LookupError as e:
        return Response({"detail": str(e)}, status=404)


@api_view(["GET"])
def caja_dia(request):
    """Cuadre de caja: recibos cobrados en un dia y totales por forma de pago. ?fecha=AAAA-MM-DD (hoy)."""
    texto = request.query_params.get("fecha")
    try:
        dia = date.fromisoformat(texto) if texto else date.today()
    except ValueError:
        raise ValidationError({"fecha": "Use el formato AAAA-MM-DD."})
    del_dia = m.Factura.objects.filter(fecha_cancelacion=dia)
    validos = del_dia.filter(anulada=False)
    totales = validos.aggregate(
        total=Sum("monto"), efectivo=Sum("monto_efectivo"), deposito=Sum("monto_deposito"),
        cheque=Sum("monto_cheque"), tarjeta_debito=Sum("monto_tarjeta_debito"),
        tarjeta_credito=Sum("monto_tarjeta_credito"), divisas=Sum("monto_divisas", filter=Q(pago_divisas=True)))
    abonos = m.Abono.objects.filter(fecha_cancelacion=dia, anulado=False)
    return Response({
        "fecha": dia,
        "recibos": validos.count(),
        "anulados": del_dia.filter(anulada=True).count(),
        **{k: v if v is not None else Decimal(0) for k, v in totales.items()},
        "abonos": abonos.filter(monto__gt=0).aggregate(t=Sum("monto"))["t"] or Decimal(0),
        "notas_credito": abonos.filter(monto__lt=0).count(),
    })


@api_view(["GET"])
def resumen(request):
    """Indicadores para la pantalla de inicio del frontend."""
    conf = m.Configuracion.actual()
    hoy = date.today()
    hasta_mes = hoy.month if conf.periodo_actual == hoy.year else 12
    socios = m.Socio.objects.aggregate(
        total=Count("id"),
        propietarios=Count("id", filter=Q(tipo_socio="P")),
        uso=Count("id", filter=Q(tipo_socio="U")),
        exonerados=Count("id", filter=Q(exonerado=True) | Q(exonerado_total=True)),
    )
    facturas_periodo = m.Factura.objects.filter(fecha_emision__year=conf.periodo_actual, anulada=False)
    morosos = (m.SocioMensualidad.objects
               .filter(anio=conf.periodo_actual, mes__lte=hasta_mes, pagado=False,
                       socio__exonerado=False, socio__exonerado_total=False, socio__band_legado=True,
                       socio__nombres__isnull=False)
               .values("socio").distinct().count())
    return Response({
        "periodo_actual": conf.periodo_actual,
        "socios": socios,
        "beneficiarios_activos": m.Beneficiario.objects.filter(activo=True).count(),
        "facturas_periodo": facturas_periodo.count(),
        "monto_facturado_periodo": facturas_periodo.aggregate(t=Sum("monto"))["t"] or Decimal(0),
        "socios_morosos_periodo": morosos,
        # Acciones sin titular: las que el club tiene disponibles para la venta
        "acciones_disponibles": m.Socio.objects.filter(nombres__isnull=True).aggregate(
            total=Count("id"), propietario=Count("id", filter=Q(tipo_socio="P")),
            uso=Count("id", filter=Q(tipo_socio="U"))),
        "incidencias_pendientes": m.MigracionIncidencia.objects.exclude(tipo="CORREGIDA")
                                                               .filter(resuelta=False).count(),
        "grupo_escritura": GRUPO_ACCESO_TOTAL,
    })


# -----------------------------------------------------------------------------
# Tasa del dólar del BCV (menú lateral y Configuración)
# -----------------------------------------------------------------------------

@api_view(["GET", "POST"])
def tasa_bcv(request):
    """GET: tasa vigente, la próxima publicada y el estado de la consulta (?historial=30 agrega las últimas).
    POST {tasa, fecha_valor} (acceso total): escribirla a mano cuando el BCV no responde."""
    from . import tasa_bcv as tb

    if request.method == "GET":
        return Response(tb.estado(historial=min(_entero(request, "historial", 0), 100)))
    if not tiene_acceso_total(request.user):
        raise PermissionDenied("Solo un usuario con acceso total puede cargar la tasa.")
    try:
        valor = tb.a_decimal(str(request.data.get("tasa") or ""))
    except tb.TasaError as e:
        raise ValidationError({"tasa": str(e)})
    try:
        fecha_valor = date.fromisoformat(str(request.data.get("fecha_valor") or ""))
    except ValueError:
        raise ValidationError({"fecha_valor": "Indique la fecha valor (AAAA-MM-DD)."})
    hoy = timezone.localdate()
    if not hoy - timedelta(days=60) <= fecha_valor <= hoy + timedelta(days=7):
        raise ValidationError({"fecha_valor": "La fecha valor debe ser de los últimos 60 días o de la próxima semana."})
    try:
        return Response(tb.registrar(valor, fecha_valor, "manual", request.user.username), status=201)
    except tb.TasaError as e:
        raise ValidationError({"tasa": str(e)})


@api_view(["POST"])
@throttle_classes([OperacionesPesadas])
def tasa_bcv_actualizar(request):
    """POST: consulta el BCV ahora (acceso total)."""
    from . import tasa_bcv as tb

    if not tiene_acceso_total(request.user):
        raise PermissionDenied("Solo un usuario con acceso total puede actualizar la tasa.")
    try:
        return Response(tb.actualizar())
    except tb.TasaError as e:
        return Response({"detail": str(e)}, status=502)


# -----------------------------------------------------------------------------
# Red local (Configuración -> Red local y fotos)
# -----------------------------------------------------------------------------

@api_view(["GET"])
def red_local(request):
    """Cómo entran las demás computadoras del club y en qué quedaron las fotos que Access tenía en la red."""
    import ntpath
    import socket
    import sys
    from collections import Counter

    from django.conf import settings

    from sicga.settings import _direcciones_locales
    from .seguridad import ip_de

    if not tiene_acceso_total(request.user):
        raise PermissionDenied("Solo un usuario con acceso total puede ver la configuración de red.")
    nombre, puerto = socket.gethostname(), settings.PUERTO
    direcciones = _direcciones_locales()

    def contar(modelo, condicion):
        rutas = list(modelo.objects.filter(condicion).values_list("ruta_foto", flat=True))
        en_sistema = sum(1 for r in rutas if r and not fotos.es_ruta_legada(r) and fotos.ruta_media(r))
        de_access = [r for r in rutas if r and fotos.es_ruta_legada(r)]
        return en_sistema, de_access

    socios_ok, socios_access = contar(m.Socio, Q(nombres__isnull=False))
    benef_ok, benef_access = contar(m.Beneficiario, Q())
    carpetas = Counter(ntpath.dirname(r) for r in socios_access + benef_access)
    cliente = ip_de(request)
    return Response({
        "equipo": nombre,
        "puerto": puerto,
        "red_local": settings.RED_LOCAL,
        "servidor": "waitress" if len(sys.argv) > 1 and sys.argv[1] == "servir" else "desarrollo",
        "enlaces": [f"http://{nombre}:{puerto}/", *(f"http://{ip}:{puerto}/" for ip in direcciones)],
        "desde": cliente,
        "esta_computadora": cliente in ("127.0.0.1", "::1"),
        "fotos": {
            "socios_en_sistema": socios_ok, "beneficiarios_en_sistema": benef_ok,
            "socios_solo_en_access": len(socios_access), "beneficiarios_solo_en_access": len(benef_access),
            "carpeta_access": carpetas.most_common(1)[0][0] if carpetas else None,
        },
    })


# -----------------------------------------------------------------------------
# Retenciones de IVA
# -----------------------------------------------------------------------------

class RetencionIvaViewSet(mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                          viewsets.GenericViewSet):
    """Facturas de proveedores con la retención de IVA. Sin modificar ni borrar: un comprobante emitido se anula."""
    queryset = m.RetencionIva.objects.select_related("egreso")
    serializer_class = s.RetencionIvaSerializer
    filterset_class = f.RetencionIvaFilter
    search_fields = ["numero_comprobante", "proveedor_nombre", "proveedor_rif", "numero_factura", "numero_control"]
    ordering_fields = ["fecha_comprobante", "numero_comprobante", "fecha_factura", "monto_retenido"]

    @transaction.atomic
    def perform_create(self, serializer):
        datos = serializer.validated_data
        # La fila de configuración hace de candado: dos usuarios a la vez no toman el mismo número
        m.Configuracion.objects.select_for_update().get(pk=1)
        fecha = datos["fecha_comprobante"]
        numero = datos.get("numero_comprobante")
        if numero:
            if numero[:6] != f"{fecha:%Y%m}":
                raise ValidationError({"numero_comprobante": f"Debe empezar por {fecha:%Y%m} (año y mes de la fecha "
                                                             "del comprobante)."})
            if m.RetencionIva.objects.filter(numero_comprobante=numero).exists():
                raise ValidationError({"numero_comprobante": "Ya existe un comprobante con ese número."})
        else:
            numero = retenciones.siguiente_numero(fecha, self._ultimo_secuencial())
        periodo, quincena = retenciones.periodo_y_quincena(fecha)
        serializer.save(numero_comprobante=numero, periodo=periodo, quincena=quincena,
                        creado_por=self.request.user.username)

    @staticmethod
    def _ultimo_secuencial() -> str | None:
        """Comprobante con el secuencial más alto (el secuencial no se reinicia cada mes)."""
        numeros = m.RetencionIva.objects.values_list("numero_comprobante", flat=True)
        return max(numeros, key=lambda n: int(n[6:]), default=None)

    @action(detail=True, methods=["post"])
    def anular(self, request, pk=None):
        retencion = self.get_object()
        if retencion.anulada:
            raise ValidationError({"detail": "El comprobante ya está anulado."})
        motivo = (request.data.get("motivo") or "").strip()
        if not motivo:
            raise ValidationError({"motivo": "Indique el motivo de la anulación."})
        retencion.anulada, retencion.fecha_anulacion, retencion.motivo_anulacion = True, date.today(), motivo[:200]
        retencion.save(update_fields=["anulada", "fecha_anulacion", "motivo_anulacion"])
        return Response(self.get_serializer(retencion).data)

    @action(detail=True)
    def comprobante(self, request, pk=None):
        """Comprobante de retención de IVA (PDF) para entregar al proveedor."""
        retencion = self.get_object()
        return _pdf(retenciones_pdf.comprobante(retencion), f"comprobante-retencion-{retencion.numero_comprobante}")

    def _del_periodo(self, request):
        anio, mes = _entero(request, "anio", None), _entero(request, "mes", None)
        if not anio or not mes or not 1 <= mes <= 12:
            raise ValidationError({"detail": "Indique el año y el mes (y si quiere, la quincena)."})
        qs = m.RetencionIva.objects.filter(fecha_comprobante__year=anio, fecha_comprobante__month=mes)
        quincena = _entero(request, "quincena", None)
        if quincena not in (None, 1, 2):
            raise ValidationError({"quincena": "La quincena es 1 o 2."})
        if quincena:
            qs = qs.filter(quincena=quincena)
        return anio, mes, quincena, qs.order_by("numero_comprobante")

    @action(detail=False)
    def resumen(self, request):
        """Totales del período (sin anulados): «Monto total retenido = suma de las retenciones»."""
        anio, mes, quincena, qs = self._del_periodo(request)
        validas = qs.filter(anulada=False)
        campos = ["base_imponible", "monto_iva", "monto_exento", "total_factura", "monto_retenido", "monto_pagar"]
        totales = validas.aggregate(**{c: Sum(c) for c in campos}, comprobantes=Count("id"))
        por_quincena = {}
        for q in (1, 2):
            x = validas.filter(quincena=q).aggregate(retenido=Sum("monto_retenido"), comprobantes=Count("id"))
            por_quincena[q] = {"retenido": x["retenido"] or 0, "comprobantes": x["comprobantes"]}
        return Response({"anio": anio, "mes": mes, "quincena": quincena, "anulados": qs.filter(anulada=True).count(),
                         **{c: totales[c] or 0 for c in campos}, "comprobantes": totales["comprobantes"],
                         "por_quincena": por_quincena})

    @action(detail=False)
    def txt(self, request):
        """Archivo de las retenciones del período para el portal del SENIAT (texto separado por tabuladores)."""
        anio, mes, quincena, qs = self._del_periodo(request)
        contenido = retenciones.archivo_txt(qs, club.RIF)
        nombre = f"retenciones-iva-{anio}{mes:02d}" + (f"-quincena{quincena}" if quincena else "") + ".txt"
        respuesta = HttpResponse(contenido, content_type="text/plain; charset=utf-8")
        respuesta["Content-Disposition"] = f'attachment; filename="{nombre}"'
        return respuesta

    @action(detail=False)
    def relacion(self, request):
        """Relación de comprobantes del período (PDF) con el total retenido."""
        anio, mes, quincena, qs = self._del_periodo(request)
        nombre = f"relacion-retenciones-{anio}{mes:02d}" + (f"-q{quincena}" if quincena else "")
        return _pdf(retenciones_pdf.relacion(list(qs), anio, mes, quincena), nombre)

    @action(detail=False)
    def proveedores(self, request):
        """Proveedores ya usados (RIF y nombre más reciente), para completar el formulario."""
        vistos = {}
        for rif, nombre in m.RetencionIva.objects.order_by("-creado_en").values_list("proveedor_rif", "proveedor_nombre"):
            vistos.setdefault(rif, nombre)
        return Response([{"rif": r, "nombre": n} for r, n in sorted(vistos.items(), key=lambda x: x[1])])
