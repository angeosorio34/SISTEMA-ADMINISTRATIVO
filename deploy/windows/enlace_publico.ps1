<#
  Enlace temporal para mostrar SGA-GALLEGO desde otra computadora por internet, sin estar presente.

    powershell -ExecutionPolicy Bypass -File enlace_publico.ps1          abre el enlace y muestra la dirección
    powershell -ExecutionPolicy Bypass -File detener_enlace.ps1          lo cierra

  Cómo funciona:
  - Un segundo servidor del sistema en el puerto 8002, solo en esta computadora, en modo «detrás de un túnel» (HTTPS,
    IP real de cada visitante para los límites de intentos). No hace copias automáticas ni consulta la tasa del BCV
    (eso lo sigue haciendo el servidor normal del puerto 8000).
  - Un túnel rápido de Cloudflare (cloudflared, firmado por Cloudflare Inc.) que da una dirección https://….trycloudflare.com
    al azar. No se publica en ningún lado y los buscadores no la indexan; igual hace falta usuario y contraseña.
  - Mientras el túnel esté abierto, la computadora no se suspende sola y el servidor 8002 se reinicia si se cae.
    La dirección cambia si se cierra el túnel o se reinicia la computadora: hay que enviar la nueva.

  La dirección queda en %LOCALAPPDATA%\SGA-GALLEGO\registros\enlace.txt.
#>
param([switch]$Vigilar)
$ErrorActionPreference = "Stop"

$Raiz = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Backend = Join-Path $Raiz "backend"
$Python = Join-Path $Backend ".venv\Scripts\python.exe"
$Datos = Join-Path $env:LOCALAPPDATA "SGA-GALLEGO"
$Registros = Join-Path $Datos "registros"
$Cloudflared = Join-Path $Datos "tunel\cloudflared.exe"
$Puerto = 8002
$ArchivoEnlace = Join-Path $Registros "enlace.txt"
$RegistroTunel = Join-Path $Registros "tunel.log"
New-Item -ItemType Directory -Force $Registros | Out-Null

function Anotar([string]$Texto) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Texto" | Add-Content -Path (Join-Path $Registros "inicio.log") -Encoding UTF8
    Write-Host $Texto
}
function Escuchando([int]$P) { return [bool](Get-NetTCPConnection -LocalPort $P -State Listen -ErrorAction SilentlyContinue) }
function Tunel { return Get-CimInstance Win32_Process -Filter "Name = 'cloudflared.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match "127\.0\.0\.1:$Puerto" } }

function Iniciar-Servidor {
    # Variables solo para este servidor (el proceso hijo las hereda); backend\.env no se toca
    $env:DJANGO_DETRAS_DE_PROXY = "true"
    $env:DJANGO_ALLOWED_HOSTS = ".trycloudflare.com,localhost,127.0.0.1"
    $env:SGA_RED_LOCAL = "0"
    $env:SGA_RESPALDOS_AUTOMATICOS = "0"
    $env:SGA_TASA_BCV = "0"
    Start-Process -FilePath $Python -ArgumentList @("manage.py", "servir", "--puerto", "$Puerto") -WorkingDirectory $Backend `
        -WindowStyle Hidden -RedirectStandardOutput (Join-Path $Registros "servidor-enlace.log") `
        -RedirectStandardError (Join-Path $Registros "servidor-enlace-errores.log")
    for ($i = 0; $i -lt 90 -and -not (Escuchando $Puerto); $i++) { Start-Sleep -Milliseconds 500 }
    return (Escuchando $Puerto)
}

# --- Modo vigilancia (proceso oculto que dura lo que dure el túnel) ---------------------------------------------
if ($Vigilar) {
    # Que Windows no suspenda la computadora mientras el enlace esté abierto (la pantalla sí puede apagarse)
    Add-Type -Namespace SGA -Name Energia -MemberDefinition '[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint esFlags);'
    # ES_CONTINUOUS | ES_SYSTEM_REQUIRED (en decimal: PowerShell 5.1 lee 0x80000001 como número negativo)
    [void][SGA.Energia]::SetThreadExecutionState([uint32]2147483649)
    while (Tunel) {
        if (-not (Escuchando $Puerto)) {
            Anotar "Enlace: el servidor del puerto $Puerto no respondía; se reinicia."
            [void](Iniciar-Servidor)
        }
        Start-Sleep -Seconds 30
    }
    [void][SGA.Energia]::SetThreadExecutionState([uint32]2147483648)      # ES_CONTINUOUS: vuelve a permitir la suspensión
    exit 0
}

# --- Abrir el enlace ---------------------------------------------------------------------------------------------
if (-not (Test-Path $Cloudflared)) { Anotar "ERROR: falta $Cloudflared"; exit 1 }
& (Join-Path $PSScriptRoot "iniciar_sga.ps1") | Out-Null         # la base de datos y el sistema normal en marcha

if (-not (Escuchando $Puerto)) {
    Anotar "Enlace: iniciando el servidor para el túnel en el puerto $Puerto..."
    if (-not (Iniciar-Servidor)) { Anotar "ERROR: no arrancó. Revise $Registros\servidor-enlace-errores.log"; exit 1 }
}

$direccion = $null
if ((Tunel) -and (Test-Path $ArchivoEnlace)) {
    $direccion = (Get-Content $ArchivoEnlace -Raw).Trim()
} else {
    Remove-Item $RegistroTunel -ErrorAction SilentlyContinue
    # Conexión por HTTP/2 (TCP). La predeterminada, QUIC (UDP), se cortaba cada ~10 minutos con el punto de acceso
    # móvil (20/10/2026: «no recent network activity»). Por variable de entorno: si una versión no la conociera, la
    # ignora (una opción desconocida en la línea de comandos impediría arrancar el túnel).
    $env:TUNNEL_TRANSPORT_PROTOCOL = "http2"
    Start-Process -FilePath $Cloudflared -WindowStyle Hidden -ArgumentList @(
        "tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:$Puerto", "--logfile", "`"$RegistroTunel`"")
    for ($i = 0; $i -lt 120 -and -not $direccion; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-Path $RegistroTunel) {
            $m = Select-String -Path $RegistroTunel -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' | Select-Object -First 1
            if ($m) { $direccion = $m.Matches[0].Value }
        }
    }
    if (-not $direccion) { Anotar "ERROR: el túnel no dio una dirección. Revise $RegistroTunel (¿hay internet?)"; exit 1 }
    Set-Content -Path $ArchivoEnlace -Value $direccion -Encoding UTF8
    Anotar "Enlace abierto: $direccion"
}
# Vigilancia oculta (una sola): no dejar dormir la computadora y reiniciar el servidor 8002 si se cae
$vigilando = Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match "enlace_publico\.ps1.*-Vigilar" }
if (-not $vigilando) {
    Start-Process -FilePath "powershell.exe" -WindowStyle Hidden -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "-Vigilar")
}
Write-Host ""
Write-Host "Enlace para compartir: $direccion"
