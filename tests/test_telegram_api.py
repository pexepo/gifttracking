import unittest

from gift_tracking.telegram_api import GiftTelegramApi, slug_prefix_from_title


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.phone_codes: dict[str, object] = {}
        self.signed_in_code: tuple | None = None
        self.signed_in_password: str | None = None

    async def connect(self) -> None:
        pass

    async def is_user_authorized(self) -> bool:
        return False

    async def send_code_request(self, phone: str) -> object:
        code = object()
        self.phone_codes[phone] = code
        return code

    async def sign_in(self, *args, **kwargs) -> None:
        self.signed_in_code = args if args else (kwargs.get("phone"), kwargs.get("code"))

    async def sign_in_password(self, password: str) -> None:
        self.signed_in_password = password

    async def send_message(self, entity, text: str) -> None:
        self.sent.append((getattr(entity, "id", entity), text))


class FakeEntity:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class TelegramApiTests(unittest.TestCase):
    def test_slug_prefix(self) -> None:
        self.assertEqual(slug_prefix_from_title("Plush Pepe"), "PlushPepe")
        self.assertEqual(slug_prefix_from_title("Jack-in-the-Box!"), "JackintheBox")

    def test_auth_primitives_delegate_to_client(self) -> None:
        api = GiftTelegramApi(1, "hash", "session")
        client = FakeClient()
        api.client = client

        import asyncio

        asyncio.run(api.connect())
        self.assertFalse(asyncio.run(api.is_authorized()))
        sent = asyncio.run(api.send_code_request("+12345"))
        self.assertIs(sent, client.phone_codes["+12345"])
        asyncio.run(api.sign_in("+12345", "12345", "hash-value"))
        self.assertEqual(client.signed_in_code, ("+12345", "12345"))
        asyncio.run(api.sign_in_password("secret"))
        self.assertEqual(client.signed_in_password, "secret")

    def test_send_message_to_user_uses_cached_entity(self) -> None:
        api = GiftTelegramApi(1, "hash", "session")
        client = FakeClient()
        api.client = client
        api.remember_owner(FakeEntity(42))

        import asyncio

        asyncio.run(api.send_message_to_user(42, "hello"))
        self.assertEqual(client.sent, [(42, "hello")])

    def test_send_message_to_user_fails_without_entity(self) -> None:
        api = GiftTelegramApi(1, "hash", "session")
        api.client = FakeClient()

        import asyncio

        with self.assertRaises(ValueError):
            asyncio.run(api.send_message_to_user(999, "hello"))


if __name__ == "__main__":
    unittest.main()