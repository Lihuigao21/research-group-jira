#!/usr/bin/env bash
set -euo pipefail
cd /home/frappe/frappe-bench
site_name="${SITE_NAME:-lab.localhost}"
if [[ ! -f "sites/${site_name}/site_config.json" ]]; then
  bench new-site "$site_name" --db-host db --mariadb-root-password "$DB_ROOT_PASSWORD" \
    --admin-password "$ADMIN_PASSWORD" --mariadb-user-host-login-scope='%'
fi
if ! bench --site "$site_name" list-apps --format text | grep -q research_group; then
  bench --site "$site_name" install-app research_group
fi
bench --site "$site_name" migrate
bench --site "$site_name" disable-scheduler
bench use "$site_name"

