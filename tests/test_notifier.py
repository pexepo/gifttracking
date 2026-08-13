import unittest
from datetime import UTC, datetime
from io import BytesIO
from unittest.mock import patch
import urllib.error

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

    def test_filter_menu_shows_blacklist(self) -> None:
        state = FilterMenuState(
            owner_username_required=True,
            backdrop_filter_enabled=False,
            backdrop_filters=(),
            blocked_owner_username_substrings=(),
            blacklisted_collections=("Plush Pepe",),
        )
        from gift_tracking.notifier import BotNotifier

        text = BotNotifier._filter_menu_text(state)
        self.assertIn("Коллекции: <b>блеклист: <code>Plush Pepe</code></b>", text)
        self.assertNotIn("Модели", text)

    def test_filter_menu_keyboard_has_blacklist_and_no_models(self) -> None:
        state = FilterMenuState(
            owner_username_required=True,
            backdrop_filter_enabled=False,
            backdrop_filters=(),
            blocked_owner_username_substrings=(),
            blacklisted_collections=("Plush Pepe",),
        )
        from gift_tracking.notifier import BotNotifier

        keyboard = BotNotifier._filter_menu_keyboard(state)
        data = [
            button["callback_data"]
            for row in keyboard["inline_keyboard"]
            for button in row
        ]
        self.assertIn("edit_blacklisted_collections", data)
        self.assertNotIn("edit_model_filters", data)
        self.assertNotIn("toggle_model_filter", data)
        self.assertNotIn("code_noop", data)

    def test_get_updates_timeout_is_treated_as_long_poll_timeout(self) -> None:
        notifier = BotNotifier("token", "1", "Europe/Minsk")
        with patch.object(notifier, "_call", side_effect=BotLongPollTimeout("Long poll timeout")):
            with self.assertRaises(BotLongPollTimeout):
                import asyncio

                asyncio.run(notifier.get_updates())

    def test_http_error_includes_bot_api_description(self) -> None:
        notifier = BotNotifier("token", "1", "Europe/Minsk")
        error = urllib.error.HTTPError(
            url="https://api.telegram.org",
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=BytesIO(
                b'{"ok":false,"error_code":400,"description":"Bad Request: chat not found"}'
            ),
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(
                Exception, "Bad Request: chat not found"
            ):
                notifier._call("sendMessage", {"chat_id": "1", "text": "x"})

    def test_edit_message_not_modified_is_ignored(self) -> None:
        notifier = BotNotifier("token", "1", "Europe/Minsk")
        error = urllib.error.HTTPError(
            url="https://api.telegram.org",
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=BytesIO(
                b'{"ok":false,"error_code":400,"description":"Bad Request: message is not modified: specified new message content and reply markup are exactly the same as a current content and reply markup of the message"}'
            ),
        )
        with patch("urllib.request.urlopen", side_effect=error):
            self.assertEqual(
                notifier._call("editMessageText", {"chat_id": "1", "message_id": 1}),
                {},
            )


from gift_tracking.models import GiftEvent, MarketPrice, PriceInfo
from gift_tracking.notifier import format_price, render_owner_message


class TemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.event = GiftEvent(
            slug="PlushPepe-2826",
            gift_id=1,
            title="Plush Pepe",
            number=2826,
            link="https://t.me/nft/PlushPepe-2826",
            owner_name="Owner",
            owner_username=None,
            owner_address=None,
            attributes=(
                Attribute("model", "Spectrum", "3%"),
                Attribute("backdrop", "Coral Red", "1.5%"),
                Attribute("symbol", "Star", "0.4%"),
            ),
            availability_issued=2826,
            availability_total=2861,
            detected_at=datetime.now(UTC),
            owner_user_id=42,
        )

    def test_renders_all_placeholders(self) -> None:
        price = PriceInfo(
            collection="Plush Pepe",
            model="Spectrum",
            backdrop="Coral Red",
            markets=(MarketPrice("Tonnel", price_ton=12.5),),
            fetched_at=datetime.now(UTC),
        )
        text = render_owner_message(
            "Куплю {title} #{number} ({model}/{backdrop}) за {price}: {link}",
            self.event,
            price,
        )
        self.assertIn("Куплю Plush Pepe #2826", text)
        self.assertIn("(Spectrum/Coral Red)", text)
        self.assertIn("12.5 TON", text)
        self.assertIn("https://t.me/nft/PlushPepe-2826", text)

    def test_renders_without_price(self) -> None:
        text = render_owner_message("Цена: {price}", self.event, None)
        self.assertEqual(text, "Цена: по запросу")

    def test_unknown_placeholders_are_left_alone(self) -> None:
        text = render_owner_message("Привет {wat}", self.event, None)
        self.assertEqual(text, "Привет {wat}")


class FormatPriceTests(unittest.TestCase):
    def test_join_markets(self) -> None:
        price = PriceInfo(
            collection="X",
            model="Y",
            backdrop="Z",
            markets=(
                MarketPrice("Tonnel", price_ton=12.5),
                MarketPrice("MRKT", price_stars=950.0),
            ),
            fetched_at=datetime.now(UTC),
        )
        text = format_price(price)
        self.assertIn("Tonnel: 12.5 TON", text)
        self.assertIn("MRKT: 950 ⭐", text)

    def test_none(self) -> None:
        self.assertEqual(format_price(None), "по запросу")


from gift_tracking.models import MenuSettings
from gift_tracking.notifier import BotNotifier


class KeyboardTests(unittest.TestCase):
    def test_code_keyboard_has_digits_controls(self) -> None:
        keyboard = BotNotifier.code_keyboard("12")
        rows = keyboard["inline_keyboard"]
        labels = [button["text"] for row in rows for button in row]
        self.assertIn("1", labels)
        self.assertIn("0", labels)
        self.assertIn("⌫", labels)
        self.assertIn("Отправить", labels)
        self.assertIn("12", labels)

    def test_login_keyboard_requests_contact(self) -> None:
        keyboard = BotNotifier.login_keyboard()
        button = keyboard["keyboard"][0][0]
        self.assertTrue(button["request_contact"])

    def test_main_menu_has_three_sections(self) -> None:
        keyboard = BotNotifier.main_menu_keyboard()
        labels = [button["text"] for row in keyboard["inline_keyboard"] for button in row]
        self.assertIn("⚙️ Фильтры", labels)
        self.assertIn("🛠 Настройки", labels)
        self.assertIn("👤 Аккаунт", labels)

    def test_settings_menu_keyboard(self) -> None:
        settings = MenuSettings()
        keyboard = BotNotifier.settings_menu_keyboard(settings)
        labels = [button["text"] for row in keyboard["inline_keyboard"] for button in row]
        self.assertIn("✏️ Шаблон сообщения", labels)
        self.assertIn("🔑 API-ключ", labels)
        self.assertIn("⚠️ Проверить ключ", labels)
        self.assertIn("✅ Авто-цена", labels)
        self.assertIn("✅ Отправка владельцу", labels)

    def test_account_menu_keyboard(self) -> None:
        keyboard = BotNotifier.account_menu_keyboard(True)
        labels = [button["text"] for row in keyboard["inline_keyboard"] for button in row]
        self.assertIn("🚪 Выйти", labels)


class MenuTextTests(unittest.TestCase):
    def test_settings_menu_text_shows_template(self) -> None:
        settings = MenuSettings(owner_message_template="Куплю {title}")
        text = BotNotifier._settings_menu_text(settings)
        self.assertIn("Куплю {title}", text)


if __name__ == "__main__":
    unittest.main()
