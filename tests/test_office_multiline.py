"""Значения, разбитые на несколько абзацев или записанные с другими пробелами.

Случай из практики: название университета на титульном листе занимало три
абзаца. Программа записывала его в базу, но в SAFE-DOCX оно оставалось, и
итоговая проверка этого не замечала.
"""

import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path

from core.anonymizer import anonymize_text
from core.models import Entity, Mapping
from core.residuals import ResidualValuesError, find_residual_keys, normalize_spaces


DOCX_AVAILABLE = importlib.util.find_spec("docx") is not None

UNIVERSITY = (
    "Российский государственный университет нефти и газа "
    "(национальный исследовательский университет) имени И. М. Губкина"
)
TITLE_LINES = [
    "Министерство образования и науки Российской Федерации",
    "Федеральное государственное автономное образовательное учреждение высшего образования",
    "«Российский государственный университет нефти и газа",
    "(национальный исследовательский университет)",
    "имени И. М. Губкина»",
    "Домашнее задание №1",
]
TITLE_ENTITIES = [
    Entity(type="COMPANY", value=UNIVERSITY),
    Entity(type="COMPANY", value="Федеральное государственное автономное образовательное учреждение высшего образования"),
    Entity(type="COMPANY", value="Министерство образования и науки Российской Федерации"),
]


class ResidualCheckTests(unittest.TestCase):
    def test_spaces_line_breaks_and_nbsp_are_treated_alike(self):
        self.assertEqual(normalize_spaces("газа\n(нац. ун-т)\t имени­И."), "газа (нац. ун-т) имениИ.")

    def test_value_split_into_lines_is_found(self):
        mapping = Mapping.create(source_name="t", entities={"COMPANY_001": UNIVERSITY})
        text = "«Российский государственный университет нефти и газа\n(национальный исследовательский университет)\nимени И. М. Губкина»"

        self.assertEqual(find_residual_keys(text, mapping), ["COMPANY_001"])

    def test_value_with_non_breaking_spaces_is_found(self):
        mapping = Mapping.create(source_name="t", entities={"PERSON_001": "Петров А. В."})

        self.assertEqual(find_residual_keys("Исполнитель: Петров А. В.", mapping), ["PERSON_001"])

    def test_placeholders_are_not_residuals(self):
        mapping = Mapping.create(source_name="t", entities={"PERSON_001": "Петров А. В."})

        self.assertEqual(find_residual_keys("Исполнитель: ⟦PERSON_001⟧", mapping), [])


class AnonymizerSpacingTests(unittest.TestCase):
    def test_non_flexible_type_with_non_breaking_space_is_replaced(self):
        text = "Адрес: ул. Ленина, д. 5"
        result = anonymize_text(text, [Entity(type="ADDRESS", value="ул. Ленина, д. 5")], "t.txt")

        self.assertEqual(result.text, "Адрес: ⟦ADDRESS_001⟧")

    def test_value_split_into_lines_is_replaced_in_text(self):
        text = "\n".join(TITLE_LINES)
        result = anonymize_text(text, TITLE_ENTITIES, "t.txt")

        self.assertNotIn("Губкина", result.text)
        self.assertEqual(find_residual_keys(result.text, result.mapping), [])


@unittest.skipUnless(DOCX_AVAILABLE, "python-docx is unavailable")
class DocxMultilineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def make_docx(self, lines, name="title.docx"):
        from docx import Document

        path = self.tmp / name
        document = Document()
        for line in lines:
            document.add_paragraph(line)
        document.save(path)
        return path

    def anonymize(self, path, entities):
        from core.office import anonymize_office_file, extract_office_text

        result = anonymize_text(extract_office_text(path), entities, path.name)
        safe = self.tmp / "safe.docx"
        anonymize_office_file(path, safe, result.mapping)
        return safe, result.mapping

    def paragraphs(self, path):
        from docx import Document

        return [paragraph.text for paragraph in Document(path).paragraphs]

    def test_title_page_from_the_report_is_fully_anonymized(self):
        safe, mapping = self.anonymize(self.make_docx(TITLE_LINES), TITLE_ENTITIES)

        paragraphs = self.paragraphs(safe)
        joined = "\n".join(paragraphs)
        self.assertNotIn("Губкина", joined)
        self.assertNotIn("нефти и газа", joined)
        self.assertNotIn("национальный исследовательский", joined)
        self.assertEqual(paragraphs[0], "⟦COMPANY_003⟧")
        self.assertEqual(paragraphs[1], "⟦COMPANY_002⟧")
        # Метка — в первой строке значения, закрывающая кавычка остаётся рядом,
        # следующие две строки пустые, но сами абзацы сохранены.
        self.assertEqual(paragraphs[2], "«⟦COMPANY_001⟧»")
        self.assertEqual(paragraphs[3:6], ["", "", "Домашнее задание №1"])
        self.assertEqual(mapping.entities["COMPANY_001"], UNIVERSITY)

    def test_multiline_value_round_trip_restores_full_name(self):
        from docx import Document
        from core.office import restore_docx_file

        safe, mapping = self.anonymize(self.make_docx(TITLE_LINES), TITLE_ENTITIES)
        final = self.tmp / "final.docx"
        restore_docx_file(safe, final, mapping)

        paragraphs = [paragraph.text for paragraph in Document(final).paragraphs]
        self.assertEqual(paragraphs[2], f"«{UNIVERSITY}»")
        self.assertEqual(paragraphs[0], "Министерство образования и науки Российской Федерации")

    def test_empty_paragraph_between_lines_is_handled(self):
        lines = ["«Российский государственный университет нефти и газа", "", "(национальный исследовательский университет)", "имени И. М. Губкина»"]
        safe, _ = self.anonymize(self.make_docx(lines), TITLE_ENTITIES[:1])

        self.assertNotIn("Губкина", "\n".join(self.paragraphs(safe)))

    def test_paragraph_formatting_and_count_are_kept(self):
        from docx import Document

        path = self.make_docx(TITLE_LINES)
        document = Document(path)
        document.paragraphs[4].runs[0].bold = True
        document.save(path)

        safe, _ = self.anonymize(path, TITLE_ENTITIES)

        self.assertEqual(len(Document(safe).paragraphs), len(TITLE_LINES))

    def test_value_in_different_table_cells_is_not_merged_and_is_reported(self):
        from docx import Document
        from core.office import anonymize_office_file

        path = self.tmp / "table.docx"
        document = Document()
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "Петров"
        table.cell(0, 1).text = "Алексей"
        document.save(path)
        # Модель вернула значение, «склеенное» из двух разных ячеек таблицы.
        mapping = Mapping.create(
            source_name="table.docx",
            entities={"PERSON_001": "Петров Алексей"},
            aliases={"PERSON_001": ["Петров\nАлексей"]},
        )

        with self.assertRaises(ResidualValuesError) as raised:
            anonymize_office_file(path, self.tmp / "safe.docx", mapping)

        self.assertEqual(raised.exception.keys, ["PERSON_001"])
        self.assertFalse((self.tmp / "safe.docx").exists())
        self.assertEqual([p for p in self.tmp.iterdir() if p.name.startswith(".")], [])

    def test_residual_value_stops_anonymization(self):
        from core.office import anonymize_office_file

        path = self.make_docx(["Исполнитель: Петров А. В."])
        # Значение в базе записано с обычными пробелами, вариантов нет — раньше проверка это пропускала.
        mapping = Mapping.create(source_name="t", entities={"PERSON_001": "Петров А. В."})

        with self.assertRaises(ResidualValuesError):
            anonymize_office_file(path, self.tmp / "safe.docx", mapping)

    def test_safe_docx_is_still_valid_word_xml(self):
        safe, _ = self.anonymize(self.make_docx(TITLE_LINES), TITLE_ENTITIES)

        with zipfile.ZipFile(safe) as archive:
            xml = archive.read("word/document.xml").decode("utf-8")
        self.assertIn("<w:document", xml)
        self.assertNotIn("ns0:", xml)


if __name__ == "__main__":
    unittest.main()