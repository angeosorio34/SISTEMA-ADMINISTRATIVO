@echo off
rem Abre SGA-GALLEGO a las demás computadoras de la red del club (pide permisos de administrador).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0permitir_red_local.ps1" %*
