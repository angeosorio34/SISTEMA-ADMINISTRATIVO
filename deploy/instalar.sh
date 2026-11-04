#!/usr/bin/env bash
# Instala (o actualiza) SGA-GALLEGO en un servidor Ubuntu 24.04. Se ejecuta como root desde el paquete subido:
#
#   bash deploy/instalar.sh <dominio> [base.dump]
#
# <dominio>   nombre con que se entra: p. ej. 164-90-12-34.sslip.io o sga.centrogallego.com
# [base.dump] copia de la base (pg_dump -Fc) que se carga SOLO si la base "sicga" aún no existe
#
# Deja: PostgreSQL 17 con la base "sicga" (usuario propio, sin permisos de administrador), el sistema en
# /opt/sga-gallego servido por gunicorn como servicio, Caddy con HTTPS automático (Let's Encrypt),
# copia de la base todos los días (se guardan 30) y el cortafuegos con solo SSH, HTTP y HTTPS abiertos.
# Volver a ejecutarlo con un paquete nuevo actualiza el programa sin tocar los datos.
set -euo pipefail
DOMINIO="${1:?Indique el dominio, p. ej. 164-90-12-34.sslip.io}"
DUMP="${2:-}"
APP=/opt/sga-gallego
ORIGEN="$(cd "$(dirname "$0")/.." && pwd)"
export DEBIAN_FRONTEND=noninteractive

echo "== 1. Paquetes del sistema (PostgreSQL 17, Caddy, Python)"
apt-get update -q
apt-get install -y -q python3-venv curl gnupg ufw unattended-upgrades postgresql-common \
  debian-keyring debian-archive-keyring apt-transport-https
[ -f /etc/apt/sources.list.d/pgdg.sources ] || [ -f /etc/apt/sources.list.d/pgdg.list ] \
  || /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
if [ ! -f /usr/share/keyrings/caddy-stable-archive-keyring.gpg ]; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
fi
apt-get update -q
apt-get install -y -q postgresql-17 caddy
timedatectl set-timezone America/Caracas

echo "== 2. Memoria de intercambio (2 GB) para los informes grandes"
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== 3. Usuario y archivos del programa"
id sga >/dev/null 2>&1 || useradd --system --home-dir "$APP" --shell /usr/sbin/nologin sga
mkdir -p "$APP/media" "$APP/respaldos" /var/backups/sga
rm -rf "$APP/backend" "$APP/frontend" "$APP/deploy"
mkdir -p "$APP/frontend"
cp -a "$ORIGEN/backend" "$APP/backend"
cp -a "$ORIGEN/frontend/dist" "$APP/frontend/dist"
cp -a "$ORIGEN/deploy" "$APP/deploy"
[ -d "$ORIGEN/media" ] && cp -an "$ORIGEN/media/." "$APP/media/" || true

echo "== 4. Python y dependencias"
[ -x "$APP/venv/bin/python" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$APP/backend/requirements-servidor.txt"

echo "== 5. Base de datos"
[ -f "$APP/.clave_bd" ] || openssl rand -hex 24 > "$APP/.clave_bd"
[ -f "$APP/.clave_django" ] || openssl rand -base64 48 | tr -d '\n=+/' > "$APP/.clave_django"
CLAVE_BD="$(cat "$APP/.clave_bd")"
sudo -u postgres psql -v ON_ERROR_STOP=1 -q -v clave="$CLAVE_BD" <<'SQL'
SELECT format('CREATE ROLE sga LOGIN PASSWORD %L', :'clave') WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sga')
\gexec
SELECT format('ALTER ROLE sga PASSWORD %L', :'clave')
\gexec
-- Restaurar una copia desde Configuración crea una base aparte y luego la renombra
ALTER ROLE sga CREATEDB;
SQL
if ! sudo -u postgres psql -Atqc "SELECT 1 FROM pg_database WHERE datname = 'sicga'" | grep -q 1; then
  sudo -u postgres createdb -O sga -E UTF8 -T template0 sicga
  if [ -n "$DUMP" ]; then
    echo "   cargando $DUMP ..."
    # El usuario postgres no puede leer /root: la copia y su índice se pasan por /var/tmp
    install -m 644 "$DUMP" /var/tmp/sga-carga.dump
    pg_restore -l /var/tmp/sga-carga.dump > /var/tmp/sga-carga.lista
    # Las extensiones las crea el administrador; todo lo demás queda a nombre del usuario "sga"
    grep -oP ' EXTENSION - \K\S+' /var/tmp/sga-carga.lista | sort -u | while read -r ext; do
      sudo -u postgres psql -q -d sicga -c "CREATE EXTENSION IF NOT EXISTS \"$ext\""
    done
    grep -v ' EXTENSION ' /var/tmp/sga-carga.lista > /var/tmp/sga-carga.sin-ext
    chmod 644 /var/tmp/sga-carga.sin-ext
    sudo -u postgres pg_restore -d sicga --no-owner --role=sga --no-privileges -e \
      -L /var/tmp/sga-carga.sin-ext /var/tmp/sga-carga.dump
    rm -f /var/tmp/sga-carga.dump /var/tmp/sga-carga.lista /var/tmp/sga-carga.sin-ext
  fi
fi

echo "== 6. Configuración (modo seguro, HTTPS detrás de Caddy)"
cat > "$APP/backend/.env" <<ENV
DJANGO_DEBUG=false
DJANGO_SECRET_KEY=$(cat "$APP/.clave_django")
DJANGO_ALLOWED_HOSTS=$DOMINIO,localhost,127.0.0.1
DJANGO_CSRF_TRUSTED_ORIGINS=https://$DOMINIO
DJANGO_DETRAS_DE_PROXY=true
PG_DSN=postgresql://sga:$CLAVE_BD@localhost:5432/sicga
MEDIA_ROOT=$APP/media
FRONTEND_DIST=$APP/frontend/dist
SGA_RESPALDOS_DIR=$APP/respaldos
ENV
chown -R sga:sga "$APP"
chmod 600 "$APP/backend/.env" "$APP/.clave_bd" "$APP/.clave_django"
cd "$APP/backend"
sudo -u sga "$APP/venv/bin/python" manage.py migrate --noinput
sudo -u sga "$APP/venv/bin/python" manage.py collectstatic --noinput -v 0
sudo -u sga "$APP/venv/bin/python" manage.py check --deploy --fail-level ERROR

echo "== 7. Servicio web (gunicorn) y HTTPS (Caddy)"
install -m 644 "$APP/deploy/sga-gallego.service" /etc/systemd/system/sga-gallego.service
sed "s/{{DOMINIO}}/$DOMINIO/g" "$APP/deploy/Caddyfile" > /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable --now sga-gallego
systemctl restart sga-gallego
systemctl reload caddy || systemctl restart caddy

echo "== 8. Copias de seguridad diarias y cortafuegos"
install -m 755 "$APP/deploy/respaldo.sh" /usr/local/sbin/sga-respaldo
echo "30 3 * * * root /usr/local/sbin/sga-respaldo" > /etc/cron.d/sga-respaldo
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null
dpkg-reconfigure -f noninteractive unattended-upgrades

sleep 3
echo "== Listo: https://$DOMINIO"
curl -s -o /dev/null -w "   respuesta local del sistema: %{http_code}\n" http://127.0.0.1:8000/
