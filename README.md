# SICGA: migración de Access a PostgreSQL + Django REST

```
SICGA.12.5.11.accdb          base original (Access, protegida con contraseña)
migracion/
  01_schema.sql              DDL PostgreSQL (esquemas public + legacy)
  02_etl_access_to_pg.py     ETL Access -> PostgreSQL con validación
backend/                     API Django + Django REST Framework
  core/models.py             modelos (managed = False, el esquema lo define 01_schema.sql)
  core/serializers.py, views.py, urls.py, filters.py, permissions.py
  core/management/commands/importar_usuarios.py
frontend/                    React 19 + Vite + TypeScript (consume la API)
  src/pages/                 Panel, Socios, Morosos, Facturas, Cobros, Egresos, Configuracion, Incidencias
```

## Puesta en marcha

1. **Crear la base y el usuario en PostgreSQL** (con psql o pgAdmin):
   ```sql
   CREATE ROLE sicga LOGIN PASSWORD 'una-clave-segura';
   CREATE DATABASE sicga OWNER sicga ENCODING 'UTF8';
   ```

2. **Migrar los datos** (Windows con el driver ODBC de Access de 64 bits):
   ```powershell
   cd migracion
   copy .env.example .env          # completar ACCESS_PWD y PG_DSN
   .\.venv\Scripts\python.exe 02_etl_access_to_pg.py --crear-esquema
   ```
   Al final se muestra una tabla de conciliación (filas y sumas de importes origen vs destino).
   Si algo falla, se hace ROLLBACK. Para volver a cargar los datos: ejecutar de nuevo sin `--crear-esquema`.
   Lo que no se pudo migrar limpiamente queda en la tabla `migracion_incidencia` (menú Sistema →
   Incidencias). Las 18 incidencias del `.accdb` actual están revisadas una a una (`REVISADAS` en el
   script): al migrar quedan resueltas con su explicación y la tabla final muestra «sin revisar: 0».
   Entre ellas, el recibo 00252 y su depósito, que tenían el código «328» sin la letra, se asignan a
   P328: su titular de entonces pagaba con ese nombre. Un pago con un código que no es una acción
   («1», «328») toma el socio del recibo que paga. Los dos depósitos cuyos recibos se borraron en Access
   (16798 y 17206) se conservan sin recibo para la conciliación bancaria. Si en una migración futura
   aparece una incidencia nueva, queda pendiente para revisarla en esa pantalla.

3. **Backend Django**:
   ```powershell
   cd backend
   copy .env.example .env          # completar DJANGO_SECRET_KEY y PG_DSN
   .\.venv\Scripts\python.exe manage.py migrate                 # tablas de Django (auth, tokens...)
   .\.venv\Scripts\python.exe manage.py importar_usuarios --clave-temporal "Cambiar-2026"
   .\.venv\Scripts\python.exe manage.py createsuperuser         # opcional, para /admin/
   .\.venv\Scripts\python.exe manage.py runserver
   ```
   - API navegable: http://localhost:8000/api/
   - Admin: http://localhost:8000/admin/

   Si se reejecuta el ETL, las tablas de Django (usuarios, tokens) no se tocan.

4. **Frontend React**:
   ```powershell
   cd frontend
   npm install
   npm run dev        # desarrollo: http://localhost:5173 (reenvia /api a Django en :8000)
   npm run build      # produccion: genera frontend/dist, que Django sirve en http://localhost:8000/
   ```
   Pantallas: panel con indicadores, socios (busqueda, ficha con beneficiarios, mensualidades por
   año, estado de cuenta y cuotas extra, alta/edicion), morosos con exportacion CSV, facturas
   (filtros, detalle con pagos, emision y anulacion), cobros (depositos, cheques, abonos), egresos,
   configuracion e incidencias de la migracion. Los usuarios de consulta no ven las acciones de escritura.

   Diseño: el inicio de sesión y el interior siguen las imágenes de referencia del cliente. El interior
   es un menú lateral color petróleo y un área de trabajo con fondo «aurora» (`src/assets/aurora.jpg`,
   tomado de la referencia) sobre el que van tarjetas, tablas y botones de vidrio translúcido. Colores,
   radios y sombras son variables en `:root` de `src/styles.css`; el diseño es claro y no cambia con
   el modo oscuro del sistema. En laptops (hasta 1600 px de ancho) la letra de las tarjetas se reduce
   un poco para que quepan los cinco indicadores del panel. El menú lateral se compacta según la altura
   de la ventana (por debajo de 900 y de 780 px) para verse entero sin barra de desplazamiento; solo en
   ventanas de menos de ~620 px aparece una barra fina y oscura.

   Al emitir una factura de mantenimiento (M001) se eligen los meses pendientes que paga: quedan
   marcados como pagados en la misma transaccion. Si no se indica numero, se asigna el siguiente
   de `configuracion.ultima_factura`.

### Fotos del sistema anterior

Access guardaba las fotos en la carpeta FOTOS SICGA de la computadora de la caja
(`\\DESKTOP-2GPI84A\Users\AIO S7100\Desktop\FOTOS SICGA`). El sistema nuevo nunca abre esas rutas (se
conectaría por la red a esa computadora). Para traer las fotos, copie la carpeta al servidor y ejecute:

```powershell
python manage.py importar_fotos "D:\FOTOS SICGA" --simular    # informa cuántas encuentra
python manage.py importar_fotos "D:\FOTOS SICGA"
```

Cada foto se asigna solo al socio o beneficiario cuyo código es el nombre del archivo (P043.jpg,
P423-4.jpg): en Access 151 socios apuntaban por error a la misma foto P01.jpg.

## Documentos impresos (mismo formato que Access)

Los PDF reproducen los informes originales de SICGA: el motor (`backend/core/reportes/`) lee el
diseño exportado de Access y lo dibuja con las mismas posiciones, fuentes, logo y formatos
(`Bs 1.234,56`, fecha larga en español). Ver `backend/core/reportes/plantillas/LEEME.md`.

| Documento | Dónde | Informe de Access |
|---|---|---|
| Recibo de pago original | Detalle de factura → "Recibo original" | FacturaCxC |
| Recibo de pago (copia fiel) | Detalle de factura → "Copia fiel" | FacturaCons |
| Solvencia | Ficha del socio → año + "Solvencia" | Solvencia |
| Comprobante de egreso | Egresos → "Comprobante" | InfVaucher |
| Cheque (sobre chequera preimpresa) | Egresos → "Cheque" | InfCheque |

## Informes (listados de Access)

Menú **Informes**: 46 listados con el formato original, agrupados como los menús de Access
(Socios, Morosidad, Contabilidad, Egresos, Concesionarios, Administración). API: `GET /api/reportes/`
y `GET /api/reportes/<clave>/?desde=&hasta=&anio=&hasta_mes=&socio=&concesion=`.
Datos de cada informe: `backend/core/reportes/listados.py`.

Errores del sistema Access que se corrigieron al migrar los informes:

- **Totales duplicados**: Access sumaba subtotales y totales en los eventos `Print`/`Format`, que se
  disparan varias veces al repaginar. Ahora se calculan una sola vez sobre los datos.
- **Morosos**: incluían a socios al día, exonerados y las 200 acciones libres (sin titular). Ahora solo
  socios con titular, con meses pendientes y que pagan cuota de mantenimiento (casilla «Band» de
  Access, que sus consultas de morosos y solventes sí exigían): 453 en octubre de 2026, no 655.
- **Socios solventes**: el informe no tenía filtro y listaba a todos; ahora solo los que están al día.
- **Estado de resultados**: el detalle de 7 twips dejaba una franja en blanco por factura y los
  subinformes de egresos estaban dañados; ahora sale completo en una página.
- **Cheques / efectivo**: el mismo informe imprimía los dos títulos encimados; ahora son dos informes.
- **Facturas sin tipo de pago**: Estado de resultados, Resumen y Detalle de ingresos las omitían sin
  aviso (unían con TIPOSPAGOS). En ene-sep 2026 eran 246 facturas por Bs 77,3 M: el balance pasaba de
  +13,7 M a -63,6 M. Ahora aparecen en la línea "Sin tipo de pago (S/T)".
- **"el:" suelto**: el VBA mostraba la etiqueta "el:" justo en el caso equivocado (rango de fechas).
- **Formulario inexistente**: "Solventes" leía `FormListaMorosoActual`, que ya no existía.

### Objetos dañados del .accdb

42 objetos (32 formularios y 10 informes) estaban dañados en `SICGA.12.5.11.accdb`: Access no los
puede exportar ni siquiera tras *Compactar y reparar*. Se recuperaron abriéndolos en vista Diseño y
leyendo cada propiedad; `migracion/03_reconstruir_informes.py` reconstruye la plantilla de los
informes (solventes, acciones adicionales, planilla de ingreso, solvencia y recibos de concesionarios).
Los formularios dañados se reimplementaron como pantallas: Concesionarios, marcar meses y cuotas
a mano (TildaMttoyExt), corregir/eliminar depósitos y cheques (MenuEliminarFacturas) e Informes
(MenuContabilidad, MenuMorosidad, MenuSocios).

## Caja (cobro a socios)

Menú **Finanzas → Caja (cobrar)**, y el botón «Cobrar» del Panel, de la ficha del socio y de Morosos. Es
el «Recibos de Pago» de Access (SelecSocioxNom → PagoMensualidad / PagoMensualidadAA):

1. Se busca al socio (número de acción, apellido o cédula).
2. Se ve lo que debe: vencido del período, años anteriores, cuotas extra y saldo a favor. Cada mes pagado
   muestra el número del recibo con que se pagó.
3. Se marcan meses (un año por recibo, como en Access) o cuotas, se reparte el pago entre efectivo,
   depósito, cheque, tarjetas o divisas, y **Cobrar e imprimir recibo** emite el recibo y abre el original.
   Si el pago no alcanza o sobra, no deja emitir, igual que Access. El dinero que el socio deja a cuenta se
   registra en «Abono a cuenta» y después se usa con «Pagar con el saldo a favor» (nota de crédito).
4. Abajo, el **cuadre de caja** del día (o de otra fecha): total cobrado por forma de pago, la lista de
   recibos y los informes «Detalle de ingresos» y «Recibos cobrados» de ese día.

Si un mes del período tiene una tarifa muy por debajo de las demás, la caja avisa antes de cobrar. La de
**enero de 2026 vino de Access en Bs 12** (los recibos reales de enero fueron de unos Bs 8.000): corríjala
en Configuración → Tarifas.

## Otros ingresos (alquileres, concesionarios, carnet...)

Menú **Finanzas → Otros ingresos** (también desde Caja, Facturas y el botón «Cobrar» de cada concesionario).
Es el «Recibos de Pago → Por Otros Ingresos» de Access (formulario GeneraFac, «Alquiler de Instalaciones u
Otros»): todos los tipos de pago menos mantenimiento (M001) y cuota extraordinaria (Z002).

1. Se elige el tipo (alquiler del salón, concesionarios, eventos, carnet, comité de damas u otros). En
   «Otros» hay que especificar el concepto; en los demás el concepto es el nombre del tipo, como en Access.
2. Si es socio, se escribe el número de la acción y se completan nombre y cédula; en los concesionarios el
   recibo va a nombre del concesionario y se marcan los meses que cancela (obligatorio, como en Access). Si
   debe años anteriores, avisa y propone el más antiguo. «Pago por aporte» calcula el monto como % del
   aporte × ingresos del período y guarda el % en el concesionario.
3. El total se propone en depósito (como GeneraFac) y se reparte entre las formas de pago; si no suman el
   total, no deja emitir. **Emitir e imprimir recibo** abre el recibo original (informe FacturaCxC).
4. Abajo, la lista de recibos de otros ingresos con filtro por tipo y fechas, su PDF y exportación CSV
   (en la API: `GET /api/facturas/?otros_ingresos=true`). Incluye los 843 recibos que Access guardó **sin tipo
   de pago** (GeneraFac no lo exigía; p. ej. «Oficina deportiva» y aportes al salón de 2023 a 2026), que se
   marcan «sin tipo» y se pueden filtrar (`?sin_tipo=true`).

## Retenciones de IVA (facturas de proveedores)

Menú **Finanzas → Retenciones IVA**. Módulo nuevo (no existía en Access), pedido con notas a mano y una factura
de ejemplo. El club, como agente de retención, retiene parte del IVA de cada factura de compra y paga al
proveedor el resto:

    Monto de la factura (A) = base imponible + IVA + exento
    IVA retenido            = 75 % del IVA        (o 100 % en los casos de la norma)
    Monto a pagar           = A − IVA retenido
                            → 75 %:  base imponible + 25 % del IVA + exento
                            → 100 %: base imponible + exento
    Monto total retenido del período = Σ de las retenciones (sin anulados)

Ejemplo (factura C-000457, control 00-023932): base 294.621,16 + IVA 16 % 47.139,38 = A 341.760,54; con 75 % se
retienen 35.354,54 y se pagan **306.406,00**; con 100 % se retienen 47.139,38 y se pagan 294.621,16.

1. **Registrar factura**: RIF del proveedor (con el dígito verificador comprobado, y autocompletado de los ya
   usados), nombre, fecha, n.º de factura y de control, base imponible, alícuota (16 %, 8 % o 31 %), IVA (se
   calcula y se puede corregir con el de la factura; se acepta 1 Bs de diferencia por redondeo) y exento. El
   recuadro de la derecha muestra el cálculo en vivo. Con 100 % hay que elegir el motivo.
2. Al guardar se emite el **comprobante de retención** (PDF para el proveedor) con número de 14 dígitos: año y mes
   (AAAAMM) + 8 dígitos que siguen sin reiniciarse cada mes. Si el club ya traía una numeración, se escribe el
   primer número a mano y los siguientes continúan desde ahí. La misma factura no se puede retener dos veces.
3. **Pagar** abre «Elaborar un pago» (Egresos) con el proveedor, el monto a pagar y el concepto; el pago queda
   enlazado y la retención pasa a «Pagada · voucher». Si se anula el pago, vuelve a «Por pagar».
4. Un comprobante emitido no se modifica ni se borra: se **anula** con motivo (queda en el listado tachado, no se
   declara) y la factura se registra de nuevo.
5. Arriba, por año, mes y quincena (1 al 15 / 16 al fin de mes): **monto total retenido**, monto de las facturas
   (A), monto pagado, la **Relación** en PDF y el **Archivo TXT** para el portal del SENIAT (16 campos separados por
   tabulador: RIF del agente, período, fecha de la factura, tipo C, tipo de documento 01, RIF del proveedor, n.º
   de factura y de control, total, base, retenido, documento afectado 0, comprobante, exento, alícuota y
   expediente 0). Exportación CSV del listado.

Datos del club como agente de retención: `backend/core/club.py` (razón social, RIF J-08014425-2, dirección).
**El contador del club debe confirmar** los porcentajes, el formato del TXT y el RIF antes de declarar.
API: `/api/retenciones-iva/` (alta, listado y filtros `anio`, `mes`, `quincena`, `pagada`, `anulada`),
`/{id}/comprobante/`, `/{id}/anular/`, `/resumen/`, `/relacion/`, `/txt/`, `/proveedores/`; el egreso se enlaza
con `retencion_iva` en `POST /api/cheques-emitidos/`. Pruebas: `test_retenciones.py` (83) y `e2e/retenciones.mjs` (60).

## Copias de seguridad

Menú **Sistema → Configuración → pestaña Copias de seguridad** (solo usuarios con acceso total):

- **Copias automáticas** diaria, semanal y anual, con hora, día y cuántas se conservan. Por defecto: todos los días
  a las 18:00 (las últimas 7), los viernes (5) y el 31 de diciembre (10). Las hace el propio servidor; si la
  computadora estaba apagada a esa hora, la copia se hace en cuanto se abre el sistema. Si fallan, el panel lo avisa.
- **Hacer copia ahora**, **Descargar** (para guardarla en un pendrive o en Google Drive), **Restaurar** y **Eliminar**.
- **Subir copia de otra computadora** y restaurarla.

Cada copia es un `.zip` con la base completa (`pg_dump`), las fotos y un manifiesto (fecha, equipo, filas de cada
tabla y huella sha256). Se guardan en `%LOCALAPPDATA%\SGA-GALLEGO\respaldos` (en el servidor,
`/opt/sga-gallego/respaldos`; otra carpeta con `SGA_RESPALDOS_DIR`). **Nunca** en la carpeta temporal de Windows: el
Sensor de almacenamiento borra lo que considera sin usar (así se dañó la base de prueba el 17-10-2026).

Restaurar no toca la base en uso hasta haber comprobado la copia (`core/respaldos.py`):
1. huella del manifiesto y lista de objetos del pg_dump (solo tablas, índices, restricciones, secuencias y dominios:
   nada de funciones ni disparadores);
2. carga en una base aparte con un usuario de PostgreSQL sin privilegios;
3. las mismas filas que dice el manifiesto y estructura idéntica a la del sistema (`core/esquema_referencia.txt`).
   Una copia de una **versión anterior** del sistema (su huella está en `core/esquemas_anteriores.txt`) se
   actualiza sola: se aplican las migraciones pendientes en la base aparte y después se compara la estructura.
   Así, las copias hechas antes del módulo de Retenciones de IVA se restauran y quedan con la tabla nueva;
4. copia «Antes de restaurar» de los datos actuales (para deshacer) e intercambio de nombres: `sicga` →
   `sicga_anterior` y `media` → `media_anterior`.

Hay que escribir RESTAURAR y la propia clave; después se entra con los usuarios y claves de la copia.

**Cambio de computadora:** en la nueva se instala el sistema con la base vacía (`createdb -T template0 sicga`); al
abrirlo, la pantalla de inicio ofrece restaurar la copia. Solo desde esa misma computadora, nunca por un túnel o
proxy. También por consola:

    python manage.py respaldo crear | listar | automaticas
    python manage.py respaldo restaurar <copia.zip>
    python manage.py respaldo esquema [--escribir]   # tras una migración nueva, regenerar la referencia
                                                     # (la huella anterior pasa a esquemas_anteriores.txt)

Al arrancar, `manage.py servir` aplica las migraciones pendientes (p. ej. la tabla de retenciones al actualizar).

En el servidor, el usuario `sga` de PostgreSQL necesita `CREATEDB` (lo da `deploy/instalar.sh`).

### Cargar lo nuevo desde Access

En la misma pestaña, **Cargar lo nuevo desde Access (SICGA)**: mientras se siga usando Access, se sube su `.accdb`
(con la clave de la base, que no se guarda en ningún lado). El sistema trae solo lo que falta (recibos, pagos,
depósitos, cheques, mensualidades, socios, beneficiarios, egresos, parámetros) y lo pone en su lugar, sin duplicar.
Primero **Analizar** (no guarda nada) muestra por tipo de dato cuántos son nuevos, cuántos se actualizan, cuántos ya
estaban, los conflictos y lo borrado en Access, con el detalle. Después **Cargar** lo guarda en una sola transacción,
con una copia «Antes de cargar Access» previa para poder deshacer.

Cómo decide (`core/importar_access.py`): compara a tres bandas, registro por registro y campo por campo:
- **Base:** el esquema `legacy`, la copia 1:1 de Access de la última carga (al principio, la de la migración).
- **Access:** el archivo subido.
- **Sistema:** lo que hay hoy.

Los tres pasan por la misma transformación de `migracion/02_etl_access_to_pg.py`, comparados por claves naturales
(código de acción, número de recibo, socio + año + mes...):
- **Cambió solo en Access:** se copia.
- **Cambió en los dos lados con valores distintos:** conflicto; se deja lo del sistema y se informa.
- **Borrado en Access:** aquí no se borra, solo se informa.
- **Contadores** (último recibo, control, voucher): se queda el mayor de los dos.

Se rechaza un archivo más antiguo que el último cargado (o de otra base), porque desharía datos. Al terminar, la base
pasa a ser ese Access, así la siguiente carga solo trae lo posterior; de `USUARIOS` nunca se guarda la clave en texto
plano. Necesita Windows con el controlador de Access y `pyodbc` (en `requirements.txt` solo para Windows); en el
servidor Linux la tarjeta avisa que no está disponible.

## Red local del club y fotos de Access

**Cómo era en Access.** No usaba tablas vinculadas: cada computadora abría el mismo `.accdb`, y el botón de la foto la
guardaba en una carpeta compartida de otra PC. Lo habitual era
`\\DESKTOP-2GPI84A\Users\AIO S7100\Desktop\FOTOS SICGA` (568 socios y 765 beneficiarios); las demás rutas eran
`C:\Documents and Settings\CENTRO GALLEGO\Escritorio\FOTOS SICGA`, puesta fija en el código de 5 formularios. Las rutas
guardadas estaban mal en muchos casos (151 socios apuntan a `P01.jpg`).

**Cómo queda.** Una computadora es el servidor y las demás entran con el navegador. Todo está en `deploy\windows\`:

| Archivo | Para qué |
|---|---|
| `SGA-GALLEGO.cmd` | Doble clic: inicia la base y el sistema si hace falta, y abre el navegador. |
| `iniciar_sga.ps1` | Inicia PostgreSQL y el servidor (`manage.py servir`, con waitress), sin duplicar lo que ya está en marcha. Anota en `registros\inicio.log`. |
| `detener_sga.ps1 [-ConBase]` | Detiene solo el proceso que atiende el puerto del sistema y, con `-ConBase`, la base. |
| `inicio_automatico.ps1 [-Quitar]` | Tarea programada «SGA-GALLEGO» del usuario, sin permisos de administrador. Ejecuta `iniciar_oculto.vbs` (sin ventanas): al iniciar sesión, al despertar de la suspensión y cada 10 minutos. Si todo está en marcha, no hace nada. |
| `Red del club.cmd` (`permitir_red_local.ps1 [-Quitar]`) | Abre el sistema a la red del club: pone `SGA_RED_LOCAL=1` en `backend\.env` y crea reglas del cortafuegos (ver abajo). Pide permisos de administrador. |
| `Traer de Access.cmd` (`traer_de_access.ps1`) | Trae por la red la carpeta `FOTOS SICGA` y una copia del `.accdb`, y pone las fotos en el sistema (ver abajo). |

Se usa la tarea programada en lugar de un acceso directo de Inicio porque «Apagar» con el inicio rápido de Windows cierra la sesión: la base se cierra de golpe (PostgreSQL se recupera solo al arrancar, sin perder datos), y al despertar de la suspensión no se vuelve a iniciar sesión. Probado cerrando de golpe la base y el sistema: la tarea los levantó en 19 s.

`permitir_red_local.ps1` deja el cortafuegos así:
- Permite el puerto 8000 solo en redes Privadas o de dominio, y solo desde la misma subred.
- Lo bloquea en redes Públicas.
- Si la red está marcada como Pública, pregunta si es la del club y solo entonces la marca Privada.
- **VPN (TunnelBear...):** mientras está conectada, Windows deja de reconocer la red del club («Red no identificada») y
  la trata como Pública, así que las demás computadoras no pueden entrar. El script la detecta y pide desconectarla.
  Después reconecta el wifi, porque Windows no vuelve a identificar la red hasta reconectarse. Si la VPN se vuelve a
  conectar sola, lo avisa: hay que desactivar su conexión automática, o en TunnelBear agregar `C.G.P` a las redes de
  confianza.

- **`manage.py servir`** (waitress) reemplaza a `runserver`: atiende a varias computadoras a la vez.
  - Escucha solo en `127.0.0.1`, salvo con `SGA_RED_LOCAL=1`.
  - Si PostgreSQL no responde, lo dice en segundos (`connect_timeout=5`) en lugar de quedarse esperando.
- **Con `SGA_RED_LOCAL=1`** el sistema acepta el nombre de la computadora (`DESKTOP-XXXX`, `desktop-xxxx.local`) y sus
  direcciones privadas. Un nombre ajeno se rechaza, y la protección CSRF compara el origen con esa dirección.
  - **Configuración → Red local y fotos** muestra los enlaces para las demás computadoras y el estado de las fotos.
  - La restauración sin sesión de una computadora nueva sigue siendo solo desde el propio servidor.
- **Las fotos** viven ahora con los demás datos, en `%LOCALAPPDATA%\SGA-GALLEGO\media`, fuera de la carpeta del
  programa. Se muestran solo con sesión y van en las copias de seguridad.
- **Para traer las de Access** hay tres caminos:
  - **Por la red:** doble clic en `deploy\windows\Traer de Access.cmd`. Busca las computadoras del club con carpetas
    compartidas, entra con el usuario y la clave de Windows de cada una (la ventana de Windows los pide, no se guardan y
    la conexión se cierra al terminar). Busca `FOTOS SICGA` y los `.accdb` y copia la carpeta con más fotos y el
    `.accdb` más reciente (primero los `SICGA*`). El original se abre solo para leer, aunque Access lo esté usando.
    Hace una copia de seguridad del sistema, pone las fotos y borra la copia temporal. La copia del `.accdb` queda en
    `%LOCALAPPDATA%\SGA-GALLEGO\access` para «Cargar lo nuevo desde Access». Anota en `registros\traer_access.log`.
  - **Con una memoria USB:** copiar `FOTOS SICGA` y el `.accdb` a la memoria y ejecutar
    `traer_de_access.ps1 -Origen E:\` (hace lo mismo, sin red).
  - **Desde el navegador:** en la PC de Access, clic derecho en `FOTOS SICGA` → Enviar a → Carpeta comprimida, y subir
    el `.zip` en esa pantalla. Primero se revisa sin guardar nada.

  Siempre:
  - Cada foto se pone por el código exacto (`P043.jpg`, `P423-4.jpg`).
  - Solo se aceptan imágenes reales; se guardan sin metadatos y con un máximo de 1600 px.
  - No se tocan las fotos ya cargadas en el sistema nuevo, salvo «Reemplazar».
  - Por consola: `manage.py importar_fotos <carpeta o .zip> [--simular]`.

  **Las PCs del club (SERVIDORCG, DESKTOP-SH618II, DESKTOP-31JPB01) comparten sus carpetas solo para invitados** (sin
  clave, como lo dejó Access). Windows 11 exige la firma SMB y rechaza esas conexiones (error 0xC05D0003). La exigencia
  se mantiene: se entra con un usuario de esa PC que tenga clave, o se usa la memoria USB. Una cuenta sin clave no sirve
  por la red, porque Windows lo impide.

**En la red local la conexión va por HTTP.** Las claves no salen del club, pero cualquiera conectado al mismo wifi
podría verlas, así que la red del club debe tener contraseña (WPA2). Para cifrar también dentro del club hay que
instalar un certificado en cada computadora; está pendiente si se quiere.

## Tasa del dólar del BCV

**Menú lateral:** una línea «TASA BCV  Bs 881,07  19/10», encima del nombre del usuario, con el estilo de los títulos
del menú.
- Al pasar el ratón se ve la tasa completa, la fecha valor y la que rige desde el próximo día hábil.
- Un clic lleva a **Configuración → Tasa oficial del BCV**, donde están la vigente, la próxima, el historial, el
  botón «Actualizar ahora» y la carga a mano.
- En pantallas bajas el menú se compacta un poco más para que la línea quepa sin barra de desplazamiento.
  Verificado en 12 tamaños, de 1280×600 a 2200×978.

**Cómo se obtiene** (`core/tasa_bcv.py`):
- El servidor lee la portada de https://www.bcv.org.ve/ (bloque «USD» y su «Fecha Valor»), siempre por HTTPS con el
  certificado verificado.
- Consulta al arrancar y cada 2 horas, de 7:00 a 22:00. Si falla, reintenta a los 30 minutos.
- La tasa vigente es la última con fecha valor de hoy o anterior. La que el BCV publica por la tarde para el día
  siguiente aparece como «próxima».
- Si el BCV no responde, el menú muestra un punto ámbar y el motivo. Un usuario con acceso total puede cargarla a mano;
  cuando el BCV publique la de esa fecha, la oficial reemplaza a la manual.
- Las tasas se guardan en `%LOCALAPPDATA%\SGA-GALLEGO\tasa_bcv.json` (últimas 500). No van en la base porque es un dato
  público que se vuelve a consultar.
- `SGA_TASA_BCV=0` desactiva la consulta automática (pruebas o una computadora sin internet).

**API:** `GET /api/tasa-bcv/` para cualquier usuario, y `POST /api/tasa-bcv/` (carga a mano) y
`POST /api/tasa-bcv/actualizar/` para acceso total.

## Seguridad, privacidad y accesibilidad

Revisión hecha el 17-18/10/2026. Pruebas: `test_seguridad.py` (60 comprobaciones), `e2e/accesibilidad.mjs` (axe-core,
WCAG 2.1 AA, 19 pantallas) y todas las pruebas anteriores, también con el servidor en modo producción.

**Claves y configuración**
- La clave secreta de Django va en `backend/.env`, que no está en Git ni en los paquetes (plantilla:
  `backend/.env.example`). Sin ella, el sistema no arranca en modo producción (`DJANGO_DEBUG=false`).
- La base de datos pide contraseña (`scram-sha-256`) y escucha solo en `localhost`. El sistema entra con el usuario
  `sga`: es dueño de la base y no tiene privilegios de superusuario, así que no puede ejecutar programas ni leer
  archivos del servidor. Solo tiene `CREATEDB`, para restaurar copias. Las claves de `sga` y de `postgres` están en
  `%APPDATA%\postgresql\pgpass.conf`, el archivo estándar de PostgreSQL en el perfil de Windows, fuera del proyecto.
- `deploy/publicar_github.sh` se detiene sin guardar si encuentra archivos prohibidos o algo que parece una clave
  (contraseñas en direcciones `postgresql://`, claves de API, llaves privadas).

**Sesión y acceso** (`core/autenticacion.py`, `core/seguridad.py`)
- **La sesión:**
  - Va en una cookie `HttpOnly` y `SameSite=Strict` (y `Secure` con HTTPS): el código de la página no puede leerla.
  - Vence a las 12 horas (`SGA_SESION_HORAS`). «Cerrar sesión» la anula en el servidor.
  - Las escrituras exigen la cabecera `X-SGA: 1` y el mismo origen (protección CSRF).
  - Los programas usan `POST /api/auth/token/` y `Authorization: Token`.
- **Intentos de entrada:**
  - 5 fallos seguidos bloquean el usuario 15 minutos, exista o no; reiniciarle la clave lo desbloquea.
  - Límite de 20 intentos por minuto y por IP, y un campo trampa invisible para robots.
- **Límites de uso:** `SGA_LIMITE_*`, con 40 operaciones pesadas por hora (copias, restaurar, cargar Access).
- **Contraseñas:**
  - Se guardan con Argon2id; las antiguas con PBKDF2 se convierten al entrar.
  - Mínimo 10 caracteres, sin claves comunes ni solo números.
- **Toda la API pide sesión**, salvo entrar, salir, `sesion/` e `instalacion/`. En producción no existen `/admin/` ni
  la API navegable.
- **Permisos:**
  - Consulta solo lee.
  - Usuarios y copias de seguridad son solo para el nivel de acceso total.
  - Solo un superusuario modifica a otro superusuario.
- **Campos protegidos:**
  - Un recibo emitido no se modifica ni se borra: solo se anula, y un anulado no se reactiva.
  - Los abonos y el libro diario no se crean ni se editan a mano.
  - No se registra un socio nuevo ni la venta de una acción sin confirmar la autorización de datos (planilla firmada).

**Datos y respuestas**
- **Entradas:** consultas solo con el ORM o con `psycopg.sql`, sin texto armado a mano. Probado con parámetros y
  cuerpos basura en todas las rutas: sin errores 500.
- **Errores:** los de la base no muestran tablas ni SQL; el detalle de cada error 500 queda en `registros/errores.log`.
- **Fotos:**
  - Se aceptan solo si son imágenes de verdad (por su contenido, no por el nombre) y hasta 8 MB.
  - Se vuelven a guardar sin metadatos (GPS, modelo del teléfono) y a un máximo de 1600 px.
  - Se rechazan las imágenes «bomba».
- **CSV:** un texto que empieza por `= + - @` no se ejecuta como fórmula en Excel.
- **Registro de seguridad** (`%LOCALAPPDATA%\SGA-GALLEGO\registros\seguridad.log`):
  - Guarda entradas, fallos, bloqueos, restauraciones, cargas desde Access y cambios de usuarios.
  - Nunca guarda claves. Si alguien escribe su clave en el campo «usuario», no se anota.

**Cabeceras:**
- `Content-Security-Policy`: solo recursos propios, sin `eval` y sin incrustar el sistema en otro sitio.
- `X-Frame-Options`, `nosniff`, `Referrer-Policy`, `Permissions-Policy`, `Cross-Origin-*`.
- `X-Robots-Tag: noindex` y `robots.txt`.
- La API responde con `Cache-Control: no-store`.
- En internet (`DJANGO_DETRAS_DE_PROXY=true`): redirección a HTTPS y HSTS de 1 año.

**Dependencias:** sin vulnerabilidades conocidas el 18/10/2026. Para revisarlo otra vez:
`npm audit` en `frontend/` y `pip-audit` sobre `pip freeze` del backend.

**Caché en Windows:** la caché en archivos de Django falla con dos solicitudes a la vez (Windows no deja reemplazar
un archivo abierto). Por eso en Windows la caché va en memoria, porque el sistema corre en un solo proceso; en Linux
va en archivos, compartida entre los procesos de gunicorn.

**Privacidad y textos legales** (`/legal/...`, enlaces al pie de cada pantalla y en el inicio de sesión)
- Páginas: aviso de privacidad, política de cookies, términos de uso y datos del club con su marco legal venezolano.
  Incluyen anulaciones y devoluciones, propiedad intelectual y licencias.
- Los datos del club (razón social, RIF, dirección, teléfono, correo) salen de los recibos oficiales de Access y
  están en `frontend/src/legal/club.ts`.
- Los textos son una **versión preliminar**: deben revisarlos el asesor legal y aprobarlos la Junta Directiva.
- **Cookies y analíticas:** el sistema solo usa la cookie de sesión, que es necesaria. El aviso de cookies informa y
  no pide consentimiento. No hay analíticas ni recursos de otros sitios: las fuentes van dentro del sistema y la CSP
  lo impide.

**Accesibilidad:** 0 problemas de WCAG 2.1 AA según axe en las 19 pantallas revisadas.
- Las fotos tienen texto alternativo con el nombre de la persona.
- Los botones y campos tienen nombre.
- Los colores de texto se oscurecieron un poco para tener contraste 4,5:1 incluso sobre la zona más oscura de la aurora.
- Los enlaces dentro de un texto van subrayados.

## Enlace temporal para mostrar el sistema (sin estar presente)

`deploy\windows\enlace_publico.ps1` abre una dirección `https://….trycloudflare.com` para entrar desde cualquier
computadora; `deploy\windows\detener_enlace.ps1` la cierra. La dirección queda en
`%LOCALAPPDATA%\SGA-GALLEGO\registros\enlace.txt`.

**Cómo funciona**
- Un segundo servidor del sistema en el puerto 8002 sirve a un túnel rápido de Cloudflare (`cloudflared.exe`, firmado
  por Cloudflare Inc., en `%LOCALAPPDATA%\SGA-GALLEGO\tunel\`).
- Ese servidor corre en modo «detrás de un túnel»:
  - HTTPS obligatorio.
  - Cookie de sesión `Secure`.
  - La IP real de cada visitante en los límites de intentos y en el registro de seguridad.
  - Sin la restauración inicial.
- No hace copias automáticas ni consulta la tasa del BCV; eso lo sigue haciendo el servidor normal del puerto 8000.
- Una vigilancia oculta impide que Windows suspenda la computadora mientras el enlace está abierto y reinicia el
  servidor 8002 si se cae. Probado: volvió en 27 s, con la misma dirección.
- Los archivos del diseño (`/assets/`) se envían comprimidos y quedan en caché del navegador por un año (sus nombres
  cambian con cada versión). Así la primera carga por internet bajó de ~15 s a ~4-5 s; los datos (`/api/`) no se
  comprimen ni se guardan.

**Límites**
- La computadora debe seguir encendida, enchufada, con internet y **con la tapa abierta**. Cerrar la tapa la suspende
  igual.
- Si se cierra el túnel o se reinicia la computadora, la dirección cambia y hay que enviar la nueva.

**Cortes de internet («Error inesperado del servidor» por el enlace)**
- El 20/10/2026 el usuario vio «Error inesperado del servidor» en algunas pantallas usando el enlace. El servidor no
  falló: `errores.log` quedó vacío y el recorrido completo (33 pantallas con sus pestañas y los 46 informes en PDF)
  no dio ningún error.
- La causa fue el internet de la laptop (punto de acceso móvil). El túnel perdió la conexión cada ~10 minutos y
  Cloudflare respondió con su propia página de error (502/530), que la página no podía leer como respuesta del sistema.
- **El túnel va por HTTP/2 (TCP)** con `TUNNEL_TRANSPORT_PROTOCOL=http2`. La conexión predeterminada, QUIC (UDP), es la
  que se cortaba con el punto de acceso.
- **Las lecturas se reintentan solas** (`solicitar()` en `frontend/src/api/client.ts`): pantallas, PDFs, fotos y
  descargas, 3 veces en ~10 s. Las escrituras nunca, porque repetir un cobro lo duplicaría.
- **Mensajes claros si el corte sigue:**
  - Al leer: «Se perdió la conexión con el sistema (falló el internet o el enlace)…».
  - Al guardar: «Se cortó la conexión mientras se enviaba. Antes de repetir la operación, revise si quedó guardada…».
- Para el enlace conviene un internet estable (cable o Wi-Fi fijo) mejor que un punto de acceso móvil.
- Para que otros miren sin modificar nada, usar un usuario de **consulta**. El usuario `invitado` se creó el 20-10-2026
  para eso; desactívelo en Usuarios cuando ya no haga falta.

## Servidor en internet (24 horas)

La carpeta `deploy/` instala el sistema en un servidor Ubuntu 24.04 (por ejemplo un Droplet de DigitalOcean):

1. En esta PC: `bash deploy/empaquetar.sh <carpeta>` compila el frontend y deja `sga-paquete.tar.gz` (programa)
   y `sicga.dump` (copia de la base).
2. Se suben los dos al servidor y, como root: `bash deploy/instalar.sh <dominio> /root/sicga.dump`.
   - `<dominio>` puede ser el propio del club o, mientras tanto, `<ip-con-guiones>.sslip.io`.
   - Instala PostgreSQL 17, el sistema en `/opt/sga-gallego` (gunicorn como servicio `sga-gallego`), Caddy con
     HTTPS automático, copias diarias de la base en `/var/backups/sga` (30 días) y el cortafuegos (22/80/443).
   - El modo es seguro (sin DEBUG) y las claves de la base y de Django se generan en el servidor.
3. Para actualizar el programa se repite lo mismo: la base ya existente no se toca.

## Uso del salón

Menú **Socios → Uso del salón** (el «Módulo Uso del Salón» de Access, formulario ConsultaSalon). En Access la
tabla USOSALON quedó vacía (0 filas al migrar): el uso real del salón está en los recibos, así que arriba se
listan los **alquileres y reservas del salón**: recibos A001 «Alquiler del salón» y los que nombran el salón en
el concepto (588 recibos, por año o todos, con PDF de cada uno y CSV; API `?salon=true`). Debajo, el registro
de uso por socio (número de acción, fecha y observación), con búsqueda y el listado impreso del año. Los mismos registros se ven en la ficha de cada socio (botón «Uso del salón»). La lista de socios indica
que la ficha se abre con un clic en la fila o con el botón «Ficha».

## Módulos y botones de Access y su equivalente

Inventario hecho sobre los 90 formularios y 230 botones de `SICGA.12.5.11.accdb` (incluidos los 42
objetos dañados, recuperados desde la vista Diseño).

| Menú / formulario de Access | Botones | Sistema nuevo |
|---|---|---|
| MenuSocios · Socios · SociosTot | Consulta, Ingresar/Consultar beneficiarios, Adjuntar foto, **Liberar acción**, Imprimir planilla, Uso del salón, Visualiza cuotas canceladas | Socios → ficha: pestañas Beneficiarios (alta, edición, eliminar, foto), Mensualidades y Cuotas extra (marcar a mano), **Uso del salón**, foto; botones **Liberar acción**, Planilla |
| MenuSocios (listados) | De Acciones, De Acciones Libres | Informes → Socios: «Listado de acciones (todas, con su titular)» y «Acciones disponibles para la venta (libres)» |
| AgregAcc | Ingresa acciones PROPIETARIOS / Añade acciones de USO | **Acciones disponibles** (o Socios) → **Agregar acciones** (P/U + cantidad) |
| SelecAccion · IngSocios · RepSocio · ValSocio | Ingreso de un socio en una acción libre (venta) | Socios → **Acciones disponibles** (menú y Panel) → **Vender**: datos del comprador; si su cédula ya es titular de otra acción copia sus datos (RepSocio); muestra el nombre de las referencias (ValSocio); propone no cobrar los meses anteriores al ingreso (casillas «mes facturado»); casilla **Paga cuota de mantenimiento** (Band) |
| ExonerarAccion | Exonerar acción, Suspender exoneración, Recibos emitidos | Ficha → **Exonerar / Suspender exoneración** (como en Access, quita / devuelve «paga cuota de mantenimiento»); Estado de cuenta |
| MenuModulos «Recibos de Pago» · SelecSocioxNom · PagoMensualidad · PagoMensualidadAA | Generar recibo, **Pago con abono**, Generar solvencia, Recibos emitidos, Modificar montos, Transacciones realizadas, Cancelar operación | **Caja (cobrar)**: buscar socio, lo que debe (período, años anteriores, cuotas extra), cobrar e imprimir el recibo, pagar con el saldo a favor, Solvencia, Recibos emitidos, Modificar montos y cuadre de caja del día |
| MenuModulos «Recibos de Pago → Por Otros Ingresos» · GeneraFac · FormCONC · FormConcMOR · ConsConc · FacturasXConces | Imprimir factura (por pantalla / en papel), meses del concesionario, deuda de años anteriores, pago por aporte, recibos emitidos por esta concesión | **Otros ingresos**: emitir e imprimir el recibo (FacturaCxC), meses y aporte del concesionario, lista de recibos de otros ingresos; Finanzas → **Concesionarios** |
| FacturasXSocio · AbonosXSocio | Ver factura, Listar recibos | Ficha → Estado de cuenta; informe "Recibos emitidos a un socio" |
| MenuEliminarFacturas · FormAnulaFac · FormAnulaCheq · FormAnulaDep · FormPagxAbono | Anula recibo, Elimina/Modifica cheque, Anula/Modifica depósito, Anular solvencias x abonos | Factura → Anular (**revierte meses, cuotas y concesión**); Cobros → Corregir; Cobros → Abonos → **Anular** (NC) |
| MenuCheques · IntroCheque · IntroCargCta · AnulaCheque · AnulaVaucher | Elaborar pago, Cargo en cuenta, Anular | Egresos → Elaborar pago (banco "CARGO EN CUENTA"), Anular, Comprobante, Cheque |
| MenuContabilidad · SaldoCaja | Detalle de ingresos, Resumen, Estado de resultados, Recibos emitidos/cobrados/anulados, Cheques recibidos, **Libro diario**, **Mov. de divisas**, **Saldo en caja** | Informes → Contabilidad; Configuración → **Saldo en caja (divisas)** |
| MenuMorosidad | Morosos (mantenimiento, cuota extra, años anteriores, +6 meses), Solventes, Relaciones de solvencia/facturas por socio y concesionario | Informes → Morosidad y Concesionarios; Socios → Morosos |
| MenuModulos · ConsMontos · ConsMontosAA | Consulta/Modificación de montos, Montos años anteriores, Reinicia socios/concesionarios | Configuración: parámetros, **tarifas por año** (recalcula deudas), cuotas extra, catálogos, Nuevo período |
| ConsUsr · IngUsr · ModUsr · AccIniUsr | Desplegar, Crear, Modificar acceso, Eliminar, Reiniciar clave | Sistema → **Usuarios** |
| MenuProveedor · ConsultaRegion | Ingreso, Consulta/Modificación, Listados, Provincias | Administración → Proveedores; Configuración → Provincias; Informes |
| MenuActivos · IngActivos · ConsModActivos | Ingreso, Consulta / Modificación, Listado de activos en uso, Listado de activos de baja | Administración → **Activos fijos**: pestañas En uso / De baja / Todos, buscador y filtro por ubicación, **Ingresar activo** (referencia automática si se deja vacía), Editar, **Dar de baja** / Reactivar, Eliminar, totales de unidades y los dos listados impresos |

**Liberar acción** hace lo mismo que `LimpiaAcc`: borra los datos del titular y sus beneficiarios, quita
«paga cuota de mantenimiento» y conserva las observaciones de la acción (su historial: «Traspaso de…»).
A diferencia de Access, también conserva los recibos y los meses ya pagados del titular anterior.
**Nuevo socio** crea una acción con un número nuevo y sus 12 meses del período, igual que «Agregar acciones».

Sin equivalente a propósito: `FormaPago`/`CancelaCxC` (en Access ya no funcionaba: dependía de un
formulario inexistente) y "Eliminar de lista de morosos de años anteriores" (la morosidad se calcula
sola a partir de los pagos; no hay lista que mantener a mano).

Los importes se escriben con el formato venezolano: `1.500`, `1.500,00` o `1500,50` (también se
acepta `1500.50`). Un punto seguido de tres cifras se toma siempre como separador de miles, de modo
que `1.500` es mil quinientos y no 1,50 (vale para facturas, egresos, cobros, configuración e informes).

## API REST

Autenticación por token: `POST /api/auth/token/` con `{"username", "password"}` → `{"token"}`.
Después, enviar la cabecera `Authorization: Token <token>`.

| Recurso | Ruta |
|---|---|
| Usuario actual | `GET /api/auth/yo/` |
| Panel de inicio | `GET /api/resumen/` |
| Configuración | `GET/PATCH /api/configuracion/` |
| Socios | `/api/socios/` (`?search=`, `?tipo_socio=P`, `?exonerado=true`) |
| Detalle del socio | `/api/socios/{id}/beneficiarios/`, `mensualidades/?anio=`, `cuotas-extra/`, `estado-cuenta/` |
| Morosos | `GET /api/socios/morosos/?anio=2026&hasta_mes=10` |
| Facturas | `/api/facturas/` (POST admite `meses_pagados: [8, 9]`; `?fecha_desde=`, `?fecha_hasta=`, `?socio_codigo=`, `?tipo_pago_codigo=`, `?sin_socio=true`) |
| Pagos de una factura | `GET /api/facturas/{id}/pagos/` |
| Caja | `GET /api/socios/{id}/deuda/` (lo que debe), `GET /api/caja/dia/?fecha=` (cuadre del día) |
| Cobros | `/api/abonos/`, `/api/depositos/`, `/api/cheques-recibidos/` |
| Egresos | `/api/cheques-emitidos/`, `/api/libro-diario/` |
| Catálogos | `/api/provincias/`, `/api/tipos-pago/`, `/api/bancos/`, `/api/tarifas/`, `/api/cuotas-extra/`, `/api/concesiones/` |
| Otros | `/api/proveedores/`, `/api/activos/`, `/api/uso-salon/`, `/api/incidencias-migracion/` |
| PDF | `/api/facturas/{id}/pdf/?tipo=original`, `/api/socios/{id}/solvencia/?anio=`, `/api/cheques-emitidos/{id}/comprobante/`, `/api/cheques-emitidos/{id}/imprimir-cheque/` |
| Procesos | `POST /api/configuracion/nuevo-periodo/` `{"anio": 2027}`, `POST /api/incidencias-migracion/{id}/resolver/` `{"nota": "..."}` |

- **Permisos**: usuarios de nivel 0 (grupo `acceso_total`) leen y escriben; los de nivel 1 (grupo `consulta`) solo leen.
- **Paginación**: 50 registros por página (`?page=`, `?page_size=` hasta 500).
- **Importes**: viajan siempre como texto (`"1200.0000"`) para no perder precisión, también en las respuestas
  calculadas (estado de cuenta, saldo a favor, caja en divisas, resumen).
- **Borrados**: intentar borrar un registro con dependencias (por ejemplo, un socio con facturas) devuelve `409`.
