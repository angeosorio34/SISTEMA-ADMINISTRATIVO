from django.contrib import admin

from . import models as m


class BeneficiarioInline(admin.TabularInline):
    model = m.Beneficiario
    extra = 0
    fields = ["codigo", "nombre_completo", "cedula", "parentesco", "fecha_nacimiento", "activo"]


@admin.register(m.Socio)
class SocioAdmin(admin.ModelAdmin):
    list_display = ["codigo", "tipo_socio", "apellidos", "nombres", "cedula", "celular", "exonerado"]
    list_filter = ["tipo_socio", "exonerado", "provincia", "es_gallego"]
    search_fields = ["codigo", "apellidos", "nombres", "cedula"]
    inlines = [BeneficiarioInline]


@admin.register(m.Factura)
class FacturaAdmin(admin.ModelAdmin):
    list_display = ["numero", "fecha_emision", "socio", "cliente_nombre", "tipo_pago", "monto", "anulada"]
    list_filter = ["anulada", "tipo_pago", "pago_divisas"]
    search_fields = ["numero", "cliente_nombre", "cliente_rif"]
    raw_id_fields = ["socio"]
    date_hierarchy = "fecha_emision"


@admin.register(m.MigracionIncidencia)
class MigracionIncidenciaAdmin(admin.ModelAdmin):
    list_display = ["tabla_origen", "tipo", "clave_origen", "detalle"]
    list_filter = ["tabla_origen", "tipo"]
    search_fields = ["clave_origen", "detalle"]


for modelo in [m.Provincia, m.TipoPago, m.Banco, m.TarifaMensual, m.CuotaExtra, m.Concesion,
               m.Beneficiario, m.Abono, m.ChequeRecibido, m.Deposito, m.ChequeEmitido,
               m.Proveedor, m.ActivoFijo, m.Configuracion]:
    admin.site.register(modelo)
