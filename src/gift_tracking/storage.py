from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import Collection, GiftEvent, MenuSettings, RuntimeFilters


class Storage:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        self.connection.executescript(
            """
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS collections (
                gift_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                slug_prefix TEXT NOT NULL UNIQUE,
                last_issued INTEGER,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS gifts (
                slug TEXT PRIMARY KEY,
                gift_id INTEGER NOT NULL,
                number INTEGER NOT NULL,
                detected_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                notified INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS missing_gifts (
                slug TEXT PRIMARY KEY,
                gift_id INTEGER NOT NULL,
                number INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS gifts_notified_idx ON gifts(notified, detected_at);
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def upsert_collection(self, collection: Collection) -> Collection:
        self.connection.execute(
            """
            INSERT INTO collections(gift_id, title, slug_prefix, last_issued)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(gift_id) DO UPDATE SET
                title = excluded.title,
                slug_prefix = excluded.slug_prefix,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                collection.gift_id,
                collection.title,
                collection.slug_prefix,
                collection.last_issued,
            ),
        )
        self.connection.commit()
        return self.get_collection(collection.gift_id)

    def get_collection(self, gift_id: int) -> Collection:
        row = self.connection.execute(
            "SELECT gift_id, title, slug_prefix, last_issued FROM collections WHERE gift_id = ?",
            (gift_id,),
        ).fetchone()
        if row is None:
            raise KeyError(gift_id)
        return Collection(
            gift_id=row["gift_id"],
            title=row["title"],
            slug_prefix=row["slug_prefix"],
            last_issued=row["last_issued"],
        )

    def list_collections(self) -> list[Collection]:
        rows = self.connection.execute(
            "SELECT gift_id, title, slug_prefix, last_issued FROM collections ORDER BY gift_id"
        ).fetchall()
        return [
            Collection(
                gift_id=row["gift_id"],
                title=row["title"],
                slug_prefix=row["slug_prefix"],
                last_issued=row["last_issued"],
            )
            for row in rows
        ]

    def set_last_issued(self, gift_id: int, value: int) -> None:
        self.connection.execute(
            "UPDATE collections SET last_issued = ?, updated_at = CURRENT_TIMESTAMP WHERE gift_id = ?",
            (value, gift_id),
        )
        self.connection.commit()

    def record_gift(self, event: GiftEvent) -> bool:
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO gifts(slug, gift_id, number, detected_at, payload)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                event.slug,
                event.gift_id,
                event.number,
                event.detected_at.isoformat(),
                json.dumps(event.to_dict(), ensure_ascii=False),
            ),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def pending_notifications(self, limit: int = 100) -> list[GiftEvent]:
        rows = self.connection.execute(
            "SELECT payload FROM gifts WHERE notified = 0 ORDER BY detected_at LIMIT ?",
            (limit,),
        ).fetchall()
        return [GiftEvent.from_dict(json.loads(row["payload"])) for row in rows]

    def mark_notified(self, slug: str) -> None:
        self.connection.execute("UPDATE gifts SET notified = 1 WHERE slug = ?", (slug,))
        self.connection.commit()

    def load_runtime_filters(self) -> RuntimeFilters | None:
        row = self.connection.execute(
            "SELECT value FROM settings WHERE key = ?", ("runtime_filters",)
        ).fetchone()
        if row is None:
            return None
        return RuntimeFilters.from_dict(json.loads(row["value"]))

    def save_runtime_filters(self, filters: RuntimeFilters) -> None:
        self.connection.execute(
            """
            INSERT INTO settings(key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP
            """,
            ("runtime_filters", json.dumps(filters.to_dict(), ensure_ascii=False)),
        )
        self.connection.commit()

    def load_menu_settings(self) -> MenuSettings | None:
        row = self.connection.execute(
            "SELECT value FROM settings WHERE key = ?", ("menu_settings",)
        ).fetchone()
        if row is None:
            return None
        return MenuSettings.from_dict(json.loads(row["value"]))

    def save_menu_settings(self, settings: MenuSettings) -> None:
        self.connection.execute(
            """
            INSERT INTO settings(key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP
            """,
            ("menu_settings", json.dumps(settings.to_dict(), ensure_ascii=False)),
        )
        self.connection.commit()

    def increment_missing_gift_attempt(self, gift_id: int, slug: str, number: int) -> int:
        self.connection.execute(
            """
            INSERT INTO missing_gifts(slug, gift_id, number, attempts)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(slug) DO UPDATE SET
                attempts = attempts + 1,
                updated_at = CURRENT_TIMESTAMP
            """,
            (slug, gift_id, number),
        )
        row = self.connection.execute(
            "SELECT attempts FROM missing_gifts WHERE slug = ?", (slug,)
        ).fetchone()
        self.connection.commit()
        return int(row["attempts"])

    def clear_missing_gift(self, slug: str) -> None:
        self.connection.execute("DELETE FROM missing_gifts WHERE slug = ?", (slug,))
        self.connection.commit()
