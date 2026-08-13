from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from dataclasses import replace
from time import monotonic

from telethon import errors

from .config import Config
from .models import Collection, GiftEvent, MenuSettings, PriceInfo, RuntimeFilters
from .notifier import (
    BotLongPollTimeout,
    BotNotifier,
    FilterMenuState,
    NotificationError,
    attribute_value,
    format_notification,
    format_price,
    render_owner_message,
)
from .pricing import GiftSatelliteClient
from .storage import Storage
from .telegram_api import GiftTelegramApi

LOGGER = logging.getLogger(__name__)
MISSING_GIFT_RETRY_LIMIT = 3


def _matches_backdrop(event, allowed_backdrops: tuple[str, ...]) -> bool:
    if not allowed_backdrops:
        return True
    for attribute in event.attributes:
        if attribute.kind == "backdrop" and attribute.name.casefold() in allowed_backdrops:
            return True
    return False


def _blocked_owner_username(
    owner_username: str | None, blocked_substrings: tuple[str, ...]
) -> str | None:
    if not owner_username:
        return None
    username = owner_username.casefold()
    for part in blocked_substrings:
        if part in username:
            return part
    return None


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(part.strip().casefold() for part in value.split(",") if part.strip())
    )


def _parse_price(value: str) -> float | None:
    try:
        return float(value.replace(",", ".").strip())
    except ValueError:
        return None


@dataclass(slots=True)
class PendingEdit:
    kind: str
    menu_message_id: int | None = None


class GiftMonitor:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.storage = Storage(config.database_path)
        self.api = GiftTelegramApi(config.api_id, config.api_hash, config.session)
        self.notifier = BotNotifier(
            config.bot_token,
            config.notify_chat_id,
            config.timezone,
            ca_file=config.bot_api_ca_file,
            insecure_ssl=config.bot_api_insecure_ssl,
        )
        self._last_catalog_refresh = 0.0
        self._cursor = 0
        saved_filters = self.storage.load_runtime_filters()
        self._runtime_filters = saved_filters or RuntimeFilters(
            require_owner_username=config.require_owner_username,
            backdrop_filter_enabled=bool(config.backdrop_filters),
            backdrop_filters=config.backdrop_filters,
            blocked_owner_username_substrings=config.blocked_owner_username_substrings,
        )
        saved_settings = self.storage.load_menu_settings()
        if saved_settings is not None:
            self._menu_settings = saved_settings
        else:
            self._menu_settings = MenuSettings(
                satellite_api_key=config.satellite_api_key,
                satellite_api_url=config.satellite_api_url,
            )
        self._pricing: GiftSatelliteClient | None = None
        self._pricing_credentials: tuple[str, str] | None = None
        self._pending_edit: PendingEdit | None = None

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
        try:
            startup_notified = False
            while True:
                try:
                    if (
                        self._last_catalog_refresh == 0
                        or monotonic() - self._last_catalog_refresh
                        >= self.config.catalog_refresh_seconds
                    ):
                        await self.refresh_catalog()
                    await self.send_pending_notifications()
                    if not startup_notified:
                        collections = self.storage.list_collections()
                        await self._safe_status_message(
                            "✅ <b>Gift Tracking запущен</b>\n"
                            f"Коллекций в мониторинге: {len(collections)}"
                        )
                        await self._safe_filter_menu()
                        startup_notified = True
                    await self.check_next_batch()
                    await self.send_pending_notifications()
                except errors.RPCError:
                    LOGGER.exception("Временная ошибка Telegram")
                except (TimeoutError, OSError):
                    LOGGER.exception("Временная сетевая ошибка")
                await asyncio.sleep(self.config.poll_interval_seconds)
        finally:
            control_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await control_task
            await self.api.disconnect()
            self.storage.close()

    async def refresh_catalog(self) -> None:
        LOGGER.info("Обновление каталога подарков")
        if self.config.collection_prefixes:
            await self._add_configured_collections()
        else:
            for candidate in await self.api.catalog():
                try:
                    anchor, _ = await self.api.unique_gift(f"{candidate.slug_prefix}-1")
                except errors.RPCError as exc:
                    LOGGER.debug(
                        "Коллекция %s пока не unique: %s", candidate.title, exc
                    )
                    continue
                actual_prefix = anchor.slug.rsplit("-", 1)[0]
                self.storage.upsert_collection(
                    replace(candidate, slug_prefix=actual_prefix)
                )
        self._last_catalog_refresh = monotonic()
        LOGGER.info("Активных коллекций: %s", len(self.storage.list_collections()))

    async def _add_configured_collections(self) -> None:
        for prefix in self.config.collection_prefixes:
            try:
                anchor, _ = await self.api.unique_gift(f"{prefix}-1")
            except errors.RPCError as exc:
                LOGGER.warning("Не удалось открыть коллекцию %s: %s", prefix, exc)
                continue
            self.storage.upsert_collection(
                Collection(anchor.gift_id, anchor.title, anchor.slug.rsplit("-", 1)[0])
            )

    async def check_next_batch(self) -> None:
        collections = self.storage.list_collections()
        if not collections:
            LOGGER.warning("Нет активных unique-коллекций")
            return
        start = self._cursor % len(collections)
        count = min(self.config.collections_per_cycle, len(collections))
        batch = [
            collections[(start + index) % len(collections)] for index in range(count)
        ]
        self._cursor = (start + count) % len(collections)
        for collection in batch:
            try:
                await self.check_collection(collection)
            except errors.RPCError as exc:
                LOGGER.warning(
                    "Ошибка Telegram для %s: %s", collection.slug_prefix, exc
                )
            except (TimeoutError, OSError) as exc:
                LOGGER.warning("Сетевая ошибка для %s: %s", collection.slug_prefix, exc)

    async def check_collection(self, collection: Collection) -> None:
        anchor, _ = await self.api.unique_gift(f"{collection.slug_prefix}-1")
        current = anchor.availability_issued
        if collection.last_issued is None:
            await self._initialize_collection(collection, current)
            return
        if current <= collection.last_issued:
            return

        end = min(
            current,
            collection.last_issued + self.config.max_new_gifts_per_check,
        )
        LOGGER.info(
            "%s: новые номера %s..%s",
            collection.slug_prefix,
            collection.last_issued + 1,
            end,
        )
        for number in range(collection.last_issued + 1, end + 1):
            slug = f"{collection.slug_prefix}-{number}"
            try:
                event, _ = await self.api.unique_gift(slug)
            except errors.RPCError:
                attempts = self.storage.increment_missing_gift_attempt(
                    collection.gift_id, slug, number
                )
                if attempts >= MISSING_GIFT_RETRY_LIMIT:
                    self.storage.set_last_issued(collection.gift_id, number)
                    self.storage.clear_missing_gift(slug)
                    LOGGER.info(
                        "%s пропущен после %s попыток: номер, вероятно, ушел в craft или недоступен",
                        slug,
                        attempts,
                    )
                    continue
                # The issued counter can become visible shortly before the gift itself.
                LOGGER.info("%s еще не доступен, повторю позже (%s/%s)", slug, attempts, MISSING_GIFT_RETRY_LIMIT)
                break
            self.storage.clear_missing_gift(slug)
            self.storage.record_gift(event)
            self.storage.set_last_issued(collection.gift_id, number)

    async def _initialize_collection(
        self, collection: Collection, current: int
    ) -> None:
        if self.config.backfill_count == 0:
            self.storage.set_last_issued(collection.gift_id, current)
            LOGGER.info("%s: базовый номер %s", collection.slug_prefix, current)
            return

        start = max(1, current - self.config.backfill_count + 1)
        self.storage.set_last_issued(collection.gift_id, start - 1)
        initialized = replace(collection, last_issued=start - 1)
        await self.check_collection(initialized)

    def _matches_models(self, event: GiftEvent) -> bool:
        if not self._runtime_filters.model_filter_enabled or not self._runtime_filters.model_filters:
            return True
        return any(
            getattr(attribute, "name", "").casefold() in self._runtime_filters.model_filters
            for attribute in event.attributes
            if attribute.kind == "model"
        )

    async def _fetch_price_for(self, event: GiftEvent) -> PriceInfo | None:
        if not self._menu_settings.auto_price_enabled:
            return None
        if not self._menu_settings.satellite_api_key or not self._menu_settings.satellite_api_url:
            return None
        if self._pricing is None or (
            self._pricing_credentials is not None
            and self._pricing_credentials
            != (
                self._menu_settings.satellite_api_key,
                self._menu_settings.satellite_api_url,
            )
        ):
            self._pricing = GiftSatelliteClient(
                self._menu_settings.satellite_api_key,
                self._menu_settings.satellite_api_url,
                insecure_ssl=self.config.bot_api_insecure_ssl,
            )
            self._pricing_credentials = (
                self._menu_settings.satellite_api_key,
                self._menu_settings.satellite_api_url,
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

    async def send_pending_notifications(self) -> None:
        pending: list[GiftEvent] = []
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
            pending.append(event)
        prices = await asyncio.gather(*(self._fetch_price_for(event) for event in pending))
        for event, price in zip(pending, prices):
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

    async def _safe_status_message(self, text: str) -> None:
        try:
            await self.notifier.send_text(text)
        except NotificationError as exc:
            LOGGER.error("Стартовое уведомление не отправлено: %s", exc)

    async def _safe_filter_menu(self) -> None:
        try:
            await self.notifier.send_filter_menu(self._filter_menu_state())
        except NotificationError as exc:
            LOGGER.error("Меню фильтров не отправлено: %s", exc)

    def _filter_menu_state(self) -> FilterMenuState:
        return FilterMenuState(
            owner_username_required=self._runtime_filters.require_owner_username,
            backdrop_filter_enabled=self._runtime_filters.backdrop_filter_enabled,
            backdrop_filters=self._runtime_filters.backdrop_filters,
            blocked_owner_username_substrings=self._runtime_filters.blocked_owner_username_substrings,
            model_filter_enabled=self._runtime_filters.model_filter_enabled,
            model_filters=self._runtime_filters.model_filters,
            min_price=self._runtime_filters.min_price,
            max_price=self._runtime_filters.max_price,
        )

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

    def _save_runtime_filters(self) -> None:
        self.storage.save_runtime_filters(self._runtime_filters)

    def _save_menu_settings(self) -> None:
        self.storage.save_menu_settings(self._menu_settings)

    async def _control_loop(self) -> None:
        while True:
            try:
                updates = await self.notifier.get_updates()
                for update in updates:
                    await self._handle_update(update)
            except BotLongPollTimeout:
                continue
            except NotificationError as exc:
                LOGGER.error("Ошибка меню фильтров: %s", exc)
                await asyncio.sleep(5)

    async def _handle_update(self, update: dict[str, object]) -> None:
        message = update.get("message")
        if isinstance(message, dict):
            await self._handle_message(message)
        callback_query = update.get("callback_query")
        if isinstance(callback_query, dict):
            await self._handle_callback_query(callback_query)

    async def _handle_message(self, message: dict[str, object]) -> None:
        chat = message.get("chat")
        text = message.get("text")
        if not isinstance(chat, dict) or not isinstance(text, str):
            return
        chat_id = str(chat.get("id"))
        if chat_id != self.config.notify_chat_id:
            return
        if text in {"/start", "/filters", "/menu"}:
            if text == "/menu":
                await self.notifier.send_menu(chat_id=chat_id)
            else:
                await self.notifier.send_filter_menu(self._filter_menu_state(), chat_id=chat_id)
            return
        if text == "/settings":
            await self.notifier.send_settings_menu(self._menu_settings, chat_id=chat_id)
            return
        if text == "/cancel" and self._pending_edit is not None:
            self._pending_edit = None
            await self.notifier.send_text("Редактирование отменено.", chat_id=chat_id)
            await self.notifier.send_filter_menu(self._filter_menu_state(), chat_id=chat_id)
            return
        if self._pending_edit is not None:
            await self._apply_pending_edit(chat_id, text)

    async def _handle_callback_query(self, callback_query: dict[str, object]) -> None:
        data = callback_query.get("data")
        callback_query_id = callback_query.get("id")
        message = callback_query.get("message")
        if not isinstance(data, str) or not isinstance(callback_query_id, str):
            return
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        message_id = message.get("message_id")
        if not isinstance(chat, dict) or not isinstance(message_id, int):
            return
        chat_id = str(chat.get("id"))
        if chat_id != self.config.notify_chat_id:
            return

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
        elif data == "toggle_owner_username":
            self._runtime_filters = replace(
                self._runtime_filters,
                require_owner_username=not self._runtime_filters.require_owner_username,
            )
            self._save_runtime_filters()
        elif data == "toggle_backdrop_filter" and self._runtime_filters.backdrop_filters:
            self._runtime_filters = replace(
                self._runtime_filters,
                backdrop_filter_enabled=not self._runtime_filters.backdrop_filter_enabled,
            )
            self._save_runtime_filters()
        elif data == "edit_backdrop_filters":
            self._pending_edit = PendingEdit("backdrop_filters", menu_message_id=message_id)
            answer = "Пришли новые фоны через запятую"
            await self.notifier.send_text(
                "Отправь список фонов через запятую. Пример: <code>Coral Red,Ocean Blue</code>\n"
                "Пустое сообщение или <code>none</code> очистит список.\n"
                "Для отмены: <code>/cancel</code>",
                chat_id=chat_id,
            )
        elif data == "edit_blocked_usernames":
            self._pending_edit = PendingEdit("blocked_usernames", menu_message_id=message_id)
            answer = "Пришли запрещённые части username"
            await self.notifier.send_text(
                "Отправь запрещённые части username через запятую. Пример: <code>bank,storage,market</code>\n"
                "Пустое сообщение или <code>none</code> очистит список.\n"
                "Для отмены: <code>/cancel</code>",
                chat_id=chat_id,
            )
        elif data == "refresh_filters":
            answer = "Текущее состояние меню"
        else:
            answer = "Неизвестная команда"

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

    async def _apply_pending_edit(self, chat_id: str, text: str) -> None:
        pending_edit = self._pending_edit
        self._pending_edit = None
        normalized = text.strip()
        values = () if not normalized or normalized.casefold() == "none" else _split_csv(normalized)
        if pending_edit is None:
            return
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
