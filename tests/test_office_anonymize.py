import importlib.util
import io
import re
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path

import app
from core.database import load_mapping
from core.detector import DetectorError
from core.models import Entity
from core.office import read_document_id


DOCX_AVAILABLE = importlib.util.find_spec("docx") is not None


class OfficeFakeDetector:
    def detect_text(self, text):
        return [
            Entity(type="PERSON", value="Петров А.В.", confidence=0.99),
            Entity(type="COMPANY", value='ООО "Альфа Телеком"', confidence=0.99),
            Entity(type="OBJECT", value="ВРН-Север-17", confidence=0.99),
            Entity(type="IP", value="10.74.16.1/24", confidence=0.99),
        ]


class FailingDetector:
    def detect_text(self, text):
        raise DetectorError("invalid model response")


class PunctuationVariantDetector:
    def detect_text(self, text):
        return [Entity(type="PERSON", value="ИП Некрылова Г.В.", confidence=0.99)]


class PersonOnlyDetector:
    def detect_text(self, text):
        return [Entity(type="PERSON", value="Петров А.В.", confidence=0.99)]


def run_anonymize(input_path, detector):
    """Run `anonymize` with a temporary database next to the input file."""
    db_path = input_path.parent / "test.db"
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = app.run(
        ["anonymize", "--db", str(db_path), str(input_path)],
        detector_factory=lambda args: detector,
        stdout=stdout,
        stderr=stderr,
    )
    match = re.search(r"Document ID: ([0-9A-F]{8})", stdout.getvalue())
    document_id = match.group(1) if match else None
    return code, stdout.getvalue(), stderr.getvalue(), document_id, db_path


def safe_path_for(input_path, document_id):
    return input_path.with_name(f"{input_path.stem}_SAFE_{document_id}{input_path.suffix}")


def count_documents(db_path):
    if not db_path.exists():
        return 0
    with closing(sqlite3.connect(db_path)) as connection:
        return connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]


@unittest.skipUnless(DOCX_AVAILABLE, "python-docx is unavailable")
class DocxAnonymizeTests(unittest.TestCase):
    def test_anonymize_writes_safe_docx_and_saves_values_to_database(self):
        from docx import Document

        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "report.docx"
            document = Document()
            document.add_paragraph("Инженер Петров А.В. прибыл на узел ВРН-Север-17.")
            table = document.add_table(rows=1, cols=2)
            table.cell(0, 0).text = 'Заказчик ООО "Альфа Телеком"'
            table.cell(0, 1).text = "IP 10.74.16.1/24"
            document.save(input_path)

            code, stdout, stderr, document_id, db_path = run_anonymize(input_path, OfficeFakeDetector())

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            safe_path = safe_path_for(input_path, document_id)
            self.assertTrue(safe_path.exists())
            self.assertEqual(list(Path(tmp).glob("*.mapping.json")), [])

            safe_document = Document(safe_path)
            safe_text = "\n".join(_docx_text(safe_document))
            self.assertNotIn("Петров А.В.", safe_text)
            self.assertNotIn('ООО "Альфа Телеком"', safe_text)
            self.assertNotIn("ВРН-Север-17", safe_text)
            self.assertNotIn("10.74.16.1/24", safe_text)
            self.assertIn("⟦PERSON_001⟧", safe_text)
            self.assertIn("⟦COMPANY_001⟧", safe_text)
            self.assertIn("⟦OBJECT_001⟧", safe_text)
            self.assertIn("⟦IP_001⟧", safe_text)

            self.assertEqual(read_document_id(safe_path), document_id)
            mapping = load_mapping(document_id, db_path)
            self.assertEqual(mapping.source_name, "report.docx")
            self.assertEqual(mapping.entities["PERSON_001"], "Петров А.В.")

    def test_docx_anonymize_replaces_punctuation_variant(self):
        from docx import Document

        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "report.docx"
            document = Document()
            document.add_paragraph("на объекте ИП Некрылова Г.В,, расположенного.")
            document.add_paragraph("Проектное решение согласовать с ИП Некрылова Г.В.")
            document.save(input_path)

            code, stdout, stderr, document_id, db_path = run_anonymize(input_path, PunctuationVariantDetector())

            self.assertEqual(code, 0)
            safe_document = Document(safe_path_for(input_path, document_id))
            safe_text = "\n".join(_docx_text(safe_document))
            self.assertEqual(safe_text.count("⟦PERSON_001⟧"), 2)
            self.assertNotIn("ИП Некрылова", safe_text)
            self.assertEqual(stderr, "")

    def test_docx_masks_multiline_labeled_address_missed_by_model(self):
        from docx import Document

        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "report.docx"
            document = Document()
            document.add_paragraph("Ответственный: Петров А.В.")
            document.add_paragraph("Юридический адрес: 123456, Московская область,")
            document.add_paragraph("г. Примерск, ул. Тестовая, д. 1,")
            document.add_paragraph("помещение 2, офис 3")
            document.add_paragraph("e-mail: user@example.com")
            document.save(input_path)

            code, stdout, stderr, document_id, db_path = run_anonymize(input_path, PersonOnlyDetector())

            self.assertEqual(code, 0)
            safe_document = Document(safe_path_for(input_path, document_id))
            safe_text = "\n".join(_docx_text(safe_document))
            self.assertNotIn("123456", safe_text)
            self.assertNotIn("Примерск", safe_text)
            self.assertNotIn("Тестовая", safe_text)
            self.assertNotIn("user@example.com", safe_text)
            self.assertIn("⟦ADDRESS_", safe_text)
            self.assertIn("⟦EMAIL_", safe_text)
            self.assertEqual(stderr, "")

    def test_docx_detector_failure_writes_nothing(self):
        from docx import Document

        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "report.docx"
            document = Document()
            document.add_paragraph("Пароль Qwerty-2026!")
            document.save(input_path)

            code, stdout, stderr, document_id, db_path = run_anonymize(input_path, FailingDetector())

            self.assertNotEqual(code, 0)
            self.assertEqual(stdout, "")
            self.assertEqual(list(Path(tmp).glob("report_SAFE_*")), [])
            self.assertEqual(count_documents(db_path), 0)
            self.assertNotIn("Qwerty-2026!", stderr)


class UnsupportedOfficeFormatsTests(unittest.TestCase):
    def test_xlsx_and_pptx_are_not_supported(self):
        from core.office import is_supported_office_file

        self.assertTrue(is_supported_office_file(Path("report.docx")))
        self.assertFalse(is_supported_office_file(Path("plan.xlsx")))
        self.assertFalse(is_supported_office_file(Path("deck.pptx")))


def _docx_text(document):
    for paragraph in document.paragraphs:
        yield paragraph.text
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                yield cell.text


if __name__ == "__main__":
    unittest.main()