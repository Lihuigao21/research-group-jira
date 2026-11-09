"""Generate credentials without printing them or replacing existing secrets."""
import json
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / "deploy" / ".env"
DEMO = ROOT / "runtime" / "secrets" / "demo-credentials.json"


def write_new_private_file(path, content):
    """Claim a new file atomically; concurrent calls never replace credentials."""
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    owned = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(content)
            destination.flush()
            os.fsync(destination.fileno())
    except OSError:
        try:
            current = os.lstat(path)
            if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                os.unlink(path)
        except OSError:
            pass
        raise
    return True


def main():
    (ROOT / "runtime" / "secrets").mkdir(parents=True, exist_ok=True)
    for directory in ("sites", "logs", "db", "redis", "backups"):
        (ROOT / "runtime" / directory).mkdir(exist_ok=True)
    write_new_private_file(ENV, ("RG_IMAGE=research-group-jira:0.1.0\nSITE_NAME=lab.localhost\n"
                                 "BIND_ADDRESS=127.0.0.1\nHTTP_PORT=8088\n"
                                 f"DB_ROOT_PASSWORD={secrets.token_urlsafe(36)}\n"
                                 f"ADMIN_PASSWORD={secrets.token_urlsafe(30)}\n").encode("utf-8"))
    accounts = ["teacher.demo@example.invalid", "student.one.demo@example.invalid",
                "student.two.demo@example.invalid", "secretary.demo@example.invalid"]
    write_new_private_file(DEMO, (json.dumps({a: secrets.token_urlsafe(24) for a in accounts}, indent=2) + "\n").encode())
    print("Private config ready at deploy/.env; fictional account credentials: runtime/secrets/demo-credentials.json")


if __name__ == "__main__":
    main()

