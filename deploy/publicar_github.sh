#!/usr/bin/env bash
# Copia LIMPIA del sistema para el repositorio privado de GitHub que usa Render (plan gratuito).
#   bash deploy/publicar_github.sh <carpeta-del-repo> [mensaje]
# Solo lleva el programa (backend sin .venv/.env/fotos), el frontend compilado y render.yaml.
# NUNCA lleva el .accdb de Access, la carpeta de migración ni copias de la base.
set -euo pipefail
REPO="${1:?Indique la carpeta del repositorio}"
MENSAJE="${2:-Actualización del sistema}"
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"

(cd "$RAIZ/frontend" && npm run build >/dev/null 2>&1)
mkdir -p "$REPO"
cd "$REPO"
[ -d .git ] || git init -q -b main
# Se reemplaza todo menos la historia de git
find . -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
mkdir -p frontend
cp -r "$RAIZ/backend" ./backend
rm -rf backend/.venv backend/.env backend/media backend/media_anterior backend/staticfiles
find backend -name __pycache__ -type d -prune -exec rm -rf {} +
cp -r "$RAIZ/frontend/dist" ./frontend/dist
cp "$RAIZ/deploy/render.yaml" ./render.yaml
cat > .gitignore <<'IGN'
.venv/
__pycache__/
.env
.env.*
!.env.example
*.accdb
*.mdb
*.dump
*.zip
*.log
*.pem
*.key
pgpass*
.pgpass
backend/media/
backend/media_anterior/
backend/staticfiles/
respaldos/
registros/
IGN
printf '# SGA-GALLEGO\n\nSistema Gallego de Administración (Centro Gallego). Copia para el servidor; el proyecto completo está en la PC del club.\n' > README.md
git add -A

# Revisión antes de guardar: si aparece un archivo prohibido o algo que parece una clave, no se guarda nada
PROHIBIDOS="$(git diff --cached --name-only | grep -iE '(^|/)\.env$|\.(accdb|mdb|dump|zip|log|pem|key)$|pgpass' || true)"
if [ -n "$PROHIBIDOS" ]; then
  echo "ALTO: estos archivos no deben ir a GitHub:"; echo "$PROHIBIDOS"; git reset -q; exit 1
fi
SECRETOS="$(git diff --cached -U0 | grep -E '^\+' | grep -v -iE 'cambiar|ejemplo|CLAVE@|<clave>' \
  | grep -iE "(secret[_-]?key|password|passwd|pwd|token|api[_-]?key)[\"' ]*[:=][ \"']*[A-Za-z0-9_+/=-]{16,}|postgres(ql)?://[^:@/ ]+:[^@/ ]+@|-----BEGIN [A-Z ]*PRIVATE KEY|ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,}" || true)"
if [ -n "$SECRETOS" ]; then
  echo "ALTO: posibles claves en los cambios (no se muestran completas):"
  echo "$SECRETOS" | cut -c1-40 | sed 's/$/…/'
  git reset -q; exit 1
fi

git -c user.name="SGA-GALLEGO" -c user.email="sga-gallego@localhost" commit -q -m "$MENSAJE" || echo "(sin cambios)"
git log --oneline -1
