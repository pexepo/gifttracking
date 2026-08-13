from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import Any

from telethon import TelegramClient, errors, types
from telethon.tl.functions import payments

from .models import Attribute, Collection, GiftEvent

LOGGER = logging.getLogger(__name__)


def slug_prefix_from_title(title: str) -> str:
    """Match Telegram's collectible slug convention for catalog titles."""
    return "".join(re.findall(r"[A-Za-z0-9]+", title))


def _rarity_text(rarity: Any) -> str | None:
    if isinstance(rarity, types.StarGiftAttributeRarity):
        value = rarity.permille / 10
        return f"{value:g}%"
    labels = {
        "StarGiftAttributeRarityUncommon": "необычный",
        "StarGiftAttributeRarityRare": "редкий",
        "StarGiftAttributeRarityEpic": "эпический",
        "StarGiftAttributeRarityLegendary": "легендарный",
    }
    return labels.get(type(rarity).__name__)


def _attributes(gift: types.StarGiftUnique) -> tuple[Attribute, ...]:
    parsed: list[Attribute] = []
    for item in gift.attributes:
        if isinstance(item, types.StarGiftAttributeModel):
            parsed.append(Attribute("model", item.name, _rarity_text(item.rarity)))
        elif isinstance(item, types.StarGiftAttributeBackdrop):
            parsed.append(Attribute("backdrop", item.name, _rarity_text(item.rarity)))
        elif isinstance(item, types.StarGiftAttributePattern):
            parsed.append(Attribute("symbol", item.name, _rarity_text(item.rarity)))
    return tuple(parsed)


def _owner(result: Any, gift: types.StarGiftUnique) -> tuple[str | None, str | None]:
    peer = gift.owner_id
    if isinstance(peer, types.PeerUser):
        entity = next((user for user in result.users if user.id == peer.user_id), None)
        if entity:
            name = (
                " ".join(
                    filter(
                        None,
                        (
                            getattr(entity, "first_name", None),
                            getattr(entity, "last_name", None),
                        ),
                    )
                )
                or None
            )
            return name, getattr(entity, "username", None)
    elif isinstance(peer, types.PeerChannel):
        entity = next(
            (chat for chat in result.chats if chat.id == peer.channel_id), None
        )
        if entity:
            return getattr(entity, "title", None), getattr(entity, "username", None)
    return gift.owner_name, None


class GiftTelegramApi:
    def __init__(self, api_id: int, api_hash: str, session: str) -> None:
        self.client = TelegramClient(session, api_id, api_hash)
        self._known_users: dict[int, Any] = {}

    async def connect(self) -> None:
        await self.client.connect()

    async def start(self) -> None:
        await self.connect()

    async def is_authorized(self) -> bool:
        return bool(await self.client.is_user_authorized())

    async def send_code_request(self, phone: str) -> Any:
        return await self.client.send_code_request(phone)

    async def sign_in(self, phone: str, code: str, phone_code_hash: str) -> None:
        await self.client.sign_in(phone, code, phone_code_hash=phone_code_hash)

    async def sign_in_password(self, password: str) -> None:
        sign_in_password = getattr(self.client, "sign_in_password", None)
        if sign_in_password is not None:
            await sign_in_password(password)
        else:
            await self.client.sign_in(password=password)

    async def get_me_phone(self) -> str | None:
        me = await self.client.get_me()
        return getattr(me, "phone", None)

    def remember_owner(self, entity: Any) -> None:
        if entity is not None and getattr(entity, "id", None) is not None:
            self._known_users[int(entity.id)] = entity

    async def send_message_to_user(self, user_id: int, text: str) -> None:
        entity = self._known_users.get(user_id)
        if entity is None:
            raise ValueError(f"Нет данных о владельце {user_id}")
        await self.client.send_message(entity, text)

    async def disconnect(self) -> None:
        await self.client.disconnect()

    async def invoke(self, request: Any) -> Any:
        while True:
            try:
                return await self.client(request)
            except errors.FloodWaitError as exc:
                wait_for = max(1, exc.seconds + 1)
                LOGGER.warning("Telegram запросил паузу %s секунд", wait_for)
                await asyncio.sleep(wait_for)

    async def catalog(self) -> list[Collection]:
        result = await self.invoke(payments.GetStarGiftsRequest(hash=0))
        collections: list[Collection] = []
        for gift in result.gifts:
            if not isinstance(gift, types.StarGift) or not gift.title:
                continue
            prefix = slug_prefix_from_title(gift.title)
            if prefix:
                collections.append(Collection(gift.id, gift.title, prefix))
        return collections

    async def unique_gift(self, slug: str) -> tuple[GiftEvent, Any]:
        result = await self.invoke(payments.GetUniqueStarGiftRequest(slug=slug))
        gift = result.gift
        if not isinstance(gift, types.StarGiftUnique):
            raise TypeError(f"{slug} не является уникальным подарком")
        owner_name, owner_username = _owner(result, gift)
        peer = gift.owner_id
        owner_user_id = None
        if isinstance(peer, types.PeerUser):
            owner_user_id = peer.user_id
            entity = next((user for user in result.users if user.id == peer.user_id), None)
            if entity:
                self.remember_owner(entity)
        event = GiftEvent(
            slug=gift.slug,
            gift_id=gift.gift_id,
            title=gift.title,
            number=gift.num,
            link=f"https://t.me/nft/{gift.slug}",
            owner_name=owner_name,
            owner_username=owner_username,
            owner_address=gift.owner_address,
            attributes=_attributes(gift),
            availability_issued=gift.availability_issued,
            availability_total=gift.availability_total,
            detected_at=datetime.now(UTC),
            owner_user_id=owner_user_id,
        )
        return event, result
