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
        timeout: int = 8,
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
        def quoted(value: str) -> str:
            return urllib.parse.quote(value, safe="")

        return (
            f"{self.base_url}/prices?"
            f"collection={quoted(collection)}&model={quoted(model)}&backdrop={quoted(backdrop)}"
        )

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
        except urllib.error.HTTPError as exc:
            raise SatellitePricingError(f"HTTP {exc.code}: {exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, json.JSONDecodeError) as exc:
            raise SatellitePricingError(f"Сетевая ошибка: {exc}") from exc