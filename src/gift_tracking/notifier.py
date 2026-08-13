from __future__ import annotations

import asyncio
import html
import json
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import GiftEvent, MarketPrice, MenuSettings, PriceInfo

ATTRIBUTE_LABELS = {
    "model": "Модель",
    "backdrop": "Фон",
    "symbol": "Символ",
}


class NotificationError(RuntimeError):
    pass


class BotLongPollTimeout(NotificationError):
    pass


@dataclass(frozen=True, slots=True)
class FilterMenuState:
    owner_username_required: bool
    backdrop_filter_enabled: bool
    backdrop_filters: tuple[str, ...]
    blocked_owner_username_substrings: tuple[str, ...]


def format_notification(event: GiftEvent, timezone: str) -> str:
    local_time = event.detected_at.astimezone(ZoneInfo(timezone))
    owner = html.escape(event.owner_name or "скрыт")
    if event.owner_username:
        owner = f"{owner} (@{html.escape(event.owner_username)})"
    elif event.owner_address:
        owner = f"{owner} (TON: <code>{html.escape(event.owner_address)}</code>)"

    lines = [
        "🎁 <b>Новый уникальный подарок</b>",
        "",
        f"<b>{html.escape(event.title)} #{event.number}</b>",
        f"Владелец: {owner}",
    ]
    for attribute in event.attributes:
        label = ATTRIBUTE_LABELS.get(attribute.kind, attribute.kind.capitalize())
        rarity = f" · {html.escape(attribute.rarity)}" if attribute.rarity else ""
        lines.append(f"{label}: <b>{html.escape(attribute.name)}</b>{rarity}")
    lines.extend(
        [
            f"Выпущено: {event.availability_issued}/{event.availability_total}",
            f"Обнаружен: <b>{local_time:%Y-%m-%d %H:%M:%S %Z}</b>",
            "",
            html.escape(event.link),
        ]
    )
    return "\n".join(lines)


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


class BotNotifier:
    def __init__(
        self,
        token: str,
        chat_id: str,
        timezone: str,
        *,
        ca_file: Path | None = None,
        insecure_ssl: bool = False,
    ) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self.timezone = timezone
        self.ssl_context = self._build_ssl_context(
            ca_file=ca_file, insecure_ssl=insecure_ssl
        )
        self._update_offset = 0

    @staticmethod
    def _build_ssl_context(
        *, ca_file: Path | None, insecure_ssl: bool
    ) -> ssl.SSLContext:
        if insecure_ssl:
            return ssl._create_unverified_context()
        if ca_file is not None:
            return ssl.create_default_context(cafile=str(ca_file))
        return ssl.create_default_context()

    async def send_event(self, event: GiftEvent) -> None:
        keyboard = None
        if event.owner_username:
            keyboard = {
                "inline_keyboard": [
                    [
                        {
                            "text": "Написать владельцу",
                            "url": f"https://t.me/{event.owner_username}",
                        }
                    ]
                ]
            }
        await self.send_text(format_notification(event, self.timezone), keyboard=keyboard)

    async def send_text(
        self,
        text: str,
        *,
        keyboard: dict[str, object] | None = None,
        chat_id: str | None = None,
    ) -> dict[str, object]:
        payload = {
            "chat_id": chat_id or self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
        if keyboard is not None:
            payload["reply_markup"] = keyboard
        return await asyncio.to_thread(self._call, "sendMessage", payload)

    async def send_filter_menu(
        self, state: FilterMenuState, *, chat_id: str | None = None
    ) -> dict[str, object]:
        return await self.send_text(
            self._filter_menu_text(state),
            keyboard=self._filter_menu_keyboard(state),
            chat_id=chat_id,
        )

    async def update_filter_menu(
        self, chat_id: str, message_id: int, state: FilterMenuState
    ) -> None:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": self._filter_menu_text(state),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": self._filter_menu_keyboard(state),
        }
        await asyncio.to_thread(self._call, "editMessageText", payload)

    async def answer_callback_query(self, callback_query_id: str, text: str) -> None:
        await asyncio.to_thread(
            self._call,
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text},
        )

    async def get_updates(self, timeout: int = 30) -> list[dict[str, object]]:
        payload = {"timeout": timeout}
        if self._update_offset:
            payload["offset"] = self._update_offset
        result = await asyncio.to_thread(self._call, "getUpdates", payload, timeout + 10)
        updates = [item for item in result if isinstance(item, dict)]
        if updates:
            self._update_offset = max(int(item["update_id"]) for item in updates) + 1
        return updates

    @staticmethod
    def _filter_menu_text(state: FilterMenuState) -> str:
        backdrop_line = "выключен"
        if state.backdrop_filters:
            names = ", ".join(state.backdrop_filters)
            backdrop_line = (
                f"включен: <code>{html.escape(names)}</code>"
                if state.backdrop_filter_enabled
                else f"на паузе: <code>{html.escape(names)}</code>"
            )
        return "\n".join(
            [
                "⚙️ <b>Фильтры Gift Tracking</b>",
                "",
                "ЛС владельцу: "
                + (
                    "<b>только с публичным @username</b>"
                    if state.owner_username_required
                    else "<b>без ограничения</b>"
                ),
                "Исключённые username: <b>"
                + (
                    html.escape(", ".join(state.blocked_owner_username_substrings))
                    if state.blocked_owner_username_substrings
                    else "нет"
                )
                + "</b>",
                f"Фон: <b>{backdrop_line}</b>",
            ]
        )

    @staticmethod
    def _filter_menu_keyboard(state: FilterMenuState) -> dict[str, object]:
        keyboard = [
            [
                {
                    "text": (
                        "ЛС: только @username"
                        if state.owner_username_required
                        else "ЛС: все владельцы"
                    ),
                    "callback_data": "toggle_owner_username",
                }
            ]
        ]
        if state.backdrop_filters:
            keyboard.append(
                [
                    {
                        "text": (
                            "Фон: включен"
                            if state.backdrop_filter_enabled
                            else "Фон: выключен"
                        ),
                        "callback_data": "toggle_backdrop_filter",
                    }
                ]
            )
        keyboard.append(
            [{"text": "Редактировать фоны", "callback_data": "edit_backdrop_filters"}]
        )
        keyboard.append(
            [
                {
                    "text": "Редактировать blacklist username",
                    "callback_data": "edit_blocked_usernames",
                }
            ]
        )
        keyboard.append([{"text": "Обновить", "callback_data": "refresh_filters"}])
        return {"inline_keyboard": keyboard}

    @staticmethod
    def code_keyboard(buffer: str) -> dict[str, object]:
        rows = [[{"text": digit, "callback_data": f"code_digit_{digit}"} for digit in row] for row in ("123", "456", "789")]
        empty_row = [
            {"text": "⌫", "callback_data": "code_backspace"},
            {"text": "0", "callback_data": "code_digit_0"},
            {"text": "Отправить", "callback_data": "code_submit"},
        ]
        if buffer:
            display = [{"text": buffer, "callback_data": "code_noop"}]
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
        return {
            "inline_keyboard": [
                [{"text": "✏️ Шаблон сообщения", "callback_data": "edit_owner_template"}],
                [{"text": "🔑 API-ключ", "callback_data": "edit_api_key"}],
                [{"text": "⚠️ Проверить ключ", "callback_data": "check_api_key"}],
                [{"text": "🌐 URL API", "callback_data": "edit_api_url"}],
                [{"text": f"{price_state} Авто-цена", "callback_data": "toggle_auto_price"}],
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

    def _call(
        self, method: str, payload: dict[str, object], timeout: int = 20
    ) -> dict[str, object] | list[object]:
        request = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=timeout, context=self.ssl_context
            ) as response:
                body = json.loads(response.read().decode("utf-8"))
        except TimeoutError as exc:
            if method == "getUpdates":
                raise BotLongPollTimeout("Long poll timeout") from exc
            raise NotificationError(f"Ошибка Bot API: {exc}") from exc
        except socket.timeout as exc:
            if method == "getUpdates":
                raise BotLongPollTimeout("Long poll timeout") from exc
            raise NotificationError(f"Ошибка Bot API: {exc}") from exc
        except urllib.error.HTTPError as exc:
            details = str(exc)
            try:
                error_body = exc.read().decode("utf-8")
            except Exception:
                error_body = ""
            if error_body:
                try:
                    parsed = json.loads(error_body)
                except json.JSONDecodeError:
                    details = f"{details}: {error_body}"
                else:
                    description = parsed.get("description")
                    if description:
                        if (
                            method == "editMessageText"
                            and "message is not modified" in description.casefold()
                        ):
                            return {}
                        details = f"{details}: {description}"
                    else:
                        details = f"{details}: {parsed}"
            raise NotificationError(f"Ошибка Bot API: {details}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise NotificationError(f"Ошибка Bot API: {exc}") from exc
        if not body.get("ok"):
            raise NotificationError(f"Bot API отклонил сообщение: {body}")
        return body.get("result", {})
