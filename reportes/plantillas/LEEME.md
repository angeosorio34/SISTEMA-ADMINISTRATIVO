# Plantillas de informes de Access

Estos archivos son el diseño **original** de los informes de SICGA.accdb, exportados con
`Application.SaveAsText acReport, "<Informe>", "<archivo>"` y convertidos a UTF-8.
`core/reportes/motor.py` los lee y dibuja cada control (etiquetas, cuadros de texto,
rectángulos, líneas, casillas y el logotipo incrustado) en las mismas coordenadas, con las
mismas fuentes y formatos. Por eso los PDF salen iguales que los impresos desde Access.

| Archivo           | Documento                                | Endpoint                                          |
|-------------------|------------------------------------------|---------------------------------------------------|
| `FacturaCxC.txt`  | Recibo de pago (ORIGINAL, al emitir)     | `GET /api/facturas/{id}/pdf/?tipo=original`       |
| `FacturaCons.txt` | Recibo de pago (COPIA FIEL AL ORIGINAL)  | `GET /api/facturas/{id}/pdf/`                     |
| `Solvencia.txt`   | Solvencia del socio por año              | `GET /api/socios/{id}/solvencia/?anio=2026`       |
| `InfVaucher.txt`  | Comprobante de egreso                    | `GET /api/cheques-emitidos/{id}/comprobante/`     |
| `InfCheque.txt`   | Cheque sobre la chequera preimpresa      | `GET /api/cheques-emitidos/{id}/imprimir-cheque/` |

Qué datos recibe cada informe se define en `core/reportes/documentos.py`.

## Plantillas derivadas

Dos plantillas parten de un informe de Access y le agregan lo que hacía falta, con el mismo membrete y estilo:

| Archivo | Parte de | Cambio |
|---|---|---|
| `SociosBeneficiarios.txt` | `Subinforme SOCIOS.txt` (ficha breve del socio, de la planilla) | título propio y subinforme de beneficiarios bajo cada socio |
| `ListaAccionesTodas.txt` | `ListaAcciones.txt` ("Listado de Acciones Libres") | título "Listado de Acciones", columna TITULAR (o "DISPONIBLE PARA LA VENTA") y pie con los totales |

## Cambiar el diseño

- **Cambio pequeño** (un texto del membrete, un teléfono): edite el `Caption ="..."` en el `.txt`.
- **Rediseño en Access**: modifique el informe en una copia de la base y vuelva a exportarlo
  (PowerShell, con Access instalado):

  ```powershell
  $acc = New-Object -ComObject Access.Application
  $acc.AutomationSecurity = 3                     # sin macros ni VBA
  $acc.OpenCurrentDatabase("C:\ruta\copia.accdb", $false, "<clave>")
  $acc.SaveAsText(3, "FacturaCons", "C:\tmp\FacturaCons.txt")
  $acc.CloseCurrentDatabase(); $acc.Quit()
  ```

  El archivo sale en UTF-16; guárdelo en UTF-8 en esta carpeta. Si la copia abre el formulario
  de inicio (login), quite antes la propiedad `StartUpForm` de la copia con DAO.

## Fuentes

El motor usa Calibri, Arial y Arial Narrow de `C:\Windows\Fonts`. En un servidor Linux use las
equivalentes métricas libres (Carlito y Liberation Sans/Narrow, paquetes `fonts-crosextra-carlito`
y `fonts-liberation`): ocupan el mismo ancho, así que el diseño no se mueve.

## Monto en letras

En Access lo escribía a mano el cajero. `documentos.monto_en_letras()` usa el estilo mayoritario
de 2025-2026 (`"UN MIL DOSCIENTOS CON 50 CTS"`, `"... SIN CTS"`), comparado contra los 10.900
cheques históricos.
