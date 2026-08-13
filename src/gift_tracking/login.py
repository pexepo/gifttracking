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