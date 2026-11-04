@echo off
rem Trae de la computadora de Access (por la red del club) la carpeta FOTOS SICGA y una copia del .accdb.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0traer_de_access.ps1" %*
