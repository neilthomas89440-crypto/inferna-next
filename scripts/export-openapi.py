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

spec = app.openapi()

# --- Gateway OpenAPI hygiene -------------------------------------------
# Gateway handlers read the raw Request and raise OpenAI-style errors, so
# FastAPI cannot infer those contracts from signatures; shape the generated
# spec here instead of hand-editing docs/openapi.yaml. Applied to a freshly
# generated spec on every run, so this stays idempotent.

GATEWAY_ERROR_CODES = ("400", "401", "403", "404", "413", "502")
GATEWAY_PATHS = (
    "/v1/chat/completions",
    "/v1/embeddings",
    "/v1/audio/transcriptions",
    "/v1/models",
)

for gateway_path in GATEWAY_PATHS:
    for operation in spec["paths"].get(gateway_path, {}).values():
        if not isinstance(operation, dict) or "responses" not in operation:
            continue
        for code in GATEWAY_ERROR_CODES:
            response = operation["responses"].get(code)
            if isinstance(response, dict):
                response["content"] = {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/GatewayError"}
                    }
                }
        # Raw-Request handlers bypass FastAPI request validation entirely,
        # so the generated 422 HTTPValidationError response is unreachable.
        operation["responses"].pop("422", None)

# Raw application/octet-stream bodies carry no form fields; the model is
# supplied as a query parameter for that content type.
_transcriptions = spec["paths"].get("/v1/audio/transcriptions", {}).get("post")
_params = _transcriptions.setdefault("parameters", []) if _transcriptions else []
if not any(p.get("name") == "model" and p.get("in") == "query" for p in _params):
    _params.append(
        {
            "name": "model",
            "in": "query",
            "required": False,
            "schema": {"type": "string"},
            "description": "Required for application/octet-stream bodies",
        }
    )

spec.setdefault("components", {}).setdefault("schemas", {})["GatewayError"] = {
    "title": "GatewayError",
    "type": "object",
    "description": "OpenAI-style error envelope returned by /v1/* gateway routes.",
    "required": ["error"],
    "properties": {
        "error": {
            "type": "object",
            "required": ["message", "type", "code"],
            "properties": {
                "message": {"type": "string"},
                "type": {"type": "string"},
                "code": {"type": "string"},
            },
        }
    },
}
# ------------------------------------------------------------------------

out = Path({out!r})
out.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
"""



def generate(target: Path) -> None:
    # PROG contains literal braces (dict/set literals), so str.format is not
    # usable; substitute the output path by marker replacement instead.
    prog = PROG.replace("{out!r}", repr(str(target)))
    subprocess.run(
        ["uv", "run", "python", "-c", prog],
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
