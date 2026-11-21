"""Validate and atomically persist a satellite's editable name and room."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def identity_text(value: Any, *, required: bool) -> str:
    """Use the same 63-byte UTF-8 limits as the Native and Echo firmware."""
    if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Name and room must be text without control characters")
    value = value.strip(" ")
    if (required and not value) or len(value.encode("utf-8")) > 63:
        raise ValueError("Name must be 1–63 bytes and room at most 63 bytes")
    return str(value)


def save_identity(path: Path | None, name: str, room: str) -> None:
    """Preserve connection settings and commit both identity fields together."""
    if path is None:
        raise ValueError("Persistent satellite configuration is unavailable")
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(config, dict):
        raise ValueError("Invalid satellite configuration")
    config.update(name=name, room=room)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(config, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
