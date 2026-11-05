import tempfile
import unittest
from pathlib import Path

from core.database import (
    DocumentNotFoundError,
    find_documents_by_placeholders,
    init_db,
    save_document,
)
from core.document_lookup import (
    SOURCE_EXPLICIT,
    SOURCE_FILENAME,
    SOURCE_PROPERTIES,
    SOURCE_PROPERTIES_AND_FILENAME,
    AmbiguousDocumentError,
    DocumentIdConflictError,
    DocumentIdNotFoundError,
    InvalidDocumentIdError,
    placeholder_keys_in,
    resolve_document_id,
)
from core.models import Mapping


try:
    from docx import Document
except ImportError:  # pragma: no cover - exercised only without the optional test dependency
    Document = None


# Сценарий из задачи: одинаковая метка ⟦PERSON_001⟧ в двух документах означает разных людей.
IVANOV_DOC = "AAAA0001"   # ⟦PERSON_001⟧ = Иванов
PETROV_DOC = "BBBB0002"   # ⟦PERSON_001⟧ = Петров, ⟦IP_001⟧ = IP-адрес


class DocumentLookupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "test.db"
        init_db(self.db_path)

    def tearDown(self):
        self._tmp.cleanup()

    def save(self, document_id, entities):
        save_document(
            document_id,
            Mapping.create(source_name=f"{document_id}.txt", entities=entities),
            self.db_path,
        )

    def save_ivanov_and_petrov(self):
        self.save(IVANOV_DOC, {"PERSON_001": "Иванов"})
        self.save(PETROV_DOC, {"PERSON_001": "Петров", "IP_001": "10.0.0.1"})

    def docx_with_number(self, name, document_id, text="⟦PERSON_001⟧"):
        from core.office import write_document_id

        path = self.tmp / name
        document = Document()
        document.add_paragraph(text)
        document.save(path)
        write_document_id(path, document_id)
        return path

    # --- извлечение меток и поиск по ним ---

    def test_placeholder_keys_are_extracted_from_text(self):
        keys = placeholder_keys_in("⟦PERSON_001⟧ и ⟦IP_002⟧, снова ⟦PERSON_001⟧, [[NOT_1]]")

        self.assertEqual(keys, {"PERSON_001", "IP_002"})

    def test_search_keeps_only_documents_owning_all_file_placeholders(self):
        self.save_ivanov_and_petrov()

        matches = find_documents_by_placeholders({"PERSON_001", "IP_001"}, self.db_path)

        self.assertEqual([match.id for match in matches], [PETROV_DOC])
        self.assertEqual(matches[0].missing_placeholders, 0)

    def test_search_lists_fewer_missing_first_as_a_hint(self):
        self.save_ivanov_and_petrov()

        matches = find_documents_by_placeholders({"PERSON_001"}, self.db_path)

        self.assertEqual([match.id for match in matches], [IVANOV_DOC, PETROV_DOC])
        self.assertEqual([match.missing_placeholders for match in matches], [0, 1])

    # --- 1. явный номер ---

    def test_explicit_number_is_used_even_if_file_name_has_another(self):
        self.save_ivanov_and_petrov()

        resolution = resolve_document_id(
            self.tmp / f"report_SAFE_{IVANOV_DOC}.txt",
            "⟦PERSON_001⟧",
            self.db_path,
            explicit_id=PETROV_DOC.lower(),
        )

        self.assertEqual(resolution.id, PETROV_DOC)
        self.assertEqual(resolution.source, SOURCE_EXPLICIT)

    def test_unknown_explicit_number_fails_without_choosing_another(self):
        self.save_ivanov_and_petrov()

        with self.assertRaises(DocumentIdNotFoundError) as raised:
            resolve_document_id(
                self.tmp / f"report_SAFE_{PETROV_DOC}.txt",
                "⟦PERSON_001⟧",
                self.db_path,
                explicit_id="FFFFFFFF",
            )

        self.assertEqual(raised.exception.document_id, "FFFFFFFF")
        self.assertEqual(raised.exception.source, SOURCE_EXPLICIT)

    def test_invalid_explicit_number_is_rejected(self):
        self.save_ivanov_and_petrov()

        with self.assertRaises(InvalidDocumentIdError):
            resolve_document_id(self.tmp / "answer.txt", "⟦PERSON_001⟧", self.db_path, explicit_id="12345")

    # --- 2. номер в файле ---

    def test_number_in_file_name_is_used(self):
        self.save_ivanov_and_petrov()

        resolution = resolve_document_id(
            self.tmp / f"report_SAFE_{PETROV_DOC.lower()}_edited.txt", "⟦PERSON_001⟧", self.db_path
        )

        self.assertEqual(resolution.id, PETROV_DOC)
        self.assertEqual(resolution.source, SOURCE_FILENAME)

    def test_unknown_number_in_file_name_fails_without_falling_back_to_placeholders(self):
        self.save(IVANOV_DOC, {"PERSON_001": "Иванов"})

        with self.assertRaises(DocumentIdNotFoundError) as raised:
            resolve_document_id(self.tmp / "report_SAFE_FFFFFFFF.txt", "⟦PERSON_001⟧", self.db_path)

        self.assertEqual(raised.exception.document_id, "FFFFFFFF")
        self.assertEqual(raised.exception.source, SOURCE_FILENAME)

    @unittest.skipUnless(Document is not None, "python-docx is unavailable")
    def test_number_in_docx_properties_survives_renaming(self):
        self.save_ivanov_and_petrov()
        path = self.docx_with_number("answer.docx", PETROV_DOC)

        resolution = resolve_document_id(path, "⟦PERSON_001⟧", self.db_path)

        self.assertEqual(resolution.id, PETROV_DOC)
        self.assertEqual(resolution.source, SOURCE_PROPERTIES)

    @unittest.skipUnless(Document is not None, "python-docx is unavailable")
    def test_same_number_in_properties_and_file_name_is_used(self):
        self.save_ivanov_and_petrov()
        path = self.docx_with_number(f"report_SAFE_{PETROV_DOC}.docx", PETROV_DOC)

        resolution = resolve_document_id(path, "⟦PERSON_001⟧", self.db_path)

        self.assertEqual(resolution.id, PETROV_DOC)
        self.assertEqual(resolution.source, SOURCE_PROPERTIES_AND_FILENAME)

    @unittest.skipUnless(Document is not None, "python-docx is unavailable")
    def test_different_numbers_in_properties_and_file_name_are_a_conflict(self):
        self.save_ivanov_and_petrov()
        path = self.docx_with_number(f"report_SAFE_{IVANOV_DOC}.docx", PETROV_DOC)

        with self.assertRaises(DocumentIdConflictError) as raised:
            resolve_document_id(path, "⟦PERSON_001⟧", self.db_path)

        error = raised.exception
        self.assertEqual(error.filename_id, IVANOV_DOC)
        self.assertEqual(error.properties_id, PETROV_DOC)
        self.assertEqual(error.filename_document.id, IVANOV_DOC)
        self.assertEqual(error.properties_document.id, PETROV_DOC)

    @unittest.skipUnless(Document is not None, "python-docx is unavailable")
    def test_explicit_number_resolves_conflict(self):
        self.save_ivanov_and_petrov()
        path = self.docx_with_number(f"report_SAFE_{IVANOV_DOC}.docx", PETROV_DOC)

        resolution = resolve_document_id(path, "⟦PERSON_001⟧", self.db_path, explicit_id=PETROV_DOC)

        self.assertEqual(resolution.id, PETROV_DOC)

    # --- 3. номера нет: только выбор пользователя ---

    def test_task_scenario_edited_file_without_number_offers_choice_not_ivanov(self):
        # Из документа Петрова удалили абзац с ⟦IP_001⟧ и сохранили под новым именем.
        self.save_ivanov_and_petrov()

        with self.assertRaises(AmbiguousDocumentError) as raised:
            resolve_document_id(self.tmp / "renamed.txt", "Инженер ⟦PERSON_001⟧ на объекте.", self.db_path)

        self.assertEqual({candidate.id for candidate in raised.exception.candidates}, {IVANOV_DOC, PETROV_DOC})

    def test_single_candidate_still_requires_choice(self):
        self.save(PETROV_DOC, {"PERSON_001": "Петров", "IP_001": "10.0.0.1"})

        with self.assertRaises(AmbiguousDocumentError) as raised:
            resolve_document_id(self.tmp / "answer.txt", "⟦PERSON_001⟧ и ⟦IP_001⟧", self.db_path)

        self.assertEqual([candidate.id for candidate in raised.exception.candidates], [PETROV_DOC])

    def test_chosen_candidate_is_used(self):
        self.save_ivanov_and_petrov()

        resolution = resolve_document_id(
            self.tmp / "answer.txt", "⟦PERSON_001⟧", self.db_path, explicit_id=PETROV_DOC
        )

        self.assertEqual(resolution.id, PETROV_DOC)

    def test_file_without_number_and_placeholders_is_not_found(self):
        self.save(IVANOV_DOC, {"PERSON_001": "Иванов"})

        with self.assertRaises(DocumentNotFoundError):
            resolve_document_id(self.tmp / "answer.txt", "обычный текст", self.db_path)

    def test_placeholders_owned_by_no_document_are_not_found(self):
        self.save(IVANOV_DOC, {"PERSON_001": "Иванов"})

        with self.assertRaises(DocumentNotFoundError) as raised:
            resolve_document_id(self.tmp / "answer.txt", "⟦PERSON_001⟧ ⟦IP_001⟧", self.db_path)

        self.assertNotIsInstance(raised.exception, AmbiguousDocumentError)


if __name__ == "__main__":
    unittest.main()