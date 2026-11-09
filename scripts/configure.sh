#!/usr/bin/env bash
set -euo pipefail
cd /home/frappe/frappe-bench
mkdir -p sites/assets logs
# The official main-entrypoint.sh already links sites/assets to the image assets.
if [[ ! -f sites/common_site_config.json ]]; then
    printf '{}\n' > sites/common_site_config.json
fi
printf 'frappe\nresearch_group\n' > sites/apps.txt
bench set-config -g db_host db
bench set-config -gp db_port 3306
bench set-config -g redis_cache redis://redis-cache:6379
# RQ blocks for 405s by default; keep the socket timeout comfortably above it.
bench set-config -g redis_queue 'redis://redis-queue:6379?socket_timeout=600&socket_connect_timeout=5'
bench set-config -g redis_socketio redis://redis-queue:6379
bench set-config -gp socketio_port 9000
bench set-config -g webserver_host http://backend
bench set-config -gp webserver_port 8000
bench set-config -gp max_file_size 53477376
bench set-config -gp rg_max_file_size 52428800
bench set-config -gp mute_emails 1
bench set-config -gp developer_mode 0
bench set-config -g chromium_path /usr/lib/chromium/chromium-headless-shell

