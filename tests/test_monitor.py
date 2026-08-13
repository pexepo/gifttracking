import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from telethon import errors

from gift_tracking.config import Config
from gift_tracking.models import (
    Attribute,
    Collection,
    GiftEvent,
    MarketPrice,
    MenuSettings,
    PriceInfo,
)
from gift_tracking.monitor import GiftMonitor


def event(number: int, issued: int) -> GiftEvent:
    return GiftEvent(
        slug=f"PlushPepe-{number}",
        gift_id=1,
        title="Plush Pepe",
        number=number,
        link=f"https://t.me/nft/PlushPepe-{number}",
        owner_name="Owner",
        owner_username="owner",
        owner_address=None,
        attributes=(Attribute("model", "Pumpkin", "3%"),),
        availability_issued=issued,
        availability_total=10,
        detected_at=datetime.now(UTC),
    )


class FakeApi:
    def __init__(self, events: dict[str, GiftEvent]) -> None:
        self.events = events
        self.pm_sent: list[tuple[int, str]] = []

    async def unique_gift(self, slug: str):
        return self.events[slug], object()

    async def send_message_to_user(self, user_id: int, text: str) -> None:
        self.pm_sent.append((user_id, text))


class MissingGiftApi(FakeApi):
    def __init__(self, events: dict[str, GiftEvent], missing_slugs: set[str]) -> None:
        super().__init__(events)
        self.missing_slugs = missing_slugs

    async def unique_gift(self, slug: str):
        if slug in self.missing_slugs:
            raise errors.RPCError(None, "missing")
        return await super().unique_gift(slug)


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[GiftEvent] = []
        self.status_messages: list[tuple[str, str | None]] = []
        self.filter_menus: list[tuple[object, str | None]] = []
        self.updated_filter_menus: list[tuple[str, int, object]] = []
        self.callback_answers: list[tuple[str, str]] = []
        self.menus: list[tuple[str | None]] = []
        self.settings_menus: list[tuple[object, str | None]] = []
        self.updated_settings_menus: list[tuple[str, int, object]] = []
        self.account_menus: list[tuple[bool, str | None, str | None]] = []

    async def send_event(self, gift_event: GiftEvent) -> None:
        self.sent.append(gift_event)

    async def send_text(self, text: str, *, keyboard=None, chat_id=None):
        self.status_messages.append((text, chat_id))
        return {"message_id": len(self.status_messages)}

    async def send_filter_menu(self, state, *, chat_id=None):
        self.filter_menus.append((state, chat_id))
        return {"message_id": len(self.filter_menus)}

    async def update_filter_menu(self, chat_id: str, message_id: int, state) -> None:
        self.updated_filter_menus.append((chat_id, message_id, state))

    async def answer_callback_query(self, callback_query_id: str, text: str) -> None:
        self.callback_answers.append((callback_query_id, text))

    async def send_menu(self, *, chat_id=None):
        self.menus.append((chat_id,))
        return {"message_id": len(self.menus)}

    async def send_settings_menu(self, settings, *, chat_id=None):
        self.settings_menus.append((settings, chat_id))
        return {"message_id": len(self.settings_menus)}

    async def update_settings_menu(self, chat_id: str, message_id: int, settings) -> None:
        self.updated_settings_menus.append((chat_id, message_id, settings))

    async def send_account_menu(self, authorized, phone, *, chat_id=None):
        self.account_menus.append((authorized, phone, chat_id))
        return {"message_id": len(self.account_menus)}


class FakePricing:
    def __init__(self, price: PriceInfo | None) -> None:
        self.price = price
        self.calls: list[tuple[str, str, str]] = []

    async def fetch_price(self, collection: str, model: str, backdrop: str):
        self.calls.append((collection, model, backdrop))
        return self.price

    async def check_key(self) -> tuple[bool, str]:
        return self.price is not None, "ok"


def close_monitor(monitor: GiftMonitor) -> None:
    monitor.storage.close()
    client = getattr(monitor.api, "client", None)
    if client is not None:
        client.session.close()


def make_config(directory: str, **overrides) -> Config:
    params = dict(
        api_id=1,
        api_hash="hash",
        bot_token="token",
        notify_chat_id="1",
        session=str(Path(directory) / "session"),
        database_path=Path(directory) / "state.sqlite3",
        poll_interval_seconds=5,
        collections_per_cycle=10,
        catalog_refresh_seconds=3600,
        backfill_count=0,
        max_new_gifts_per_check=100,
        timezone="Europe/Minsk",
        collection_prefixes=(),
        backdrop_filters=(),
        require_owner_username=False,
        blocked_owner_username_substrings=("bank", "storage"),
        log_level="INFO",
    )
    params.update(overrides)
    return Config(**params)


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_detects_new_number_and_notifies_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            monitor = GiftMonitor(config)
            stored = monitor.storage.upsert_collection(
                Collection(1, "Plush Pepe", "PlushPepe")
            )
            monitor.storage.set_last_issued(1, 1)
            stored = replace(stored, last_issued=1)
            monitor.api = FakeApi(
                {
                    "PlushPepe-1": event(1, 2),
                    "PlushPepe-2": event(2, 2),
                }
            )
            notifier = FakeNotifier()
            monitor.notifier = notifier

            await monitor.check_collection(stored)
            await monitor.send_pending_notifications()
            await monitor.check_collection(monitor.storage.get_collection(1))
            await monitor.send_pending_notifications()

            self.assertTrue(
                any("PlushPepe-2" in text or "Plush Pepe #2" in text for text, _ in notifier.status_messages)
            )
            self.assertEqual(monitor.storage.get_collection(1).last_issued, 2)
            close_monitor(monitor)

    async def test_skips_notification_without_public_username(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory, require_owner_username=True)
            monitor = GiftMonitor(config)
            monitor.storage.record_gift(
                GiftEvent(
                    slug="PlushPepe-2",
                    gift_id=1,
                    title="Plush Pepe",
                    number=2,
                    link="https://t.me/nft/PlushPepe-2",
                    owner_name="Owner",
                    owner_username=None,
                    owner_address="addr",
                    attributes=(Attribute("backdrop", "Coral Red", "1.5%"),),
                    availability_issued=2,
                    availability_total=10,
                    detected_at=datetime.now(UTC),
                )
            )
            notifier = FakeNotifier()
            monitor.notifier = notifier

            await monitor.send_pending_notifications()

            self.assertEqual(notifier.sent, [])
            self.assertEqual(monitor.storage.pending_notifications(), [])
            close_monitor(monitor)

    async def test_skips_notification_when_backdrop_does_not_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory, backdrop_filters=("coral red",))
            monitor = GiftMonitor(config)
            matching = GiftEvent(
                slug="PlushPepe-2",
                gift_id=1,
                title="Plush Pepe",
                number=2,
                link="https://t.me/nft/PlushPepe-2",
                owner_name="Owner",
                owner_username="owner",
                owner_address=None,
                attributes=(Attribute("backdrop", "Coral Red", "1.5%"),),
                availability_issued=2,
                availability_total=10,
                detected_at=datetime.now(UTC),
            )
            skipped = GiftEvent(
                slug="PlushPepe-3",
                gift_id=1,
                title="Plush Pepe",
                number=3,
                link="https://t.me/nft/PlushPepe-3",
                owner_name="Owner",
                owner_username="owner",
                owner_address=None,
                attributes=(Attribute("backdrop", "Ocean Blue", "2%"),),
                availability_issued=3,
                availability_total=10,
                detected_at=datetime.now(UTC),
            )
            monitor.storage.record_gift(skipped)
            monitor.storage.record_gift(matching)
            notifier = FakeNotifier()
            monitor.notifier = notifier

            await monitor.send_pending_notifications()

            self.assertTrue(
                any("PlushPepe-2" in text or "Plush Pepe #2" in text for text, _ in notifier.status_messages)
            )
            self.assertEqual(monitor.storage.pending_notifications(), [])
            close_monitor(monitor)

    async def test_skips_notification_for_marketplace_like_username(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            monitor = GiftMonitor(config)
            monitor.storage.record_gift(
                GiftEvent(
                    slug="PlushPepe-4",
                    gift_id=1,
                    title="Plush Pepe",
                    number=4,
                    link="https://t.me/nft/PlushPepe-4",
                    owner_name="Marketplace",
                    owner_username="bestgiftbank",
                    owner_address=None,
                    attributes=(Attribute("backdrop", "Coral Red", "1.5%"),),
                    availability_issued=4,
                    availability_total=10,
                    detected_at=datetime.now(UTC),
                )
            )
            notifier = FakeNotifier()
            monitor.notifier = notifier

            await monitor.send_pending_notifications()

            self.assertEqual(notifier.sent, [])
            self.assertEqual(monitor.storage.pending_notifications(), [])
            close_monitor(monitor)

    async def test_callback_toggles_runtime_filters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory, backdrop_filters=("coral red",))
            monitor = GiftMonitor(config)
            try:
                notifier = FakeNotifier()
                monitor.notifier = notifier

                await monitor._handle_message(
                    {"chat": {"id": 1}, "text": "/filters"}
                )
                await monitor._handle_callback_query(
                    {
                        "id": "cb1",
                        "data": "toggle_owner_username",
                        "message": {"chat": {"id": 1}, "message_id": 77},
                    }
                )
                await monitor._handle_callback_query(
                    {
                        "id": "cb2",
                        "data": "toggle_backdrop_filter",
                        "message": {"chat": {"id": 1}, "message_id": 77},
                    }
                )

                self.assertTrue(monitor._runtime_filters.require_owner_username)
                self.assertFalse(monitor._runtime_filters.backdrop_filter_enabled)
                self.assertEqual(len(notifier.filter_menus), 1)
                self.assertEqual(len(notifier.updated_filter_menus), 2)
                self.assertEqual(
                    notifier.callback_answers,
                    [("cb1", "Фильтры обновлены"), ("cb2", "Фильтры обновлены")],
                )
                self.assertEqual(
                    monitor.storage.load_runtime_filters(), monitor._runtime_filters
                )
            finally:
                close_monitor(monitor)

    async def test_toggle_owner_username_preserves_model_and_price_filters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            monitor = GiftMonitor(config)
            try:
                notifier = FakeNotifier()
                monitor.notifier = notifier
                monitor._runtime_filters = replace(
                    monitor._runtime_filters,
                    model_filter_enabled=True,
                    model_filters=("Albino",),
                    min_price=1.0,
                    max_price=50.0,
                )

                await monitor._handle_callback_query(
                    {
                        "id": "cb-x",
                        "data": "toggle_owner_username",
                        "message": {"chat": {"id": 1}, "message_id": 77},
                    }
                )

                self.assertTrue(monitor._runtime_filters.require_owner_username)
                self.assertEqual(monitor._runtime_filters.model_filters, ("Albino",))
                self.assertEqual(monitor._runtime_filters.max_price, 50.0)
                self.assertEqual(
                    monitor.storage.load_runtime_filters(), monitor._runtime_filters
                )
            finally:
                close_monitor(monitor)

    async def test_skips_crafted_gap_after_retry_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            monitor = GiftMonitor(config)
            stored = monitor.storage.upsert_collection(
                Collection(1, "Plush Pepe", "PlushPepe")
            )
            monitor.storage.set_last_issued(1, 1)
            stored = replace(stored, last_issued=1)
            monitor.api = MissingGiftApi(
                {
                    "PlushPepe-1": event(1, 3),
                    "PlushPepe-3": event(3, 3),
                },
                {"PlushPepe-2"},
            )
            notifier = FakeNotifier()
            monitor.notifier = notifier

            await monitor.check_collection(stored)
            await monitor.check_collection(monitor.storage.get_collection(1))
            await monitor.check_collection(monitor.storage.get_collection(1))
            await monitor.check_collection(monitor.storage.get_collection(1))
            await monitor.send_pending_notifications()

            self.assertTrue(
                any("PlushPepe-3" in text or "Plush Pepe #3" in text for text, _ in notifier.status_messages)
            )
            self.assertEqual(monitor.storage.get_collection(1).last_issued, 3)
            close_monitor(monitor)

    async def test_edits_filters_from_telegram_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory, backdrop_filters=("coral red",))
            monitor = GiftMonitor(config)
            try:
                notifier = FakeNotifier()
                monitor.notifier = notifier

                await monitor._handle_callback_query(
                    {
                        "id": "cb3",
                        "data": "edit_backdrop_filters",
                        "message": {"chat": {"id": 1}, "message_id": 88},
                    }
                )
                await monitor._handle_message(
                    {"chat": {"id": 1}, "text": "Coral Red, Ocean Blue"}
                )
                await monitor._handle_callback_query(
                    {
                        "id": "cb4",
                        "data": "edit_blocked_usernames",
                        "message": {"chat": {"id": 1}, "message_id": 89},
                    }
                )
                await monitor._handle_message(
                    {"chat": {"id": 1}, "text": "bank, storage, market"}
                )

                self.assertEqual(
                    monitor._runtime_filters.backdrop_filters,
                    ("coral red", "ocean blue"),
                )
                self.assertTrue(monitor._runtime_filters.backdrop_filter_enabled)
                self.assertEqual(
                    monitor._runtime_filters.blocked_owner_username_substrings,
                    ("bank", "storage", "market"),
                )
                self.assertEqual(
                    monitor.storage.load_runtime_filters(), monitor._runtime_filters
                )
            finally:
                close_monitor(monitor)

    async def test_sends_owner_message_with_price_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            monitor = GiftMonitor(config)
            price = PriceInfo(
                collection="Plush Pepe",
                model="Pumpkin",
                backdrop="",
                markets=(MarketPrice("Tonnel", price_ton=9.5),),
                fetched_at=datetime.now(UTC),
            )
            api = FakeApi({"PlushPepe-2": event(2, 2)})
            monitor.api = api
            monitor._pricing = FakePricing(price)
            monitor._menu_settings = MenuSettings(
                owner_message_template="Куплю {title} #{number} за {price}",
                satellite_api_key="k",
                satellite_api_url="https://api.example.com",
                auto_price_enabled=True,
                send_to_owner_enabled=True,
            )
            gift = replace(event(2, 2), owner_user_id=42)
            monitor.storage.record_gift(gift)
            notifier = FakeNotifier()
            monitor.notifier = notifier

            await monitor.send_pending_notifications()

            self.assertEqual(len(api.pm_sent), 1)
            user_id, text = api.pm_sent[0]
            self.assertEqual(user_id, 42)
            self.assertIn("Plush Pepe #2", text)
            self.assertIn("9.5 TON", text)
            self.assertIn("✅", [t for t, _ in notifier.status_messages][-1] or "")
            self.assertEqual(monitor.storage.pending_notifications(), [])
            close_monitor(monitor)

    async def test_skips_owner_pm_when_user_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            monitor = GiftMonitor(config)
            monitor._menu_settings = MenuSettings(
                owner_message_template="Куплю {title} за {price}",
                satellite_api_key="k",
                satellite_api_url="https://api.example.com",
                auto_price_enabled=False,
                send_to_owner_enabled=True,
            )
            gift = event(2, 2)  # owner_user_id is None
            monitor.storage.record_gift(gift)
            notifier = FakeNotifier()
            monitor.notifier = notifier

            await monitor.send_pending_notifications()

            self.assertEqual(len(notifier.status_messages), 1)
            self.assertNotIn("✅", notifier.status_messages[0][0].splitlines()[-1])
            self.assertEqual(monitor.storage.pending_notifications(), [])
            close_monitor(monitor)

    async def test_model_filter_skips_non_matching(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            monitor = GiftMonitor(config)
            monitor._runtime_filters = replace(
                monitor._runtime_filters,
                model_filter_enabled=True,
                model_filters=("Albino",),
            )
            gift = event(2, 2)
            monitor.storage.record_gift(gift)
            notifier = FakeNotifier()
            monitor.notifier = notifier

            await monitor.send_pending_notifications()

            self.assertEqual(notifier.status_messages, [])
            close_monitor(monitor)

    async def test_price_range_skips_outside(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            monitor = GiftMonitor(config)
            price = PriceInfo(
                collection="Plush Pepe",
                model="Pumpkin",
                backdrop="",
                markets=(MarketPrice("Tonnel", price_ton=50.0),),
                fetched_at=datetime.now(UTC),
            )
            monitor._pricing = FakePricing(price)
            monitor._runtime_filters = replace(
                monitor._runtime_filters, min_price=None, max_price=20.0
            )
            monitor._menu_settings = MenuSettings(
                owner_message_template="Куплю {title}",
                satellite_api_key="k",
                satellite_api_url="https://api.example.com",
                auto_price_enabled=True,
                send_to_owner_enabled=False,
            )
            monitor.storage.record_gift(event(2, 2))
            notifier = FakeNotifier()
            monitor.notifier = notifier

            await monitor.send_pending_notifications()

            self.assertEqual(notifier.status_messages, [])
            close_monitor(monitor)

    async def test_menu_navigation_shows_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            monitor = GiftMonitor(config)
            try:
                notifier = FakeNotifier()
                monitor.notifier = notifier

                await monitor._handle_message({"chat": {"id": 1}, "text": "/menu"})
                await monitor._handle_callback_query(
                    {
                        "id": "cb-menu",
                        "data": "menu_settings",
                        "message": {"chat": {"id": 1}, "message_id": 55},
                    }
                )

                self.assertEqual(len(notifier.menus), 1)
                self.assertEqual(len(notifier.updated_settings_menus), 1)
                self.assertEqual(notifier.updated_settings_menus[0][1], 55)
            finally:
                close_monitor(monitor)

    async def test_edits_owner_template_from_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            monitor = GiftMonitor(config)
            try:
                notifier = FakeNotifier()
                monitor.notifier = notifier

                await monitor._handle_callback_query(
                    {
                        "id": "cb-tpl",
                        "data": "edit_owner_template",
                        "message": {"chat": {"id": 1}, "message_id": 66},
                    }
                )
                await monitor._handle_message(
                    {"chat": {"id": 1}, "text": "Куплю {title} #{number}!"}
                )

                self.assertEqual(
                    monitor._menu_settings.owner_message_template,
                    "Куплю {title} #{number}!",
                )
                self.assertEqual(
                    monitor.storage.load_menu_settings(), monitor._menu_settings
                )
            finally:
                close_monitor(monitor)

    async def test_toggles_send_to_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            monitor = GiftMonitor(config)
            try:
                notifier = FakeNotifier()
                monitor.notifier = notifier

                await monitor._handle_callback_query(
                    {
                        "id": "cb-send",
                        "data": "toggle_send_owner",
                        "message": {"chat": {"id": 1}, "message_id": 77},
                    }
                )

                self.assertFalse(monitor._menu_settings.send_to_owner_enabled)
                self.assertEqual(
                    monitor.storage.load_menu_settings(), monitor._menu_settings
                )
            finally:
                close_monitor(monitor)


if __name__ == "__main__":
    unittest.main()
