import unittest

from gift_tracking.telegram_api import slug_prefix_from_title


class TelegramApiTests(unittest.TestCase):
    def test_slug_prefix(self) -> None:
        self.assertEqual(slug_prefix_from_title("Plush Pepe"), "PlushPepe")
        self.assertEqual(slug_prefix_from_title("Jack-in-the-Box!"), "JackintheBox")


if __name__ == "__main__":
    unittest.main()
