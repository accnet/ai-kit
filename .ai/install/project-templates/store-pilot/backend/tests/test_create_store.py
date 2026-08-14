import unittest

from backend.application.create_store import create_store


class CreateStoreTests(unittest.TestCase):
    def test_creates_active_store(self) -> None:
        self.assertEqual(create_store("store-1", "North Market").status, "active")

    def test_rejects_blank_name(self) -> None:
        with self.assertRaises(ValueError):
            create_store("store-1", " ")


if __name__ == "__main__":
    unittest.main()
