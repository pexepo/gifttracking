import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from gift_tracking.models import Collection, GiftEvent, RuntimeFilters
from gift_tracking.storage import Storage


class StorageTests(unittest.TestCase):
    def test_deduplicates_gifts_and_keeps_pending_notification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "state.sqlite3")
            collection = storage.upsert_collection(
                Collection(1, "Plush Pepe", "PlushPepe")
            )
            self.assertIsNone(collection.last_issued)
            event = GiftEvent(
                slug="PlushPepe-1",
                gift_id=1,
                title="Plush Pepe",
                number=1,
                link="https://t.me/nft/PlushPepe-1",
                owner_name=None,
                owner_username=None,
                owner_address=None,
                attributes=(),
                availability_issued=1,
                availability_total=10,
                detected_at=datetime.now(UTC),
            )
            self.assertTrue(storage.record_gift(event))
            self.assertFalse(storage.record_gift(event))
            self.assertEqual(storage.pending_notifications(), [event])
            storage.mark_notified(event.slug)
            self.assertEqual(storage.pending_notifications(), [])
            storage.close()

    def test_persists_runtime_filters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "state.sqlite3")
            filters = RuntimeFilters(
                require_owner_username=True,
                backdrop_filter_enabled=True,
                backdrop_filters=("coral red", "ocean blue"),
                blocked_owner_username_substrings=("bank", "storage"),
            )
            storage.save_runtime_filters(filters)
            self.assertEqual(storage.load_runtime_filters(), filters)
            storage.close()

    def test_tracks_missing_gift_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "state.sqlite3")
            self.assertEqual(
                storage.increment_missing_gift_attempt(1, "PlushPepe-2", 2), 1
            )
            self.assertEqual(
                storage.increment_missing_gift_attempt(1, "PlushPepe-2", 2), 2
            )
            storage.clear_missing_gift("PlushPepe-2")
            self.assertEqual(
                storage.increment_missing_gift_attempt(1, "PlushPepe-2", 2), 1
            )
            storage.close()


if __name__ == "__main__":
    unittest.main()
