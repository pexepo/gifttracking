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
        self.assertEqual(filters.blacklisted_collections, ())
        self.assertTrue(filters.notifications_enabled)
        self.assertIsNone(filters.min_price)
        self.assertIsNone(filters.max_price)

    def test_round_trip_with_new_fields(self) -> None:
        filters = RuntimeFilters(
            require_owner_username=True,
            backdrop_filter_enabled=True,
            backdrop_filters=("coral red",),
            blocked_owner_username_substrings=("bank",),
            notifications_enabled=False,
            blacklisted_collections=("Plush Pepe",),
            min_price=1.5,
            max_price=100.0,
        )
        self.assertEqual(RuntimeFilters.from_dict(filters.to_dict()), filters)

    def test_runtime_filters_new_shape(self) -> None:
        state = RuntimeFilters(
            require_owner_username=True,
            backdrop_filter_enabled=False,
            backdrop_filters=("Coral Red",),
            blocked_owner_username_substrings=("bank",),
            blacklisted_collections=("Plush Pepe",),
        )
        self.assertTrue(state.notifications_enabled)
        self.assertEqual(state.blacklisted_collections, ("Plush Pepe",))
        self.assertNotIn("model_filter_enabled", state.to_dict())
        self.assertNotIn("model_filters", state.to_dict())

    def test_runtime_filters_from_dict_backward_compatible(self) -> None:
        state = RuntimeFilters.from_dict(
            {
                "require_owner_username": True,
                "backdrop_filter_enabled": False,
                "backdrop_filters": [],
                "blocked_owner_username_substrings": ["bank"],
                "model_filter_enabled": True,
                "model_filters": ["Albino"],
            }
        )
        self.assertEqual(state.blacklisted_collections, ())
        self.assertTrue(state.notifications_enabled)
        self.assertEqual(RuntimeFilters.from_dict({}).notifications_enabled, True)


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