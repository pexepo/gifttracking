import unittest

from gift_tracking.login import LoginFlow, normalize_phone


class NormalizePhoneTests(unittest.TestCase):
    def test_strips_formatting(self) -> None:
        self.assertEqual(normalize_phone("+7 (912) 345-67-89"), "+79123456789")
        self.assertEqual(normalize_phone("+375291234567"), "+375291234567")
        self.assertEqual(normalize_phone(""), "")
        self.assertEqual(normalize_phone("abc+def"), "+")


class FakeNotifier:
    def __init__(self) -> None:
        self.updates: list[dict] = []
        self.sent_texts: list[dict] = []
        self.code_prompts: list[str] = []
        self.code_prompt_messages: dict[str, int] = {}

    async def get_updates(self, timeout: int = 30) -> list[dict]:
        return [self.updates.pop(0)] if self.updates else []

    async def send_text(self, text, *, keyboard=None, chat_id=None):
        self.sent_texts.append({"text": text, "chat_id": chat_id})
        return {"message_id": len(self.sent_texts)}

    async def send_login_prompt(self, chat_id):
        self.sent_texts.append({"text": "login prompt", "chat_id": chat_id})
        return {"message_id": len(self.sent_texts)}

    async def answer_callback_query(self, callback_query_id, text):
        pass

    async def send_code_prompt(self, chat_id, buffer=""):
        self.code_prompts.append(buffer)
        self.code_prompt_messages[f"{chat_id}:{buffer}"] = len(self.sent_texts) + 1
        return {"message_id": 10}

    async def update_code_prompt(self, chat_id, message_id, buffer):
        pass


class FakeApi:
    def __init__(self, with_password: bool = False) -> None:
        self.with_password = with_password
        self.connected = False
        self.authorized = False
        self.code_requested: str | None = None
        self.signed_code: tuple | None = None
        self.signed_password: str | None = None

    async def connect(self) -> None:
        self.connected = True

    async def is_authorized(self) -> bool:
        return self.authorized

    async def send_code_request(self, phone: str):
        self.code_requested = phone
        return "hash-value"

    async def sign_in(self, phone: str, code: str, phone_code_hash: str) -> None:
        if self.with_password:
            from telethon import errors

            raise errors.SessionPasswordNeededError(None)
        self.signed_code = (phone, code)
        self.authorized = True

    async def sign_in_password(self, password: str) -> None:
        self.signed_password = password
        self.authorized = True


class LoginFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_flow_phone_code_sign_in(self) -> None:
        api = FakeApi()
        notifier = FakeNotifier()
        flow = LoginFlow(api, notifier, "1")
        notifier.updates.append(
            {"message": {"chat": {"id": 1}, "contact": {"phone_number": "+7 912 345-67-89"}}}
        )
        notifier.updates.append(
            {"message": {"chat": {"id": 1}, "text": "12345"}}
        )
        self.assertTrue(await flow.run())
        self.assertTrue(api.connected)
        self.assertIn("+79123456789", api.code_requested or "")
        self.assertEqual(api.signed_code, ("+79123456789", "12345"))

    async def test_code_entered_via_inline_keyboard(self) -> None:
        api = FakeApi()
        notifier = FakeNotifier()
        flow = LoginFlow(api, notifier, "1")
        notifier.updates.append(
            {"message": {"chat": {"id": 1}, "contact": {"phone_number": "+12223334444"}}}
        )
        for digit in "1", "2", "3":
            notifier.updates.append(
                {
                    "callback_query": {
                        "id": f"cb{digit}",
                        "data": f"code_digit_{digit}",
                        "message": {"chat": {"id": 1}, "message_id": 10},
                    }
                }
            )
        notifier.updates.append(
            {
                "callback_query": {
                    "id": "cb-submit",
                    "data": "code_submit",
                    "message": {"chat": {"id": 1}, "message_id": 10},
                }
            }
        )
        self.assertTrue(await flow.run())
        self.assertEqual(api.signed_code, ("+12223334444", "123"))

    async def test_2fa_password(self) -> None:
        api = FakeApi(with_password=True)
        notifier = FakeNotifier()
        flow = LoginFlow(api, notifier, "1")
        notifier.updates.append(
            {"message": {"chat": {"id": 1}, "contact": {"phone_number": "+12223334444"}}}
        )
        notifier.updates.append(
            {"message": {"chat": {"id": 1}, "text": "98765"}}
        )
        notifier.updates.append(
            {"message": {"chat": {"id": 1}, "text": "supersecret"}}
        )
        self.assertTrue(await flow.run())
        self.assertEqual(api.signed_password, "supersecret")

    async def test_cancel_returns_false(self) -> None:
        api = FakeApi()
        notifier = FakeNotifier()
        flow = LoginFlow(api, notifier, "1")
        notifier.updates.append({"message": {"chat": {"id": 1}, "text": "/cancel"}})
        self.assertFalse(await flow.run())

    async def test_skips_when_already_authorized(self) -> None:
        api = FakeApi()
        api.authorized = True
        notifier = FakeNotifier()
        flow = LoginFlow(api, notifier, "1")
        self.assertTrue(await flow.run())
        self.assertIsNone(api.code_requested)


if __name__ == "__main__":
    unittest.main()