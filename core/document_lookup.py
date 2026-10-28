from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Set

from core.database import (
    DEFAULT_DB_PATH,
    DocumentMatch,
    DocumentNotFoundError,
    document_exists,
    find_documents_by_placeholders,
    normalize_document_id,
)
from core.office import read_document_id


FILENAME_ID_RE = re.compile(r"_SAFE_([0-9A-Fa-f]{8})(?![0-9A-Fa-f])")
PLACEHOLDER_KEY_RE = re.compile(r"⟦([A-Z0-9_]+)⟧")


class AmbiguousDocumentError(DocumentNotFoundError):
    """Raised when several stored documents fit the uploaded file."""

    def __init__(self, candidates: List[DocumentMatch]) -> None:
        self.candidates = candidates
        super().__init__("several documents match the uploaded file")


def placeholder_keys_in(text: str) -> Set[str]:
    return set(PLACEHOLDER_KEY_RE.findall(text))


def resolve_document_id(
    file_path: Path,
    text: str,
    db_path: Path = DEFAULT_DB_PATH,
    explicit_id: Optional[str] = None,
    original_filename: Optional[str] = None,
) -> str:
    """Find which stored document an uploaded file belongs to.

    Order of checks:
      1. number chosen by the user (only when several documents matched before);
      2. number stored in the Office file properties;
      3. number in the file name, e.g. report_SAFE_39F3C1EE.docx;
      4. the set of placeholders found in the file.
    """
    candidates_by_marker = [
        explicit_id,
        read_document_id(file_path),
        _id_from_filename(original_filename or file_path.name),
    ]
    for candidate in candidates_by_marker:
        if candidate and document_exists(candidate, db_path):
            return normalize_document_id(candidate)

    matches = find_documents_by_placeholders(placeholder_keys_in(text), db_path)
    if not matches:
        raise DocumentNotFoundError("document not found")

    complete = [match for match in matches if match.is_complete]
    if len(complete) == 1:
        return complete[0].id
    if not complete and len(matches) == 1:
        # Some placeholders are missing; restore will report which ones.
        return matches[0].id

    raise AmbiguousDocumentError(complete or matches)


def _id_from_filename(filename: str) -> Optional[str]:
    match = FILENAME_ID_RE.search(filename)
    return match.group(1).upper() if match else None