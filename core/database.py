from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List
from uuid import uuid4

from core.models import Mapping


DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "gateway.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id          TEXT PRIMARY KEY,
    source_name TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS entities (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id     TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    placeholder_key TEXT NOT NULL,
    value           TEXT NOT NULL,
    UNIQUE (document_id, placeholder_key)
);
"""


class DatabaseError(RuntimeError):
    """Raised when the local database cannot be used safely."""


class DocumentNotFoundError(DatabaseError):
    """Raised when no document exists for the requested id."""


@dataclass(frozen=True)
class DocumentMatch:
    """A document whose placeholders appear in an uploaded file."""

    id: str
    source_name: str
    created_at: str
    total_placeholders: int
    matched_placeholders: int

    @property
    def is_complete(self) -> bool:
        return self.total_placeholders == self.matched_placeholders


def new_document_id() -> str:
    """Short document number that is shown to the user, e.g. 39F3C1EE."""
    return uuid4().hex[:8].upper()


def normalize_document_id(document_id: str) -> str:
    return document_id.strip().upper()


def connect(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    try:
        connection = connect(db_path)
        try:
            connection.executescript(SCHEMA)
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        raise DatabaseError("database could not be initialized") from exc


def save_document(
    document_id: str,
    mapping: Mapping,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    """Save a document and all replaced values in one transaction."""
    document_id = normalize_document_id(document_id)

    try:
        connection = connect(db_path)
        try:
            with connection:
                connection.execute(
                    "INSERT INTO documents (id, source_name) VALUES (?, ?)",
                    (document_id, mapping.source_name),
                )
                connection.executemany(
                    "INSERT INTO entities (document_id, placeholder_key, value) VALUES (?, ?, ?)",
                    [(document_id, key, value) for key, value in mapping.entities.items()],
                )
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        raise DatabaseError("document could not be saved") from exc


def load_mapping(document_id: str, db_path: Path = DEFAULT_DB_PATH) -> Mapping:
    """Return the replaced values of a document as a Mapping object."""
    document_id = normalize_document_id(document_id)

    try:
        connection = connect(db_path)
        try:
            document = connection.execute(
                "SELECT source_name, created_at FROM documents WHERE id = ?",
                (document_id,),
            ).fetchone()
            rows = connection.execute(
                "SELECT placeholder_key, value FROM entities "
                "WHERE document_id = ? ORDER BY placeholder_key",
                (document_id,),
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        raise DatabaseError("document could not be read") from exc

    if document is None:
        raise DocumentNotFoundError("document not found")

    source_name, created_at = document
    return Mapping(
        document_id=document_id,
        source_name=source_name,
        entities={key: value for key, value in rows},
        created_at_utc=created_at,
    )


def document_exists(document_id: str, db_path: Path = DEFAULT_DB_PATH) -> bool:
    document_id = normalize_document_id(document_id)
    try:
        connection = connect(db_path)
        try:
            row = connection.execute(
                "SELECT 1 FROM documents WHERE id = ?",
                (document_id,),
            ).fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        raise DatabaseError("document could not be read") from exc
    return row is not None


def find_documents_by_placeholders(
    placeholder_keys: Iterable[str],
    db_path: Path = DEFAULT_DB_PATH,
) -> List[DocumentMatch]:
    """Find documents that own every placeholder found in a file.

    A document matches when all given placeholders belong to it. Documents
    whose placeholders are all present in the file come first.
    """
    keys = sorted(set(placeholder_keys))
    if not keys:
        return []

    marks = ", ".join("?" for _ in keys)
    query = f"""
        SELECT d.id, d.source_name, d.created_at,
               COUNT(*) AS total,
               SUM(CASE WHEN e.placeholder_key IN ({marks}) THEN 1 ELSE 0 END) AS matched
        FROM documents AS d
        JOIN entities AS e ON e.document_id = d.id
        GROUP BY d.id
        HAVING matched = ?
        ORDER BY (COUNT(*) = matched) DESC, d.created_at DESC
    """
    try:
        connection = connect(db_path)
        try:
            rows = connection.execute(query, (*keys, len(keys))).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        raise DatabaseError("documents could not be searched") from exc

    return [
        DocumentMatch(
            id=row[0],
            source_name=row[1],
            created_at=str(row[2]),
            total_placeholders=row[3],
            matched_placeholders=row[4],
        )
        for row in rows
    ]