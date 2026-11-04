<#
  Trae de las computadoras de Access, por la red del club, la carpeta FOTOS SICGA y una copia del .accdb, y pone las
  fotos en el sistema. Se usa con doble clic en «Traer de Access.cmd», o:

    traer_de_access.ps1                         busca las computadoras de la red y pregunta en cuáles buscar
    traer_de_access.ps1 -Equipo SERVIDORCG      solo en esa computadora
    traer_de_access.ps1 -Origen E:\             en una carpeta o una memoria USB (sin red)

  - Entra con el usuario y la clave de Windows de cada computadora. Los pide la ventana de Windows, no se guardan en
    ningún lado y la conexión se cierra al terminar.
  - Solo lee: el .accdb original y la carpeta de fotos quedan como estaban.
  - Una computadora que solo deja entrar como invitado (sin clave) no sirve: este Windows exige la firma SMB y la
    mantiene. El script lo explica y dice cómo hacerlo (usuario con clave en esa PC, o una memoria USB con -Origen).
  - Antes de poner las fotos hace una copia de seguridad del sistema. No reemplaza las fotos ya cargadas.
  - La copia del .accdb queda en %LOCALAPPDATA%\SGA-GALLEGO\access, para «Cargar lo nuevo desde Access».
  Anota lo que hace en %LOCALAPPDATA%\SGA-GALLEGO\registros\traer_access.log.
#>
param([string[]]$Equipo, [string]$Origen, [switch]$SinPausa)
$ErrorActionPreference = "Stop"

$Raiz = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Backend = Join-Path $Raiz "backend"
$Python = Join-Path $Backend ".venv\Scripts\python.exe"
$Datos = if ($env:SGA_DATOS_DIR) { $env:SGA_DATOS_DIR } else { Join-Path $env:LOCALAPPDATA "SGA-GALLEGO" }
$CarpetaAccess = Join-Path $Datos "access"
$Registros = Join-Path $Datos "registros"
New-Item -ItemType Directory -Force $CarpetaAccess, $Registros | Out-Null

$Imagenes = ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"          # las mismas que core/fotos.py
$NoRecorrer = "Windows", "Program Files", "Program Files (x86)", "ProgramData", "AppData", "`$Recycle.Bin",
              "System Volume Information", "Recovery", "PerfLogs", "MSOCache", "Config.Msi", "node_modules", ".git"
$PatronVpn = 'TunnelBear|VPN|WireGuard|Wintun|TAP-|OpenVPN|NordLynx|Proton|Fortinet|AnyConnect|Hamachi'

function Anotar([string]$Texto, [string]$Color = "Gray") {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Texto" | Add-Content -Path (Join-Path $Registros "traer_access.log") -Encoding UTF8
    Write-Host $Texto -ForegroundColor $Color
}

function Pausa {
    if (-not $SinPausa) { Read-Host "Pulse Enter para cerrar" | Out-Null }
}

# Conexiones de red de Windows (mpr.dll) y carpetas compartidas de una computadora (netapi32.dll)
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class RedSga {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    class NETRESOURCE {
        public int dwScope = 0; public int dwType = 0; public int dwDisplayType = 0; public int dwUsage = 0;
        public string lpLocalName = null; public string lpRemoteName; public string lpComment = null;
        public string lpProvider = null;
    }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct SHARE_INFO_1 { public string netname; public uint type; public string remark; }
    [DllImport("mpr.dll", CharSet = CharSet.Unicode)]
    static extern int WNetAddConnection2(NETRESOURCE recurso, string clave, string usuario, int opciones);
    [DllImport("mpr.dll", CharSet = CharSet.Unicode)]
    static extern int WNetCancelConnection2(string nombre, int opciones, bool forzar);
    [DllImport("netapi32.dll", CharSet = CharSet.Unicode)]
    static extern int NetShareEnum(string servidor, int nivel, out IntPtr datos, int maximo, out int leidos,
                                   out int total, ref int continuar);
    [DllImport("netapi32.dll")]
    static extern int NetApiBufferFree(IntPtr datos);

    // Sin opciones: la conexión no se recuerda ni se guarda la clave; dura hasta Desconectar
    public static int Conectar(string remoto, string usuario, string clave) {
        NETRESOURCE r = new NETRESOURCE();
        r.lpRemoteName = remoto;
        return WNetAddConnection2(r, clave, usuario, 0);
    }
    public static int Desconectar(string remoto) { return WNetCancelConnection2(remoto, 0, true); }

    // Carpetas compartidas de disco (también las administrativas C$, D$...)
    public static string[] Carpetas(string servidor) {
        List<string> lista = new List<string>();
        IntPtr datos; int leidos, total, continuar = 0;
        if (NetShareEnum(servidor, 1, out datos, -1, out leidos, out total, ref continuar) != 0) return lista.ToArray();
        int tam = Marshal.SizeOf(typeof(SHARE_INFO_1));
        for (int i = 0; i < leidos; i++) {
            SHARE_INFO_1 s = (SHARE_INFO_1)Marshal.PtrToStructure(new IntPtr(datos.ToInt64() + i * tam), typeof(SHARE_INFO_1));
            if ((s.type & 0xFF) == 0) lista.Add(s.netname);
        }
        NetApiBufferFree(datos);
        return lista.ToArray();
    }
}
'@

function Buscar-EnCarpeta([string]$Inicio, [int]$Niveles = 6) {
    # Busca carpetas FOTOS SICGA y archivos .accdb hasta $Niveles carpetas de profundidad (2 minutos como máximo)
    $fotos = @(); $bases = @()
    $pendientes = New-Object System.Collections.Queue
    $pendientes.Enqueue(@($Inicio, 0))
    $limite = (Get-Date).AddMinutes(2)
    while ($pendientes.Count -and (Get-Date) -lt $limite) {
        $ruta, $nivel = $pendientes.Dequeue()
        $carpeta = New-Object IO.DirectoryInfo $ruta
        try { $bases += @($carpeta.GetFiles("*.accdb")) } catch { continue }
        if ($nivel -ge $Niveles) { continue }
        try { $hijas = $carpeta.GetDirectories() } catch { continue }
        foreach ($h in $hijas) {
            if ($h.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
            if ($h.Name -like "FOTOS SICGA*") { $fotos += $h }
            elseif ($NoRecorrer -notcontains $h.Name) { $pendientes.Enqueue(@($h.FullName, ($nivel + 1))) }
        }
    }
    return @{ fotos = $fotos; bases = $bases }
}

function Fotos-En([IO.DirectoryInfo]$Carpeta) {
    return @($Carpeta.GetFiles() | Where-Object { $Imagenes -contains $_.Extension.ToLower() })
}

function Copiar-Base([IO.FileInfo]$Archivo, [string]$A) {
    # Abre el original solo para leer, dejando que Access lo siga usando mientras tanto
    $destino = Join-Path $A ("{0} (copia {1:yyyy-MM-dd HHmm}){2}" -f $Archivo.BaseName, (Get-Date), $Archivo.Extension)
    $de = New-Object IO.FileStream($Archivo.FullName, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    try {
        $hacia = [IO.File]::Create($destino)
        try { $de.CopyTo($hacia) } finally { $hacia.Dispose() }
    } finally { $de.Dispose() }
    return $destino
}

function Traer-De([string]$Nombre, [string[]]$Raices, [string]$A) {
    # Busca en $Raices la carpeta FOTOS SICGA con más fotos y el .accdb más reciente (primero los SICGA*), y los
    # copia a $A. Las fotos se juntan en $A\FOTOS SICGA: si ya está una del mismo nombre, queda la más nueva.
    $r = [ordered]@{ equipo = $Nombre; fotos = $null; copiadas = 0; base = $null; copia_base = $null }
    $carpetas = @(); $bases = @()
    foreach ($raiz in $Raices) {
        Anotar "  Buscando en $raiz ..."
        $hallado = Buscar-EnCarpeta $raiz
        $carpetas += $hallado.fotos
        $bases += $hallado.bases
        if ($carpetas -and $bases) { break }
    }
    foreach ($b in $bases | Sort-Object LastWriteTime -Descending | Select-Object -First 8) {
        Anotar ("  .accdb: {0}  ({1:dd/MM/yyyy HH:mm}, {2:N1} MB)" -f $b.FullName, $b.LastWriteTime, ($b.Length / 1MB))
    }
    $base = $bases | Sort-Object @{ Expression = { $_.Name -like "SICGA*" }; Descending = $true },
                                 @{ Expression = "LastWriteTime"; Descending = $true } | Select-Object -First 1
    if ($base) {
        $r.base = $base.FullName
        try {
            $r.copia_base = Copiar-Base $base $A
            Anotar "  Copia de la base: $($r.copia_base)" "Green"
        } catch { Anotar "  No se pudo copiar $($base.FullName): $($_.Exception.Message)" "Yellow" }
    }
    $mejor = $carpetas | Sort-Object { (Fotos-En $_).Count } -Descending | Select-Object -First 1
    if ($mejor) {
        $r.fotos = $mejor.FullName
        $hacia = Join-Path $A "FOTOS SICGA"
        New-Item -ItemType Directory -Force $hacia | Out-Null
        foreach ($f in Fotos-En $mejor) {
            $destino = Join-Path $hacia $f.Name
            if ((Test-Path -LiteralPath $destino) -and (Get-Item -LiteralPath $destino).LastWriteTime -ge $f.LastWriteTime) { continue }
            try { $f.CopyTo($destino, $true) | Out-Null; $r.copiadas++ }
            catch { Anotar "  No se pudo copiar $($f.Name): $($_.Exception.Message)" "Yellow" }
        }
        Anotar "  Fotos: $($r.copiadas) copiadas de $($mejor.FullName)" "Green"
    }
    if (-not $base -and -not $mejor) { Anotar "  No se encontró la carpeta FOTOS SICGA ni archivos .accdb." "Yellow" }
    return $r
}

function Raices-De([string]$Pc) {
    # Primero las carpetas compartidas normales; después las administrativas (C$...), que solo abre un administrador
    $nombres = [RedSga]::Carpetas("\\$Pc") | Where-Object { $_ -notin "ADMIN$", "print$" }
    return @($nombres | Sort-Object { $_.EndsWith('$') }, { $_ } | ForEach-Object { "\\$Pc\$_" })
}

# ---------------------------------------------------------------- elegir las computadoras y entrar
function Elegir-Equipos {
    Anotar "Buscando computadoras con carpetas compartidas en la red..."
    $vpn = @(Get-NetAdapter | Where-Object { $_.InterfaceDescription -match $PatronVpn } | ForEach-Object { $_.ifIndex })
    $propias = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object {
        $vpn -notcontains $_.InterfaceIndex -and $_.IPAddress -match '^(192\.168\.|10\.|172\.(1[6-9]|2\d|3[01])\.)' })
    $intentos = @{}
    foreach ($red in $propias | ForEach-Object { ($_.IPAddress -split '\.')[0..2] -join '.' } | Sort-Object -Unique) {
        foreach ($i in 1..254) {
            $ip = "$red.$i"
            if ($propias.IPAddress -contains $ip) { continue }
            $c = New-Object Net.Sockets.TcpClient
            $intentos[$ip] = @($c, $c.ConnectAsync($ip, 445))
        }
    }
    Start-Sleep -Milliseconds 1500
    $equipos = @()
    foreach ($ip in $intentos.Keys | Sort-Object { [version]$_ }) {
        $c, $t = $intentos[$ip]
        if ($t.Status -eq "RanToCompletion" -and $c.Connected) {
            # Nombre NetBIOS del servidor de archivos (<20>); con 15 letras va pegado: DESKTOP-SH618II<20>
            $linea = nbtstat -A $ip | Where-Object { $_ -match '^\s*(\S[^<]*?)\s*<20>' } | Select-Object -First 1
            $nombre = if ($linea -match '^\s*(\S[^<]*?)\s*<20>') { $Matches[1] } else { $ip }
            $equipos += [pscustomobject]@{ Nombre = $nombre; Ip = $ip }
        }
        $c.Close()
    }
    if (-not $equipos) {
        Anotar "No hay computadoras con carpetas compartidas en esta red." "Yellow"
        return @()
    }
    Write-Host ""
    Write-Host "Computadoras con carpetas compartidas:"
    for ($i = 0; $i -lt $equipos.Count; $i++) { Write-Host ("  {0}) {1,-18} {2}" -f ($i + 1), $equipos[$i].Nombre, $equipos[$i].Ip) }
    if ($equipos.Nombre -notcontains "DESKTOP-2GPI84A") {
        Write-Host "  En Access las fotos estaban en DESKTOP-2GPI84A (la PC de la caja): no aparece (apagada o con otro nombre)." -ForegroundColor DarkGray
    }
    $respuesta = Read-Host "¿En cuáles buscar? Números separados por coma (Enter = todas)"
    if (-not $respuesta.Trim()) { return @($equipos.Nombre) }
    return @($respuesta -split '[,; ]+' | Where-Object { $_ -match '^\d+$' -and [int]$_ -ge 1 -and [int]$_ -le $equipos.Count } |
             ForEach-Object { $equipos[[int]$_ - 1].Nombre })
}

function Entrar([string]$Pc) {
    # $true si quedó conectado a $Pc. Primero con la sesión actual de Windows; si no, con el usuario de esa PC.
    # net use da el motivo exacto: 3227320323 (0xC05D0003) = esa PC solo deja entrar como invitado, sin firma SMB
    $prueba = cmd /c "net use \\$Pc\IPC`$ 2>&1"
    $codigo = $LASTEXITCODE
    $soloInvitado = "$prueba" -match '3227320323|3227320322|1272'
    if ($codigo -eq 0) {
        cmd /c "net use \\$Pc\IPC`$ /delete /y >nul 2>&1"
        if ([RedSga]::Conectar("\\$Pc\IPC$", $null, $null) -eq 0) { return $true }
    } elseif (-not $soloInvitado -and "$prueba" -match '\b(53|64|67|1231|1232)\b') {
        Anotar "  No se llega a $Pc por la red (apagada o fuera de la red)." "Yellow"
        return $false
    }
    for ($intento = 1; $intento -le 3; $intento++) {
        $credencial = $null
        try {
            $credencial = Get-Credential -UserName "$Pc\" -Message ("Usuario y clave de Windows de $Pc (con los que se " +
                "entra a esa computadora). No se guardan en ningún lado.")
        } catch { }
        if (-not $credencial) { break }
        $usuario = $credencial.UserName.Trim()
        if ($usuario -notmatch '[\\@]') { $usuario = "$Pc\$usuario" }
        $e = [RedSga]::Conectar("\\$Pc\IPC$", $usuario, $credencial.GetNetworkCredential().Password)
        $credencial = $null
        if ($e -eq 0) { return $true }
        $motivo = switch ($e) {
            { $_ -in 86, 1326 } { "El usuario o la clave no son correctos."; break }
            1327 { "Windows no deja entrar por la red con una cuenta sin clave."; break }
            1219 { "Ya hay otra conexión abierta a $Pc con otro usuario: ciérrela (o reinicie la sesión) y vuelva a probar."; break }
            5 { "Acceso denegado: $Pc no reconoce ese usuario."; break }
            default { "Error ${e}: $((New-Object ComponentModel.Win32Exception($e)).Message)" }
        }
        Anotar "  $motivo" "Yellow"
        if ($e -eq 1327 -or $e -eq 1219) { break }
    }
    if ($soloInvitado) {
        Anotar "  $Pc comparte sus carpetas solo para invitados (sin clave), y este Windows exige la firma SMB por seguridad." "Yellow"
        Anotar "  Opciones: en $Pc, entrar con un usuario que tenga clave; o copiar FOTOS SICGA y el .accdb a una memoria USB" "Yellow"
        Anotar "  y ejecutar: traer_de_access.ps1 -Origen E:\   (la letra de la memoria)." "Yellow"
    }
    return $false
}

# ---------------------------------------------------------------- principal
if (-not (Test-Path $Python)) { Anotar "ERROR: falta el entorno de Python ($Python)." "Red"; Pausa; exit 1 }
Anotar "=== Traer de Access: fotos (FOTOS SICGA) y copia del .accdb ===" "Cyan"
$TempFotos = Join-Path $CarpetaAccess "FOTOS SICGA"
Remove-Item $TempFotos -Recurse -Force -ErrorAction SilentlyContinue
$resultados = @()

if ($Origen) {
    if (-not (Test-Path $Origen)) { Anotar "ERROR: no existe $Origen" "Red"; Pausa; exit 1 }
    $resultados += Traer-De $Origen @((Resolve-Path $Origen).ProviderPath) $CarpetaAccess
} else {
    $equipos = if ($Equipo) { $Equipo } else { Elegir-Equipos }
    foreach ($pc in $equipos) {
        Anotar "--- $pc ---" "Cyan"
        if (Entrar $pc) {
            try { $resultados += Traer-De $pc (Raices-De $pc) $CarpetaAccess }
            finally { [void][RedSga]::Desconectar("\\$pc\IPC$") }
        }
    }
}

# ---------------------------------------------------------------- poner las fotos en el sistema
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
function Sistema([string[]]$Argumentos) {
    $ErrorActionPreference = "Continue"
    Push-Location $Backend
    try { & $Python manage.py @Argumentos 2>&1 | ForEach-Object { Anotar "  $_" } } finally { Pop-Location }
    return $LASTEXITCODE
}

Write-Host ""
$hayFotos = (Test-Path $TempFotos) -and (Get-ChildItem $TempFotos -File | Select-Object -First 1)
if ($hayFotos) {
    Anotar "Copia de seguridad del sistema antes de poner las fotos..." "Cyan"
    if ((Sistema @("respaldo", "crear")) -ne 0) {
        Anotar "ERROR: no se pudo hacer la copia de seguridad; las fotos no se pusieron (quedan en $TempFotos)." "Red"
        Pausa; exit 1
    }
    Anotar "Poniendo las fotos en el sistema..." "Cyan"
    if ((Sistema @("importar_fotos", $TempFotos)) -eq 0) {
        Remove-Item $TempFotos -Recurse -Force             # ya están en el sistema; los originales siguen en Access
        Anotar "Fotos listas: se ven en la ficha de cada socio y beneficiario." "Green"
    } else {
        Anotar "ERROR al poner las fotos (quedan copiadas en $TempFotos)." "Red"
    }
} else {
    Anotar "No se trajo ninguna foto." "Yellow"
}

$copias = @($resultados | Where-Object { $_.copia_base } | ForEach-Object { $_.copia_base })
if ($copias) {
    Anotar "Copia del .accdb de Access: $($copias -join '; ')" "Green"
    Anotar ("Para traer lo nuevo de Access: Configuración > Copias de seguridad > Cargar lo nuevo desde Access, " +
            "y elegir ese archivo.")
}
Pausa
