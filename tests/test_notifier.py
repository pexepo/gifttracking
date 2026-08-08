import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from gift_tracking.models import Attribute, GiftEvent
from gift_tracking.notifier import BotLongPollTimeout, BotNotifier, FilterMenuState, format_notification


class NotifierTests(unittest.TestCase):
    def test_notification_contains_requested_fields(self) -> None:
        event = GiftEvent(
            slug="PlushPepe-2826",
            gift_id=1,
            title="Plush Pepe",
            number=2826,
            link="https://t.me/nft/PlushPepe-2826",
            owner_name="Owner <Test>",
            owner_username="owner_test",
            owner_address=None,
            attributes=(
                Attribute("model", "Spectrum", "3%"),
                Attribute("backdrop", "Coral Red", "1.5%"),
                Attribute("symbol", "Star", "0.4%"),
            ),
            availability_issued=2826,
            availability_total=2861,
            detected_at=datetime(2026, 8, 8, 0, 30, tzinfo=UTC),
        )
        message = format_notification(event, "Europe/Minsk")
        self.assertIn("Plush Pepe #2826", message)
        self.assertIn("@owner_test", message)
        self.assertIn("Spectrum", message)
        self.assertIn("2026-08-08 03:30:00", message)
        self.assertIn("https://t.me/nft/PlushPepe-2826", message)
        self.assertNotIn('<a href="https://t.me/owner_test">', message)
        self.assertNotIn("Owner <Test>", message)

    def test_filter_menu_state_shape(self) -> None:
        state = FilterMenuState(
            owner_username_required=True,
            backdrop_filter_enabled=False,
            backdrop_filters=("Coral Red", "Ocean Blue"),
            blocked_owner_username_substrings=("bank", "storage"),
        )
        self.assertTrue(state.owner_username_required)
        self.assertFalse(state.backdrop_filter_enabled)
        self.assertEqual(state.backdrop_filters, ("Coral Red", "Ocean Blue"))
        self.assertEqual(state.blocked_owner_username_substrings, ("bank", "storage"))

    def test_filter_menu_mentions_blacklist(self) -> None:
        state = FilterMenuState(
            owner_username_required=False,
            backdrop_filter_enabled=True,
            backdrop_filters=("coral red",),
            blocked_owner_username_substrings=("bank", "storage"),
        )
        from gift_tracking.notifier import BotNotifier

        text = BotNotifier._filter_menu_text(state)
        self.assertIn("bank, storage", text)
        self.assertIn("coral red", text)

    def test_get_updates_timeout_is_treated_as_long_poll_timeout(self) -> None:
        notifier = BotNotifier("token", "1", "Europe/Minsk")
        with patch.object(notifier, "_call", side_effect=BotLongPollTimeout("Long poll timeout")):
            with self.assertRaises(BotLongPollTimeout):
                import asyncio

                asyncio.run(notifier.get_updates())


if __name__ == "__main__":
    unittest.main()
