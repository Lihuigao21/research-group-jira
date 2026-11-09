"""Preserve the external authority only for the pinned Frappe HTTP upstream."""
from pathlib import Path
import re
import sys

OLD = "proxy_set_header Host $host;"
NEW = "proxy_set_header Host $http_host;"
BACKEND = r"(?ms)^\tlocation @webserver \{\n.*?^\t\}"


def patch_template(text):
    blocks = list(re.finditer(BACKEND, text))
    assert len(blocks) == 1, "Unexpected upstream Frappe backend location"
    block = blocks[0]
    section = block.group()
    assert section.count(OLD) == 1 and NEW not in section, "Unexpected upstream backend Host directive"
    return text[:block.start()] + section.replace(OLD, NEW) + text[block.end():]


def main(path):
    path = Path(path)
    path.write_bytes(patch_template(path.read_bytes().decode("utf-8")).encode("utf-8"))


if __name__ == "__main__":
    main(sys.argv[1])
