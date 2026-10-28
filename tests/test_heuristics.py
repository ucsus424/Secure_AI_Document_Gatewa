import unittest

from core.detector import DetectorError
from core.heuristics import find_obvious_entities, require_no_obvious_entities


class HeuristicDetectionTests(unittest.TestCase):
    def test_finds_structured_multiline_address_and_email(self):
        text = "\n".join(
            [
                "Юридический адрес: 123456, Московская область,",
                "г. Примерск, ул. Тестовая, д. 1,",
                "помещение 2, офис 3",
                "e-mail: user@example.com",
            ]
        )

        entities = find_obvious_entities(text)

        self.assertEqual(
            [entity.type for entity in entities].count("ADDRESS"),
            3,
        )
        self.assertIn("EMAIL", [entity.type for entity in entities])

    def test_final_validation_rejects_obvious_residual(self):
        with self.assertRaises(DetectorError):
            require_no_obvious_entities("Контакт: user@example.com")

    def test_final_validation_accepts_placeholders(self):
        require_no_obvious_entities(
            "Юридический адрес: ⟦ADDRESS_001⟧\n"
            "e-mail: ⟦EMAIL_001⟧"
        )


if __name__ == "__main__":
    unittest.main()
