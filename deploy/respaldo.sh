#!/usr/bin/env bash
# Copia diaria de la base de SGA-GALLEGO (cron 03:30). Se guardan las de los últimos 30 días.
# Restaurar una copia: sudo -u postgres pg_restore -d sicga --clean --if-exists /var/backups/sga/<archivo>.dump
set -euo pipefail
DESTINO=/var/backups/sga
mkdir -p "$DESTINO"
ARCHIVO="$DESTINO/sicga-$(date +%Y%m%d-%H%M).dump"
sudo -u postgres pg_dump -Fc sicga > "$ARCHIVO.tmp" && mv "$ARCHIVO.tmp" "$ARCHIVO"
chmod 600 "$ARCHIVO"
find "$DESTINO" -name 'sicga-*.dump' -mtime +30 -delete
