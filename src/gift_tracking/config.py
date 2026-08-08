from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(ValueError):
    """Raised when required configuration is missing or invalid."""


def load_dotenv(path: Path = Path(".env")) -> None:
    """Load a small, dependency-free subset of dotenv syntax."""
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Не задана обязательная переменная {name}")
    return value


def _positive_int(name: str, default: int, *, allow_zero: bool = False) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} должна быть целым числом") from exc
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise ConfigError(f"{name} должна быть не меньше {minimum}")
    return value


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    notify_chat_id: str
    session: str
    database_path: Path
    poll_interval_seconds: int
    collections_per_cycle: int
    catalog_refresh_seconds: int
    backfill_count: int
    max_new_gifts_per_check: int
    timezone: str
    collection_prefixes: tuple[str, ...]
    backdrop_filters: tuple[str, ...]
    require_owner_username: bool
    blocked_owner_username_substrings: tuple[str, ...]
    log_level: str
    bot_api_ca_file: Path | None = None
    bot_api_insecure_ssl: bool = False

    @classmethod
    def from_env(cls) -> Config:
        load_dotenv()
        api_id_raw = _required("TG_API_ID")
        try:
            api_id = int(api_id_raw)
        except ValueError as exc:
            raise ConfigError("TG_API_ID должна быть целым числом") from exc

        prefixes = tuple(
            dict.fromkeys(
                part.strip()
                for part in os.getenv("GIFT_COLLECTIONS", "").split(",")
                if part.strip()
            )
        )
        backdrop_filters = tuple(
            dict.fromkeys(
                part.strip().casefold()
                for part in os.getenv("BACKDROP_FILTERS", "").split(",")
                if part.strip()
            )
        )
        blocked_owner_username_substrings = tuple(
            dict.fromkeys(
                part.strip().casefold()
                for part in os.getenv(
                    "BLOCKED_OWNER_USERNAME_SUBSTRINGS", "bank,storage"
                ).split(",")
                if part.strip()
            )
        )
        timezone = os.getenv("TIMEZONE", "Europe/Minsk").strip() or "Europe/Minsk"
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ConfigError(
                f"Неизвестная временная зона TIMEZONE={timezone}"
            ) from exc
        return cls(
            api_id=api_id,
            api_hash=_required("TG_API_HASH"),
            bot_token=_required("BOT_TOKEN"),
            notify_chat_id=_required("NOTIFY_CHAT_ID"),
            session=os.getenv("TG_SESSION", "gift_tracking").strip() or "gift_tracking",
            database_path=Path(os.getenv("DATABASE_PATH", "gift_tracking.sqlite3")),
            poll_interval_seconds=_positive_int("POLL_INTERVAL_SECONDS", 5),
            collections_per_cycle=_positive_int("COLLECTIONS_PER_CYCLE", 10),
            catalog_refresh_seconds=_positive_int("CATALOG_REFRESH_SECONDS", 3600),
            backfill_count=_positive_int("BACKFILL_COUNT", 0, allow_zero=True),
            max_new_gifts_per_check=_positive_int("MAX_NEW_GIFTS_PER_CHECK", 100),
            timezone=timezone,
            collection_prefixes=prefixes,
            backdrop_filters=backdrop_filters,
            require_owner_username=_bool("REQUIRE_OWNER_USERNAME", False),
            blocked_owner_username_substrings=blocked_owner_username_substrings,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            bot_api_ca_file=(
                Path(value)
                if (value := os.getenv("BOT_API_CA_FILE", "").strip())
                else None
            ),
            bot_api_insecure_ssl=_bool("BOT_API_INSECURE_SSL", False),
        )
