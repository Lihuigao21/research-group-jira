"""Adapt the retained legacy assertions to one explicit candidate/run identity."""
import contextlib
import http.cookiejar
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import types
import urllib.parse
import urllib.request

import acceptance_config as cfg
import role_http as h


def adapted_source(path):
    source = Path(path).read_bytes().decode("utf-8")
    # Keep historical assertion sources unchanged on disk. Only explicit run
    # dependencies are rebound in memory, before their main() is called.
    for expression in (
        'json.loads((ROOT / "runtime/secrets/demo-credentials.json").read_bytes())',
        'json.loads((ROOT / "runtime/secrets/demo-credentials.json").read_text(encoding="utf-8"))',
    ):
        source = source.replace(expression, "role_credentials()")
    if Path(path).name == "legacy-ai-source.py":
        source = source.replace('ROOT = Path(__file__).resolve().parents[3]', "ROOT = cfg.ROOT")
        source = source.replace('one.opener.addheaders = [("Origin", "http://localhost:8089")]',
                                'one.opener.addheaders = [("Origin", role_wrong_origin())]')
    return source


def load(name, path):
    module = types.ModuleType(name)
    module.__file__ = str(path)
    port = urllib.parse.urlsplit(cfg.current().base).port
    module.__dict__.update(cfg=cfg, role_credentials=h.credentials,
                           role_wrong_origin=lambda: "http://localhost:" + str(65534 if port == 65535 else 65535))
    exec(compile(adapted_source(path), str(path), "exec"), module.__dict__)
    return module


def adapt(core, kind):
    ctx = cfg.current()
    scratch = cfg.contained(ctx.scratch / ("legacy-" + kind), ctx.scratch)
    (scratch / "work").mkdir(parents=True, exist_ok=False)
    (scratch / "records").mkdir()
    core.BASE, core.TASK = ctx.base, scratch
    core.RESULTS, core.check = h.RESULTS, h.check
    original = core.Client.__init__

    def init(self):
        original(self)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self.jar), cfg.LocalRedirects())
        self.opener.addheaders = [("Origin", ctx.base), ("Referer", ctx.base + "/desk")]

    core.Client.__init__ = init

    def candidate_administrator():
        values = cfg.credentials(required=(*cfg.DEMO_USERS, "Administrator"))
        return core.Client().login_user("Administrator", values["Administrator"], "candidate acceptance administrator")

    core.local_administrator = candidate_administrator

    def candidate_temp_paths(filenames):
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", name) for name in filenames):
            raise AssertionError("Unexpected native upload probe filename")
        probe = ("import json,sys; from pathlib import Path; "
                 "request=json.loads(sys.stdin.read()); root=Path('/home/frappe/frappe-bench/sites')/request['site']; "
                 "paths=[root/area/'files'/('.temp-'+name) for area in ('public','private') for name in request['names']]; "
                 "print(json.dumps([str(path) for path in paths if path.exists()]))")
        result = subprocess.run(["docker", "exec", "-i", ctx.container,
                                 "/home/frappe/frappe-bench/env/bin/python", "-c", probe],
                                input=json.dumps({"site": ctx.site, "names": filenames}).encode("utf-8"),
                                cwd=cfg.ROOT, capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError("Candidate native temporary-file probe failed")
        return json.loads(result.stdout)

    core.native_temp_paths = candidate_temp_paths
    return scratch


def main(kind):
    if kind not in {"core", "surveys", "ai"}:
        raise ValueError("Unknown legacy suite")
    if kind == "core":
        cfg.credentials(required=(*cfg.DEMO_USERS, "Administrator"))
    source = (cfg.SOURCE / "legacy-ai-source.py" if kind == "ai" else
              cfg.ROOT / "scripts" / ("verify-http.py" if kind == "core" else "verify-surveys-http.py"))
    module = load("role_legacy_" + kind, source)
    core = module if kind == "core" else module.core
    scratch = adapt(core, kind)
    module.TASK = scratch
    if kind != "core":
        module.CHECKS, module.check = h.RESULTS, h.check
    if kind == "ai":
        original_ask = module.ask

        def station_ask(client, conversation, prompt, topic=None):
            return original_ask(client, conversation, "本站" + prompt, topic)

        module.ask = station_ask
    records, error = {}, None
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            module.main()
            if kind == "core":
                core.cleanup_extra_teacher()
                records = core.IDS
            elif kind == "surveys":
                records = module.RECORDS
    except Exception as caught:
        # Exception strings can contain response data; preserve the type and
        # failed check only through the sanitized assertion ledger.
        error = type(caught).__name__
        if kind == "core":
            records = core.IDS
    output = h.save_receipt("legacy-" + kind + "-http-" + cfg.current().attempt + ".json", error, records)
    print(json.dumps({"ok": error is None, "suite": kind, "checks": len(h.RESULTS), "receipt": str(output)}))
    return 0 if error is None else 1
