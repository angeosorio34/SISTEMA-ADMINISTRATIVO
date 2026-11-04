<#
  Abre SGA-GALLEGO a las demás computadoras de la red del club (reemplaza el .accdb compartido de Access).
  Ejecutar en la computadora que hace de servidor; pide permisos de administrador (para el cortafuegos).

    permitir_red_local.ps1            activa: cortafuegos + SGA_RED_LOCAL=1 en backend\.env + reinicia el sistema
    permitir_red_local.ps1 -Quitar    vuelve a «solo esta computadora»

  Cortafuegos de Windows:
    - permite el puerto del sistema solo en redes Privadas o de dominio y solo desde la misma subred (LocalSubnet);
    - lo bloquea en redes Públicas (wifi de un café, módem móvil...), aunque alguien haya aceptado antes el aviso
      de Windows para Python.
  La red del club debe estar marcada como «Privada» en Windows. Si está como Pública, el script pregunta si es la red
  del club y solo entonces la marca Privada.
  Una VPN conectada (TunnelBear...) hace que Windows deje de reconocer la red del club («Red no identificada») y la
  trate como Pública: el script lo detecta y pide desconectarla.
#>
param([switch]$Quitar, [int]$Puerto = 8000)

$esAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $esAdmin) {
    $argumentos = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Puerto $Puerto" + $(if ($Quitar) { " -Quitar" } else { "" })
    Start-Process powershell.exe -Verb RunAs -ArgumentList $argumentos
    exit 0
}

$Raiz = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$ArchivoEnv = Join-Path $Raiz "backend\.env"

function Fijar-Variable([string]$Nombre, [string]$Valor) {
    $lineas = @()
    if (Test-Path $ArchivoEnv) { $lineas = @(Get-Content $ArchivoEnv -Encoding UTF8) }
    $lineas = @($lineas | Where-Object { $_ -notmatch "^\s*$Nombre\s*=" }) + "$Nombre=$Valor"
    [IO.File]::WriteAllLines($ArchivoEnv, [string[]]$lineas, (New-Object Text.UTF8Encoding($false)))
}

$PatronVpn = 'TunnelBear|VPN|WireGuard|Wintun|TAP-|OpenVPN|NordLynx|Proton|Fortinet|AnyConnect|Hamachi'
$PatronSinNombre = 'no identificada|Unidentified|Identificando|Identifying'

function Red-Local {
    # Conexión (wifi o cable) por la que sale esta computadora, sin contar las VPN
    $vpn = @(Get-NetAdapter | Where-Object { $_.InterfaceDescription -match $PatronVpn } | ForEach-Object { $_.ifIndex })
    $ruta = Get-NetRoute -DestinationPrefix 0.0.0.0/0 -ErrorAction SilentlyContinue | Where-Object { $vpn -notcontains $_.ifIndex } |
        Sort-Object { $_.RouteMetric + (Get-NetIPInterface -InterfaceIndex $_.ifIndex -AddressFamily IPv4).InterfaceMetric } |
        Select-Object -First 1
    if ($ruta) { Get-NetConnectionProfile -InterfaceIndex $ruta.ifIndex -ErrorAction SilentlyContinue }
}

function Revisar-Red {
    $vpn = @(Get-NetAdapter | Where-Object { $_.Status -eq "Up" -and $_.InterfaceDescription -match $PatronVpn })
    if ($vpn) {
        Write-Warning ("Hay una VPN conectada ($($vpn[0].InterfaceDescription)). Mientras esté encendida, Windows no " +
                       "reconoce la red del club y la trata como Pública: las otras computadoras no podrán entrar. " +
                       "Desconéctela en el club (en TunnelBear también se puede agregar la red del club a las redes de confianza).")
        Read-Host "Desconecte la VPN y pulse Enter para seguir" | Out-Null
        Start-Sleep -Seconds 5
        if (Get-NetAdapter | Where-Object { $_.Status -eq "Up" -and $_.InterfaceDescription -match $PatronVpn }) {
            Write-Warning ("La VPN sigue conectada (o se volvió a conectar sola). Desactive en ella la conexión automática " +
                           "y vuelva a ejecutar este script.")
            return
        }
    }
    # Al apagar la VPN, Windows no vuelve a identificar el wifi hasta reconectarse: se reconecta a la misma red
    $red = Red-Local
    if ($red -and $red.Name -match $PatronSinNombre) {
        $wifi = Get-NetAdapter -InterfaceIndex $red.InterfaceIndex
        $ssid = (netsh wlan show interfaces | Select-String '^\s*SSID\s*:\s*(.+)$' | Select-Object -First 1).Matches.Groups[1].Value
        if ($ssid -and $wifi.PhysicalMediaType -match 'Native 802.11|Wireless') {
            Write-Host "Reconectando el wifi «$ssid» para que Windows reconozca la red..."
            netsh wlan disconnect interface="$($wifi.Name)" | Out-Null
            Start-Sleep -Seconds 2
            netsh wlan connect name="$ssid" interface="$($wifi.Name)" | Out-Null
            for ($i = 0; $i -lt 30 -and "$((Red-Local).Name)" -notmatch "^$([regex]::Escape($ssid))$"; $i++) { Start-Sleep -Seconds 1 }
        }
    }
    $red = Red-Local
    if (-not $red) { Write-Warning "Esta computadora no está conectada a ninguna red."; return }
    if ($red.NetworkCategory -ne "Public") { Write-Host "La red «$($red.Name)» está marcada como $($red.NetworkCategory)."; return }
    if ($red.Name -match $PatronSinNombre) {
        Write-Warning ("Windows no reconoce esta red («$($red.Name)»), así que la trata como Pública y desde ella no se podrá " +
                       "entrar. Si hay una VPN encendida, desconéctela y vuelva a ejecutar este script.")
        return
    }
    $respuesta = Read-Host "La red «$($red.Name)» está marcada como Pública. Si es la red del club, ¿marcarla como Privada? (S/N)"
    if ($respuesta -match '^\s*[sS]') {
        Set-NetConnectionProfile -InterfaceIndex $red.InterfaceIndex -NetworkCategory Private
        Write-Host "La red «$($red.Name)» quedó marcada como Privada."
    } else {
        Write-Warning "La red «$($red.Name)» sigue como Pública: desde ella no se podrá entrar."
    }
}

Get-NetFirewallRule -DisplayName "SGA-GALLEGO*" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
if ($Quitar) {
    Fijar-Variable "SGA_RED_LOCAL" "0"
    Write-Host "Red local desactivada: el sistema solo responde en esta computadora."
} else {
    New-NetFirewallRule -DisplayName "SGA-GALLEGO: permitir desde la red del club (puerto $Puerto)" -Direction Inbound `
        -Protocol TCP -LocalPort $Puerto -Action Allow -Profile Private, Domain -RemoteAddress LocalSubnet | Out-Null
    New-NetFirewallRule -DisplayName "SGA-GALLEGO: bloquear en redes públicas (puerto $Puerto)" -Direction Inbound `
        -Protocol TCP -LocalPort $Puerto -Action Block -Profile Public | Out-Null
    Fijar-Variable "SGA_RED_LOCAL" "1"
    Fijar-Variable "SGA_PUERTO" "$Puerto"
    Write-Host "Red local activada en el puerto $Puerto."
    Revisar-Red
    $red = Red-Local
    $ip = if ($red) { Get-NetIPAddress -InterfaceIndex $red.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                      Select-Object -First 1 -ExpandProperty IPAddress }
    Write-Host "Desde las otras computadoras: http://$($env:COMPUTERNAME):$Puerto/$(if ($ip) { "  (o http://$($ip):$Puerto/)" })"
}

# Reiniciar el sistema para que tome el cambio
& (Join-Path $PSScriptRoot "detener_sga.ps1")
& (Join-Path $PSScriptRoot "iniciar_sga.ps1")
Read-Host "Listo. Pulse Enter para cerrar"
