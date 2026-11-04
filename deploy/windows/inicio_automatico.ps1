<#
  Mantiene SGA-GALLEGO en marcha en esta computadora con una tarea programada de Windows (del usuario actual; no pide
  permisos de administrador). La tarea ejecuta iniciar_sga.ps1, sin ninguna ventana:
    - al iniciar sesión;
    - al despertar de la suspensión o la hibernación;
    - cada 10 minutos, por si la base o el sistema se cerraron (si ya están en marcha, no hace nada).

    powershell -ExecutionPolicy Bypass -File inicio_automatico.ps1          (activar)
    powershell -ExecutionPolicy Bypass -File inicio_automatico.ps1 -Quitar  (desactivar)

  Si Windows no deja crear la tarea, usa un acceso directo en la carpeta Inicio (solo al iniciar sesión).
#>
param([switch]$Quitar)

$Nombre = "SGA-GALLEGO"
$Acceso = Join-Path ([Environment]::GetFolderPath("Startup")) "SGA-GALLEGO.lnk"
$Usuario = "$env:USERDOMAIN\$env:USERNAME"

Unregister-ScheduledTask -TaskName $Nombre -Confirm:$false -ErrorAction SilentlyContinue
if ($Quitar) {
    Remove-Item $Acceso -ErrorAction SilentlyContinue
    Write-Host "Inicio automático desactivado."
    exit 0
}

$accion = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\wscript.exe" `
    -Argument "`"$(Join-Path $PSScriptRoot 'iniciar_oculto.vbs')`"" -WorkingDirectory $PSScriptRoot
$alEntrar = New-ScheduledTaskTrigger -AtLogOn -User $Usuario
$cada10 = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 10)
# Al despertar: evento 1 de Power-Troubleshooter («El sistema se reanudó desde un estado de bajo consumo»)
$clase = Get-CimClass -ClassName MSFT_TaskEventTrigger -Namespace Root/Microsoft/Windows/TaskScheduler
$alDespertar = New-CimInstance -CimClass $clase -ClientOnly
$alDespertar.Enabled = $true
$alDespertar.Subscription = '<QueryList><Query Id="0" Path="System"><Select Path="System">' +
    "*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1]]</Select></Query></QueryList>"
$alDespertar.Delay = "PT20S"
$ajustes = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal -UserId $Usuario -LogonType Interactive -RunLevel Limited

try {
    Register-ScheduledTask -TaskName $Nombre -Description "Mantiene en marcha SGA-GALLEGO (base de datos y servidor)" `
        -Action $accion -Trigger @($alEntrar, $cada10, $alDespertar) -Settings $ajustes -Principal $principal -ErrorAction Stop | Out-Null
    Remove-Item $Acceso -ErrorAction SilentlyContinue        # la tarea reemplaza al acceso directo
    Write-Host "Inicio automático activado: tarea programada «$Nombre» (al iniciar sesión, al despertar y cada 10 minutos)."
} catch {
    Write-Warning "No se pudo crear la tarea programada ($($_.Exception.Message)). Se usa el acceso directo de Inicio."
    $shell = New-Object -ComObject WScript.Shell
    $a = $shell.CreateShortcut($Acceso)
    $a.TargetPath = "$env:SystemRoot\System32\wscript.exe"
    $a.Arguments = "`"$(Join-Path $PSScriptRoot 'iniciar_oculto.vbs')`""
    $a.WorkingDirectory = $PSScriptRoot
    $a.Description = "Inicia SGA-GALLEGO (base de datos y servidor) al entrar a Windows"
    $a.Save()
    Write-Host "Inicio automático activado (acceso directo): $Acceso"
}
