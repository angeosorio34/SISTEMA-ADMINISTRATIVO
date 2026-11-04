from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import respaldos_vistas as rv
from . import seguridad
from . import views as v
from .usuarios import UsuarioViewSet

router = DefaultRouter()
# Catalogos y parametros
router.register("provincias", v.ProvinciaViewSet)
router.register("tipos-pago", v.TipoPagoViewSet)
router.register("bancos", v.BancoViewSet)
router.register("niveles-acceso", v.NivelAccesoViewSet)
router.register("tarifas", v.TarifaMensualViewSet)
router.register("cuotas-extra", v.CuotaExtraViewSet)
router.register("calendario-cobro", v.CalendarioCobroViewSet)
router.register("concesiones", v.ConcesionViewSet)
router.register("concesiones-mensualidades", v.ConcesionMensualidadViewSet)
# Socios
router.register("socios", v.SocioViewSet, basename="socio")
router.register("beneficiarios", v.BeneficiarioViewSet)
router.register("mensualidades", v.SocioMensualidadViewSet)
# Facturacion y cobros
router.register("facturas", v.FacturaViewSet)
router.register("abonos", v.AbonoViewSet)
router.register("cheques-recibidos", v.ChequeRecibidoViewSet)
router.register("depositos", v.DepositoViewSet)
router.register("cheques-emitidos", v.ChequeEmitidoViewSet)
router.register("retenciones-iva", v.RetencionIvaViewSet)
router.register("libro-diario", v.LibroDiarioViewSet)
# Otros
router.register("proveedores", v.ProveedorViewSet)
router.register("activos", v.ActivoFijoViewSet)
router.register("uso-salon", v.UsoSalonViewSet)
router.register("incidencias-migracion", v.MigracionIncidenciaViewSet)
router.register("usuarios", UsuarioViewSet, basename="usuario")

urlpatterns = [
    path("auth/entrar/", seguridad.entrar, name="auth-entrar"),
    path("auth/salir/", seguridad.salir, name="auth-salir"),
    path("auth/sesion/", seguridad.sesion, name="auth-sesion"),
    path("auth/token/", seguridad.token_para_programas, name="auth-token"),
    path("auth/yo/", v.yo, name="auth-yo"),
    path("configuracion/", v.ConfiguracionView.as_view(), name="configuracion"),
    path("configuracion/nuevo-periodo/", v.nuevo_periodo, name="nuevo-periodo"),
    path("resumen/", v.resumen, name="resumen"),
    path("caja-divisas/", v.caja_divisas, name="caja-divisas"),
    path("caja/dia/", v.caja_dia, name="caja-dia"),
    path("tasa-bcv/", v.tasa_bcv, name="tasa-bcv"),
    path("tasa-bcv/actualizar/", v.tasa_bcv_actualizar, name="tasa-bcv-actualizar"),
    path("red/", v.red_local, name="red-local"),
    path("reportes/", v.reportes_catalogo, name="reportes"),
    path("reportes/<str:clave>/", v.reporte_pdf, name="reporte-pdf"),
    # Copias de seguridad (Configuración) y primera restauración en una computadora nueva
    path("respaldos/", rv.estado, name="respaldos"),
    path("respaldos/programacion/", rv.programacion, name="respaldos-programacion"),
    path("respaldos/crear/", rv.crear, name="respaldos-crear"),
    path("respaldos/subir/", rv.subir, name="respaldos-subir"),
    path("respaldos/access/", rv.access_estado, name="access-estado"),
    path("respaldos/access/analizar/", rv.access_analizar, name="access-analizar"),
    path("respaldos/access/cargar/", rv.access_cargar, name="access-cargar"),
    path("respaldos/access/descartar/", rv.access_descartar, name="access-descartar"),
    path("respaldos/fotos-access/", rv.fotos_access, name="fotos-access"),
    path("respaldos/<str:nombre>/", rv.eliminar, name="respaldos-eliminar"),
    path("respaldos/<str:nombre>/descargar/", rv.descargar, name="respaldos-descargar"),
    path("respaldos/<str:nombre>/restaurar/", rv.restaurar, name="respaldos-restaurar"),
    path("instalacion/", rv.instalacion, name="instalacion"),
    path("instalacion/restaurar/", rv.instalacion_restaurar, name="instalacion-restaurar"),
    path("", include(router.urls)),
]
