#!/usr/bin/env bash
set -euo pipefail
cd /home/frappe/frappe-bench
site_name="${SITE_NAME:-lab.localhost}"
# Native backup bundles DB, public/private files and site configuration together.
bench --site "$site_name" backup --with-files --compress
printf 'Backup files are in sites/%s/private/backups/. Preserve matching timestamps and site config.\n' "$site_name"

