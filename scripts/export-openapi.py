#!/usr/bin/env python3
"""Export the FastAPI OpenAPI spec to docs/openapi.yaml.

Usage:
    python scripts/export-openapi.py          # write docs/openapi.yaml
    python scripts/export-openapi.py --check  # fail if the spec is stale (CI)

Run from the repo root; the server venv provides yaml + the app package.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
OUT = ROOT / "docs" / "openapi.yaml"

PROG = """
from pathlib import Path
import yaml
from inferna_server.main import app

out = Path({out!r})
out.write_text(yaml.safe_dump(app.openapi(), sort_keys=False), encoding="utf-8")
"""


def generate(target: Path) -> None:
    subprocess.run(
        ["uv", "run", "python", "-c", PROG.format(out=str(target))],
        cwd=SERVER,
        check=True,
    )


def main() -> int:
    if "--check" in sys.argv:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "openapi.yaml"
            generate(tmp)
            if not OUT.exists() or OUT.read_text(encoding="utf-8") != tmp.read_text(encoding="utf-8"):
                print("docs/openapi.yaml is out of date; run scripts/export-openapi.py")
                return 1
        print("docs/openapi.yaml is up to date")
        return 0
    generate(OUT)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
