"""Authenticated native Socket.IO transport against only the isolated candidate."""
import json
import sys
import urllib.parse
import urllib.request

import role_http as h
import acceptance_config as cfg


def run():
    client = h.Client().login("one")
    site = cfg.current().site
    for origin, expected in ((None, True), (h.BASE, True), ("https://outside.example.invalid", False)):
        def request(path, content=None):
            headers = {"Content-Type": "text/plain;charset=UTF-8"}
            if origin:
                headers["Origin"] = origin
            with client.opener.open(urllib.request.Request(h.BASE + path, data=content, headers=headers), timeout=15) as response:
                return response.read()

        opened = request("/socket.io/?EIO=4&transport=polling")
        engine = json.loads(opened[1:])["sid"]
        path = "/socket.io/?" + urllib.parse.urlencode({"EIO": 4, "transport": "polling", "sid": engine})
        try:
            request(path, ("40/" + site + ",").encode())
            packets = request(path).split(b"\x1e")
            accepted = any(packet.startswith(("40/" + site + ",").encode()) for packet in packets)
            rejected = any(packet.startswith(("44/" + site + ",").encode()) for packet in packets)
            h.check("authenticated Socket.IO " + (origin or "no Origin"), accepted if expected else rejected and not accepted)
        finally:
            request(path, b"1")


def main():
    error = None
    try:
        run()
    except Exception as exc:
        error = type(exc).__name__
    output = h.save_receipt("realtime-http-" + cfg.current().attempt + ".json", error)
    print(json.dumps({"ok": error is None, "checks": len(h.RESULTS), "receipt": str(output)}))
    return 0 if error is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
