import os
import unittest
from pathlib import Path
from unittest.mock import patch

from gift_tracking.config import Config, ConfigError

BASE_ENV = {
    "TG_API_ID": "123",
    "TG_API_HASH": "hash",
    "BOT_TOKEN": "token",
    "NOTIFY_CHAT_ID": "456",
}


class ConfigTests(unittest.TestCase):
    def test_defaults_and_collection_deduplication(self) -> None:
        env = {**BASE_ENV, "GIFT_COLLECTIONS": "PlushPepe, AstralShard,PlushPepe"}
        with patch.dict(os.environ, env, clear=True), patch(
            "gift_tracking.config.load_dotenv"
        ):
            config = Config.from_env()
        self.assertEqual(config.collection_prefixes, ("PlushPepe", "AstralShard"))
        self.assertEqual(config.backfill_count, 0)
        self.assertEqual(config.backdrop_filters, ())
        self.assertFalse(config.require_owner_username)
        self.assertEqual(config.blocked_owner_username_substrings, ("bank", "storage"))
        self.assertIsNone(config.bot_api_ca_file)
        self.assertFalse(config.bot_api_insecure_ssl)

    def test_optional_bot_api_tls_settings(self) -> None:
        env = {
            **BASE_ENV,
            "BOT_API_CA_FILE": "/tmp/corporate-ca.pem",
            "BOT_API_INSECURE_SSL": "true",
            "BACKDROP_FILTERS": "Coral Red, Ocean Blue,Coral Red",
            "REQUIRE_OWNER_USERNAME": "yes",
            "BLOCKED_OWNER_USERNAME_SUBSTRINGS": "bank,storage,market",
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "gift_tracking.config.load_dotenv"
        ):
            config = Config.from_env()
        self.assertEqual(config.bot_api_ca_file, Path("/tmp/corporate-ca.pem"))
        self.assertTrue(config.bot_api_insecure_ssl)
        self.assertEqual(config.backdrop_filters, ("coral red", "ocean blue"))
        self.assertTrue(config.require_owner_username)
        self.assertEqual(
            config.blocked_owner_username_substrings,
            ("bank", "storage", "market"),
        )

    def test_missing_required_value(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("gift_tracking.config.load_dotenv"),
            self.assertRaises(ConfigError),
        ):
            Config.from_env()


if __name__ == "__main__":
    unittest.main()
