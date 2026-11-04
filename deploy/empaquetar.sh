#!/usr/bin/env bash
# Prepara en esta PC lo que se sube al servidor (Git Bash):
#   bash deploy/empaquetar.sh <carpeta-salida> [PG_DSN de la base a copiar]
# Deja en la carpeta: sga-paquete.tar.gz (programa + frontend compilado + deploy/) y sicga.dump (copia de la base).
set -euo pipefail
SALIDA="${1:?Indique la carpeta de salida}"
DSN="${2:-postgresql://postgres@localhost:55432/sicga}"
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
PGB="/c/Program Files/PostgreSQL/17/bin"
mkdir -p "$SALIDA"

echo "== frontend"
(cd "$RAIZ/frontend" && npm run build >/dev/null 2>&1)

echo "== programa"
# --force-local: en Windows "C:/..." no es un equipo remoto
tar --force-local -czf "$SALIDA/sga-paquete.tar.gz" -C "$RAIZ" \
  --exclude='backend/.venv' --exclude='__pycache__' --exclude='backend/.env' \
  --exclude='backend/media' --exclude='backend/media_anterior' --exclude='backend/staticfiles' \
  backend frontend/dist deploy

echo "== base de datos"
"$PGB/pg_dump.exe" -Fc -d "$DSN" -f "$SALIDA/sicga.dump"

ls -la "$SALIDA"
