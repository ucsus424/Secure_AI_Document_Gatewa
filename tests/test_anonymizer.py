import unittest

from core.anonymizer import anonymize_text
from core.models import Entity


class AnonymizerTests(unittest.TestCase):
    def test_reuses_placeholder_for_same_value(self):
        text = "Петров А.В. согласовал работы. Петров А.В. будет на объекте."
        entities = [Entity(type="PERSON", value="Петров А.В.", confidence=0.99)]

        result = anonymize_text(text, entities, source_name="note.txt")

        self.assertEqual(result.text.count("⟦PERSON_001⟧"), 2)
        self.assertNotIn("Петров А.В.", result.text)
        self.assertEqual(result.mapping.entities, {"PERSON_001": "Петров А.В."})

    def test_replaces_longer_values_before_shorter_overlaps(self):
        text = "Адрес интерфейса 10.74.16.1/24, gateway 10.74.16.1."
        entities = [
            Entity(type="IP", value="10.74.16.1", confidence=0.99),
            Entity(type="IP", value="10.74.16.1/24", confidence=0.99),
        ]

        result = anonymize_text(text, entities, source_name="ip.txt")

        self.assertIn("⟦IP_001⟧", result.text)
        self.assertIn("⟦IP_002⟧", result.text)
        self.assertNotIn("10.74.16.1", result.text)
        self.assertEqual(result.mapping.entities["IP_001"], "10.74.16.1/24")
        self.assertEqual(result.mapping.entities["IP_002"], "10.74.16.1")

    def test_ignores_entities_that_are_not_present(self):
        text = "Нет чувствительных данных."
        entities = [Entity(type="PERSON", value="Петров А.В.", confidence=0.99)]

        result = anonymize_text(text, entities, source_name="clean.txt")

        self.assertEqual(result.text, text)
        self.assertEqual(result.mapping.entities, {})

    def test_replaces_punctuation_variant_with_same_placeholder(self):
        text = (
            "на объекте ИП Некрылова Г.В,, расположенного по адресу.\n"
            "Проектное решение согласовать с ИП Некрылова Г.В."
        )
        entities = [Entity(type="PERSON", value="ИП Некрылова Г.В.", confidence=0.99)]

        result = anonymize_text(text, entities, source_name="variant.txt")

        self.assertEqual(result.text.count("⟦PERSON_001⟧"), 2)
        self.assertNotIn("ИП Некрылова", result.text)
        self.assertEqual(result.mapping.entities, {"PERSON_001": "ИП Некрылова Г.В."})
        self.assertEqual(result.mapping.aliases, {"PERSON_001": ["ИП Некрылова Г.В"]})

    def test_does_not_record_surface_alias_that_was_not_applied(self):
        text = "на монтаж оборудования ПАО «МТС» на объекте."
        entities = [Entity(type="COMPANY", value="ПАО «МТС»", confidence=0.99)]

        result = anonymize_text(text, entities, source_name="company.txt")

        self.assertEqual(result.text, "на монтаж оборудования ⟦COMPANY_001⟧ на объекте.")
        self.assertEqual(result.mapping.entities, {"COMPANY_001": "ПАО «МТС»"})
        self.assertEqual(result.mapping.aliases, {})


if __name__ == "__main__":
    unittest.main()
