<#
  Cierra el enlace temporal de enlace_publico.ps1: el túnel de Cloudflare y el servidor del puerto 8002.
  El sistema normal (puerto 8000) y la base de datos siguen en marcha.

    powershell -ExecutionPolicy Bypass -File detener_enlace.ps1
#>
$Raiz = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Backend = Join-Path $Raiz "backend"
$Puerto = 8002
$Registros = Join-Path $env:LOCALAPPDATA "SGA-GALLEGO\registros"

# 1. El túnel (solo el que apunta al puerto 8002)
$tuneles = @(Get-CimInstance Win32_Process -Filter "Name = 'cloudflared.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match "127\.0\.0\.1:$Puerto" })
foreach ($t in $tuneles) { Stop-Process -Id $t.ProcessId -Force -ErrorAction SilentlyContinue }

# 2. El servidor del puerto 8002 (y el lanzador de Python del .venv que lo abrió)
$cerrados = 0
foreach ($id in @(Get-NetTCPConnection -LocalPort $Puerto -State Listen -ErrorAction SilentlyContinue |
                  Select-Object -ExpandProperty OwningProcess -Unique)) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId = $id" -ErrorAction SilentlyContinue
    if (-not $p -or $p.Name -ne "python.exe" -or $p.CommandLine -notmatch "manage\.py") { continue }
    $padre = Get-CimInstance Win32_Process -Filter "ProcessId = $($p.ParentProcessId)" -ErrorAction SilentlyContinue
    Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
    $cerrados++
    if ($padre -and $padre.Name -eq "python.exe" -and $padre.ExecutablePath -like "$Backend\.venv\*") {
        Stop-Process -Id $padre.ProcessId -Force -ErrorAction SilentlyContinue
    }
}
# 3. La vigilancia termina sola al no encontrar el túnel (y vuelve a permitir la suspensión)
Remove-Item (Join-Path $Registros "enlace.txt") -ErrorAction SilentlyContinue
"$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') Enlace cerrado." | Add-Content -Path (Join-Path $Registros "inicio.log") -Encoding UTF8
Write-Host "Enlace cerrado (túnel: $($tuneles.Count), servidor: $cerrados). El sistema normal sigue en http://localhost:8000/"
