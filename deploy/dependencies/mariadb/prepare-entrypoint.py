"""Adapt the vendored official entrypoint's version constant to the pinned package.

The original GPL-2.0 source remains beside this script, unchanged. This produces
a marked derivative with one version substitution; initialization is unchanged.
"""

import hashlib
import re
import sys
from pathlib import Path

EXPECTED_SHA256 = {
    "docker-entrypoint.sh": "d98f4b3febace529b7042bf2880777bfcebdd691d1a122c50dd59ee40963a7d2",
    "healthcheck.sh": "592f89cb39308f8425693221b0476932918e2f3e451cad39b822e23ea0103ef0",
    "LICENSE": "ab15fd526bd8dd18a9e77ebc139656bf4d33e97fc7238cd11bf60e2b9b8666c6",
}


def prepare(source: Path, target: Path, package_version: str) -> None:
    original = source.read_bytes()
    for name, expected in EXPECTED_SHA256.items():
        if hashlib.sha256((source.parent / name).read_bytes()).hexdigest() != expected:
            raise SystemExit(f"Vendored MariaDB {name} SHA-256 mismatch")
    if package_version != "1:11.8.6-0+deb13u1":
        raise SystemExit("Review the entrypoint adaptation before changing the MariaDB package")
    version = re.fullmatch(r"\d+:(\d+\.\d+\.\d+)-[A-Za-z0-9+~.]+", package_version)
    if not version:
        raise SystemExit("Unexpected MariaDB Debian package version")
    text = original.decode("utf-8")
    before = '_mariadb_version() {\n\techo -n "11.8.9-MariaDB"\n}'
    after = f'_mariadb_version() {{\n\techo -n "{version[1]}-MariaDB"\n}}'
    if text.count(before) != 1:
        raise SystemExit("Official MariaDB version function changed")
    text = text.replace(before, after)
    notice = (
        "# Modified by Research Group Jira, 2026-11-06: version constant only;\n"
        "# use pinned Debian MariaDB 11.8.6 instead of upstream branch 11.8.9.\n"
        "# Original source and GPL-2.0 license: /usr/local/share/"
        "research-group-dependencies/mariadb-vendor/.\n"
    )
    text = text.replace("#!/bin/bash\n", "#!/bin/bash\n" + notice, 1)
    target.write_text(text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: prepare-entrypoint.py SOURCE TARGET DEBIAN_VERSION")
    prepare(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])
