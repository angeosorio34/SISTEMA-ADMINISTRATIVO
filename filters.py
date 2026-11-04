import django_filters as df
from django.db.models import Q

from . import models as m
from .cobros import TIPOS_SOLO_SOCIOS


class SocioFilter(df.FilterSet):
    # Accion libre = sin titular (en Access: Nombres vacio; "De Acciones Libres")
    libre = df.BooleanFilter(field_name="nombres", lookup_expr="isnull")

    class Meta:
        model = m.Socio
        fields = {
            "tipo_socio": ["exact"],
            "exonerado": ["exact"],
            "provincia": ["exact"],
            "es_gallego": ["exact"],
            "fecha_ingreso": ["gte", "lte"],
        }


class FacturaFilter(df.FilterSet):
    fecha_desde = df.DateFilter(field_name="fecha_emision", lookup_expr="gte")
    fecha_hasta = df.DateFilter(field_name="fecha_emision", lookup_expr="lte")
    tipo_pago_codigo = df.CharFilter(field_name="tipo_pago__codigo", lookup_expr="iexact")
    socio_codigo = df.CharFilter(field_name="socio__codigo", lookup_expr="iexact")
    sin_socio = df.BooleanFilter(field_name="socio", lookup_expr="isnull")
    # ?otros_ingresos=true: recibos de "Recibos de Pago -> Por Otros Ingresos" (todo menos M001 y Z002).
    # Incluye los 843 recibos que Access guardó sin tipo de pago (GeneraFac no lo exigía).
    otros_ingresos = df.BooleanFilter(method="por_otros_ingresos")
    sin_tipo = df.BooleanFilter(field_name="tipo_pago", lookup_expr="isnull")
    # ?salon=true: alquileres y reservas del salón. En Access la tabla USOSALON quedó vacía; el uso real
    # del salón está en los recibos A001 "Alquiler del salón" y en los que lo nombran en el concepto.
    salon = df.BooleanFilter(method="por_salon")

    class Meta:
        model = m.Factura
        fields = ["numero", "socio", "tipo_pago", "anulada", "estado", "pago_divisas", "fecha_cancelacion"]

    def por_salon(self, qs, name, value):
        if not value:
            return qs
        return qs.filter(Q(tipo_pago__codigo="A001") | Q(descripcion__iregex=r"sal[oóÓ]n"))                  .exclude(tipo_pago__codigo__in=TIPOS_SOLO_SOCIOS)

    def por_otros_ingresos(self, qs, name, value):
        if value is None:
            return qs
        if value:
            return qs.exclude(tipo_pago__codigo__in=TIPOS_SOLO_SOCIOS)
        return qs.filter(tipo_pago__codigo__in=TIPOS_SOLO_SOCIOS)


class ActivoFilter(df.FilterSet):
    """?estado=uso (sin fecha de baja, "Listado de activos en uso") | baja; ?ubicacion= (exacta)."""
    estado = df.ChoiceFilter(choices=[("uso", "En uso"), ("baja", "De baja")], method="por_estado")
    ubicacion = df.CharFilter(field_name="ubicacion", lookup_expr="iexact")

    class Meta:
        model = m.ActivoFijo
        fields = ["codigo"]

    def por_estado(self, qs, name, value):
        return qs.filter(fecha_salida__isnull=(value == "uso"))


class UsoSalonFilter(df.FilterSet):
    """Modulo Uso del Salon (ConsultaSalon de Access): ?anio=2026, ?socio_codigo=P041."""
    anio = df.NumberFilter(field_name="fecha_uso", lookup_expr="year")
    socio_codigo = df.CharFilter(field_name="socio__codigo", lookup_expr="iexact")

    class Meta:
        model = m.UsoSalon
        fields = ["socio", "fecha_uso"]


class AbonoFilter(df.FilterSet):
    socio_codigo = df.CharFilter(field_name="socio__codigo", lookup_expr="iexact")

    class Meta:
        model = m.Abono
        fields = ["numero_documento", "socio", "factura_relacionada", "anio_deuda", "anulado"]


class PagoFilter(df.FilterSet):
    """Comun para cheques recibidos y depositos."""
    fecha_desde = df.DateFilter(field_name="fecha_pago", lookup_expr="gte")
    fecha_hasta = df.DateFilter(field_name="fecha_pago", lookup_expr="lte")
    socio_codigo = df.CharFilter(field_name="socio__codigo", lookup_expr="iexact")


class ChequeRecibidoFilter(PagoFilter):
    class Meta:
        model = m.ChequeRecibido
        fields = ["socio", "factura", "numero_cheque"]


class DepositoFilter(PagoFilter):
    class Meta:
        model = m.Deposito
        fields = ["socio", "factura", "banco", "numero_deposito"]


class ChequeEmitidoFilter(df.FilterSet):
    fecha_desde = df.DateFilter(field_name="fecha", lookup_expr="gte")
    fecha_hasta = df.DateFilter(field_name="fecha", lookup_expr="lte")

    class Meta:
        model = m.ChequeEmitido
        fields = ["banco", "anulado", "pago_divisas", "numero_cheque"]


class IncidenciaFilter(df.FilterSet):
    """?estado=pendiente | resuelta | corregida (corregida = arreglada automaticamente por el ETL)."""
    estado = df.ChoiceFilter(method="filtrar_estado", choices=[
        ("pendiente", "Pendiente"), ("resuelta", "Resuelta a mano"), ("corregida", "Corregida automaticamente")])

    class Meta:
        model = m.MigracionIncidencia
        fields = ["tabla_origen", "tipo", "resuelta"]

    def filtrar_estado(self, qs, nombre, valor):
        if valor == "corregida":
            return qs.filter(tipo="CORREGIDA")
        return qs.exclude(tipo="CORREGIDA").filter(resuelta=(valor == "resuelta"))


class RetencionIvaFilter(df.FilterSet):
    """?anio=2026&mes=11&quincena=1 (período del comprobante), ?proveedor_rif=, ?anulada=, ?pagada=true|false."""
    anio = df.NumberFilter(method="filtrar_anio")
    mes = df.NumberFilter(method="filtrar_mes")
    pagada = df.BooleanFilter(method="filtrar_pagada")

    class Meta:
        model = m.RetencionIva
        fields = ["quincena", "proveedor_rif", "anulada", "porcentaje_retencion"]

    def filtrar_anio(self, qs, nombre, valor):
        return qs.filter(fecha_comprobante__year=valor)

    def filtrar_mes(self, qs, nombre, valor):
        return qs.filter(fecha_comprobante__month=valor)

    def filtrar_pagada(self, qs, nombre, valor):
        pagadas = Q(egreso__isnull=False, egreso__anulado=False)
        return qs.filter(pagadas) if valor else qs.exclude(pagadas)
