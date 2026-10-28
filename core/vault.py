from __future__ import annotations

import json
from pathlib import Path

from core.models import Mapping


class VaultError(RuntimeError):
    """Raised when a local mapping file cannot be read or written safely."""


def write_mapping(path: Path, mapping: Mapping) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_name(f".{path.name}.tmp")
        temporary_path.write_text(
            json.dumps(mapping.to_json(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.replace(path)
    except OSError as exc:
        raise VaultError("mapping could not be written") from exc


def read_mapping(path: Path) -> Mapping:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("mapping root must be an object")
        return Mapping.from_json(payload)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise VaultError("mapping could not be read") from exc
