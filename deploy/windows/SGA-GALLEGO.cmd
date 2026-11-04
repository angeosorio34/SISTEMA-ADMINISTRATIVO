@echo off
rem Abre SGA-GALLEGO: inicia la base de datos y el servidor si hace falta, y abre el navegador.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0iniciar_sga.ps1" -AbrirNavegador
if errorlevel 1 pause
