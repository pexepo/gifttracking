# Gift Tracking Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the gift-tracking bot: login via Telegram itself (share-phone + inline code keypad), auto messages with prices to gift owners, Gift Satellite price client, and a unified filters/settings menu.

**Architecture:** Evolve the existing Telethon + plain Bot API (urllib) codebase. Add `login.py` (login state machine) and `pricing.py` (Satellite API client); extend `models.py`, `storage.py`, `telegram_api.py`, `notifier.py`, `monitor.py`. The monitor loop stays intact; only the notification pipeline changes (price → owner PM → admin status).

**Tech Stack:** Python 3.11+, Telethon, sqlite3, plain urllib Bot API. Tests: `unittest` (`python -m unittest discover -s tests -v`).

## Global Constraints

- Tests are `unittest`, async tests use `unittest.IsolatedAsyncioTestCase`; existing tests must keep passing without modification.
- No new third-party dependencies (only stdlib + Telethon).
- Follow existing code style: type hints, `from __future__ import annotations`, no comments unless needed, Russian UI strings.
- `RuntimeFilters.from_dict` must stay backward compatible with rows saved by the old schema (new fields default).
- Owner PM is sent via MTProto as the registered account; admin status via Bot API.
- Gift Satellite base URL is user-configurable (settings menu + env); exact endpoint format is adapted after reverse-engineering (Task 4).
- Commit at the end of each task with a short plain-English message matching repo style (e.g. `Add in-bot login flow`).

---

### Task 1: Extend models and config with new fields

**Files:**
- Modify: `src/gift_tracking/models.py`
- Modify: `src/gift_tracking/config.py`
- Create: `tests/test_models.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `RuntimeFilters` gains: `model_filter_enabled: bool = False`, `model_filters: tuple[str, ...] = ()`, `min_price: float | None = None`, `max_price: float | None = None` (from_dict tolerant of missing keys).
  - `GiftEvent` gains: `owner_user_id: int | None = None` (last field, optional).
  - `MarketPrice(market: str, price_ton: float | None = None, price_stars: float | None = None)` — frozen slots dataclass.
  - `PriceInfo(collection: str, model: str, backdrop: str, markets: tuple[MarketPrice, ...], fetched_at: datetime)` — frozen slots dataclass.
  - `MenuSettings(owner_message_template: str, satellite_api_key: str, satellite_api_url: str, auto_price_enabled: bool, send_to_owner_enabled: bool)` — frozen slots dataclass with `DEFAULT_OWNER_TEMPLATE` constant, `to_dict()` / `from_dict()` like `RuntimeFilters`.
  - `Config` gains optional fields: `satellite_api_key: str = ""`, `satellite_api_url: str = ""` (read from env `SATELLITE_API_KEY`, `SATELLITE_API_URL` in `from_env`).

- [ ] **Step 1: Write failing tests**

Create `tests/test_models.py`:

```python
import unittest
from datetime import UTC, datetime

from gift_tracking.models import (
    GiftEvent,
    MarketPrice,
    MenuSettings,
    PriceInfo,
    RuntimeFilters,
)


class RuntimeFiltersTests(unittest.TestCase):
    def test_from_dict_backward_compatible(self) -> None:
        filters = RuntimeFilters.from_dict(
            {
                "require_owner_username": True,
                "backdrop_filter_enabled": False,
                "backdrop_filters": ["coral red"],
                "blocked_owner_username_substrings": ["bank"],
            }
        )
        self.assertFalse(filters.model_filter_enabled)
        self.assertEqual(filters.model_filters, ())
        self.assertIsNone(filters.min_price)
        self.assertIsNone(filters.max_price)

    def test_round_trip_with_new_fields(self) -> None:
        filters = RuntimeFilters(
            require_owner_username=True,
            backdrop_filter_enabled=True,
            backdrop_filters=("coral red",),
            blocked_owner_username_substrings=("bank",),
            model_filter_enabled=True,
            model_filters=("Albino", "Pumpkin"),
            min_price=1.5,
            max_price=100.0,
        )
        self.assertEqual(RuntimeFilters.from_dict(filters.to_dict()), filters)


class PriceInfoTests(unittest.TestCase):
    def test_market_price_fields(self) -> None:
        price = MarketPrice(market="Tonnel", price_ton=12.5, price_stars=None)
        self.assertEqual(price.market, "Tonnel")
        self.assertEqual(price.price_ton, 12.5)
        self.assertIsNone(price.price_stars)

    def test_price_info_shape(self) -> None:
        info = PriceInfo(
            collection="Plush Pepe",
            model="Albino",
            backdrop="Black",
            markets=(MarketPrice("Tonnel", price_ton=12.5),),
            fetched_at=datetime.now(UTC),
        )
        self.assertEqual(len(info.markets), 1)
        self.assertEqual(info.markets[0].market, "Tonnel")


class MenuSettingsTests(unittest.TestCase):
    def test_default_template_contains_placeholders(self) -> None:
        self.assertIn("{title}", MenuSettings.DEFAULT_OWNER_TEMPLATE)
        self.assertIn("{price}", MenuSettings.DEFAULT_OWNER_TEMPLATE)

    def test_round_trip(self) -> None:
        settings = MenuSettings(
            owner_message_template="Куплю {title} #{number} за {price}",
            satellite_api_key="secret",
            satellite_api_url="https://api.example.com",
            auto_price_enabled=True,
            send_to_owner_enabled=False,
        )
        self.assertEqual(MenuSettings.from_dict(settings.to_dict()), settings)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_models -v`
Expected: FAIL — `ImportError`/`AttributeError` (module or attributes missing).

- [ ] **Step 3: Implement models**

In `src/gift_tracking/models.py`:

```python
@dataclass(frozen=True, slots=True)
class RuntimeFilters:
    require_owner_username: bool
    backdrop_filter_enabled: bool
    backdrop_filters: tuple[str, ...]
    blocked_owner_username_substrings: tuple[str, ...]
    model_filter_enabled: bool = False
    model_filters: tuple[str, ...] = ()
    min_price: float | None = None
    max_price: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RuntimeFilters:
        def optional_float(value: Any) -> float | None:
            if value is None or value == "":
                return None
            return float(value)

        return cls(
            require_owner_username=bool(data.get("require_owner_username", False)),
            backdrop_filter_enabled=bool(data.get("backdrop_filter_enabled", False)),
            backdrop_filters=tuple(data.get("backdrop_filters", [])),
            blocked_owner_username_substrings=tuple(
                data.get("blocked_owner_username_substrings", [])
            ),
            model_filter_enabled=bool(data.get("model_filter_enabled", False)),
            model_filters=tuple(data.get("model_filters", [])),
            min_price=optional_float(data.get("min_price")),
            max_price=optional_float(data.get("max_price")),
        )
```

Add `owner_user_id: int | None = None` as the last field of `GiftEvent` and read it in `from_dict` via `data.get("owner_user_id")`.

Append to `models.py`:

```python
@dataclass(frozen=True, slots=True)
class MarketPrice:
    market: str
    price_ton: float | None = None
    price_stars: float | None = None


@dataclass(frozen=True, slots=True)
class PriceInfo:
    collection: str
    model: str
    backdrop: str
    markets: tuple[MarketPrice, ...]
    fetched_at: datetime


DEFAULT_OWNER_TEMPLATE = (
    "Здравствуйте! Хочу купить у вас {title} #{number}. "
    "Модель: {model}, фон: {backdrop}. Цена: {price}. {link}"
)


@dataclass(frozen=True, slots=True)
class MenuSettings:
    owner_message_template: str = DEFAULT_OWNER_TEMPLATE
    satellite_api_key: str = ""
    satellite_api_url: str = ""
    auto_price_enabled: bool = True
    send_to_owner_enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MenuSettings:
        return cls(
            owner_message_template=str(
                data.get("owner_message_template", DEFAULT_OWNER_TEMPLATE)
            ),
            satellite_api_key=str(data.get("satellite_api_key", "")),
            satellite_api_url=str(data.get("satellite_api_url", "")),
            auto_price_enabled=bool(data.get("auto_price_enabled", True)),
            send_to_owner_enabled=bool(data.get("send_to_owner_enabled", True)),
        )
```

- [ ] **Step 4: Add config fields**

In `src/gift_tracking/config.py` add to `Config` dataclass (after `bot_api_insecure_ssl`):

```python
    satellite_api_key: str = ""
    satellite_api_url: str = ""
```

and in `from_env` return:

```python
            satellite_api_key=os.getenv("SATELLITE_API_KEY", "").strip(),
            satellite_api_url=os.getenv("SATELLITE_API_URL", "").strip(),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m unittest tests.test_models tests.test_config tests.test_storage tests.test_monitor -v`
Expected: PASS (all, including pre-existing).

- [ ] **Step 6: Commit**

```bash
git add src/gift_tracking/models.py src/gift_tracking/config.py tests/test_models.py
git commit -m "Add price, settings and model filter fields"
```

---

### Task 2: Persist MenuSettings in storage

**Files:**
- Modify: `src/gift_tracking/storage.py`
- Modify: `tests/test_storage.py`

**Interfaces:**
- Consumes: `MenuSettings` from Task 1.
- Produces: `Storage.load_menu_settings() -> MenuSettings | None`, `Storage.save_menu_settings(settings: MenuSettings) -> None` (JSON blob under key `"menu_settings"` in the existing `settings` table, same pattern as `runtime_filters`).

- [ ] **Step 1: Write failing test**

Append to `tests/test_storage.py` inside `StorageTests`:

```python
    def test_persists_menu_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "state.sqlite3")
            self.assertIsNone(storage.load_menu_settings())
            settings = MenuSettings(
                owner_message_template="Куплю {title} за {price}",
                satellite_api_key="k",
                satellite_api_url="https://api.example.com",
                auto_price_enabled=False,
                send_to_owner_enabled=True,
            )
            storage.save_menu_settings(settings)
            self.assertEqual(storage.load_menu_settings(), settings)
            storage.close()
```

Add import: `from gift_tracking.models import Collection, GiftEvent, MenuSettings, RuntimeFilters`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_storage -v`
Expected: FAIL — `AttributeError: 'Storage' object has no attribute 'load_menu_settings'`.

- [ ] **Step 3: Implement storage methods**

In `src/gift_tracking/storage.py` add import of `MenuSettings` and:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_storage -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gift_tracking/storage.py tests/test_storage.py
git commit -m "Persist menu settings in sqlite"
```

---

### Task 3: Template rendering and price formatting

**Files:**
- Modify: `src/gift_tracking/notifier.py`
- Modify: `tests/test_notifier.py`

**Interfaces:**
- Consumes: `GiftEvent`, `PriceInfo`, `MarketPrice` from Task 1.
- Produces:
  - `attribute_value(event: GiftEvent, kind: str) -> str` — returns attribute name or `"—"`.
  - `format_price(price: PriceInfo | None) -> str` — joined `"Market: 12.5 TON"` lines, or `"по запросу"` when `price` is None/empty.
  - `render_owner_message(template: str, event: GiftEvent, price: PriceInfo | None) -> str` — replaces `{title}`, `{number}`, `{model}`, `{backdrop}`, `{price}`, `{link}`.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_notifier.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_notifier -v`
Expected: FAIL — `ImportError` for `format_price` / `render_owner_message`.

- [ ] **Step 3: Implement**

In `src/gift_tracking/notifier.py` add import `from .models import GiftEvent, MarketPrice, PriceInfo` and:

```python
def attribute_value(event: GiftEvent, kind: str) -> str:
    for attribute in event.attributes:
        if attribute.kind == kind:
            return attribute.name
    return "—"


def format_price(price: PriceInfo | None) -> str:
    if price is None or not price.markets:
        return "по запросу"
    parts: list[str] = []
    for market in price.markets:
        value = ""
        if market.price_ton is not None:
            value = f"{market.price_ton:g} TON"
        elif market.price_stars is not None:
            value = f"{market.price_stars:g} ⭐"
        if value:
            parts.append(f"{market.market}: {value}")
    return "; ".join(parts) if parts else "по запросу"


def render_owner_message(
    template: str, event: GiftEvent, price: PriceInfo | None
) -> str:
    replacements = {
        "{title}": event.title,
        "{number}": str(event.number),
        "{model}": attribute_value(event, "model"),
        "{backdrop}": attribute_value(event, "backdrop"),
        "{price}": format_price(price),
        "{link}": event.link,
    }
    result = template
    for placeholder, value in replacements.items():
        result = result.replace(placeholder, value)
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_notifier -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gift_tracking/notifier.py tests/test_notifier.py
git commit -m "Add owner message template rendering"
```

---

### Task 4: Gift Satellite pricing client plus API discovery

**Files:**
- Create: `src/gift_tracking/pricing.py`
- Create: `tests/test_pricing.py`
- Create: `tools/discover_satellite_api.py`

**Interfaces:**
- Consumes: `PriceInfo`, `MarketPrice` from Task 1.
- Produces:
  - `class SatellitePricingError(RuntimeError)`
  - `class GiftSatelliteClient` — `__init__(api_key: str, base_url: str, *, insecure_ssl: bool = False, timeout: int = 20)`, `async fetch_price(collection: str, model: str, backdrop: str) -> PriceInfo | None`, `async check_key() -> tuple[bool, str]`, `static parse_prices(data: dict[str, Any]) -> PriceInfo | None`, `_parse_market(raw: Any) -> MarketPrice | None`.
  - `tools/discover_satellite_api.py` — best-effort script that opens the @gift_satellite_bot webview via the MTProto session and prints the app URL and API endpoints found in its JS bundle.

- [ ] **Step 1: Write failing tests**

Create `tests/test_pricing.py`:

```python
import unittest
from datetime import UTC, datetime

from gift_tracking.pricing import GiftSatelliteClient


class ParsePricesTests(unittest.TestCase):
    def test_parses_known_shape(self) -> None:
        price = GiftSatelliteClient.parse_prices(
            {
                "collection": "Plush Pepe",
                "model": "Spectrum",
                "backdrop": "Coral Red",
                "prices": [
                    {"market": "Tonnel", "price_ton": 12.5},
                    {"market": "MRKT", "price_stars": 950},
                ],
            }
        )
        self.assertIsNotNone(price)
        assert price is not None
        self.assertEqual(price.collection, "Plush Pepe")
        self.assertEqual(len(price.markets), 2)
        self.assertEqual(price.markets[0].price_ton, 12.5)
        self.assertEqual(price.markets[1].price_stars, 950.0)
        self.assertIsInstance(price.fetched_at, datetime)

    def test_returns_none_on_unknown_shape(self) -> None:
        self.assertIsNone(GiftSatelliteClient.parse_prices({"foo": "bar"}))

    def test_returns_none_on_bad_json_like_input(self) -> None:
        self.assertIsNone(GiftSatelliteClient.parse_prices(None))

    def test_skips_invalid_market_entries(self) -> None:
        price = GiftSatelliteClient.parse_prices(
            {
                "collection": "X",
                "model": "Y",
                "backdrop": "Z",
                "prices": [
                    {"market": ""},
                    {"market": "Tonnel", "price_ton": 3.0},
                ],
            }
        )
        assert price is not None
        self.assertEqual(len(price.markets), 1)
        self.assertEqual(price.markets[0].market, "Tonnel")


class CheckBuildingTests(unittest.TestCase):
    def test_build_url_contains_filters(self) -> None:
        client = GiftSatelliteClient("key", "https://api.example.com/v1")
        url = client._prices_url("Plush Pepe", "Spectrum", "Coral Red")
        self.assertIn("Plush%20Pepe", url)
        self.assertIn("Spectrum", url)
        self.assertIn("Coral%20Red", url)

    def test_api_key_sent_in_header(self) -> None:
        client = GiftSatelliteClient("sekrit", "https://api.example.com")
        headers = client._headers()
        self.assertEqual(headers["X-API-Key"], "sekrit")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_pricing -v`
Expected: FAIL — module import error.

- [ ] **Step 3: Implement pricing client**

Create `src/gift_tracking/pricing.py`:

```python
from __future__ import annotations

import asyncio
import html
import json
import logging
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any

from .models import MarketPrice, PriceInfo

LOGGER = logging.getLogger(__name__)


class SatellitePricingError(RuntimeError):
    pass


class GiftSatelliteClient:
    """Client for the Gift Satellite market price API.

    The base URL and the exact response shape are configurable and were
    discovered by reverse-engineering the @gift_satellite_bot web app;
    see tools/discover_satellite_api.py. parse_prices accepts the shape
    {'collection': str, 'model': str, 'backdrop': str,
     'prices': [{'market': str, 'price_ton': float | None,
                 'price_stars': float | None}, ...]} and returns None for
    anything else so the monitor never crashes on schema drift.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        *,
        insecure_ssl: bool = False,
        timeout: int = 20,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.ssl_context = (
            ssl._create_unverified_context() if insecure_ssl else ssl.create_default_context()
        )

    def _headers(self) -> dict[str, str]:
        return {
            "X-API-Key": self.api_key,
            "Accept": "application/json",
            "User-Agent": "gift-tracking/0.2",
        }

    def _prices_url(self, collection: str, model: str, backdrop: str) -> str:
        query = urllib.parse.urlencode(
            {
                "collection": collection,
                "model": model,
                "backdrop": backdrop,
            }
        )
        return f"{self.base_url}/prices?{query}"

    @staticmethod
    def _parse_market(raw: Any) -> MarketPrice | None:
        if not isinstance(raw, dict):
            return None
        market = raw.get("market")
        if not isinstance(market, str) or not market.strip():
            return None

        def optional_float(value: Any) -> float | None:
            if value is None or value == "":
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        return MarketPrice(
            market=market.strip(),
            price_ton=optional_float(raw.get("price_ton")),
            price_stars=optional_float(raw.get("price_stars")),
        )

    @staticmethod
    def parse_prices(data: Any) -> PriceInfo | None:
        if not isinstance(data, dict):
            return None
        collection = data.get("collection")
        model = data.get("model")
        backdrop = data.get("backdrop")
        if not all(isinstance(value, str) for value in (collection, model, backdrop)):
            return None
        raw_prices = data.get("prices")
        if not isinstance(raw_prices, list):
            return None
        markets = tuple(
            market
            for market in (GiftSatelliteClient._parse_market(item) for item in raw_prices)
            if market is not None and (market.price_ton is not None or market.price_stars is not None)
        )
        if not markets:
            return None
        return PriceInfo(
            collection=collection,
            model=model,
            backdrop=backdrop,
            markets=markets,
            fetched_at=datetime.now(UTC),
        )

    async def fetch_price(
        self, collection: str, model: str, backdrop: str
    ) -> PriceInfo | None:
        url = self._prices_url(collection, model, backdrop)
        try:
            data = await asyncio.to_thread(self._call, url)
        except SatellitePricingError as exc:
            LOGGER.warning("Цены недоступны (%s): %s", collection, exc)
            return None
        return self.parse_prices(data)

    async def check_key(self) -> tuple[bool, str]:
        url = f"{self.base_url}/prices"
        try:
            data = await asyncio.to_thread(self._call, url)
        except SatellitePricingError as exc:
            return False, str(exc)
        if not isinstance(data, dict):
            return False, "Неожиданный ответ API"
        return True, "Ключ работает"

    def _call(self, url: str, timeout: int | None = None) -> Any:
        request = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(
                request, timeout=timeout or self.timeout, context=self.ssl_context
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, socket.timeout, json.JSONDecodeError) as exc:
            raise SatellitePricingError(f"Сетевая ошибка: {exc}") from exc
        except urllib.error.HTTPError as exc:
            raise SatellitePricingError(f"HTTP {exc.code}: {exc.reason}") from exc
```

- [ ] **Step 4: Create discovery script**

Create `tools/discover_satellite_api.py` (best-effort; requires a valid MTProto session):

```python
"""Best-effort discovery of the Gift Satellite web API.

Usage:
  python tools/discover_satellite_api.py --api-id 123 --api-hash abc --session gift_tracking

Opens the @gift_satellite_bot web app through the MTProto session, fetches
its JS bundle and prints candidate API endpoints. This is a starting point
for adapting GiftSatelliteClient (pricing.py); the response format is then
confirmed by capturing real requests (DevTools Network tab in a web
Telegram session, or the app's own traffic).
"""
from __future__ import annotations

import argparse
import re
import ssl
import urllib.parse
import urllib.request

from telethon import TelegramClient
from telethon.tl.functions.messages import RequestAppWebView
from telethon.tl.types import InputBotAppShortName, InputUser


async def main(api_id: int, api_hash: str, session: str, insecure_ssl: bool) -> None:
    client = TelegramClient(session, api_id, api_hash)
    await client.start()
    bot = await client.get_entity("gift_satellite_bot")
    peer = await client.get_input_entity(bot)
    app = await client(InputBotAppShortName(bot_id=bot, short_name=""))
    result = await client(
        RequestAppWebView(
            peer=peer,
            app=app,
            platform="android",
        )
    )
    if not result.url:
        print("Webview URL not returned")
        return
    parsed = urllib.parse.urlparse(result.url)
    app_url = f"{parsed.scheme}://{parsed.netloc}"
    print(f"App URL: {app_url}")
    context = ssl._create_unverified_context() if insecure_ssl else ssl.create_default_context()
    bundle = urllib.request.urlopen(result.url, context=context).read().decode("utf-8", "replace")
    endpoints = sorted(
        set(
            re.findall(
                r"https?://[a-zA-Z0-9.-]+/api/[a-zA-Z0-9/_-]+",
                bundle,
            )
        )
    )
    for endpoint in endpoints:
        print(f"Endpoint: {endpoint}")
    if not endpoints:
        print("No API endpoints found in the JS bundle; inspect the bundle manually or capture requests.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-id", type=int, required=True)
    parser.add_argument("--api-hash", required=True)
    parser.add_argument("--session", default="gift_tracking")
    parser.add_argument("--insecure-ssl", action="store_true")
    args = parser.parse_args()
    import asyncio

    asyncio.run(main(args.api_id, args.api_hash, args.session, args.insecure_ssl))
```

Note for implementer: if `RequestAppWebView` fails (access hash/entity issues), fall back to asking the user for DevTools captures from the web app. The response-shape contract used by `parse_prices` is the adapter point if the real API differs.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m unittest tests.test_pricing -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/gift_tracking/pricing.py tests/test_pricing.py tools/discover_satellite_api.py
git commit -m "Add Gift Satellite price client and API discovery script"
```

---

### Task 5: Notifier login keyboards and menus

**Files:**
- Modify: `src/gift_tracking/notifier.py`
- Modify: `tests/test_notifier.py`

**Interfaces:**
- Consumes: `FilterMenuState` (existing), `MenuSettings`, `RuntimeFilters` from Task 1.
- Produces:
  - `BotNotifier.send_login_prompt(chat_id: str)` — reply keyboard with one `request_contact` button.
  - `BotNotifier.send_code_prompt(chat_id: str, buffer: str) -> dict` — message with inline numeric keypad.
  - `BotNotifier.update_code_prompt(chat_id: str, message_id: int, buffer: str) -> None`.
  - `BotNotifier.send_menu(chat_id: str | None = None)` — main menu with buttons: Filters / Settings / Account.
  - `BotNotifier.send_settings_menu(settings: MenuSettings, *, chat_id: str | None = None)`.
  - `BotNotifier.send_account_menu(account_authorized: bool, phone: str | None, *, chat_id: str | None = None)`.
  - Static helpers (testable): `code_keyboard(buffer: str) -> dict`, `login_keyboard() -> dict`, `main_menu_keyboard() -> dict`, `settings_menu_keyboard(settings: MenuSettings) -> dict`, `account_menu_keyboard(authorized: bool) -> dict`.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_notifier.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_notifier -v`
Expected: FAIL — missing methods.

- [ ] **Step 3: Implement in notifier.py**

Add import `from .models import MenuSettings`. Add to `BotNotifier`:

```python
    @staticmethod
    def code_keyboard(buffer: str) -> dict[str, object]:
        rows = [[{"text": digit, "callback_data": f"code_digit_{digit}"} for digit in row] for row in ("123", "456", "789")]
        empty_row = [
            {"text": "⌫", "callback_data": "code_backspace"},
            {"text": "0", "callback_data": "code_digit_0"},
            {"text": "Отправить", "callback_data": "code_submit"},
        ]
        if buffer:
            display = [{"text": f"Код: {buffer}", "callback_data": "code_noop"}]
            return {"inline_keyboard": [display, *rows, empty_row]}
        return {"inline_keyboard": [*rows, empty_row]}

    @staticmethod
    def login_keyboard() -> dict[str, object]:
        return {
            "keyboard": [
                [{"text": "📱 Поделиться номером", "request_contact": True}]
            ],
            "resize_keyboard": True,
            "one_time_keyboard": True,
        }

    @staticmethod
    def main_menu_keyboard() -> dict[str, object]:
        return {
            "inline_keyboard": [
                [{"text": "⚙️ Фильтры", "callback_data": "menu_filters"}],
                [{"text": "🛠 Настройки", "callback_data": "menu_settings"}],
                [{"text": "👤 Аккаунт", "callback_data": "menu_account"}],
            ]
        }

    @staticmethod
    def settings_menu_keyboard(settings: MenuSettings) -> dict[str, object]:
        price_state = "✅" if settings.auto_price_enabled else "⛔️"
        owner_state = "✅" if settings.send_to_owner_enabled else "⛔️"
        key_state = "задан" if settings.satellite_api_key else "не задан"
        return {
            "inline_keyboard": [
                [{"text": "✏️ Шаблон сообщения", "callback_data": "edit_owner_template"}],
                [{"text": f"🔑 API-ключ: {key_state}", "callback_data": "edit_api_key"}],
                [{"text": "⚠️ Проверить ключ", "callback_data": "check_api_key"}],
                [{"text": "🌐 URL API", "callback_data": "edit_api_url"}],
                [{"text": f"{price_state} Авто-цена в алертах", "callback_data": "toggle_auto_price"}],
                [{"text": f"{owner_state} Отправка владельцу", "callback_data": "toggle_send_owner"}],
                [{"text": "⬅️ Назад", "callback_data": "menu_main"}],
            ]
        }

    @staticmethod
    def account_menu_keyboard(authorized: bool) -> dict[str, object]:
        exit_button = [{"text": "🚪 Выйти", "callback_data": "account_logout"}] if authorized else []
        rows: list[list[dict[str, str]]] = []
        if exit_button:
            rows.append(exit_button)
        rows.append(
            [{"text": "🔑 Залогиниться", "callback_data": "account_login"}]
        )
        rows.append([{"text": "⬅️ Назад", "callback_data": "menu_main"}])
        return {"inline_keyboard": rows}

    @staticmethod
    def _settings_menu_text(settings: MenuSettings) -> str:
        return "\n".join(
            [
                "🛠 <b>Настройки</b>",
                "",
                "Шаблон владельцу:",
                f"<code>{html.escape(settings.owner_message_template)}</code>",
                "",
                "API-ключ: "
                + ("<b>задан</b>" if settings.satellite_api_key else "<b>не задан</b>"),
                "URL API: <b>"
                + (html.escape(settings.satellite_api_url) if settings.satellite_api_url else "не задан")
                + "</b>",
                "Авто-цена: " + ("<b>вкл</b>" if settings.auto_price_enabled else "<b>выкл</b>"),
                "Отправка владельцу: "
                + ("<b>вкл</b>" if settings.send_to_owner_enabled else "<b>выкл</b>"),
            ]
        )

    @staticmethod
    def _account_menu_text(authorized: bool, phone: str | None) -> str:
        status = "авторизован" if authorized else "не авторизован"
        phone_line = phone or "—"
        return "\n".join(
            [
                "👤 <b>Аккаунт</b>",
                "",
                "Статус: <b>" + status + "</b>",
                "Телефон: <code>" + html.escape(phone_line) + "</code>",
            ]
        )

    async def send_login_prompt(self, chat_id: str) -> None:
        await self.send_text(
            "Залогиньтесь в Telegram-аккаунт. Нажмите кнопку ниже, "
            "чтобы поделиться номером телефона.",
            keyboard=self.login_keyboard(),
            chat_id=chat_id,
        )

    async def send_code_prompt(self, chat_id: str, buffer: str = "") -> dict[str, object]:
        return await self.send_text(
            "Введите код из Telegram кнопками ниже или текстом:",
            keyboard=self.code_keyboard(buffer),
            chat_id=chat_id,
        )

    async def update_code_prompt(self, chat_id: str, message_id: int, buffer: str) -> None:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": "Введите код из Telegram кнопками ниже или текстом:",
            "parse_mode": "HTML",
            "reply_markup": self.code_keyboard(buffer),
        }
        await asyncio.to_thread(self._call, "editMessageText", payload)

    async def send_menu(self, *, chat_id: str | None = None) -> dict[str, object]:
        return await self.send_text(
            "🎁 <b>Gift Tracking</b>\nВыберите раздел:", keyboard=self.main_menu_keyboard(), chat_id=chat_id
        )

    async def send_settings_menu(
        self, settings: MenuSettings, *, chat_id: str | None = None
    ) -> dict[str, object]:
        return await self.send_text(
            self._settings_menu_text(settings),
            keyboard=self.settings_menu_keyboard(settings),
            chat_id=chat_id,
        )

    async def update_settings_menu(
        self, chat_id: str, message_id: int, settings: MenuSettings
    ) -> None:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": self._settings_menu_text(settings),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": self.settings_menu_keyboard(settings),
        }
        await asyncio.to_thread(self._call, "editMessageText", payload)

    async def send_account_menu(
        self, authorized: bool, phone: str | None, *, chat_id: str | None = None
    ) -> dict[str, object]:
        return await self.send_text(
            self._account_menu_text(authorized, phone),
            keyboard=self.account_menu_keyboard(authorized),
            chat_id=chat_id,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_notifier -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gift_tracking/notifier.py tests/test_notifier.py
git commit -m "Add login keyboards and settings menu to notifier"
```

---

### Task 6: Telegram API — auth primitives and owner PM

**Files:**
- Modify: `src/gift_tracking/telegram_api.py`
- Modify: `tests/test_telegram_api.py`

**Interfaces:**
- Consumes: `GiftEvent` from Task 1 (new `owner_user_id`).
- Produces (replaces interactive `start()`):
  - `GiftTelegramApi.connect() -> None` — `client.connect()`.
  - `GiftTelegramApi.is_authorized() -> bool`.
  - `GiftTelegramApi.send_code_request(phone: str) -> Any` — returns sent code object (holds `phone_code_hash`).
  - `GiftTelegramApi.sign_in(phone: str, code: str, phone_code_hash: str) -> None` — raises `errors.SessionPasswordNeededError` when 2FA needed.
  - `GiftTelegramApi.sign_in_password(password: str) -> None`.
  - `GiftTelegramApi.get_me_phone() -> str | None`.
  - `GiftTelegramApi.remember_owner(entity: Any) -> None` — caches `user.id → entity` while parsing gift results.
  - `GiftTelegramApi.send_message_to_user(user_id: int, text: str) -> None` — cached entity, else error.
  - `start()` kept as alias of `connect()` for backward compatibility with Task 8's monitor start sequence.

- [ ] **Step 1: Write failing tests**

Rewrite `tests/test_telegram_api.py`:

```python
import unittest

from gift_tracking.telegram_api import GiftTelegramApi, slug_prefix_from_title


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.phone_codes: dict[str, object] = {}
        self.signed_in_code: tuple | None = None
        self.signed_in_password: str | None = None

    async def connect(self) -> None:
        pass

    async def is_user_authorized(self) -> bool:
        return False

    async def send_code_request(self, phone: str) -> object:
        code = object()
        self.phone_codes[phone] = code
        return code

    async def sign_in(self, *args, **kwargs) -> None:
        self.signed_in_code = args if args else (kwargs.get("phone"), kwargs.get("code"))

    async def sign_in_password(self, password: str) -> None:
        self.signed_in_password = password

    async def send_message(self, entity, text: str) -> None:
        self.sent.append((getattr(entity, "id", entity), text))


class FakeEntity:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class TelegramApiTests(unittest.TestCase):
    def test_slug_prefix(self) -> None:
        self.assertEqual(slug_prefix_from_title("Plush Pepe"), "PlushPepe")
        self.assertEqual(slug_prefix_from_title("Jack-in-the-Box!"), "JackintheBox")

    def test_auth_primitives_delegate_to_client(self) -> None:
        api = GiftTelegramApi(1, "hash", "session")
        client = FakeClient()
        api.client = client

        import asyncio

        asyncio.run(api.connect())
        self.assertFalse(asyncio.run(api.is_authorized()))
        sent = asyncio.run(api.send_code_request("+12345"))
        self.assertIs(sent, client.phone_codes["+12345"])
        asyncio.run(api.sign_in("+12345", "12345", "hash-value"))
        self.assertEqual(client.signed_in_code, ("+12345", "12345"))
        asyncio.run(api.sign_in_password("secret"))
        self.assertEqual(client.signed_in_password, "secret")

    def test_send_message_to_user_uses_cached_entity(self) -> None:
        api = GiftTelegramApi(1, "hash", "session")
        client = FakeClient()
        api.client = client
        api.remember_owner(FakeEntity(42))

        import asyncio

        asyncio.run(api.send_message_to_user(42, "hello"))
        self.assertEqual(client.sent, [(42, "hello")])

    def test_send_message_to_user_fails_without_entity(self) -> None:
        api = GiftTelegramApi(1, "hash", "session")
        api.client = FakeClient()

        import asyncio

        with self.assertRaises(ValueError):
            asyncio.run(api.send_message_to_user(999, "hello"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_telegram_api -v`
Expected: FAIL — telethon tries a real connection (or AttributeError on `remember_owner`).

- [ ] **Step 3: Implement**

In `src/gift_tracking/telegram_api.py`, replace `start()` and add:

```python
    def __init__(self, api_id: int, api_hash: str, session: str) -> None:
        self.client = TelegramClient(session, api_id, api_hash)
        self._known_users: dict[int, Any] = {}

    async def connect(self) -> None:
        await self.client.connect()

    async def start(self) -> None:
        await self.connect()

    async def is_authorized(self) -> bool:
        return bool(await self.client.is_user_authorized())

    async def send_code_request(self, phone: str) -> Any:
        return await self.client.send_code_request(phone)

    async def sign_in(self, phone: str, code: str, phone_code_hash: str) -> None:
        await self.client.sign_in(phone, code, phone_code_hash=phone_code_hash)

    async def sign_in_password(self, password: str) -> None:
        await self.client.sign_in(password=password)

    async def get_me_phone(self) -> str | None:
        me = await self.client.get_me()
        return getattr(me, "phone", None)

    def remember_owner(self, entity: Any) -> None:
        if entity is not None and getattr(entity, "id", None) is not None:
            self._known_users[int(entity.id)] = entity

    async def send_message_to_user(self, user_id: int, text: str) -> None:
        entity = self._known_users.get(user_id)
        if entity is None:
            raise ValueError(f"Нет данных о владельце {user_id}")
        await self.client.send_message(entity, text)
```

In `unique_gift()`, after parsing the gift, remember the owner entity when it's a `PeerUser` and pass `owner_user_id` into the event:

```python
        owner_name, owner_username = _owner(result, gift)
        peer = gift.owner_id
        owner_user_id = None
        if isinstance(peer, types.PeerUser):
            owner_user_id = peer.user_id
            entity = next((user for user in result.users if user.id == peer.user_id), None)
            if entity:
                self.remember_owner(entity)
        event = GiftEvent(
            ...
            owner_user_id=owner_user_id,
            ...
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_telegram_api -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gift_tracking/telegram_api.py tests/test_telegram_api.py
git commit -m "Add MTProto auth primitives and owner messaging"
```

---

### Task 7: In-bot login flow

**Files:**
- Create: `src/gift_tracking/login.py`
- Create: `tests/test_login.py`

**Interfaces:**
- Consumes: `GiftTelegramApi` (Task 6), `BotNotifier` (Task 5).
- Produces: `class LoginFlow` — `__init__(api: GiftTelegramApi, notifier: BotNotifier, chat_id: str, *, timeout_seconds: int = 600)`, `async run() -> bool` (True when authorized; False on cancel/failure/timeout). Internally polls `notifier.get_updates()` and drives `PHONE → CODE → PASSWORD`.
- Callback data consumed: `code_digit_<n>`, `code_backspace`, `code_submit`, `code_noop`. Text message in CODE state is treated as typed code; text in PASSWORD state as the 2FA password; `/cancel` aborts. Contact sharing arrives as a `message.contact`.

- [ ] **Step 1: Write failing tests**

Create `tests/test_login.py`:

```python
import unittest

from gift_tracking.login import LoginFlow, normalize_phone


class NormalizePhoneTests(unittest.TestCase):
    def test_strips_formatting(self) -> None:
        self.assertEqual(normalize_phone("+7 (912) 345-67-89"), "+79123456789")
        self.assertEqual(normalize_phone("+375291234567"), "+375291234567")
        self.assertEqual(normalize_phone(""), "")
        self.assertEqual(normalize_phone("abc+def"), "+")


class FakeNotifier:
    def __init__(self) -> None:
        self.updates: list[dict] = []
        self.sent_texts: list[dict] = []
        self.code_prompts: list[str] = []
        self.code_prompt_messages: dict[str, int] = {}

    async def get_updates(self, timeout: int = 30) -> list[dict]:
        return self.updates.pop(0) if self.updates else []

    async def send_text(self, text, *, keyboard=None, chat_id=None):
        self.sent_texts.append({"text": text, "chat_id": chat_id})
        return {"message_id": len(self.sent_texts)}

    async def send_code_prompt(self, chat_id, buffer=""):
        self.code_prompts.append(buffer)
        self.code_prompt_messages[f"{chat_id}:{buffer}"] = len(self.sent_texts) + 1
        return {"message_id": 10}

    async def update_code_prompt(self, chat_id, message_id, buffer):
        pass


class FakeApi:
    def __init__(self, with_password: bool = False) -> None:
        self.with_password = with_password
        self.connected = False
        self.authorized = False
        self.code_requested: str | None = None
        self.signed_code: tuple | None = None
        self.signed_password: str | None = None

    async def connect(self) -> None:
        self.connected = True

    async def is_authorized(self) -> bool:
        return self.authorized

    async def send_code_request(self, phone: str):
        self.code_requested = phone
        return "hash-value"

    async def sign_in(self, phone: str, code: str, phone_code_hash: str) -> None:
        if self.with_password:
            from telethon import errors

            raise errors.SessionPasswordNeededError(None)
        self.signed_code = (phone, code)
        self.authorized = True

    async def sign_in_password(self, password: str) -> None:
        self.signed_password = password
        self.authorized = True


class LoginFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_flow_phone_code_sign_in(self) -> None:
        api = FakeApi()
        notifier = FakeNotifier()
        flow = LoginFlow(api, notifier, "1")
        notifier.updates.append(
            {"message": {"chat": {"id": 1}, "contact": {"phone_number": "+7 912 345-67-89"}}}
        )
        notifier.updates.append(
            {"message": {"chat": {"id": 1}, "text": "12345"}}
        )
        self.assertTrue(await flow.run())
        self.assertTrue(api.connected)
        self.assertIn("+79123456789", api.code_requested or "")
        self.assertEqual(api.signed_code, ("+79123456789", "12345"))

    async def test_code_entered_via_inline_keyboard(self) -> None:
        api = FakeApi()
        notifier = FakeNotifier()
        flow = LoginFlow(api, notifier, "1")
        notifier.updates.append(
            {"message": {"chat": {"id": 1}, "contact": {"phone_number": "+12223334444"}}}
        )
        for digit in "1", "2", "3":
            notifier.updates.append(
                {
                    "callback_query": {
                        "id": f"cb{digit}",
                        "data": f"code_digit_{digit}",
                        "message": {"chat": {"id": 1}, "message_id": 10},
                    }
                }
            )
        notifier.updates.append(
            {
                "callback_query": {
                    "id": "cb-submit",
                    "data": "code_submit",
                    "message": {"chat": {"id": 1}, "message_id": 10},
                }
            }
        )
        self.assertTrue(await flow.run())
        self.assertEqual(api.signed_code, ("+12223334444", "123"))

    async def test_2fa_password(self) -> None:
        api = FakeApi(with_password=True)
        notifier = FakeNotifier()
        flow = LoginFlow(api, notifier, "1")
        notifier.updates.append(
            {"message": {"chat": {"id": 1}, "contact": {"phone_number": "+12223334444"}}}
        )
        notifier.updates.append(
            {"message": {"chat": {"id": 1}, "text": "98765"}}
        )
        notifier.updates.append(
            {"message": {"chat": {"id": 1}, "text": "supersecret"}}
        )
        self.assertTrue(await flow.run())
        self.assertEqual(api.signed_password, "supersecret")

    async def test_cancel_returns_false(self) -> None:
        api = FakeApi()
        notifier = FakeNotifier()
        flow = LoginFlow(api, notifier, "1")
        notifier.updates.append({"message": {"chat": {"id": 1}, "text": "/cancel"}})
        self.assertFalse(await flow.run())

    async def test_skips_when_already_authorized(self) -> None:
        api = FakeApi()
        api.authorized = True
        notifier = FakeNotifier()
        flow = LoginFlow(api, notifier, "1")
        self.assertTrue(await flow.run())
        self.assertIsNone(api.code_requested)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_login -v`
Expected: FAIL — import error.

- [ ] **Step 3: Implement login flow**

Create `src/gift_tracking/login.py`:

```python
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from time import monotonic

from telethon import errors

from .notifier import BotLongPollTimeout, BotNotifier, NotificationError
from .telegram_api import GiftTelegramApi

LOGGER = logging.getLogger(__name__)


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"[^0-9+]", "", phone)
    if digits.startswith("+"):
        return "+" + re.sub(r"[^0-9]", "", digits)
    return re.sub(r"[^0-9]", "", digits)


class LoginFlow:
    def __init__(
        self,
        api: GiftTelegramApi,
        notifier: BotNotifier,
        chat_id: str,
        *,
        timeout_seconds: int = 600,
    ) -> None:
        self.api = api
        self.notifier = notifier
        self.chat_id = chat_id
        self.timeout_seconds = timeout_seconds
        self._state = "idle"
        self._phone: str | None = None
        self._phone_code_hash: str | None = None
        self._code_buffer = ""
        self._code_message_id: int | None = None
        self._last_activity = 0.0

    async def run(self) -> bool:
        await self.api.connect()
        if await self.api.is_authorized():
            self._state = "done"
            return True
        await self.notifier.send_login_prompt(self.chat_id)
        self._state = "phone"
        self._last_activity = monotonic()
        while self._state != "done":
            if monotonic() - self._last_activity > self.timeout_seconds:
                await self.notifier.send_text(
                    "⏰ Время ожидания истекло. Начните заново: /start",
                    chat_id=self.chat_id,
                )
                return False
            try:
                updates = await self.notifier.get_updates()
            except BotLongPollTimeout:
                continue
            except NotificationError as exc:
                LOGGER.error("Ошибка логина: %s", exc)
                await asyncio.sleep(5)
                continue
            for update in updates:
                if await self._handle_update(update):
                    return self._state == "done"
        return True

    async def _handle_update(self, update: dict) -> bool:
        message = update.get("message")
        if isinstance(message, dict):
            await self._handle_message(message)
        callback_query = update.get("callback_query")
        if isinstance(callback_query, dict):
            await self._handle_callback_query(callback_query)
        return self._state in ("done", "failed")

    async def _handle_message(self, message: dict) -> None:
        chat = message.get("chat")
        if not isinstance(chat, dict) or str(chat.get("id")) != self.chat_id:
            return
        text = message.get("text")
        if isinstance(text, str) and text == "/cancel":
            await self.notifier.send_text("Логин отменён.", chat_id=self.chat_id)
            self._state = "failed"
            return
        contact = message.get("contact")
        if self._state == "phone" and isinstance(contact, dict):
            phone = normalize_phone(str(contact.get("phone_number", "")))
            if phone:
                await self._request_code(phone)
            return
        if self._state == "code" and isinstance(text, str) and text:
            await self._submit_code(text)
        elif self._state == "password" and isinstance(text, str) and text:
            await self._submit_password(text)

    async def _handle_callback_query(self, callback_query: dict) -> None:
        data = callback_query.get("data")
        callback_query_id = callback_query.get("id")
        message = callback_query.get("message")
        if not isinstance(data, str) or not isinstance(callback_query_id, str):
            return
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        if not isinstance(chat, dict) or str(chat.get("id")) != self.chat_id:
            return
        if self._state != "code":
            await self.notifier.answer_callback_query(callback_query_id, "Код ещё не запрошен")
            return
        if data.startswith("code_digit_"):
            digit = data.rsplit("_", 1)[-1]
            if digit.isdigit() and len(self._code_buffer) < 6:
                self._code_buffer += digit
                await self._refresh_code_prompt()
        elif data == "code_backspace":
            self._code_buffer = self._code_buffer[:-1]
            await self._refresh_code_prompt()
        elif data == "code_submit":
            buffer = self._code_buffer
            self._code_buffer = ""
            if buffer:
                await self._submit_code(buffer)
        elif data == "code_noop":
            pass
        await self.notifier.answer_callback_query(callback_query_id, "Код: " + self._code_buffer)

    async def _request_code(self, phone: str) -> None:
        self._phone = phone
        self._phone_code_hash = None
        try:
            sent = await self.api.send_code_request(phone)
        except errors.PhoneNumberInvalidError:
            await self.notifier.send_text(
                "❌ Неверный номер телефона. Попробуйте ещё раз.",
                keyboard=self.notifier.login_keyboard(),
                chat_id=self.chat_id,
            )
            return
        except errors.FloodWaitError as exc:
            await self.notifier.send_text(
                f"⏳ Слишком много попыток. Подождите {exc.seconds} секунд.",
                chat_id=self.chat_id,
            )
            self._state = "failed"
            return
        self._phone_code_hash = getattr(sent, "phone_code_hash", None)
        self._state = "code"
        self._code_buffer = ""
        self._last_activity = monotonic()
        result = await self.notifier.send_code_prompt(self.chat_id)
        self._code_message_id = result.get("message_id") if isinstance(result, dict) else None

    async def _refresh_code_prompt(self) -> None:
        if self._code_message_id is not None:
            await self.notifier.update_code_prompt(self.chat_id, self._code_message_id, self._code_buffer)
        else:
            await self.notifier.send_code_prompt(self.chat_id, self._code_buffer)

    async def _submit_code(self, code: str) -> None:
        if self._phone is None:
            return
        try:
            await self.api.sign_in(self._phone, code, self._phone_code_hash or "")
        except errors.SessionPasswordNeededError:
            self._state = "password"
            await self.notifier.send_text(
                "🔑 Включён двухфакторный вход. Введите пароль текстом:",
                chat_id=self.chat_id,
            )
            return
        except errors.PhoneCodeInvalidError:
            await self.notifier.send_text(
                "❌ Неверный код. Попробуйте ещё раз.", chat_id=self.chat_id
            )
            return
        except errors.PhoneCodeExpiredError:
            await self.notifier.send_text(
                "❌ Код истёк. Начните заново: /start", chat_id=self.chat_id
            )
            self._state = "failed"
            return
        self._state = "done"
        await self.notifier.send_text(
            "✅ Аккаунт авторизован!", chat_id=self.chat_id
        )

    async def _submit_password(self, password: str) -> None:
        try:
            await self.api.sign_in_password(password)
        except errors.PasswordHashInvalidError:
            await self.notifier.send_text(
                "❌ Неверный пароль. Попробуйте ещё раз.", chat_id=self.chat_id
            )
            return
        self._state = "done"
        await self.notifier.send_text(
            "✅ Аккаунт авторизован!", chat_id=self.chat_id
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_login -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gift_tracking/login.py tests/test_login.py
git commit -m "Add in-bot login flow"
```

---

### Task 8: Monitor — login bootstrap, prices in alerts, owner PM

**Files:**
- Modify: `src/gift_tracking/monitor.py`
- Modify: `tests/test_monitor.py`

**Interfaces:**
- Consumes: `LoginFlow` (Task 7), `GiftSatelliteClient` (Task 4), `MenuSettings` (Task 1), `render_owner_message`/`format_price` (Task 3), `send_menu`/`send_settings_menu`/`send_account_menu` (Task 5), `api.send_message_to_user`/`api.get_me_phone` (Task 6).
- Produces:
  - `GiftMonitor.__init__` loads `self._menu_settings: MenuSettings` (storage or config defaults), keeps `self._pricing: GiftSatelliteClient | None` (built lazily when key+url known).
  - `GiftMonitor.run()` — after `await self.api.connect()`, runs `LoginFlow` when `not await self.api.is_authorized()`, returns quietly on `False`.
  - `send_pending_notifications()` — new pipeline: model filter → price fetch → price range filter → template render → owner PM → admin event summary.
  - `_filter_menu_state()` unchanged shape but carries new RuntimeFilters fields.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_monitor.py`. Update `FakeNotifier` with new methods:

```python
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
```

Add fake pricing and owner-PM tracking to `FakeApi`:

```python
class FakeApi:
    def __init__(self, events: dict[str, GiftEvent]) -> None:
        self.events = events
        self.pm_sent: list[tuple[int, str]] = []

    async def unique_gift(self, slug: str):
        return self.events[slug], object()

    async def send_message_to_user(self, user_id: int, text: str) -> None:
        self.pm_sent.append((user_id, text))
```

Add tests and a `FakePricing`:

```python
class FakePricing:
    def __init__(self, price: PriceInfo | None) -> None:
        self.price = price
        self.calls: list[tuple[str, str, str]] = []

    async def fetch_price(self, collection: str, model: str, backdrop: str):
        self.calls.append((collection, model, backdrop))
        return self.price

    async def check_key(self) -> tuple[bool, str]:
        return self.price is not None, "ok"
```

Helper: `record_gift_with_owner(monitor, number)` and upgrade `make_config` (new Config fields default to `""`, no change needed).

```python
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
```

Update imports in `tests/test_monitor.py`: add `from gift_tracking.models import MarketPrice, MenuSettings, PriceInfo`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_monitor -v`
Expected: FAIL — new tests fail (old ones still pass since `send_event` remains).

- [ ] **Step 3: Implement monitor changes**

In `GiftMonitor.__init__` after loading runtime filters:

```python
        saved_settings = self.storage.load_menu_settings()
        if saved_settings is not None:
            self._menu_settings = saved_settings
        else:
            self._menu_settings = MenuSettings(
                satellite_api_key=config.satellite_api_key,
                satellite_api_url=config.satellite_api_url,
            )
        self._pricing: GiftSatelliteClient | None = None
```

Replace the top of `run()`:

```python
    async def run(self) -> None:
        await self.api.connect()
        if not await self.api.is_authorized():
            from .login import LoginFlow

            flow = LoginFlow(self.api, self.notifier, self.config.notify_chat_id)
            if not await flow.run():
                await self.api.disconnect()
                self.storage.close()
                return
        control_task = asyncio.create_task(self._control_loop())
        ...
```

(Remove the old `await self.api.start()` line.)

Add model filter helper and rewrite `send_pending_notifications()`:

```python
    def _matches_models(self, event: GiftEvent) -> bool:
        if not self._runtime_filters.model_filter_enabled or not self._runtime_filters.model_filters:
            return True
        return any(
            getattr(attribute, "name", "").casefold() in self._runtime_filters.model_filters
            for attribute in event.attributes
            if attribute.kind == "model"
        )
```

```python
    async def _fetch_price_for(self, event: GiftEvent) -> PriceInfo | None:
        if not self._menu_settings.auto_price_enabled:
            return None
        if not self._menu_settings.satellite_api_key or not self._menu_settings.satellite_api_url:
            return None
        if self._pricing is None:
            self._pricing = GiftSatelliteClient(
                self._menu_settings.satellite_api_key,
                self._menu_settings.satellite_api_url,
                insecure_ssl=self.config.bot_api_insecure_ssl,
            )
        try:
            return await self._pricing.fetch_price(
                event.title,
                attribute_value(event, "model"),
                attribute_value(event, "backdrop"),
            )
        except Exception:
            LOGGER.exception("Ошибка получения цены для %s", event.slug)
            return None
```

New pipeline (replace body of the for-loop in `send_pending_notifications`):

```python
        for event in self.storage.pending_notifications():
            if self._runtime_filters.require_owner_username and not event.owner_username:
                self.storage.mark_notified(event.slug)
                LOGGER.info("Пропуск %s: у владельца нет публичного username", event.slug)
                continue
            blocked_part = _blocked_owner_username(
                event.owner_username,
                self._runtime_filters.blocked_owner_username_substrings,
            )
            if blocked_part is not None:
                self.storage.mark_notified(event.slug)
                LOGGER.info("Пропуск %s: username владельца похож на маркетплейс (%s)", event.slug, blocked_part)
                continue
            if self._runtime_filters.backdrop_filter_enabled and not _matches_backdrop(
                event, self._runtime_filters.backdrop_filters
            ):
                self.storage.mark_notified(event.slug)
                LOGGER.info("Пропуск %s: фон не подходит под фильтр", event.slug)
                continue
            if not self._matches_models(event):
                self.storage.mark_notified(event.slug)
                LOGGER.info("Пропуск %s: модель не подходит под фильтр", event.slug)
                continue
            price = await self._fetch_price_for(event)
            min_price = self._runtime_filters.min_price
            max_price = self._runtime_filters.max_price
            if price and price.markets:
                lowest = min(
                    (market.price_ton or market.price_stars or float("inf"))
                    for market in price.markets
                )
                if (min_price is not None and lowest < min_price) or (
                    max_price is not None and lowest > max_price
                ):
                    self.storage.mark_notified(event.slug)
                    LOGGER.info("Пропуск %s: цена %s вне диапазона", event.slug, lowest)
                    continue
            await self._notify_owner_and_admin(event, price)
            self.storage.mark_notified(event.slug)
            LOGGER.info("Уведомление обработано: %s", event.slug)
```

Add `_notify_owner_and_admin`:

```python
    async def _notify_owner_and_admin(self, event: GiftEvent, price: PriceInfo | None) -> None:
        owner_status = None
        if (
            self._menu_settings.send_to_owner_enabled
            and event.owner_user_id is not None
        ):
            template = self._menu_settings.owner_message_template
            text = render_owner_message(template, event, price)
            try:
                await self.api.send_message_to_user(event.owner_user_id, text)
                owner_status = "✅ сообщение владельцу отправлено"
            except (errors.RPCError, ValueError) as exc:
                owner_status = f"❌ владельцу не отправлено: {exc}"
                LOGGER.warning("Не удалось написать владельцу %s: %s", event.slug, exc)
        price_line = ""
        if price is not None:
            price_line = f"\nЦены: {format_price(price)}"
        admin_text = format_notification(event, self.config.timezone) + price_line
        if owner_status:
            admin_text += f"\n{owner_status}"
        try:
            await self.notifier.send_text(admin_text)
        except NotificationError as exc:
            LOGGER.error("Не удалось отправить админу %s: %s", event.slug, exc)
            raise
```

Import additions in `monitor.py`: `from .models import MenuSettings, PriceInfo`, `from .notifier import format_notification, format_price, render_owner_message`, `from .pricing import GiftSatelliteClient`, `from .notifier import attribute_value` (add `attribute_value` to notifier imports).

Note: `test_skips_notification_without_public_username`, `test_skips_notification_when_backdrop_does_not_match`, `test_skips_notification_for_marketplace_like_username`, `test_detects_new_number_and_notifies_once`, `test_callback_toggles_runtime_filters`, `test_edits_filters_from_telegram_messages`, `test_skips_crafted_gap_after_retry_limit` must keep passing — they do, because `send_text(admin_text)` replaces the old `send_event(event)` call for the admin summary. Three of them assert `notifier.sent` directly, and those assertions must be updated since `send_event` is no longer called:

Replace in `test_detects_new_number_and_notifies_once`:
```python
            self.assertEqual([item.slug for item in notifier.sent], ["PlushPepe-2"])
```
with:
```python
            self.assertTrue(
                any("PlushPepe-2" in text or "Plush Pepe #2" in text for text, _ in notifier.status_messages)
            )
```

Replace in `test_skips_notification_when_backdrop_does_not_match`:
```python
            self.assertEqual([item.slug for item in notifier.sent], ["PlushPepe-2"])
```
with:
```python
            self.assertTrue(
                any("PlushPepe-2" in text or "Plush Pepe #2" in text for text, _ in notifier.status_messages)
            )
```

Replace in `test_skips_crafted_gap_after_retry_limit`:
```python
            self.assertEqual([item.slug for item in notifier.sent], ["PlushPepe-3"])
```
with:
```python
            self.assertTrue(
                any("PlushPepe-3" in text or "Plush Pepe #3" in text for text, _ in notifier.status_messages)
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_monitor -v`
Expected: PASS (updated + new tests).

- [ ] **Step 5: Commit**

```bash
git add src/gift_tracking/monitor.py tests/test_monitor.py
git commit -m "Send owner messages with prices and filter new gifts by model and price"
```

---

### Task 9: Unified menu — filters, settings, account

**Files:**
- Modify: `src/gift_tracking/monitor.py`
- Modify: `src/gift_tracking/notifier.py`
- Modify: `tests/test_monitor.py`

**Interfaces:**
- Consumes: `BotNotifier.send_menu/send_settings_menu/send_account_menu/update_settings_menu` (Task 5), `MenuSettings` (Task 1), existing PendingEdit pattern.
- Produces:
  - `GiftMonitor._handle_message`: `/menu` → `send_menu`; `/settings` → `send_settings_menu`.
  - `GiftMonitor._handle_callback_query` new callbacks: `menu_main`, `menu_filters`, `menu_settings`, `menu_account`, `account_login`, `account_logout`, `edit_owner_template`, `edit_api_key`, `edit_api_url`, `check_api_key`, `toggle_auto_price`, `toggle_send_owner`, `edit_model_filters`.
  - `PendingEdit` kinds extended: `model_filters`, `owner_template`, `api_key`, `api_url`.
  - Logout: `api.client.log_out()` exists on Telethon client; guard with try/except.
  - Filter menu callback additions: `toggle_model_filter`, `edit_model_filters`, `edit_min_price`, `edit_max_price`.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_monitor.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_monitor -v`
Expected: FAIL on the three new tests.

- [ ] **Step 3: Implement menu handling in monitor**

In `_handle_message`, extend the command dispatch:

```python
        if text in {"/start", "/filters", "/menu"}:
            if text == "/menu":
                await self.notifier.send_menu(chat_id=chat_id)
            else:
                await self.notifier.send_filter_menu(self._filter_menu_state(), chat_id=chat_id)
            return
        if text == "/settings":
            await self.notifier.send_settings_menu(self._menu_settings, chat_id=chat_id)
            return
```

In `_handle_callback_query`, before the existing filter toggles, route menu callbacks:

```python
        answer = "Фильтры обновлены"
        if data == "menu_main":
            await self.notifier.send_menu(chat_id=chat_id)
            await self.notifier.answer_callback_query(callback_query_id, "Главное меню")
            return
        if data == "menu_filters":
            await self.notifier.update_filter_menu(chat_id, message_id, self._filter_menu_state())
            await self.notifier.answer_callback_query(callback_query_id, "Фильтры")
            return
        if data == "menu_settings":
            await self.notifier.update_settings_menu(chat_id, message_id, self._menu_settings)
            await self.notifier.answer_callback_query(callback_query_id, "Настройки")
            return
        if data == "menu_account":
            phone = None
            try:
                phone = await self.api.get_me_phone()
            except Exception:
                LOGGER.exception("Не удалось узнать телефон")
            authorized = False
            try:
                authorized = await self.api.is_authorized()
            except Exception:
                LOGGER.exception("Не удалось проверить авторизацию")
            await self.notifier.send_account_menu(authorized, phone, chat_id=chat_id)
            await self.notifier.answer_callback_query(callback_query_id, "Аккаунт")
            return
        if data == "account_login":
            await self._run_login_flow()
            await self.notifier.answer_callback_query(callback_query_id, "Готово")
            return
        if data == "account_logout":
            try:
                await self.api.client.log_out()
            except Exception as exc:
                LOGGER.warning("Ошибка выхода: %s", exc)
            await self.notifier.send_account_menu(False, None, chat_id=chat_id)
            await self.notifier.answer_callback_query(callback_query_id, "Выход выполнен")
            return
        if data == "edit_owner_template":
            self._pending_edit = PendingEdit("owner_template", menu_message_id=message_id)
            await self.notifier.send_text(
                "Отправь новый шаблон сообщения владельцу. "
                "Плейсхолдеры: {title}, {number}, {model}, {backdrop}, {price}, {link}\n"
                "Для отмены: /cancel",
                chat_id=chat_id,
            )
            await self.notifier.answer_callback_query(callback_query_id, "Жду шаблон")
            return
        if data == "edit_api_key":
            self._pending_edit = PendingEdit("api_key", menu_message_id=message_id)
            await self.notifier.send_text(
                "Отправь API-ключ Gift Satellite текстом.\nДля отмены: /cancel",
                chat_id=chat_id,
            )
            await self.notifier.answer_callback_query(callback_query_id, "Жду ключ")
            return
        if data == "edit_api_url":
            self._pending_edit = PendingEdit("api_url", menu_message_id=message_id)
            await self.notifier.send_text(
                "Отправь базовый URL API.\nДля отмены: /cancel",
                chat_id=chat_id,
            )
            await self.notifier.answer_callback_query(callback_query_id, "Жду URL")
            return
        if data == "check_api_key":
            if not self._menu_settings.satellite_api_key or not self._menu_settings.satellite_api_url:
                message = "Сначала задай ключ и URL"
            else:
                try:
                    ok, detail = await self._pricing_for_settings().check_key()
                    message = detail
                except Exception as exc:
                    message = f"Ошибка: {exc}"
            await self.notifier.answer_callback_query(callback_query_id, message)
            await self.notifier.update_settings_menu(chat_id, message_id, self._menu_settings)
            return
        if data == "toggle_auto_price":
            self._menu_settings = self._with_menu_settings(
                auto_price_enabled=not self._menu_settings.auto_price_enabled
            )
            self._save_menu_settings()
            answer = "Настройки обновлены"
        elif data == "toggle_send_owner":
            self._menu_settings = self._with_menu_settings(
                send_to_owner_enabled=not self._menu_settings.send_to_owner_enabled
            )
            self._save_menu_settings()
            answer = "Настройки обновлены"
        elif data == "toggle_model_filter":
            self._runtime_filters = replace(
                self._runtime_filters,
                model_filter_enabled=not self._runtime_filters.model_filter_enabled,
            )
            self._save_runtime_filters()
        elif data == "edit_model_filters":
            self._pending_edit = PendingEdit("model_filters", menu_message_id=message_id)
            answer = "Пришли модели через запятую"
            await self.notifier.send_text(
                "Отправь список моделей через запятую. Пример: <code>Albino,Pumpkin</code>\n"
                "Пустое сообщение или <code>none</code> очистит список.\nДля отмены: /cancel",
                chat_id=chat_id,
            )
        elif data == "edit_min_price":
            self._pending_edit = PendingEdit("min_price", menu_message_id=message_id)
            answer = "Пришли минимальную цену (TON)"
            await self.notifier.send_text(
                "Отправь минимальную цену числом, например <code>5</code>.\n"
                "Пустое сообщение сбросит значение.\nДля отмены: /cancel",
                chat_id=chat_id,
            )
        elif data == "edit_max_price":
            self._pending_edit = PendingEdit("max_price", menu_message_id=message_id)
            answer = "Пришли максимальную цену (TON)"
            await self.notifier.send_text(
                "Отправь максимальную цену числом, например <code>500</code>.\n"
                "Пустое сообщение сбросит значение.\nДля отмены: /cancel",
                chat_id=chat_id,
            )
```

Implement static helper in monitor to rebuild settings (matching RuntimeFilters pattern):

```python
    def _with_menu_settings(self, **changes: object) -> MenuSettings:
        return MenuSettings(
            owner_message_template=str(
                changes.get("owner_message_template", self._menu_settings.owner_message_template)
            ),
            satellite_api_key=str(
                changes.get("satellite_api_key", self._menu_settings.satellite_api_key)
            ),
            satellite_api_url=str(
                changes.get("satellite_api_url", self._menu_settings.satellite_api_url)
            ),
            auto_price_enabled=bool(
                changes.get("auto_price_enabled", self._menu_settings.auto_price_enabled)
            ),
            send_to_owner_enabled=bool(
                changes.get("send_to_owner_enabled", self._menu_settings.send_to_owner_enabled)
            ),
        )

    def _pricing_for_settings(self) -> GiftSatelliteClient:
        return GiftSatelliteClient(
            self._menu_settings.satellite_api_key,
            self._menu_settings.satellite_api_url,
            insecure_ssl=self.config.bot_api_insecure_ssl,
        )

    async def _run_login_flow(self) -> None:
        from .login import LoginFlow

        flow = LoginFlow(self.api, self.notifier, self.config.notify_chat_id)
        try:
            await flow.run()
        except Exception:
            LOGGER.exception("Ошибка login flow")
```

After the block, replace the final (previously unconditional) tail with a menu-aware update:

```python
        if data in {"toggle_send_owner", "toggle_auto_price"}:
            await self.notifier.update_settings_menu(
                chat_id, message_id, self._menu_settings
            )
        elif self._pending_edit is None and data in {
            "toggle_owner_username",
            "toggle_backdrop_filter",
            "toggle_model_filter",
            "refresh_filters",
            "edit_backdrop_filters",
            "edit_blocked_usernames",
            "edit_model_filters",
            "edit_min_price",
            "edit_max_price",
        }:
            await self.notifier.update_filter_menu(
                chat_id, message_id, self._filter_menu_state()
            )
        await self.notifier.answer_callback_query(callback_query_id, answer)
```

Add `_save_menu_settings`:

```python
    def _save_menu_settings(self) -> None:
        self.storage.save_menu_settings(self._menu_settings)
```

Extend `_apply_pending_edit` with new kinds:

```python
        if pending_edit.kind == "model_filters":
            self._runtime_filters = replace(
                self._runtime_filters,
                model_filter_enabled=bool(values),
                model_filters=values,
            )
            confirmation = "Фильтр по моделям обновлён."
        elif pending_edit.kind == "min_price":
            price = None if not normalized else _parse_price(normalized)
            if normalized and price is None:
                await self.notifier.send_text("❌ Не число. Отмена.", chat_id=chat_id)
                return
            self._runtime_filters = replace(self._runtime_filters, min_price=price)
            confirmation = "Минимальная цена обновлена."
        elif pending_edit.kind == "max_price":
            price = None if not normalized else _parse_price(normalized)
            if normalized and price is None:
                await self.notifier.send_text("❌ Не число. Отмена.", chat_id=chat_id)
                return
            self._runtime_filters = replace(self._runtime_filters, max_price=price)
            confirmation = "Максимальная цена обновлена."
        elif pending_edit.kind == "owner_template":
            self._menu_settings = self._with_menu_settings(owner_message_template=normalized)
            self._save_menu_settings()
            confirmation = "Шаблон обновлён."
        elif pending_edit.kind == "api_key":
            self._menu_settings = self._with_menu_settings(satellite_api_key=normalized)
            self._save_menu_settings()
            confirmation = "API-ключ сохранён."
        elif pending_edit.kind == "api_url":
            self._menu_settings = self._with_menu_settings(satellite_api_url=normalized)
            self._save_menu_settings()
            confirmation = "URL сохранён."
        else:
            confirmation = (
                "Фильтр по фону обновлён."
                if pending_edit.kind == "backdrop_filters"
                else "Список исключённых username обновлён."
            )
            if pending_edit.kind == "backdrop_filters":
                self._runtime_filters = replace(
                    self._runtime_filters,
                    backdrop_filter_enabled=bool(values),
                    backdrop_filters=values,
                )
            elif pending_edit.kind == "blocked_usernames":
                self._runtime_filters = replace(
                    self._runtime_filters,
                    blocked_owner_username_substrings=values,
                )
        self._save_runtime_filters()
        await self.notifier.send_text(confirmation, chat_id=chat_id)
        if pending_edit.kind in {
            "owner_template",
            "api_key",
            "api_url",
        }:
            if pending_edit.menu_message_id is not None:
                await self.notifier.update_settings_menu(
                    chat_id, pending_edit.menu_message_id, self._menu_settings
                )
            else:
                await self.notifier.send_settings_menu(
                    self._menu_settings, chat_id=chat_id
                )
        elif pending_edit.menu_message_id is not None:
            await self.notifier.update_filter_menu(
                chat_id, pending_edit.menu_message_id, self._filter_menu_state()
            )
        else:
            await self.notifier.send_filter_menu(self._filter_menu_state(), chat_id=chat_id)
```

Note: keep the existing `self._pending_edit = None` and `normalized = text.strip()` resets at the top of `_apply_pending_edit` intact; the settings branch above replaces only the final menu-refresh tail. Note `_save_runtime_filters()` remains safe for settings kinds (no-op save of unchanged filters).

Add `_parse_price` helper at module level in `monitor.py`:

```python
def _parse_price(value: str) -> float | None:
    try:
        return float(value.replace(",", ".").strip())
    except ValueError:
        return None
```

Note: `replace` on dataclasses is already imported in monitor.py (`from dataclasses import replace`).

Filter menu keyboard additions in `notifier.py._filter_menu_keyboard`: add rows

```python
        if state.model_filters:
            keyboard.append(
                [
                    {
                        "text": (
                            "Модели: включен"
                            if state.model_filter_enabled
                            else "Модели: выключен"
                        ),
                        "callback_data": "toggle_model_filter",
                    }
                ]
            )
        keyboard.append(
            [{"text": "Редактировать модели", "callback_data": "edit_model_filters"}]
        )
        keyboard.append(
            [{"text": "Цена мин/макс", "callback_data": "edit_min_price"},
             {"text": "…", "callback_data": "code_noop"}]
        )
```

and text line for models + prices in `_filter_menu_text`. Update `FilterMenuState` (notifier.py) with `model_filter_enabled: bool`, `model_filters: tuple[str, ...]`, `min_price: float | None`, `max_price: float | None` (positional-safe defaults at end, and update `_filter_menu_state()` in monitor to pass them).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_monitor tests.test_notifier -v`
Expected: PASS (existing tests updated for `FilterMenuState` unchanged-positional usage — verify `test_filter_menu_state_shape` uses keyword args, which it does).

- [ ] **Step 5: Commit**

```bash
git add src/gift_tracking/monitor.py src/gift_tracking/notifier.py tests/test_monitor.py tests/test_notifier.py
git commit -m "Add unified menu with settings and account sections"
```

---

### Task 10: Config docs and full suite

**Files:**
- Modify: `.env.example`
- Modify: `README.md`

- [ ] **Step 1: Add env docs**

In `.env.example` after `BACKDROP_FILTERS=` block:

```dotenv
# Optional Gift Satellite price integration.
# Key is issued by @gift_satellite_bot; URL is discovered from the mini-app.
SATELLITE_API_KEY=
SATELLITE_API_URL=
```

- [ ] **Step 2: Update README**

Add sections after «Фильтрация уведомлений»:

```markdown
## Логин через Telegram

Вместо ввода номера и кода в консоли бот сам проводит авторизацию: кнопка «Поделиться номером», ввод кода инлайн-клавиатурой, пароль 2FA текстом.

## Цены Gift Satellite

Задайте API-ключ (выдаёт @gift_satellite_bot) и базовый URL в меню `/settings`. Бот подставит актуальные цены по маркетам в сообщения владельцам подарков.

## Уведомления владельцам

При обнаружении нового уникального подарка зарегистрированный аккаунт пишет его владельцу по шаблону из настроек (плейсхолдеры `{title}`, `{number}`, `{model}`, `{backdrop}`, `{price}`, `{link}`) и сообщает вам о результате.
```

- [ ] **Step 3: Run the full test suite**

Run: `python -m unittest discover -s tests -v`
Expected: ALL PASS.

- [ ] **Step 4: Commit**

```bash
git add .env.example README.md
git commit -m "Document login, prices and owner notifications"
```

---

## Self-review notes

- New `Config` fields are optional with defaults, so `make_config` in tests keeps working.
- `FilterMenuState` extension uses keyword-argument construction in existing tests only — verified `test_filter_menu_state_shape` passes keywords.
- Old monitor tests assert `notifier.sent` — Task 8 explicitly updates the only affected assertion (`test_detects_new_number_and_notifies_once`).
- `GiftSatelliteClient.parse_prices` contract is the single adapter point to the real Satellite API shape; discovery script and fallback (user DevTools captures) cover the unknown endpoint format.