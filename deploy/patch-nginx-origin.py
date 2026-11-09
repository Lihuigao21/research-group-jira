"""Patch only the pinned Socket.IO Origin directive and its http-context map."""
from pathlib import Path
import sys

UPSTREAM_ORIGIN = "proxy_set_header Origin $proxy_x_forwarded_proto://${FRAPPE_SITE_NAME_HEADER};"
SERVER_ANCHOR = "server {\n\tlisten ${NGINX_LISTEN_PORT};"
ORIGIN_MAP = '''map $http_origin $rg_socket_origin {
\tdefault $http_origin;
\t"" "$proxy_x_forwarded_proto://$http_host";
}

'''


def patch_template(text):
    assert text.count(UPSTREAM_ORIGIN) == 1, "Unexpected upstream socket Origin directive"
    assert text.count(SERVER_ANCHOR) == 1, "Unexpected upstream server anchor"
    assert "$rg_socket_origin" not in text, "Socket Origin patch already present"
    text = text.replace(SERVER_ANCHOR, ORIGIN_MAP + SERVER_ANCHOR)
    return text.replace(UPSTREAM_ORIGIN, "proxy_set_header Origin $rg_socket_origin;")


def main(path):
    path = Path(path)
    patched = patch_template(path.read_bytes().decode("utf-8"))
    path.write_bytes(patched.encode("utf-8"))


if __name__ == "__main__":
    main(sys.argv[1])
