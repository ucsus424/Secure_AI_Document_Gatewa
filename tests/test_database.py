import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.database import (
    DatabaseError,
    DocumentNotFoundError,
    init_db,
    load_mapping,
    new_document_id,
    save_document,
)
from core.models import Mapping


def _mapping(entities, source_name="report.docx"):
    return Mapping.create(source_name=source_name, entities=entities)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "data" / "gateway.db"
        init_db(self.db_path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_init_creates_file_and_tables_and_can_run_twice(self):
        init_db(self.db_path)

        self.assertTrue(self.db_path.exists())
        with sqlite3.connect(self.db_path) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        self.assertIn("documents", tables)
        self.assertIn("entities", tables)

    def test_new_document_id_is_short_uppercase_hex(self):
        document_id = new_document_id()

        self.assertRegex(document_id, r"^[0-9A-F]{8}$")
        self.assertNotEqual(document_id, new_document_id())

    def test_save_and_load_round_trip(self):
        entities = {"PERSON_001": "Петров А.В.", "IP_001": "10.74.16.1/24"}
        save_document("AB12CD34", _mapping(entities), self.db_path)

        mapping = load_mapping("AB12CD34", self.db_path)

        self.assertEqual(mapping.document_id, "AB12CD34")
        self.assertEqual(mapping.source_name, "report.docx")
        self.assertEqual(mapping.entities, entities)

    def test_load_accepts_lowercase_and_spaces(self):
        save_document("AB12CD34", _mapping({"PERSON_001": "Петров А.В."}), self.db_path)

        mapping = load_mapping("  ab12cd34 ", self.db_path)

        self.assertEqual(mapping.entities, {"PERSON_001": "Петров А.В."})

    def test_documents_do_not_mix_values(self):
        save_document("AAAA0001", _mapping({"PERSON_001": "Петров А.В."}), self.db_path)
        save_document("BBBB0002", _mapping({"PERSON_001": "Иванов С.П."}), self.db_path)

        self.assertEqual(load_mapping("AAAA0001", self.db_path).entities["PERSON_001"], "Петров А.В.")
        self.assertEqual(load_mapping("BBBB0002", self.db_path).entities["PERSON_001"], "Иванов С.П.")

    def test_unknown_document_raises_not_found(self):
        with self.assertRaises(DocumentNotFoundError):
            load_mapping("FFFFFFFF", self.db_path)

    def test_duplicate_id_fails_without_partial_write(self):
        save_document("AB12CD34", _mapping({"PERSON_001": "Петров А.В."}), self.db_path)

        with self.assertRaises(DatabaseError):
            save_document("AB12CD34", _mapping({"PERSON_001": "Другой"}), self.db_path)

        mapping = load_mapping("AB12CD34", self.db_path)
        self.assertEqual(mapping.entities, {"PERSON_001": "Петров А.В."})

    def test_error_message_does_not_contain_values(self):
        save_document("AB12CD34", _mapping({"SECRET_001": "Qwerty-2026!"}), self.db_path)

        with self.assertRaises(DatabaseError) as raised:
            save_document("AB12CD34", _mapping({"SECRET_001": "Qwerty-2026!"}), self.db_path)

        self.assertNotIn("Qwerty-2026!", str(raised.exception))


if __name__ == "__main__":
    unittest.main()