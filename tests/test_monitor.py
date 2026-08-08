import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from telethon import errors

from gift_tracking.config import Config
from gift_tracking.models import Attribute, Collection, GiftEvent
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

    async def unique_gift(self, slug: str):
        return self.events[slug], object()


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

            self.assertEqual([item.slug for item in notifier.sent], ["PlushPepe-2"])
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

            self.assertEqual([item.slug for item in notifier.sent], ["PlushPepe-2"])
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

            self.assertEqual([item.slug for item in notifier.sent], ["PlushPepe-3"])
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


if __name__ == "__main__":
    unittest.main()
