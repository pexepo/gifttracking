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