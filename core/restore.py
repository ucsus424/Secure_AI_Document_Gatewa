from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List

from core.models import Mapping, format_placeholder, placeholder_keys_in


class RestoreError(RuntimeError):
    """Base error: the file cannot be restored with the chosen document."""


class UnknownPlaceholdersError(RestoreError):
    """The file contains placeholders that the chosen document does not have.

    This means the file belongs to another document (or was mixed with one),
    so restoring it would put wrong values into the result.
    """

    def __init__(self, document_id: str, unknown: Iterable[str]) -> None:
        self.document_id = document_id
        self.unknown = sorted(set(unknown))
        super().__init__(
            "file does not belong to the chosen document: "
            f"unknown placeholders={', '.join(self.unknown)}"
        )


@dataclass
class RestoreResult:
    text: str
    expected_count: int
    found_count: int
    # Placeholders of the document that are not in the file. They were most
    # likely removed while editing, so this is a warning, not an error.
    missing: List[str] = field(default_factory=list)


def check_placeholders_belong(text: str, mapping: Mapping) -> None:
    """Fail when the file has placeholders that the document does not own."""
    unknown = placeholder_keys_in(text) - set(mapping.entities)
    if unknown:
        raise UnknownPlaceholdersError(mapping.document_id, unknown)


def restore_text(text: str, mapping: Mapping) -> RestoreResult:
    check_placeholders_belong(text, mapping)

    found = [key for key in mapping.entities if format_placeholder(key) in text]
    missing = [key for key in mapping.entities if key not in found]

    restored = text
    for key, value in mapping.entities.items():
        restored = restored.replace(format_placeholder(key), value)

    return RestoreResult(
        text=restored,
        expected_count=len(mapping.entities),
        found_count=len(found),
        missing=missing,
    )