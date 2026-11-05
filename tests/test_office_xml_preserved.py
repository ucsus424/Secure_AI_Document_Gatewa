"""Проверка: SAFE- и FINAL-DOCX сохраняют разметку Word.

Раньше ElementTree переименовывал префиксы (w: -> ns0:) и выбрасывал
объявления пространств имён, на которые ссылается mc:Ignorable, — Word
сообщал «обнаружено содержимое, которое не удалось прочитать».
"""

import re
import tempfile
import unittest
import zipfile
from pathlib import Path

from core.models import Mapping
from core.office import anonymize_office_file, read_document_id, restore_docx_file, write_document_id

WORD_DOCUMENT = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<w:document'
    ' xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
    ' xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    ' xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"'
    ' xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml"'
    ' mc:Ignorable="w14 w15">'
    "<w:body>"
    '<w:p w14:paraId="1A2B3C4D"><w:r><w:t>Инженер Петров А.В. на объекте.</w:t></w:r></w:p>'
    "<!-- комментарий Word -->"
    "</w:body>"
    "</w:document>"
)
CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    "</Relationships>"
)


def _declared_prefixes(xml: str):
    return set(re.findall(r'xmlns:([A-Za-z0-9]+)="', xml))


def _ignorable_prefixes(xml: str):
    match = re.search(r'mc:Ignorable="([^"]*)"', xml)
    return set(match.group(1).split()) if match else set()


class OfficeXmlPreservedTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.source = self.tmp / "input.docx"
        with zipfile.ZipFile(self.source, "w") as archive:
            archive.writestr("[Content_Types].xml", CONTENT_TYPES)
            archive.writestr("_rels/.rels", ROOT_RELS)
            archive.writestr("word/document.xml", WORD_DOCUMENT)
        self.mapping = Mapping.create(source_name="input.docx", entities={"PERSON_001": "Петров А.В."})

    def tearDown(self):
        self._tmp.cleanup()

    def read_document_xml(self, path: Path) -> str:
        with zipfile.ZipFile(path) as archive:
            return archive.read("word/document.xml").decode("utf-8")

    def assert_word_markup_intact(self, xml: str):
        self.assertTrue(xml.startswith('<?xml version=\'1.0\' encoding=\'UTF-8\' standalone=\'yes\'?>')
                        or xml.startswith('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'))
        self.assertIn("<w:document", xml)
        self.assertIn("<w:t>", xml)
        self.assertNotIn("ns0:", xml)
        # Каждый префикс из mc:Ignorable должен быть объявлен, иначе Word считает файл повреждённым
        self.assertTrue(_ignorable_prefixes(xml) <= _declared_prefixes(xml))

    def test_safe_docx_keeps_word_prefixes_and_declarations(self):
        safe = self.tmp / "input_SAFE.docx"
        anonymize_office_file(self.source, safe, self.mapping)

        xml = self.read_document_xml(safe)
        self.assert_word_markup_intact(xml)
        self.assertIn("⟦PERSON_001⟧", xml)
        self.assertNotIn("Петров А.В.", xml)

    def test_final_docx_keeps_word_prefixes_and_declarations(self):
        safe = self.tmp / "input_SAFE.docx"
        final = self.tmp / "input_FINAL.docx"
        anonymize_office_file(self.source, safe, self.mapping)
        restore_docx_file(safe, final, self.mapping)

        xml = self.read_document_xml(final)
        self.assert_word_markup_intact(xml)
        self.assertIn("Петров А.В.", xml)

    def test_document_id_property_is_readable(self):
        safe = self.tmp / "input_SAFE.docx"
        anonymize_office_file(self.source, safe, self.mapping)
        write_document_id(safe, "AB12CD34")

        self.assertEqual(read_document_id(safe), "AB12CD34")
        self.assert_word_markup_intact(self.read_document_xml(safe))


if __name__ == "__main__":
    unittest.main()