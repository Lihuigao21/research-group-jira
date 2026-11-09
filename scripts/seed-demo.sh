#!/usr/bin/env bash
set -euo pipefail
cd /home/frappe/frappe-bench
site_name="${SITE_NAME:-lab.localhost}"
test -f "sites/$site_name/private/demo-credentials.json"
bench --site "$site_name" set-config rg_demo 1 --parse
bench --site "$site_name" execute research_group.demo.seed \
  --kwargs "{\"credentials_path\":\"/home/frappe/frappe-bench/sites/$site_name/private/demo-credentials.json\"}"

