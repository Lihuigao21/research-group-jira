"""Verify authenticated Socket.IO namespaces, including polling Origin fallback."""
import importlib.util
import json
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "tasks/20261106-frappe-v1/records/realtime-verification.json"


def main():
    spec = importlib.util.spec_from_file_location("rg_http_verification", ROOT / "scripts/verify-http.py")
    http = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(http)
    passwords = json.loads((ROOT / "runtime/secrets/demo-credentials.json").read_text(encoding="utf-8"))
    client = http.Client().login("one", passwords)
    site = next(line.split("=", 1)[1].strip() for line in
                (ROOT / "deploy/.env").read_text(encoding="utf-8").splitlines() if line.startswith("SITE_NAME="))
    results = []
    for origin, expected in ((None, True), (http.BASE, True), ("https://outside.example.invalid", False)):
        def request(path, content=None):
            headers = {"Content-Type": "text/plain;charset=UTF-8"}
            if origin:
                headers["Origin"] = origin
            with client.opener.open(urllib.request.Request(http.BASE + path, data=content, headers=headers), timeout=15) as response:
                return response.read()

        opened = request("/socket.io/?EIO=4&transport=polling")
        engine = json.loads(opened[1:])["sid"]
        path = "/socket.io/?" + urllib.parse.urlencode({"EIO": 4, "transport": "polling", "sid": engine})
        try:
            request(path, ("40/" + site + ",").encode())
            packets = request(path).split(b"\x1e")
            accepted = any(packet.startswith(("40/" + site + ",").encode()) for packet in packets)
            rejected = any(packet.startswith(("44/" + site + ",").encode()) for packet in packets)
            results.append({"origin": origin or "absent", "namespace_authenticated": accepted,
                            "namespace_rejected": rejected, "passed": accepted if expected else rejected and not accepted})
        finally:
            request(path, b"1")  # Close only this probe's Engine.IO session.
    receipt = {"checks": results, "passed": sum(row["passed"] for row in results), "total": len(results)}
    RECORD.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False))
    return 0 if receipt["passed"] == receipt["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
