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

from .models import GiftEvent

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
                        details = f"{details}: {description}"
                    else:
                        details = f"{details}: {parsed}"
            raise NotificationError(f"Ошибка Bot API: {details}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise NotificationError(f"Ошибка Bot API: {exc}") from exc
        if not body.get("ok"):
            raise NotificationError(f"Bot API отклонил сообщение: {body}")
        return body.get("result", {})
