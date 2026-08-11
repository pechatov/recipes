#!/usr/bin/env python3
import os
import re
import sys
from pathlib import Path


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Set {name}")
    return value


if len(sys.argv) != 3:
    raise SystemExit("Usage: render-compose.py TEMPLATE OUTPUT")

values = {
    "__RECIPES_IMAGE__": required("RECIPES_IMAGE_REF"),
    "__RECIPES_APP_ROOT__": required("RECIPES_APP_ROOT"),
    "__RECIPES_BIND_ADDRESS__": required("RECIPES_BIND_ADDRESS"),
    "__RECIPES_PORT__": required("RECIPES_PORT"),
}
if not re.fullmatch(r"[A-Za-z0-9._:/@-]+", values["__RECIPES_IMAGE__"]):
    raise SystemExit("Invalid RECIPES_IMAGE_REF")
if not re.fullmatch(r"/mnt/[A-Za-z0-9._/-]+", values["__RECIPES_APP_ROOT__"]):
    raise SystemExit("Invalid RECIPES_APP_ROOT")
if any(part in {".", ".."} for part in Path(values["__RECIPES_APP_ROOT__"]).parts):
    raise SystemExit("Invalid RECIPES_APP_ROOT")
if not re.fullmatch(r"[A-Za-z0-9.:-]+", values["__RECIPES_BIND_ADDRESS__"]):
    raise SystemExit("Invalid RECIPES_BIND_ADDRESS")
if not re.fullmatch(r"[1-9][0-9]{0,4}", values["__RECIPES_PORT__"]):
    raise SystemExit("Invalid RECIPES_PORT")
if int(values["__RECIPES_PORT__"]) > 65535:
    raise SystemExit("Invalid RECIPES_PORT")

template = Path(sys.argv[1]).read_text()
for marker, value in values.items():
    if marker not in template:
        raise SystemExit(f"Compose marker is missing: {marker}")
    template = template.replace(marker, value)
Path(sys.argv[2]).write_text(template)
