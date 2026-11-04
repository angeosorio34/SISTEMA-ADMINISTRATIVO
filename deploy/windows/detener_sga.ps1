<#
  Detiene el servidor web de SGA-GALLEGO (por ejemplo, para actualizar el programa).
  Con -ConBase también detiene la base de datos (PostgreSQL).

    powershell -ExecutionPolicy Bypass -File detener_sga.ps1 [-ConBase]

  Solo detiene el proceso que atiende el puerto del sistema (y el lanzador de Python que lo abrió), no otros
  programas de Python de la computadora.
#>
param([switch]$ConBase)

$Raiz = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Backend = Join-Path $Raiz "backend"
$Puerto = 8000
$ArchivoEnv = Join-Path $Backend ".env"
if (Test-Path $ArchivoEnv) {
    $linea = Select-String -Path $ArchivoEnv -Pattern '^\s*SGA_PUERTO\s*=\s*(\d+)' | Select-Object -First 1
    if ($linea) { $Puerto = [int]$linea.Matches[0].Groups[1].Value }
}

$detenidos = 0
$pids = @(Get-NetTCPConnection -LocalPort $Puerto -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
foreach ($id in $pids) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId = $id" -ErrorAction SilentlyContinue
    if (-not $p -or $p.Name -ne "python.exe" -or $p.CommandLine -notmatch "manage\.py") { continue }
    # venv de Windows: el python.exe de .venv lanza al python real; se detienen los dos
    $padre = Get-CimInstance Win32_Process -Filter "ProcessId = $($p.ParentProcessId)" -ErrorAction SilentlyContinue
    Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
    $detenidos++
    if ($padre -and $padre.Name -eq "python.exe" -and $padre.ExecutablePath -like "$Backend\.venv\*") {
        Stop-Process -Id $padre.ProcessId -Force -ErrorAction SilentlyContinue
    }
}
Write-Host "Servidor detenido en el puerto $Puerto ($detenidos)."

if ($ConBase) {
    $PgCtl = Get-ChildItem "$env:ProgramFiles\PostgreSQL\*\bin\pg_ctl.exe" -ErrorAction SilentlyContinue |
        Sort-Object { [int]($_.Directory.Parent.Name -replace '\D', '') } -Descending | Select-Object -First 1
    & $PgCtl.FullName stop -D (Join-Path $env:LOCALAPPDATA "SGA-GALLEGO\pgdata") -m fast
}
