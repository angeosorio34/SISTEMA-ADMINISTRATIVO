"""
Modelos de SICGA mapeados sobre el esquema creado por migracion/01_schema.sql.

Todas las tablas son `managed = False`: la estructura la gobiernan el DDL y el ETL,
Django solo lee y escribe filas. Si en el futuro se quiere que Django gestione el
esquema, basta con quitar `managed = False` y ejecutar `migrate --fake-initial`.

Los on_delete reproducen las reglas ON DELETE del DDL (la base de datos tambien
las hace cumplir).
"""
from django.db import models
from django.db.models import F


# =============================================================================
# Choices
# =============================================================================

class TipoSocio(models.TextChoices):
    PROPIETARIO = "P", "Propietario"
    USO = "U", "Uso"


class EstadoCivil(models.TextChoices):
    SOLTERO = "S", "Soltero/a"
    CASADO = "C", "Casado/a"
    DIVORCIADO = "D", "Divorciado/a"
    VIUDO = "V", "Viudo/a"
    UNION_ESTABLE = "U", "Unión estable de hecho"


class OrigenMensualidad(models.TextChoices):
    SOCIOS = "SOCIOS", "Periodo actual"
    RELSOLV = "RELSOLV", "Historico de solvencia"
    MOROSOS = "MOROSOS", "Reporte de morosos"


class OrigenConcesion(models.TextChoices):
    CONCESION = "CONCESION", "Periodo actual"
    RELSOLVCONC = "RELSOLVCONC", "Historico de solvencia"
    MORCONC = "MORCONC", "Reporte de morosos"


class TipoIncidencia(models.TextChoices):
    RECHAZADA = "RECHAZADA", "Rechazada"
    CORREGIDA = "CORREGIDA", "Corregida"
    REF_NO_RESUELTA = "REF_NO_RESUELTA", "Referencia no resuelta"
    DUPLICADA = "DUPLICADA", "Duplicada"


def dinero(**kwargs):
    """Dominio `dinero` del DDL: NUMERIC(18,4)."""
    return models.DecimalField(max_digits=18, decimal_places=4, **kwargs)


# =============================================================================
# Seguridad y catalogos
# =============================================================================

class NivelAcceso(models.Model):
    id = models.SmallIntegerField(primary_key=True)
    descripcion = models.CharField(max_length=100)

    class Meta:
        managed = False
        db_table = "nivel_acceso"
        ordering = ["id"]
        verbose_name = "nivel de acceso"
        verbose_name_plural = "niveles de acceso"

    def __str__(self):
        return self.descripcion


class UsuarioSistema(models.Model):
    """Usuarios del sistema Access. La autenticacion real usa django.contrib.auth
    (ver comando `importar_usuarios`)."""
    username = models.CharField(max_length=30, unique=True)
    nombre = models.CharField(max_length=150, null=True, blank=True)
    cedula = models.CharField(max_length=15, null=True, blank=True)
    nivel_acceso = models.ForeignKey(NivelAcceso, on_delete=models.PROTECT, db_column="nivel_acceso_id")
    activo = models.BooleanField(default=True)

    class Meta:
        managed = False
        db_table = "usuario_sistema"
        ordering = ["username"]
        verbose_name = "usuario del sistema"
        verbose_name_plural = "usuarios del sistema"

    def __str__(self):
        return self.username


class Provincia(models.Model):
    id = models.SmallAutoField(primary_key=True)
    nombre = models.CharField(max_length=50, unique=True)

    class Meta:
        managed = False
        db_table = "provincia"
        ordering = ["nombre"]

    def __str__(self):
        return self.nombre


class TipoPago(models.Model):
    id = models.SmallAutoField(primary_key=True)
    codigo = models.CharField(max_length=4, unique=True)
    descripcion = models.CharField(max_length=100)

    class Meta:
        managed = False
        db_table = "tipo_pago"
        ordering = ["codigo"]
        verbose_name = "tipo de pago"
        verbose_name_plural = "tipos de pago"

    def __str__(self):
        return f"{self.codigo} - {self.descripcion}"


class Banco(models.Model):
    id = models.SmallAutoField(primary_key=True)
    nombre = models.CharField(max_length=30, unique=True)
    numero_cuenta = models.CharField(max_length=30, null=True, blank=True)
    saldo = dinero(default=0)
    es_cuenta_bancaria = models.BooleanField(default=True)

    class Meta:
        managed = False
        db_table = "banco"
        ordering = ["nombre"]

    def __str__(self):
        return self.nombre


# =============================================================================
# Parametrizacion
# =============================================================================

class Configuracion(models.Model):
    """Fila unica (id = 1) con los parametros globales."""
    id = models.SmallIntegerField(primary_key=True, default=1, editable=False)
    periodo_actual = models.SmallIntegerField()
    mensualidad = dinero(null=True, blank=True)
    mensualidad_uso = dinero(null=True, blank=True)
    ultima_factura = models.IntegerField(null=True, blank=True)
    ultimo_numero_control = models.IntegerField(null=True, blank=True)
    ultimo_voucher = models.IntegerField(null=True, blank=True)
    total_acciones_propietario = models.IntegerField(null=True, blank=True)
    total_acciones_uso = models.IntegerField(null=True, blank=True)
    divisas_inicio = models.IntegerField(null=True, blank=True)
    fecha_inicio_divisas = models.DateField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "configuracion"
        verbose_name = "configuracion"
        verbose_name_plural = "configuracion"

    @classmethod
    def actual(cls) -> "Configuracion":
        return cls.objects.get(pk=1)

    def __str__(self):
        return f"Configuracion periodo {self.periodo_actual}"


class CalendarioCobro(models.Model):
    mes = models.SmallIntegerField(primary_key=True)
    dia_corte = models.SmallIntegerField()

    class Meta:
        managed = False
        db_table = "calendario_cobro"
        ordering = ["mes"]


class TarifaMensual(models.Model):
    id = models.AutoField(primary_key=True)
    anio = models.SmallIntegerField()
    mes = models.SmallIntegerField()
    monto = dinero()

    class Meta:
        managed = False
        db_table = "tarifa_mensual"
        ordering = ["-anio", "mes"]
        unique_together = [("anio", "mes")]


class CuotaExtra(models.Model):
    id = models.SmallAutoField(primary_key=True)
    numero = models.SmallIntegerField(unique=True)
    descripcion = models.CharField(max_length=80, null=True, blank=True)
    monto = dinero(default=0)
    fecha_vencimiento = models.DateField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "cuota_extra"
        ordering = ["numero"]

    def __str__(self):
        return f"Cuota {self.numero}: {self.descripcion or ''}"


# =============================================================================
# Socios y beneficiarios
# =============================================================================

class Socio(models.Model):
    codigo = models.CharField(max_length=4, unique=True)
    tipo_socio = models.CharField(max_length=1, choices=TipoSocio.choices)
    accion_adicional = models.BooleanField(default=False)
    apellidos = models.CharField(max_length=150, null=True, blank=True)
    nombres = models.CharField(max_length=150, null=True, blank=True)
    cedula = models.CharField(max_length=15, null=True, blank=True, db_index=True)
    grupo_sanguineo = models.CharField(max_length=10, null=True, blank=True)
    fecha_nacimiento = models.DateField(null=True, blank=True)
    lugar_nacimiento = models.CharField(max_length=255, null=True, blank=True)
    nacionalidad = models.CharField(max_length=30, null=True, blank=True)
    provincia = models.ForeignKey(Provincia, on_delete=models.SET_NULL, null=True, blank=True,
                                  db_column="provincia_id", related_name="socios")
    es_gallego = models.BooleanField(default=False)
    ascendencia_gallega = models.BooleanField(default=False)
    estado_civil = models.CharField(max_length=1, choices=EstadoCivil.choices, null=True, blank=True)
    profesion = models.CharField(max_length=30, null=True, blank=True)
    direccion_habitacion = models.CharField(max_length=255, null=True, blank=True)
    telefono_habitacion = models.CharField(max_length=15, null=True, blank=True)
    celular = models.CharField(max_length=15, null=True, blank=True)
    email = models.CharField(max_length=60, null=True, blank=True)
    email_alterno = models.CharField(max_length=60, null=True, blank=True)
    empresa = models.CharField(max_length=80, null=True, blank=True)
    direccion_trabajo = models.CharField(max_length=255, null=True, blank=True)
    web_trabajo = models.CharField(max_length=30, null=True, blank=True)
    telefono_trabajo = models.CharField(max_length=15, null=True, blank=True)
    fax = models.CharField(max_length=15, null=True, blank=True)
    marca_vehiculo = models.CharField(max_length=20, null=True, blank=True)
    placa_vehiculo = models.CharField(max_length=10, null=True, blank=True)
    aficiones = models.CharField(max_length=255, null=True, blank=True)
    tiempo_en_ciudad = models.CharField(max_length=20, null=True, blank=True)
    domicilio_cobro = models.CharField(max_length=255, null=True, blank=True)
    fecha_ingreso = models.DateField(null=True, blank=True)
    referencia_1 = models.CharField(max_length=10, null=True, blank=True)
    referencia_2 = models.CharField(max_length=10, null=True, blank=True)
    exonerado = models.BooleanField(default=False)
    exonerado_total = models.BooleanField(default=False)
    observaciones = models.TextField(null=True, blank=True)
    ruta_foto = models.CharField(max_length=255, null=True, blank=True)
    # Campos de Access sin uso funcional identificado; se conservan para no perder informacion.
    act_legado = models.BooleanField(default=False)
    band_legado = models.BooleanField(default=False)

    class Meta:
        managed = False
        db_table = "socio"
        ordering = ["codigo"]

    def __str__(self):
        return f"{self.codigo} - {self.nombre_completo}"

    @property
    def nombre_completo(self) -> str:
        return " ".join(filter(None, [self.nombres, self.apellidos]))


class SocioMensualidad(models.Model):
    socio = models.ForeignKey(Socio, on_delete=models.CASCADE, db_column="socio_id", related_name="mensualidades")
    anio = models.SmallIntegerField()
    mes = models.SmallIntegerField()
    pagado = models.BooleanField(default=False)
    numero_factura = models.CharField(max_length=10, null=True, blank=True)
    monto_tarifa = dinero(null=True, blank=True)
    origen = models.CharField(max_length=10, choices=OrigenMensualidad.choices, default=OrigenMensualidad.SOCIOS)

    class Meta:
        managed = False
        db_table = "socio_mensualidad"
        ordering = ["socio_id", "-anio", "mes"]
        unique_together = [("socio", "anio", "mes")]
        verbose_name = "mensualidad de socio"
        verbose_name_plural = "mensualidades de socios"


class SocioCuotaExtra(models.Model):
    socio = models.ForeignKey(Socio, on_delete=models.CASCADE, db_column="socio_id", related_name="cuotas_extra")
    cuota_extra = models.ForeignKey(CuotaExtra, on_delete=models.PROTECT, db_column="cuota_extra_id",
                                    related_name="pagos")
    pagada = models.BooleanField(default=False)
    numero_factura = models.CharField(max_length=10, null=True, blank=True)

    class Meta:
        managed = False
        db_table = "socio_cuota_extra"
        unique_together = [("socio", "cuota_extra")]


class Beneficiario(models.Model):
    codigo = models.CharField(max_length=7, unique=True)
    socio = models.ForeignKey(Socio, on_delete=models.CASCADE, db_column="socio_id", related_name="beneficiarios")
    nombre_completo = models.CharField(max_length=255, null=True, blank=True)
    cedula = models.CharField(max_length=15, null=True, blank=True, db_index=True)
    parentesco = models.CharField(max_length=20, null=True, blank=True)
    fecha_nacimiento = models.DateField(null=True, blank=True)
    grupo_sanguineo = models.CharField(max_length=10, null=True, blank=True)
    telefono = models.CharField(max_length=15, null=True, blank=True)
    email = models.CharField(max_length=60, null=True, blank=True)
    marca_vehiculo = models.TextField(null=True, blank=True)
    placa_vehiculo = models.CharField(max_length=10, null=True, blank=True)
    activo = models.BooleanField(default=True)
    ruta_foto = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        managed = False
        db_table = "beneficiario"
        ordering = ["codigo"]

    def __str__(self):
        return f"{self.codigo} - {self.nombre_completo or ''}"


# =============================================================================
# Concesiones
# =============================================================================

class Concesion(models.Model):
    id = models.SmallAutoField(primary_key=True)
    tipo_pago = models.OneToOneField(TipoPago, on_delete=models.PROTECT, db_column="tipo_pago_id",
                                     related_name="concesion")
    nombre = models.CharField(max_length=255)
    rif = models.CharField(max_length=20, null=True, blank=True)
    porcentaje_aporte = models.DecimalField(max_digits=7, decimal_places=4, default=0)

    class Meta:
        managed = False
        db_table = "concesion"
        ordering = ["nombre"]
        verbose_name_plural = "concesiones"

    def __str__(self):
        return self.nombre


class ConcesionMensualidad(models.Model):
    concesion = models.ForeignKey(Concesion, on_delete=models.CASCADE, db_column="concesion_id",
                                  related_name="mensualidades")
    anio = models.SmallIntegerField()
    mes = models.SmallIntegerField()
    pagado = models.BooleanField(default=False)
    numero_factura = models.CharField(max_length=10, null=True, blank=True)
    origen = models.CharField(max_length=12, choices=OrigenConcesion.choices, default=OrigenConcesion.CONCESION)

    class Meta:
        managed = False
        db_table = "concesion_mensualidad"
        ordering = ["concesion_id", "-anio", "mes"]
        unique_together = [("concesion", "anio", "mes")]


# =============================================================================
# Facturacion y cobros
# =============================================================================

class DesglosePagoMixin(models.Model):
    """Columnas comunes de desglose por forma de pago."""
    monto = dinero(default=0)
    monto_cheque = dinero(default=0)
    monto_deposito = dinero(default=0)
    monto_tarjeta_debito = dinero(default=0)
    monto_tarjeta_credito = dinero(default=0)
    monto_efectivo = dinero(default=0)

    class Meta:
        abstract = True


class Factura(DesglosePagoMixin):
    numero = models.CharField(max_length=10, null=True, blank=True, db_index=True)
    numero_control = models.CharField(max_length=12, null=True, blank=True)
    socio = models.ForeignKey(Socio, on_delete=models.RESTRICT, null=True, blank=True,
                              db_column="socio_id", related_name="facturas")
    codigo_socio_legado = models.CharField(max_length=10, null=True, blank=True)
    cliente_nombre = models.CharField(max_length=150, null=True, blank=True)
    cliente_rif = models.CharField(max_length=20, null=True, blank=True)
    tipo_pago = models.ForeignKey(TipoPago, on_delete=models.PROTECT, null=True, blank=True,
                                  db_column="tipo_pago_id", related_name="facturas")
    descripcion = models.CharField(max_length=150, null=True, blank=True)
    fecha_emision = models.DateField(null=True, blank=True)
    fecha_tope = models.DateField(null=True, blank=True)
    fecha_cancelacion = models.DateField(null=True, blank=True)
    numero_cuotas = models.SmallIntegerField(null=True, blank=True)
    estado = models.BooleanField(default=True)
    es_aporte = models.BooleanField(default=False)
    anulada = models.BooleanField(default=False)
    fecha_anulacion = models.DateField(null=True, blank=True)
    pago_divisas = models.BooleanField(default=False)
    monto_divisas = dinero(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "factura"
        ordering = [F("fecha_emision").desc(nulls_last=True), "-id"]

    def __str__(self):
        return f"Factura {self.numero}"


class Abono(DesglosePagoMixin):
    """Pagos parciales y notas de credito (NCmmaa, monto negativo)."""
    numero_documento = models.CharField(max_length=10, db_index=True)
    socio = models.ForeignKey(Socio, on_delete=models.RESTRICT, db_column="socio_id", related_name="abonos")
    cliente_nombre = models.CharField(max_length=150, null=True, blank=True)
    cliente_rif = models.CharField(max_length=20, null=True, blank=True)
    tipo_pago = models.ForeignKey(TipoPago, on_delete=models.PROTECT, null=True, blank=True,
                                  db_column="tipo_pago_id", related_name="abonos")
    descripcion = models.CharField(max_length=150, null=True, blank=True)
    fecha_emision = models.DateField(null=True, blank=True)
    fecha_cancelacion = models.DateField(null=True, blank=True)
    estado = models.BooleanField(default=True)
    anulado = models.BooleanField(default=False)
    fecha_anulacion = models.DateField(null=True, blank=True)
    numero_factura_relacionada = models.CharField(max_length=10, null=True, blank=True)
    factura_relacionada = models.ForeignKey(Factura, on_delete=models.SET_NULL, null=True, blank=True,
                                            db_column="factura_relacionada_id", related_name="abonos")
    anio_deuda = models.SmallIntegerField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "abono"
        ordering = [F("fecha_cancelacion").desc(nulls_last=True), "-id"]


class ChequeRecibido(models.Model):
    socio = models.ForeignKey(Socio, on_delete=models.RESTRICT, null=True, blank=True,
                              db_column="socio_id", related_name="cheques_recibidos")
    codigo_socio_legado = models.CharField(max_length=10, null=True, blank=True)
    numero_cheque = models.CharField(max_length=15, null=True, blank=True)
    numero_cuenta = models.CharField(max_length=20, null=True, blank=True)
    banco_emisor = models.CharField(max_length=255, null=True, blank=True)
    fecha_pago = models.DateField(null=True, blank=True)
    descripcion = models.CharField(max_length=150, null=True, blank=True)
    monto = dinero(default=0)
    numero_factura = models.CharField(max_length=10, null=True, blank=True)
    factura = models.ForeignKey(Factura, on_delete=models.SET_NULL, null=True, blank=True,
                                db_column="factura_id", related_name="cheques")

    class Meta:
        managed = False
        db_table = "cheque_recibido"
        ordering = [F("fecha_pago").desc(nulls_last=True), "-id"]
        verbose_name = "cheque recibido"
        verbose_name_plural = "cheques recibidos"


class Deposito(models.Model):
    socio = models.ForeignKey(Socio, on_delete=models.RESTRICT, null=True, blank=True,
                              db_column="socio_id", related_name="depositos")
    codigo_socio_legado = models.CharField(max_length=10, null=True, blank=True)
    numero_deposito = models.CharField(max_length=20, null=True, blank=True)
    banco = models.ForeignKey(Banco, on_delete=models.PROTECT, null=True, blank=True,
                              db_column="banco_id", related_name="depositos")
    banco_nombre = models.CharField(max_length=255, null=True, blank=True)
    fecha_pago = models.DateField(null=True, blank=True)
    descripcion = models.CharField(max_length=150, null=True, blank=True)
    monto = dinero(default=0)
    numero_factura = models.CharField(max_length=10, null=True, blank=True)
    factura = models.ForeignKey(Factura, on_delete=models.SET_NULL, null=True, blank=True,
                                db_column="factura_id", related_name="depositos")

    class Meta:
        managed = False
        db_table = "deposito"
        ordering = [F("fecha_pago").desc(nulls_last=True), "-id"]


class ChequeEmitido(models.Model):
    """Egresos del club (Access: CHEQUES CG)."""
    fecha = models.DateField(null=True, blank=True)
    numero_cheque = models.CharField(max_length=15, null=True, blank=True, unique=True)
    numero_cuenta = models.CharField(max_length=20, null=True, blank=True)
    banco = models.ForeignKey(Banco, on_delete=models.PROTECT, null=True, blank=True,
                              db_column="banco_id", related_name="cheques_emitidos")
    banco_nombre = models.CharField(max_length=30, null=True, blank=True)
    beneficiario = models.CharField(max_length=50, null=True, blank=True)
    monto = dinero(default=0)
    concepto = models.CharField(max_length=250, null=True, blank=True)
    monto_en_letras = models.CharField(max_length=95, null=True, blank=True)
    monto_en_letras_2 = models.CharField(max_length=30, null=True, blank=True)
    numero_voucher = models.CharField(max_length=10, null=True, blank=True)
    lugar = models.CharField(max_length=20, null=True, blank=True)
    preparado_por = models.CharField(max_length=10, null=True, blank=True)
    anulado = models.BooleanField(default=False)
    pago_divisas = models.BooleanField(default=False)
    monto_divisas = dinero(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "cheque_emitido"
        ordering = [F("fecha").desc(nulls_last=True), "-id"]
        verbose_name = "cheque emitido"
        verbose_name_plural = "cheques emitidos"


class LibroDiario(DesglosePagoMixin):
    numero_factura = models.CharField(max_length=10, null=True, blank=True)
    factura = models.ForeignKey(Factura, on_delete=models.SET_NULL, null=True, blank=True,
                                db_column="factura_id", related_name="asientos")
    socio = models.ForeignKey(Socio, on_delete=models.RESTRICT, null=True, blank=True,
                              db_column="socio_id", related_name="asientos")
    codigo_socio_legado = models.CharField(max_length=10, null=True, blank=True)
    cliente_nombre = models.CharField(max_length=150, null=True, blank=True)
    cliente_rif = models.CharField(max_length=20, null=True, blank=True)
    tipo_pago = models.ForeignKey(TipoPago, on_delete=models.PROTECT, null=True, blank=True,
                                  db_column="tipo_pago_id", related_name="asientos")
    descripcion = models.CharField(max_length=150, null=True, blank=True)
    fecha_emision = models.DateField(null=True, blank=True)
    fecha_tope = models.DateField(null=True, blank=True)
    fecha_cancelacion = models.DateField(null=True, blank=True)
    numero_cuotas = models.SmallIntegerField(null=True, blank=True)
    estado = models.BooleanField(default=True)
    anulada = models.BooleanField(default=False)
    fecha_anulacion = models.DateField(null=True, blank=True)
    numero_voucher = models.CharField(max_length=10, null=True, blank=True)
    pago_cheque = dinero(null=True, blank=True)
    pago_efectivo = dinero(null=True, blank=True)
    pago_cargo_cuenta = dinero(null=True, blank=True)
    banco_nombre = models.CharField(max_length=25, null=True, blank=True)

    class Meta:
        managed = False
        db_table = "libro_diario"
        ordering = [F("fecha_emision").desc(nulls_last=True), "-id"]
        verbose_name = "asiento de libro diario"
        verbose_name_plural = "libro diario"


# =============================================================================
# Otros
# =============================================================================

class Proveedor(models.Model):
    id = models.SmallAutoField(primary_key=True)
    codigo = models.CharField(max_length=5, unique=True, null=True, blank=True)
    nombre_empresa = models.CharField(max_length=100)
    suministro = models.CharField(max_length=255, null=True, blank=True)
    direccion = models.CharField(max_length=150, null=True, blank=True)
    telefono = models.CharField(max_length=15, null=True, blank=True)
    representante = models.CharField(max_length=100, null=True, blank=True)
    celular = models.CharField(max_length=15, null=True, blank=True)

    class Meta:
        managed = False
        db_table = "proveedor"
        ordering = ["nombre_empresa"]
        verbose_name_plural = "proveedores"

    def __str__(self):
        return self.nombre_empresa


class ActivoFijo(models.Model):
    id = models.AutoField(primary_key=True)
    codigo = models.CharField(max_length=5, unique=True, null=True, blank=True)
    descripcion = models.CharField(max_length=255)
    cantidad = models.IntegerField(default=1)
    ubicacion = models.CharField(max_length=150, null=True, blank=True)
    fecha_ingreso = models.DateField(null=True, blank=True)
    fecha_salida = models.DateField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "activo_fijo"
        ordering = ["descripcion"]
        verbose_name = "activo fijo"
        verbose_name_plural = "activos fijos"


class UsoSalon(models.Model):
    socio = models.ForeignKey(Socio, on_delete=models.CASCADE, db_column="socio_id", related_name="usos_salon")
    fecha_uso = models.DateField()
    observacion = models.CharField(max_length=150, null=True, blank=True)

    class Meta:
        managed = False
        db_table = "uso_salon"
        ordering = ["-fecha_uso"]
        verbose_name = "uso del salon"
        verbose_name_plural = "usos del salon"


class MigracionIncidencia(models.Model):
    tabla_origen = models.CharField(max_length=40)
    clave_origen = models.CharField(max_length=60, null=True, blank=True)
    tipo = models.CharField(max_length=20, choices=TipoIncidencia.choices)
    detalle = models.TextField()
    datos = models.JSONField(null=True, blank=True)
    creado_en = models.DateTimeField(auto_now_add=True)
    resuelta = models.BooleanField(default=False)
    nota_resolucion = models.TextField(null=True, blank=True)
    resuelta_por = models.CharField(max_length=150, null=True, blank=True)
    resuelta_en = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "migracion_incidencia"
        ordering = ["tabla_origen", "id"]
        verbose_name = "incidencia de migracion"
        verbose_name_plural = "incidencias de migracion"


# =============================================================================
# Retenciones de IVA (módulo nuevo: no existía en Access)
# =============================================================================

class RetencionIva(models.Model):
    """Factura de un proveedor con la retención de IVA que practica el club como agente de retención.

    Primera tabla gestionada por Django (managed = True, migración 0003): no viene del DDL de la migración.
    Monto de la factura (A) = base imponible + IVA + exento. IVA retenido = IVA x 75 % (o 100 %).
    Monto a pagar al proveedor = A - IVA retenido (con 75 %: base + 25 % del IVA + exento).
    """
    numero_comprobante = models.CharField(max_length=14, unique=True)        # AAAAMM + 8 dígitos secuenciales
    fecha_comprobante = models.DateField()
    periodo = models.CharField(max_length=6)                                  # AAAAMM de la fecha del comprobante
    quincena = models.SmallIntegerField()                                     # 1: del 1 al 15; 2: del 16 al fin de mes
    proveedor = models.ForeignKey(Proveedor, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name="retenciones_iva")
    proveedor_nombre = models.CharField(max_length=150)                       # como figura en la factura
    proveedor_rif = models.CharField(max_length=12)                           # J-12345678-9
    tipo_documento = models.CharField(max_length=2, default="01")             # 01 factura (formato del SENIAT)
    fecha_factura = models.DateField()
    numero_factura = models.CharField(max_length=20)
    numero_control = models.CharField(max_length=20)
    base_imponible = models.DecimalField(max_digits=18, decimal_places=2)
    alicuota = models.DecimalField(max_digits=5, decimal_places=2, default=16)
    monto_iva = models.DecimalField(max_digits=18, decimal_places=2)
    monto_exento = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total_factura = models.DecimalField(max_digits=18, decimal_places=2)
    porcentaje_retencion = models.SmallIntegerField(default=75)
    motivo_100 = models.CharField(max_length=200, null=True, blank=True)
    monto_retenido = models.DecimalField(max_digits=18, decimal_places=2)
    monto_pagar = models.DecimalField(max_digits=18, decimal_places=2)
    egreso = models.ForeignKey(ChequeEmitido, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="retenciones_iva")
    observacion = models.CharField(max_length=250, null=True, blank=True)
    anulada = models.BooleanField(default=False)
    fecha_anulacion = models.DateField(null=True, blank=True)
    motivo_anulacion = models.CharField(max_length=200, null=True, blank=True)
    creado_por = models.CharField(max_length=150, blank=True, default="")
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "retencion_iva"
        ordering = ["-fecha_comprobante", "-numero_comprobante"]
        verbose_name = "retención de IVA"
        verbose_name_plural = "retenciones de IVA"
        constraints = [
            models.CheckConstraint(condition=models.Q(numero_comprobante__regex=r"^[0-9]{14}$"),
                                   name="retencion_iva_numero_14_digitos"),
            models.CheckConstraint(condition=models.Q(porcentaje_retencion__in=[75, 100]),
                                   name="retencion_iva_porcentaje_75_100"),
            models.CheckConstraint(condition=models.Q(quincena__in=[1, 2]), name="retencion_iva_quincena"),
            models.CheckConstraint(condition=models.Q(base_imponible__gte=0, monto_iva__gte=0, monto_exento__gte=0,
                                                      monto_retenido__gte=0, monto_pagar__gte=0),
                                   name="retencion_iva_montos_no_negativos"),
            # Una misma factura del proveedor no se retiene dos veces (salvo que la anterior esté anulada)
            models.UniqueConstraint(fields=["proveedor_rif", "numero_factura", "numero_control"],
                                    condition=models.Q(anulada=False), name="retencion_iva_factura_unica"),
        ]

    def __str__(self):
        return f"Comprobante {self.numero_comprobante}"
