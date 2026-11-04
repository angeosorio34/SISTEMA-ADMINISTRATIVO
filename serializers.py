import re
from decimal import Decimal

from django.utils import timezone
from rest_framework import serializers

from . import cobros, retenciones
from . import models as m


# -----------------------------------------------------------------------------
# Catalogos y parametros
# -----------------------------------------------------------------------------

class NivelAccesoSerializer(serializers.ModelSerializer):
    class Meta:
        model = m.NivelAcceso
        fields = "__all__"


class ProvinciaSerializer(serializers.ModelSerializer):
    class Meta:
        model = m.Provincia
        fields = "__all__"


class TipoPagoSerializer(serializers.ModelSerializer):
    class Meta:
        model = m.TipoPago
        fields = "__all__"


class BancoSerializer(serializers.ModelSerializer):
    class Meta:
        model = m.Banco
        fields = "__all__"


class ConfiguracionSerializer(serializers.ModelSerializer):
    class Meta:
        model = m.Configuracion
        fields = "__all__"


class CalendarioCobroSerializer(serializers.ModelSerializer):
    class Meta:
        model = m.CalendarioCobro
        fields = "__all__"


class TarifaMensualSerializer(serializers.ModelSerializer):
    class Meta:
        model = m.TarifaMensual
        fields = "__all__"


class CuotaExtraSerializer(serializers.ModelSerializer):
    class Meta:
        model = m.CuotaExtra
        fields = "__all__"


class ConcesionSerializer(serializers.ModelSerializer):
    tipo_pago_codigo = serializers.CharField(source="tipo_pago.codigo", read_only=True)

    class Meta:
        model = m.Concesion
        fields = "__all__"


class ConcesionMensualidadSerializer(serializers.ModelSerializer):
    concesion_codigo = serializers.CharField(source="concesion.tipo_pago.codigo", read_only=True)

    class Meta:
        model = m.ConcesionMensualidad
        fields = "__all__"


# -----------------------------------------------------------------------------
# Socios
# -----------------------------------------------------------------------------

class BeneficiarioSerializer(serializers.ModelSerializer):
    socio_codigo = serializers.CharField(source="socio.codigo", read_only=True)

    class Meta:
        model = m.Beneficiario
        fields = "__all__"
        read_only_fields = ["ruta_foto"]          # solo con POST .../foto/
        # Sin codigo, se asigna el siguiente del socio (P001-1, P001-2, ...) como hacia Access
        extra_kwargs = {"codigo": {"required": False}}

    def create(self, validated_data):
        if not validated_data.get("codigo"):
            socio = validated_data["socio"]
            usados = [b.split("-")[-1] for b in socio.beneficiarios.values_list("codigo", flat=True)]
            siguiente = max([int(n) for n in usados if n.isdigit()] or [0]) + 1
            validated_data["codigo"] = f"{socio.codigo}-{siguiente}"
        return super().create(validated_data)


class SocioListSerializer(serializers.ModelSerializer):
    """Version ligera para listados y buscadores."""
    nombre_completo = serializers.CharField(read_only=True)
    provincia_nombre = serializers.CharField(source="provincia.nombre", read_only=True, default=None)
    num_beneficiarios = serializers.IntegerField(read_only=True)

    class Meta:
        model = m.Socio
        fields = ["id", "codigo", "tipo_socio", "nombre_completo", "apellidos", "nombres", "cedula",
                  "celular", "email", "fecha_ingreso", "exonerado", "provincia_nombre", "num_beneficiarios",
                  "band_legado", "observaciones"]


class SocioSerializer(serializers.ModelSerializer):
    nombre_completo = serializers.CharField(read_only=True)
    provincia_nombre = serializers.CharField(source="provincia.nombre", read_only=True, default=None)
    beneficiarios = BeneficiarioSerializer(many=True, read_only=True)
    # Meses del periodo que el nuevo titular no debe (casillas "Señal de mes facturado" de IngSocios)
    meses_no_cobrados = serializers.ListField(child=serializers.IntegerField(min_value=1, max_value=12),
                                              write_only=True, required=False)
    # El nuevo titular firmó la autorización de tratamiento de sus datos (planilla de inscripción): sin esa
    # confirmación no se registran datos personales (Aviso de privacidad, /legal/privacidad)
    consentimiento_datos = serializers.BooleanField(write_only=True, required=False, default=False)

    class Meta:
        model = m.Socio
        fields = "__all__"
        read_only_fields = ["ruta_foto"]          # solo con POST .../foto/

    def validate_codigo(self, value):
        value = value.strip().upper()
        if value[:1] not in ("P", "U"):
            raise serializers.ValidationError("El codigo debe empezar por P (propietario) o U (uso).")
        return value

    def validate(self, attrs):
        codigo = attrs.get("codigo", getattr(self.instance, "codigo", ""))
        tipo = attrs.get("tipo_socio", getattr(self.instance, "tipo_socio", None))
        if codigo and tipo and codigo[0] != tipo:
            raise serializers.ValidationError({"tipo_socio": "Debe coincidir con la letra inicial del codigo."})
        # Venta de una accion libre (IngSocios): el titular necesita apellidos propios; "CENTRO GALLEGO" es
        # como Access marcaba las acciones del club
        asignando = self.instance is not None and self.instance.nombres is None and bool(attrs.get("nombres"))
        nuevo_titular = asignando or (self.instance is None and bool(attrs.get("nombres")))
        if nuevo_titular:
            apellidos = (attrs.get("apellidos") or "").strip()
            if not apellidos or apellidos.upper() == "CENTRO GALLEGO":
                raise serializers.ValidationError({"apellidos": "Indique los apellidos del nuevo titular."})
            if not attrs.get("consentimiento_datos"):
                raise serializers.ValidationError({"consentimiento_datos": "Confirme que el socio firmó la autorización "
                                                                          "para el tratamiento de sus datos personales."})
            # "Acción que paga cuota de mantenimiento" (Band de Access): si, salvo que se indique lo contrario
            if "band_legado" not in self.initial_data:
                attrs["band_legado"] = True
        if attrs.get("meses_no_cobrados") and self.instance is not None and not asignando:
            raise serializers.ValidationError(
                {"meses_no_cobrados": "Solo se indican al vender una acción libre o al dar de alta un socio."})
        return attrs

    def create(self, validated_data):
        validated_data.pop("meses_no_cobrados", None)
        validated_data.pop("consentimiento_datos", None)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data.pop("meses_no_cobrados", None)
        validated_data.pop("consentimiento_datos", None)
        return super().update(instance, validated_data)


class SocioMensualidadSerializer(serializers.ModelSerializer):
    socio_codigo = serializers.CharField(source="socio.codigo", read_only=True)

    class Meta:
        model = m.SocioMensualidad
        fields = "__all__"


class SocioCuotaExtraSerializer(serializers.ModelSerializer):
    cuota_descripcion = serializers.CharField(source="cuota_extra.descripcion", read_only=True)

    class Meta:
        model = m.SocioCuotaExtra
        fields = "__all__"


class MorosoSerializer(serializers.Serializer):
    socio_id = serializers.IntegerField()
    codigo = serializers.CharField()
    nombre_completo = serializers.CharField()
    tipo_socio = serializers.CharField()
    meses_pendientes = serializers.ListField(child=serializers.IntegerField())
    cantidad_meses = serializers.IntegerField()
    monto_estimado = serializers.DecimalField(max_digits=18, decimal_places=2)


# -----------------------------------------------------------------------------
# Facturacion y cobros
# -----------------------------------------------------------------------------

DESGLOSE = ["monto_cheque", "monto_deposito", "monto_tarjeta_debito", "monto_tarjeta_credito", "monto_efectivo"]


class FacturaSerializer(serializers.ModelSerializer):
    socio_codigo = serializers.CharField(source="socio.codigo", read_only=True, default=None)
    tipo_pago_codigo = serializers.CharField(source="tipo_pago.codigo", read_only=True, default=None)
    # Solo al crear (como PagoMensualidad / PagoMensualidadAA / GeneraFac de Access):
    #   meses_pagados + anio_meses -> meses del socio que deja pagados (por defecto, el periodo actual)
    #   cuotas_pagadas             -> cuotas extraordinarias (numero 1..12)
    #   meses_concesion + anio_concesion -> meses del concesionario del tipo de pago
    #   cheque {numero, cuenta, banco} / deposito {numero, banco} -> se registran en Cobros
    #   registrar_abono            -> el recibo queda como abono a cuenta (saldo a favor)
    meses_pagados = serializers.ListField(
        child=serializers.IntegerField(min_value=1, max_value=12), write_only=True, required=False)
    anio_meses = serializers.IntegerField(write_only=True, required=False, min_value=1990, max_value=2100)
    cuotas_pagadas = serializers.ListField(
        child=serializers.IntegerField(min_value=1, max_value=12), write_only=True, required=False)
    meses_concesion = serializers.ListField(
        child=serializers.IntegerField(min_value=1, max_value=12), write_only=True, required=False)
    anio_concesion = serializers.IntegerField(write_only=True, required=False, min_value=1990, max_value=2100)
    cheque = serializers.JSONField(write_only=True, required=False)
    deposito = serializers.JSONField(write_only=True, required=False)
    registrar_abono = serializers.BooleanField(write_only=True, required=False, default=False)
    # Pago por aporte de un concesionario (GeneraFac): el % se guarda en la concesion, como PORAP en Access
    porcentaje_aporte = serializers.DecimalField(max_digits=7, decimal_places=4, min_value=0, max_value=1,
                                                 write_only=True, required=False, allow_null=True)

    EXTRAS = ("meses_pagados", "anio_meses", "cuotas_pagadas", "meses_concesion", "anio_concesion",
              "cheque", "deposito", "registrar_abono", "porcentaje_aporte")

    class Meta:
        model = m.Factura
        fields = "__all__"
        read_only_fields = ["codigo_socio_legado"]

    def _extras(self, attrs: dict) -> dict:
        periodo = m.Configuracion.actual().periodo_actual
        tipo = attrs.get("tipo_pago")
        return {
            "anio": attrs.get("anio_meses") or periodo,
            "meses": sorted(set(attrs.get("meses_pagados") or [])),
            "cuotas": sorted(set(attrs.get("cuotas_pagadas") or [])),
            "concesion": m.Concesion.objects.filter(tipo_pago=tipo).first() if tipo else None,
            "meses_concesion": sorted(set(attrs.get("meses_concesion") or [])),
            "anio_concesion": attrs.get("anio_concesion") or periodo,
            "cheque": attrs.get("cheque") or None,
            "deposito": attrs.get("deposito") or None,
            "abono": bool(attrs.get("registrar_abono")),
        }

    def create(self, validated_data):
        extras = self._extras(validated_data)
        porcentaje = validated_data.get("porcentaje_aporte")
        for campo in self.EXTRAS:
            validated_data.pop(campo, None)
        factura = super().create(validated_data)
        concesion = extras["concesion"]
        if factura.es_aporte and concesion is not None and porcentaje is not None                 and porcentaje != concesion.porcentaje_aporte:
            concesion.porcentaje_aporte = porcentaje
            concesion.save(update_fields=["porcentaje_aporte"])
        cobros.aplicar_recibo(factura, extras["anio"], extras["meses"], extras["cuotas"], extras["concesion"],
                              extras["meses_concesion"], extras["anio_concesion"], extras["cheque"],
                              extras["deposito"], extras["abono"])
        return factura

    # De un recibo ya emitido solo se puede cambiar esto (anularlo); el resto es el documento entregado al socio
    EDITABLES = ("anulada", "fecha_anulacion")

    def validate(self, attrs):
        if self.instance is not None and any(attrs.get(c) for c in self.EXTRAS):
            raise serializers.ValidationError({"meses_pagados": "Los meses, cuotas y pagos solo se indican al crear el recibo."})
        if self.instance is not None:
            otros = sorted(set(self.initial_data) - set(self.EDITABLES) - set(self.EXTRAS))
            if otros:
                raise serializers.ValidationError({otros[0]: "Un recibo emitido no se modifica: solo se puede anular."})
            if self.instance.anulada and attrs.get("anulada") is False:
                raise serializers.ValidationError({"anulada": "Un recibo anulado no se puede reactivar: emita uno nuevo."})
        if self.instance is None:
            e = self._extras(attrs)
            if e["abono"] and not attrs.get("socio"):
                raise serializers.ValidationError({"registrar_abono": "Un abono a cuenta debe ser de un socio."})
            if e["cheque"] and e["cheque"].get("numero") and not attrs.get("monto_cheque"):
                raise serializers.ValidationError({"monto_cheque": "Indique el monto pagado con cheque."})
            if e["deposito"] and e["deposito"].get("numero") and not attrs.get("monto_deposito"):
                raise serializers.ValidationError({"monto_deposito": "Indique el monto depositado."})
            try:
                cobros.validar_cobro(attrs.get("socio"), e["anio"], e["meses"], e["cuotas"], e["concesion"],
                                     e["meses_concesion"], e["anio_concesion"], e["cheque"], e["deposito"])
            except cobros.ErrorCobro as err:
                raise serializers.ValidationError(err.args[0])
            tipo = attrs.get("tipo_pago")
            if attrs.get("es_aporte") and e["concesion"] is None:
                raise serializers.ValidationError({"es_aporte": "El pago por aporte es solo para concesionarios."})
            # GeneraFac: "No ha seleccionado ningún mes..." para los recibos de concesionarios
            if e["concesion"] is not None and not e["meses_concesion"]:
                raise serializers.ValidationError(
                    {"meses_concesion": "Seleccione el mes (o los meses) que cancela el concesionario."})
            # Como en Access, si no se escribe concepto el recibo lleva el nombre del tipo de pago
            if tipo is not None and not (attrs.get("descripcion") or "").strip() and tipo.codigo != "O001":
                attrs["descripcion"] = tipo.descripcion[:150]
        # Solo para facturas nuevas: el historico de Access no siempre cuadra.
        if self.instance is None and any(attrs.get(c) for c in DESGLOSE):
            total = sum(attrs.get(c) or 0 for c in DESGLOSE)
            if total != attrs.get("monto", 0):
                raise serializers.ValidationError({"monto": f"El desglose suma {total} y el monto es {attrs.get('monto', 0)}."})
        if attrs.get("anulada") and not attrs.get("fecha_anulacion", getattr(self.instance, "fecha_anulacion", None)):
            raise serializers.ValidationError({"fecha_anulacion": "Obligatoria al anular el recibo."})
        return attrs


class AbonoSerializer(serializers.ModelSerializer):
    socio_codigo = serializers.CharField(source="socio.codigo", read_only=True)

    class Meta:
        model = m.Abono
        fields = "__all__"


class ChequeRecibidoSerializer(serializers.ModelSerializer):
    socio_codigo = serializers.CharField(source="socio.codigo", read_only=True, default=None)

    class Meta:
        model = m.ChequeRecibido
        fields = "__all__"
        read_only_fields = ["codigo_socio_legado"]


class DepositoSerializer(serializers.ModelSerializer):
    socio_codigo = serializers.CharField(source="socio.codigo", read_only=True, default=None)

    class Meta:
        model = m.Deposito
        fields = "__all__"
        read_only_fields = ["codigo_socio_legado"]


class ChequeEmitidoSerializer(serializers.ModelSerializer):
    # Pago de una factura con retención de IVA: el egreso queda unido al comprobante (solo al crear)
    retencion_iva = serializers.PrimaryKeyRelatedField(queryset=m.RetencionIva.objects.select_related("egreso"),
                                                       write_only=True, required=False, allow_null=True)

    class Meta:
        model = m.ChequeEmitido
        fields = "__all__"
        read_only_fields = ["banco_nombre"]

    def validate(self, attrs):
        retencion = attrs.get("retencion_iva")
        if retencion is not None:
            if self.instance is not None:
                raise serializers.ValidationError({"retencion_iva": "Solo se indica al registrar el pago."})
            if retencion.anulada:
                raise serializers.ValidationError({"retencion_iva": "El comprobante de retención está anulado."})
            if retencion.egreso and not retencion.egreso.anulado:
                raise serializers.ValidationError({"retencion_iva": f"Esa factura ya se pagó (voucher "
                                                                    f"{retencion.egreso.numero_voucher})."})
        if self.instance is None:
            if not (attrs.get("beneficiario") or "").strip():
                raise serializers.ValidationError({"beneficiario": "Indique a quien se paga."})
            if (attrs.get("monto") or 0) <= 0:
                raise serializers.ValidationError({"monto": "El monto debe ser mayor que cero."})
            banco = attrs.get("banco")
            if banco and banco.es_cuenta_bancaria and not attrs.get("numero_cheque"):
                raise serializers.ValidationError({"numero_cheque": f"Indique el numero de cheque de {banco.nombre}."})
        if "banco" in attrs:
            attrs["banco_nombre"] = attrs["banco"].nombre if attrs["banco"] else None
        return attrs

    def create(self, validated_data):
        retencion = validated_data.pop("retencion_iva", None)
        cheque = super().create(validated_data)
        if retencion is not None:
            retencion.egreso = cheque
            retencion.save(update_fields=["egreso"])
        return cheque


class LibroDiarioSerializer(serializers.ModelSerializer):
    class Meta:
        model = m.LibroDiario
        fields = "__all__"


# -----------------------------------------------------------------------------
# Otros
# -----------------------------------------------------------------------------

class ProveedorSerializer(serializers.ModelSerializer):
    class Meta:
        model = m.Proveedor
        fields = "__all__"


def siguiente_referencia_activo() -> str:
    """Siguiente referencia numerica de activo (00001, 00002...); las alfanumericas se respetan."""
    numeros = [int(c) for c in m.ActivoFijo.objects.exclude(codigo__isnull=True).values_list("codigo", flat=True)
               if c.isdigit()]
    return f"{max(numeros or [0]) + 1:05d}"


class ActivoFijoSerializer(serializers.ModelSerializer):
    """IngActivos / ConsModActivos de Access (Referencia, Descripcion, Cantidad, Ubicacion, Fecha de
    adquisicion y Fecha de baja). Sin referencia, se asigna la siguiente."""
    cantidad = serializers.IntegerField(default=1, min_value=0,
                                        error_messages={"min_value": "La cantidad no puede ser negativa."})

    class Meta:
        model = m.ActivoFijo
        fields = "__all__"
        extra_kwargs = {"codigo": {"required": False, "validators": []}}

    def validate_codigo(self, value):
        value = (value or "").strip().upper() or None
        if value and m.ActivoFijo.objects.filter(codigo__iexact=value) \
                .exclude(pk=getattr(self.instance, "pk", None)).exists():
            raise serializers.ValidationError(f"Ya existe un activo con la referencia {value}.")
        return value

    def validate_descripcion(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Indique la descripción del activo.")
        return value

    def validate_ubicacion(self, value):
        return (value or "").strip() or None

    def validate(self, attrs):
        ingreso = attrs.get("fecha_ingreso", getattr(self.instance, "fecha_ingreso", None))
        salida = attrs.get("fecha_salida", getattr(self.instance, "fecha_salida", None))
        if ingreso and salida and salida < ingreso:
            raise serializers.ValidationError(
                {"fecha_salida": "La fecha de baja no puede ser anterior a la de adquisición."})
        return attrs

    def create(self, validated_data):
        if not validated_data.get("codigo"):
            validated_data["codigo"] = siguiente_referencia_activo()
        return super().create(validated_data)


class UsoSalonSerializer(serializers.ModelSerializer):
    socio_codigo = serializers.CharField(source="socio.codigo", read_only=True)
    socio_nombre = serializers.CharField(source="socio.nombre_completo", read_only=True)

    class Meta:
        model = m.UsoSalon
        fields = "__all__"

    def validate_socio(self, socio):
        if not socio.nombres:  # accion libre (sin titular), mismo criterio que ?libre=true
            raise serializers.ValidationError(f"La acción {socio.codigo} no tiene titular (está disponible).")
        return socio


class MigracionIncidenciaSerializer(serializers.ModelSerializer):
    class Meta:
        model = m.MigracionIncidencia
        fields = "__all__"


# -----------------------------------------------------------------------------
# Retenciones de IVA
# -----------------------------------------------------------------------------

class RetencionIvaSerializer(serializers.ModelSerializer):
    """Se crea con los datos de la factura; los montos de la retención y el número del comprobante los pone el
    servidor. Un comprobante emitido no se modifica: se anula (acción /anular/) y se registra de nuevo."""
    egreso_voucher = serializers.CharField(source="egreso.numero_voucher", read_only=True, default=None)
    egreso_anulado = serializers.BooleanField(source="egreso.anulado", read_only=True, default=None)
    numero_comprobante = serializers.CharField(required=False, allow_blank=True, max_length=14)
    fecha_comprobante = serializers.DateField(required=False)                 # por defecto, hoy

    class Meta:
        model = m.RetencionIva
        fields = "__all__"
        read_only_fields = ["periodo", "quincena", "total_factura", "monto_retenido", "monto_pagar", "egreso",
                            "anulada", "fecha_anulacion", "motivo_anulacion", "creado_por", "creado_en"]
        validators = []         # la factura repetida se avisa en validate() con el número del comprobante

    def validate_proveedor_rif(self, valor):
        try:
            rif = retenciones.normalizar_rif(valor)
        except ValueError as e:
            raise serializers.ValidationError(str(e))
        if not retenciones.rif_valido(rif):
            raise serializers.ValidationError(
                f"El dígito verificador no corresponde: para {rif[:10]} debe ser {retenciones.digito_rif(rif)}. "
                "Revise el RIF en la factura.")
        return rif

    def validate_numero_comprobante(self, valor):
        valor = (valor or "").strip()
        if valor and not re.fullmatch(r"\d{14}", valor):
            raise serializers.ValidationError("Debe tener 14 dígitos: año y mes (AAAAMM) y 8 dígitos secuenciales.")
        return valor

    def validate(self, attrs):
        if self.instance is not None:
            raise serializers.ValidationError("Un comprobante de retención emitido no se modifica: anúlelo y "
                                              "registre la factura de nuevo.")
        for campo in ("proveedor_nombre", "numero_factura", "numero_control"):
            attrs[campo] = (attrs.get(campo) or "").strip()
            if not attrs[campo]:
                raise serializers.ValidationError({campo: "Obligatorio."})
        attrs["proveedor_nombre"] = attrs["proveedor_nombre"].upper()
        attrs.setdefault("alicuota", Decimal(16))
        attrs.setdefault("porcentaje_retencion", 75)
        attrs["fecha_comprobante"] = attrs.get("fecha_comprobante") or timezone.localdate()
        if attrs.get("alicuota") not in retenciones.ALICUOTAS:
            raise serializers.ValidationError({"alicuota": "Alícuota no válida (16 %, 8 % o 31 %)."})
        if attrs.get("porcentaje_retencion") not in retenciones.PORCENTAJES:
            raise serializers.ValidationError({"porcentaje_retencion": "La retención es del 75 % o del 100 %."})
        base, iva = attrs["base_imponible"], attrs["monto_iva"]
        exento = attrs.get("monto_exento") or Decimal(0)
        if base <= 0 and exento <= 0:
            raise serializers.ValidationError({"base_imponible": "Indique la base imponible de la factura."})
        esperado = retenciones.iva_esperado(base, attrs["alicuota"])
        if abs(iva - esperado) > retenciones.TOLERANCIA_IVA:
            raise serializers.ValidationError({"monto_iva": f"No corresponde a la base y la alícuota: debería ser "
                                                            f"cerca de {esperado}. Revise los montos de la factura."})
        if iva <= 0:
            raise serializers.ValidationError({"monto_iva": "Sin IVA no hay nada que retener."})
        if attrs["porcentaje_retencion"] == 100 and not (attrs.get("motivo_100") or "").strip():
            raise serializers.ValidationError({"motivo_100": "Indique por qué se retiene el 100 %."})
        if attrs["porcentaje_retencion"] == 75:
            attrs["motivo_100"] = None
        if attrs["fecha_factura"] > attrs["fecha_comprobante"]:
            raise serializers.ValidationError({"fecha_comprobante": "No puede ser anterior a la fecha de la factura."})
        duplicada = m.RetencionIva.objects.filter(
            proveedor_rif=attrs["proveedor_rif"], numero_factura=attrs["numero_factura"],
            numero_control=attrs["numero_control"], anulada=False).first()
        if duplicada:
            raise serializers.ValidationError({"numero_factura": f"Esta factura ya tiene la retención "
                                                                 f"{duplicada.numero_comprobante}."})
        attrs["monto_exento"] = exento
        attrs.update(retenciones.calcular(base, iva, exento, attrs["porcentaje_retencion"]))
        return attrs
