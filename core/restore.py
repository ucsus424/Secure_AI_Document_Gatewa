from __future__ import annotations

from dataclasses import dataclass
from typing import List

from core.models import Mapping, format_placeholder


class RestoreError(RuntimeError):
    def __init__(self, expected_count: int, found_count: int, missing: List[str]) -> None:
        self.expected_count = expected_count
        self.found_count = found_count
        self.missing = missing
        super().__init__(
            "restore failed: "
            f"expected placeholders={expected_count}, "
            f"found placeholders={found_count}, "
            f"missing={', '.join(missing)}"
        )


@dataclass
class RestoreResult:
    text: str
    expected_count: int
    found_count: int
    missing: List[str]


def restore_text(text: str, mapping: Mapping) -> RestoreResult:
    found = [key for key in mapping.entities if format_placeholder(key) in text]
    missing = [key for key in mapping.entities if key not in found]

    if missing:
        raise RestoreError(
            expected_count=len(mapping.entities),
            found_count=len(found),
            missing=missing,
        )

    restored = text
    for key, value in mapping.entities.items():
        restored = restored.replace(format_placeholder(key), value)

    return RestoreResult(
        text=restored,
        expected_count=len(mapping.entities),
        found_count=len(found),
        missing=[],
    )
