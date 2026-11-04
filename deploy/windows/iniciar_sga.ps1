<#
  Inicia SGA-GALLEGO en esta computadora: la base de datos (PostgreSQL) y el servidor web (waitress).
  Lo usan SGA-GALLEGO.cmd (doble clic) y el inicio automático (inicio_automatico.ps1).
  Si algo ya está en marcha, no lo vuelve a iniciar: la tarea programada lo ejecuta al iniciar sesión, al despertar de
  la suspensión y cada 10 minutos. Solo anota en %LOCALAPPDATA%\SGA-GALLEGO\registros\inicio.log cuando inicia algo.

    powershell -ExecutionPolicy Bypass -File iniciar_sga.ps1 [-AbrirNavegador]
#>
param([switch]$AbrirNavegador)
$ErrorActionPreference = "Stop"

# Una sola ejecución a la vez (al iniciar sesión pueden coincidir la tarea programada y un doble clic)
$candado = New-Object System.Threading.Mutex($false, "Local\SGA-GALLEGO-inicio")
if (-not $candado.WaitOne(0)) { exit 0 }
$inicio = $false

$Raiz = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Backend = Join-Path $Raiz "backend"
$Python = Join-Path $Backend ".venv\Scripts\python.exe"
$Datos = Join-Path $env:LOCALAPPDATA "SGA-GALLEGO"
$PgData = Join-Path $Datos "pgdata"
$Registros = Join-Path $Datos "registros"
New-Item -ItemType Directory -Force $Registros | Out-Null

function Anotar([string]$Texto) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Texto" | Add-Content -Path (Join-Path $Registros "inicio.log") -Encoding UTF8
    Write-Host $Texto
}

function Escuchando([int]$Puerto) {
    return [bool](Get-NetTCPConnection -LocalPort $Puerto -State Listen -ErrorAction SilentlyContinue)
}

# Puerto del sistema: SGA_PUERTO en backend\.env (8000 por defecto)
$Puerto = 8000
$ArchivoEnv = Join-Path $Backend ".env"
if (Test-Path $ArchivoEnv) {
    $linea = Select-String -Path $ArchivoEnv -Pattern '^\s*SGA_PUERTO\s*=\s*(\d+)' | Select-Object -First 1
    if ($linea) { $Puerto = [int]$linea.Matches[0].Groups[1].Value }
}

# PostgreSQL más nuevo instalado (C:\Program Files\PostgreSQL\<versión>\bin)
$PgCtl = Get-ChildItem "$env:ProgramFiles\PostgreSQL\*\bin\pg_ctl.exe" -ErrorAction SilentlyContinue |
    Sort-Object { [int]($_.Directory.Parent.Name -replace '\D', '') } -Descending | Select-Object -First 1
if (-not $PgCtl) { Anotar "ERROR: PostgreSQL no está instalado en esta computadora."; exit 1 }
if (-not (Test-Path $Python)) { Anotar "ERROR: falta el entorno de Python ($Python)."; exit 1 }

# 1. Base de datos
& $PgCtl.FullName status -D $PgData *> $null
if ($LASTEXITCODE -ne 0) {
    Anotar "Iniciando la base de datos..."
    $inicio = $true
    # Se espera solo a pg_ctl (no a PostgreSQL, que sigue corriendo): -w termina cuando la base acepta conexiones
    $p = Start-Process -FilePath $PgCtl.FullName -PassThru -WindowStyle Hidden -ArgumentList @(
        "start", "-D", "`"$PgData`"", "-l", "`"$(Join-Path $Registros 'postgresql.log')`"", "-w", "-t", "120")
    [void]$p.WaitForExit(150000)
    & $PgCtl.FullName status -D $PgData *> $null
    if ($LASTEXITCODE -ne 0) { Anotar "ERROR: la base de datos no arrancó. Revise $Registros\postgresql.log"; exit 1 }
}

# 2. Servidor web
if (-not (Escuchando $Puerto)) {
    Anotar "Iniciando SGA-GALLEGO en el puerto $Puerto..."
    $inicio = $true
    Start-Process -FilePath $Python -ArgumentList @("manage.py", "servir") -WorkingDirectory $Backend -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $Registros "servidor.log") -RedirectStandardError (Join-Path $Registros "servidor-errores.log")
    for ($i = 0; $i -lt 90 -and -not (Escuchando $Puerto); $i++) { Start-Sleep -Milliseconds 500 }
    if (-not (Escuchando $Puerto)) { Anotar "ERROR: el sistema no arrancó. Revise $Registros\servidor-errores.log"; exit 1 }
}

if ($inicio) { Anotar "SGA-GALLEGO listo: http://localhost:$Puerto/" } else { Write-Host "SGA-GALLEGO ya estaba en marcha: http://localhost:$Puerto/" }
if ($AbrirNavegador) { Start-Process "http://localhost:$Puerto/" }
