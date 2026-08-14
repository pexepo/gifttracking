from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, ClassVar


@dataclass(frozen=True, slots=True)
class Collection:
    gift_id: int
    title: str
    slug_prefix: str
    last_issued: int | None = None


@dataclass(frozen=True, slots=True)
class Attribute:
    kind: str
    name: str
    rarity: str | None = None


@dataclass(frozen=True, slots=True)
class GiftEvent:
    slug: str
    gift_id: int
    title: str
    number: int
    link: str
    owner_name: str | None
    owner_username: str | None
    owner_address: str | None
    attributes: tuple[Attribute, ...]
    availability_issued: int
    availability_total: int
    detected_at: datetime
    owner_user_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["detected_at"] = self.detected_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GiftEvent:
        return cls(
            slug=data["slug"],
            gift_id=int(data["gift_id"]),
            title=data["title"],
            number=int(data["number"]),
            link=data["link"],
            owner_name=data.get("owner_name"),
            owner_username=data.get("owner_username"),
            owner_address=data.get("owner_address"),
            attributes=tuple(Attribute(**item) for item in data.get("attributes", [])),
            availability_issued=int(data["availability_issued"]),
            availability_total=int(data["availability_total"]),
            detected_at=datetime.fromisoformat(data["detected_at"]),
            owner_user_id=data.get("owner_user_id"),
        )


@dataclass(frozen=True, slots=True)
class RuntimeFilters:
    require_owner_username: bool
    backdrop_filter_enabled: bool
    backdrop_filters: tuple[str, ...]
    blocked_owner_username_substrings: tuple[str, ...]
    notifications_enabled: bool = True
    blacklisted_collections: tuple[str, ...] = ()
    min_price: float | None = None
    max_price: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

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
            notifications_enabled=bool(data.get("notifications_enabled", True)),
            blacklisted_collections=tuple(data.get("blacklisted_collections", [])),
            min_price=optional_float(data.get("min_price")),
            max_price=optional_float(data.get("max_price")),
        )


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
    DEFAULT_OWNER_TEMPLATE: ClassVar[str] = DEFAULT_OWNER_TEMPLATE
    MAX_OWNER_TEMPLATES: ClassVar[int] = 10
    owner_message_templates: tuple[str, ...] = (DEFAULT_OWNER_TEMPLATE,)
    satellite_api_key: str = ""
    satellite_api_url: str = ""
    auto_price_enabled: bool = True
    send_to_owner_enabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MenuSettings:
        templates = data.get("owner_message_templates")
        if not templates:
            legacy = data.get("owner_message_template")
            if legacy:
                templates = (str(legacy),)
        if not templates:
            templates = (DEFAULT_OWNER_TEMPLATE,)
        return cls(
            owner_message_templates=tuple(
                str(template).strip() for template in templates if str(template).strip()
            )
            or (DEFAULT_OWNER_TEMPLATE,),
            satellite_api_key=str(data.get("satellite_api_key", "")),
            satellite_api_url=str(data.get("satellite_api_url", "")),
            auto_price_enabled=bool(data.get("auto_price_enabled", True)),
            send_to_owner_enabled=bool(data.get("send_to_owner_enabled", False)),
        )
