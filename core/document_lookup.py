"""Выбор документа из базы, по которому восстанавливается файл.

Правило: автоматически документ выбирается только по НОМЕРУ. Метки вроде
⟦PERSON_001⟧ повторяются почти в каждом документе, поэтому по ним программа
ничего не угадывает, а предлагает пользователю выбрать документ.

Порядок:
  1. Явный номер (пользователь ввёл или выбрал). Используется только он.
     Нет в базе -> ошибка, другой документ не подбирается.
  2. Номер в файле: свойство внутри DOCX и _SAFE_XXXXXXXX в имени.
     Разные номера -> ошибка несоответствия. Номер есть, но документа нет
     в базе -> ошибка, подбора по меткам не будет.
  3. Номера нет нигде -> документы, которым принадлежат все метки файла,
     показываются на выбор (даже если такой документ один).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from core.database import (
    DEFAULT_DB_PATH,
    DocumentInfo,
    DocumentMatch,
    DocumentNotFoundError,
    document_exists,
    find_documents_by_placeholders,
    get_document_info,
    is_valid_document_id,
    normalize_document_id,
)
from core.models import PLACEHOLDER_KEY_RE, placeholder_keys_in
from core.office import read_document_id


FILENAME_ID_RE = re.compile(r"_SAFE_([0-9A-Fa-f]{8})(?![0-9A-Fa-f])")

# Откуда взят номер документа. Передаётся в интерфейс, чтобы пользователь видел основание.
SOURCE_EXPLICIT = "explicit"
SOURCE_PROPERTIES = "properties"
SOURCE_FILENAME = "filename"
SOURCE_PROPERTIES_AND_FILENAME = "properties+filename"

__all__ = [
    "AmbiguousDocumentError",
    "DocumentIdConflictError",
    "DocumentIdNotFoundError",
    "DocumentResolution",
    "InvalidDocumentIdError",
    "PLACEHOLDER_KEY_RE",
    "placeholder_keys_in",
    "resolve_document_id",
]


@dataclass(frozen=True)
class DocumentResolution:
    id: str
    source: str


class InvalidDocumentIdError(DocumentNotFoundError):
    """The number given by the user is not an 8-character hex number."""

    def __init__(self, value: str) -> None:
        self.value = value
        super().__init__("invalid document number")


class DocumentIdNotFoundError(DocumentNotFoundError):
    """A number was given or found in the file, but no such document exists."""

    def __init__(self, document_id: str, source: str) -> None:
        self.document_id = document_id
        self.source = source
        super().__init__(f"document {document_id} not found")


class DocumentIdConflictError(DocumentNotFoundError):
    """The number in the file name differs from the number inside the DOCX."""

    def __init__(self, filename_id: str, properties_id: str, db_path: Path) -> None:
        self.filename_id = filename_id
        self.properties_id = properties_id
        self.filename_document = get_document_info(filename_id, db_path)
        self.properties_document = get_document_info(properties_id, db_path)
        super().__init__(
            f"document number mismatch: file name {filename_id}, document properties {properties_id}"
        )


class AmbiguousDocumentError(DocumentNotFoundError):
    """No number in the file: the user has to choose the document.

    Raised even when only one document fits the placeholders, because a fit
    by placeholders is not proof that it is the right document.
    """

    def __init__(self, candidates: List[DocumentMatch]) -> None:
        self.candidates = candidates
        super().__init__("the user has to choose the document")


def resolve_document_id(
    file_path: Path,
    text: str,
    db_path: Path = DEFAULT_DB_PATH,
    explicit_id: Optional[str] = None,
    original_filename: Optional[str] = None,
) -> DocumentResolution:
    """Decide which stored document an uploaded file belongs to."""
    # 1. Явный номер — только он, без подбора других документов.
    if explicit_id is not None and explicit_id.strip():
        if not is_valid_document_id(explicit_id):
            raise InvalidDocumentIdError(explicit_id)
        document_id = normalize_document_id(explicit_id)
        if not document_exists(document_id, db_path):
            raise DocumentIdNotFoundError(document_id, SOURCE_EXPLICIT)
        return DocumentResolution(document_id, SOURCE_EXPLICIT)

    # 2. Номер в самом файле.
    properties_id = _normalized(read_document_id(file_path))
    filename_id = _id_from_filename(original_filename or file_path.name)

    if properties_id and filename_id and properties_id != filename_id:
        raise DocumentIdConflictError(filename_id, properties_id, db_path)

    if properties_id or filename_id:
        document_id = properties_id or filename_id
        if properties_id and filename_id:
            source = SOURCE_PROPERTIES_AND_FILENAME
        elif properties_id:
            source = SOURCE_PROPERTIES
        else:
            source = SOURCE_FILENAME
        if not document_exists(document_id, db_path):
            raise DocumentIdNotFoundError(document_id, source)
        return DocumentResolution(document_id, source)

    # 3. Номера нет — только предложить выбор.
    keys = placeholder_keys_in(text)
    if not keys:
        raise DocumentNotFoundError("file has no document number and no placeholders")
    matches = find_documents_by_placeholders(keys, db_path)
    if not matches:
        raise DocumentNotFoundError("no document owns the placeholders of the file")
    raise AmbiguousDocumentError(matches)


def describe_conflict(error: DocumentIdConflictError) -> List[tuple[str, str, Optional[DocumentInfo]]]:
    """Both numbers of a mismatch: (source, number, document or None)."""
    return [
        (SOURCE_FILENAME, error.filename_id, error.filename_document),
        (SOURCE_PROPERTIES, error.properties_id, error.properties_document),
    ]


def _normalized(document_id: Optional[str]) -> Optional[str]:
    if document_id is None or not document_id.strip():
        return None
    return normalize_document_id(document_id)


def _id_from_filename(filename: str) -> Optional[str]:
    match = FILENAME_ID_RE.search(filename)
    return match.group(1).upper() if match else None