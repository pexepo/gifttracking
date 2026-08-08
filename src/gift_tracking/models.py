from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


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
        )


@dataclass(frozen=True, slots=True)
class RuntimeFilters:
    require_owner_username: bool
    backdrop_filter_enabled: bool
    backdrop_filters: tuple[str, ...]
    blocked_owner_username_substrings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RuntimeFilters:
        return cls(
            require_owner_username=bool(data.get("require_owner_username", False)),
            backdrop_filter_enabled=bool(data.get("backdrop_filter_enabled", False)),
            backdrop_filters=tuple(data.get("backdrop_filters", [])),
            blocked_owner_username_substrings=tuple(
                data.get("blocked_owner_username_substrings", [])
            ),
        )
