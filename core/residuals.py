from __future__ import annotations

import re
from typing import Iterable, List

from core.models import Mapping


SPACE_LIKE_RE = re.compile(r"[\s   ⁠]+")
INVISIBLE_RE = re.compile(r"[­​‌‍﻿]")


class ResidualValuesError(RuntimeError):
    """Sensitive values are still present in the SAFE output."""

    def __init__(self, keys: Iterable[str]) -> None:
        self.keys = sorted(set(keys))
        super().__init__(
            "anonymization left sensitive values in the SAFE output: " + ", ".join(self.keys)
        )


def normalize_spaces(text: str) -> str:
    return SPACE_LIKE_RE.sub(" ", INVISIBLE_RE.sub("", text)).strip()


def find_residual_keys(text: str, mapping: Mapping) -> List[str]:
    """Keys whose value (or a recorded variant of it) is still in the text."""
    normalized_text = normalize_spaces(text)
    residual: List[str] = []
    for key, value in mapping.entities.items():
        candidates = [value, *mapping.aliases.get(key, [])]
        for candidate in candidates:
            normalized = normalize_spaces(candidate)
            if normalized and normalized in normalized_text:
                residual.append(key)
                break
    return residual


def require_no_residual_values(text: str, mapping: Mapping) -> None:
    residual = find_residual_keys(text, mapping)
    if residual:
        raise ResidualValuesError(residual)