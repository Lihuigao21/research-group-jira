#!/usr/bin/env bash
# Install pinned Frappe + this app into a throwaway bench and run the real
# Frappe/MariaDB test suite. Used by CI; also works on a Linux dev machine.
#
# Requires: Python 3.14 on PATH as $PYTHON, MariaDB with utf8mb4 defaults,
# Redis on 127.0.0.1:${REDIS_PORT:-6379}. Never point it at a real site.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BENCH="${BENCH_DIR:-$ROOT/runtime/ci-bench}"
PYTHON="${PYTHON:-python3.14}"
FRAPPE_REF="${FRAPPE_REF:-v16.55.2}"
DB_HOST="${DB_HOST:-127.0.0.1}"
DB_ROOT_PASSWORD="${DB_ROOT_PASSWORD:?set DB_ROOT_PASSWORD}"
REDIS="redis://127.0.0.1:${REDIS_PORT:-6379}"
SITE="${SITE:-ci.localhost}"

# This launcher owns a new disposable bench. Refuse every existing path,
# including broken symlinks, before touching files or installing dependencies.
if [[ ! "$SITE" =~ ^ci(-[a-z0-9]+)*[.]localhost$ ]]; then
  printf '%s\n' 'SITE must be ci.localhost or ci-<lowercase-alphanumeric>.localhost.' >&2
  exit 1
fi
if [[ -e "$BENCH" || -L "$BENCH" ]]; then
  printf 'Refusing existing BENCH_DIR: %s\n' "$BENCH" >&2
  exit 1
fi
mkdir -p -- "$(dirname -- "$BENCH")"
mkdir -- "$BENCH" # Exclusive claim also rejects a concurrent creator.
BENCH="$(cd -- "$BENCH" && pwd -P)"
mkdir -- "$BENCH/apps" "$BENCH/sites" "$BENCH/logs"
git clone --depth 1 --branch "$FRAPPE_REF" https://github.com/frappe/frappe.git "$BENCH/apps/frappe"
ln -s "$ROOT/src/research_group" "$BENCH/apps/research_group"
"$PYTHON" -m venv "$BENCH/env"
"$BENCH/env/bin/pip" install --quiet --upgrade pip
"$BENCH/env/bin/pip" install --quiet -e "$BENCH/apps/frappe" -e "$BENCH/apps/research_group"
printf 'frappe\nresearch_group\n' > "$BENCH/sites/apps.txt"
cat > "$BENCH/sites/common_site_config.json" <<JSON
{"db_host": "$DB_HOST", "db_port": 3306, "redis_cache": "$REDIS", "redis_queue": "$REDIS", "redis_socketio": "$REDIS"}
JSON
mkdir -p "$BENCH/sites/assets"
frappe() { (cd "$BENCH/sites" && "$BENCH/env/bin/python" -m frappe.utils.bench_helper frappe "$@"); }
frappe new-site "$SITE" --db-root-username root --db-root-password "$DB_ROOT_PASSWORD" \
  --admin-password "ci-$(date +%s)" --mariadb-user-host-login-scope='%' --install-app research_group
frappe --site "$SITE" set-config allow_tests 1 --parse
frappe --site "$SITE" set-config mute_emails 1 --parse
frappe --site "$SITE" run-tests --app research_group "$@"
