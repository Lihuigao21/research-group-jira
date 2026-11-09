"""为本地 BMAD 审查冻结完整差异，包含未跟踪文件，不改变 Git index。"""
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "265814932d6d759714f0a2bda7379a54205e9e8c"


def git(*arguments, expected=(0,)):
    result = subprocess.run(["git", *arguments], cwd=ROOT, capture_output=True)
    if result.returncode not in expected:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    return result.stdout


def main():
    folder = ROOT / "tasks/20261106-frappe-v1/work/review"
    folder.mkdir(parents=True, exist_ok=True)
    name = datetime.now(timezone.utc).strftime("review-%Y%m%dT%H%M%S%fZ")
    target = folder / (name + ".diff")
    changed = git("diff", "--binary", BASELINE, "--")
    untracked = git("ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
    paths = [item.decode("utf-8") for item in untracked if item]
    for path in paths:
        changed += git("diff", "--no-index", "--binary", "--", "/dev/null", path, expected=(0, 1))
    if not changed:
        raise RuntimeError("差异为空，不能开始审查")
    target.write_bytes(changed)
    manifest = {"baseline": BASELINE, "diff": str(target), "bytes": len(changed),
                "sha256": hashlib.sha256(changed).hexdigest(), "untracked_files": paths}
    target.with_suffix(".json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: manifest[key] for key in ("diff", "bytes", "sha256")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
