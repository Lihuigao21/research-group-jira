#!/bin/sh
set -eu

case "${1:-}" in
    -*) set -- redis-server "$@" ;;
esac

if [ "${1:-}" = redis-server ]; then
    shift
    # Frappe connects from other containers. Compose publishes no Redis port.
    set -- redis-server --bind 0.0.0.0 --protected-mode no --dir /data "$@"
    if [ "$(id -u)" = 0 ]; then
        mkdir -p /data
        find /data -xdev ! -user redis -exec chown redis:redis '{}' +
        exec gosu redis "$@"
    fi
fi

exec "$@"
