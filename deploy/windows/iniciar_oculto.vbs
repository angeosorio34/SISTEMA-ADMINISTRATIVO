' Ejecuta iniciar_sga.ps1 sin mostrar ninguna ventana (lo usa la tarea programada de inicio_automatico.ps1).
Dim carpeta
carpeta = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
CreateObject("WScript.Shell").Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & carpeta & "\iniciar_sga.ps1""", 0, False
