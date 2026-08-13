# Collection Blacklist and Notifications Toggle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the model-filter feature with a case-insensitive substring blacklist of gift collections, and add a global «Автосообщения» toggle in the filter menu that pauses all notifications.

**Architecture:** Three spots change together: the frozen dataclasses `RuntimeFilters` (models.py) and `FilterMenuState` (notifier.py) swap model fields for `notifications_enabled` + `blacklisted_collections`; the filter menu keyboard/text re-renders the new state; `GiftMonitor` swaps `_matches_models` for a blacklist check and short-circuits `send_pending_notifications` when notifications are disabled. Spec: `docs/superpowers/specs/2026-08-14-collections-blacklist-design.md`.

**Tech Stack:** Python 3.11+, stdlib `dataclasses.replace`, Telethon (only for errors), sqlite3 storage.

## Global Constraints

- Test command convention: `PYTHONPATH=src .venv/bin/python -m unittest tests.<module> -v` and full suite `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v` (plain `python` is NOT on PATH).
- Git identity is unset; commit with `git -c user.name=pexepo -c user.email=pexepo@MacBook-Air-pexepo.local commit -m "..."`.
- All UI strings in Russian, matching existing menu copy.
- `RuntimeFilters.from_dict` must stay backward compatible with stored rows that lack the new keys (defaults: `notifications_enabled=True`, `blacklisted_collections=()`), and stored model-filter rows load without error (model keys ignored).
- No third-party imports beyond the existing stdlib + telethon.

---

### Task 1: Collection blacklist replaces model filters

**Files:**
- Modify: `src/gift_tracking/models.py:64-95` (`RuntimeFilters`, `from_dict`)
- Modify: `src/gift_tracking/notifier.py:31-40` (`FilterMenuState`), `:210-256` (`_filter_menu_text`), `:258-317` (`_filter_menu_keyboard`)
- Modify: `src/gift_tracking/monitor.py:257-264` (delete `_matches_models`; add module helper), `:300-323` (pipeline blacklist check), `:381-391` (`_filter_menu_state`), `:580-593` (replace `toggle_model_filter`/`edit_model_filters`), `:649-662` (refresh-data set), `:672-678` (pending-edit kind)
- Modify: `tests/test_models.py` (round-trip, backward-compat tests)
- Modify: `tests/test_notifier.py` (menu text/keyboard tests)
- Modify: `tests/test_monitor.py` (`test_model_filter_skips_non_matching` → blacklist tests, preservation test, edit-via-message test)

**Interfaces:**
- Consumes: existing `replace()` pattern on `RuntimeFilters`, `_split_csv(normalized)` (monitor.py:53), `storage.save_runtime_filters`/`load_runtime_filters`, `PendingEdit(kind, menu_message_id)`.
- Produces:
  - `RuntimeFilters` fields: `require_owner_username: bool`, `backdrop_filter_enabled: bool`, `backdrop_filters: tuple[str, ...]`, `blocked_owner_username_substrings: tuple[str, ...]`, `notifications_enabled: bool = True`, `blacklisted_collections: tuple[str, ...] = ()`, `min_price: float | None = None`, `max_price: float | None = None` (model fields REMOVED).
  - `FilterMenuState` mirrors those same fields.
  - `monitor._matches_blacklisted_collection(title: str, blacklisted: tuple[str, ...]) -> bool` — module-level helper (Task 2 depends on the field name `notifications_enabled`, added here, defaulting True).

- [ ] **Step 1: Write the failing model tests**

Append to `tests/test_models.py`:

```python
    def test_runtime_filters_new_shape(self) -> None:
        state = RuntimeFilters(
            require_owner_username=True,
            backdrop_filter_enabled=False,
            backdrop_filters=("Coral Red",),
            blocked_owner_username_substrings=("bank",),
            blacklisted_collections=("Plush Pepe",),
        )
        self.assertTrue(state.notifications_enabled)
        self.assertEqual(state.blacklisted_collections, ("Plush Pepe",))
        self.assertNotIn("model_filter_enabled", state.to_dict())
        self.assertNotIn("model_filters", state.to_dict())

    def test_runtime_filters_from_dict_backward_compatible(self) -> None:
        state = RuntimeFilters.from_dict(
            {
                "require_owner_username": True,
                "backdrop_filter_enabled": False,
                "backdrop_filters": [],
                "blocked_owner_username_substrings": ["bank"],
                "model_filter_enabled": True,
                "model_filters": ["Albino"],
            }
        )
        self.assertEqual(state.blacklisted_collections, ())
        self.assertTrue(state.notifications_enabled)
        self.assertEqual(RuntimeFilters.from_dict({}).notifications_enabled, True)
```

Also update the existing `test_round_trip_with_new_fields` in `tests/test_models.py` to seed `blacklisted_collections=("Plush Pepe",)` and `notifications_enabled=False` instead of model fields, and assert the round trip preserves them.

- [ ] **Step 2: Run model tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_models -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'blacklisted_collections'`.

- [ ] **Step 3: Update the data model**

`src/gift_tracking/models.py`, class `RuntimeFilters`:

```python
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
```

`src/gift_tracking/notifier.py`, class `FilterMenuState` — same two field swaps:

```python
@dataclass(frozen=True, slots=True)
class FilterMenuState:
    owner_username_required: bool
    backdrop_filter_enabled: bool
    backdrop_filters: tuple[str, ...]
    blocked_owner_username_substrings: tuple[str, ...]
    notifications_enabled: bool = True
    blacklisted_collections: tuple[str, ...] = ()
    min_price: float | None = None
    max_price: float | None = None
```

- [ ] **Step 4: Run model tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_models -v`
Expected: PASS.

- [ ] **Step 5: Write the failing monitor tests**

In `tests/test_monitor.py`, replace `test_model_filter_skips_non_matching` (currently seeds `model_filter_enabled`/`model_filters` and asserts skipping) with:

```python
    async def test_blacklist_skips_collection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            monitor = GiftMonitor(config)
            try:
                notifier = FakeNotifier()
                monitor.notifier = notifier
                monitor._runtime_filters = replace(
                    monitor._runtime_filters,
                    blacklisted_collections=("Plush Pepe",),
                )
                for number in (1, 2):
                    monitor.storage.record_gift(event(number, number))
                await monitor.send_pending_notifications()
                self.assertEqual(len(monitor.storage.pending_notifications()), 0)
                self.assertEqual(len(notifier.sent), 0)
                self.assertEqual(len(notifier.status_messages), 0)
            finally:
                close_monitor(monitor)

    async def test_blacklist_matches_substring_case_insensitively(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            monitor = GiftMonitor(config)
            try:
                notifier = FakeNotifier()
                monitor.notifier = notifier
                monitor._runtime_filters = replace(
                    monitor._runtime_filters,
                    blacklisted_collections=("PLUSH",),
                )
                monitor.storage.record_gift(event(1, 1))
                await monitor.send_pending_notifications()
                self.assertEqual(len(monitor.storage.pending_notifications()), 0)
                self.assertEqual(len(notifier.status_messages), 0)
            finally:
                close_monitor(monitor)

    async def test_edits_blacklisted_collections_from_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            monitor = GiftMonitor(config)
            try:
                notifier = FakeNotifier()
                monitor.notifier = notifier
                await monitor._handle_callback_query(
                    {
                        "id": "cb1",
                        "data": "edit_blacklisted_collections",
                        "message": {"chat": {"id": 1}, "message_id": 77},
                    }
                )
                await monitor._handle_message(
                    {"chat": {"id": 1}, "text": "Plush Pepe, Astral Shard"}
                )
                self.assertEqual(
                    monitor._runtime_filters.blacklisted_collections,
                    ("plush pepe", "astral shard"),
                )
                self.assertEqual(monitor._runtime_filters.notifications_enabled, True)
            finally:
                close_monitor(monitor)
```

Note: `notifier.sent` only reflects `send_event`; notifications go to `status_messages`, so empty `status_messages` proves nothing was sent.

Also in `tests/test_monitor.py`, update `test_toggle_owner_username_preserves_model_and_price_filters`: seed `blacklisted_collections=("Albino",)` instead of `model_filter_enabled=True, model_filters=("Albino",)`, and assert `monitor._runtime_filters.blacklisted_collections == ("Albino",)` survives the toggle (rename test to `test_toggle_owner_username_preserves_blacklist_and_price_filters`).

- [ ] **Step 6: Run monitor tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_monitor -v`
Expected: FAIL — AttributeError on removed fields (`model_filter_enabled`) or callback-data mismatch.

- [ ] **Step 7: Implement monitor changes**

`src/gift_tracking/monitor.py`:

1. Add module-level helper after `_blocked_owner_username` (line ~50):

```python
def _matches_blacklisted_collection(title: str, blacklisted: tuple[str, ...]) -> bool:
    folded = title.casefold()
    return any(part in folded for part in blacklisted)
```

2. Delete the `_matches_models` method (monitor.py:257-264).

3. In `send_pending_notifications` (monitor.py:319-322), replace the model check with:

```python
            if _matches_blacklisted_collection(
                event.title, self._runtime_filters.blacklisted_collections
            ):
                self.storage.mark_notified(event.slug)
                LOGGER.info("Пропуск %s: коллекция в блеклисте", event.slug)
                continue
```

4. `_filter_menu_state` (monitor.py:381-391):

```python
    def _filter_menu_state(self) -> FilterMenuState:
        return FilterMenuState(
            owner_username_required=self._runtime_filters.require_owner_username,
            backdrop_filter_enabled=self._runtime_filters.backdrop_filter_enabled,
            backdrop_filters=self._runtime_filters.backdrop_filters,
            blocked_owner_username_substrings=self._runtime_filters.blocked_owner_username_substrings,
            notifications_enabled=self._runtime_filters.notifications_enabled,
            blacklisted_collections=self._runtime_filters.blacklisted_collections,
            min_price=self._runtime_filters.min_price,
            max_price=self._runtime_filters.max_price,
        )
```

5. Replace the `toggle_model_filter`/`edit_model_filters` handler branch (monitor.py:580-593) with:

```python
        elif data == "edit_blacklisted_collections":
            self._pending_edit = PendingEdit(
                "blacklisted_collections", menu_message_id=message_id
            )
            answer = "Пришли блеклист коллекций"
            await self.notifier.send_text(
                "Отправь названия коллекций через запятую. Пример: <code>Plush Pepe,Astral Shard</code>\n"
                "Пустое сообщение или <code>none</code> очистит список.\nДля отмены: /cancel",
                chat_id=chat_id,
            )
```

6. In the refresh-data set (monitor.py:649-659):

```python
        elif self._pending_edit is None and data in {
            "toggle_owner_username",
            "toggle_backdrop_filter",
            "refresh_filters",
            "edit_backdrop_filters",
            "edit_blocked_usernames",
            "edit_blacklisted_collections",
            "edit_min_price",
            "edit_max_price",
        }:
```

7. In `_apply_pending_edit` (monitor.py:672-678), replace the `model_filters` branch with:

```python
        if pending_edit.kind == "blacklisted_collections":
            self._runtime_filters = replace(
                self._runtime_filters,
                blacklisted_collections=values,
            )
            confirmation = "Блеклист коллекций обновлён."
```

(`values` is already computed at the top of `_apply_pending_edit` as `() if not normalized or normalized.casefold() == "none" else _split_csv(normalized)` — matches the existing pattern.)

- [ ] **Step 8: Update the menu text and keyboard**

`src/gift_tracking/notifier.py`:

1. In `_filter_menu_text`, replace the `model_line` block (notifier.py:220-226) with:

```python
        blacklist_line = "не задан"
        if state.blacklisted_collections:
            blacklist_line = (
                f"блеклист: <code>{html.escape(', '.join(state.blacklisted_collections))}</code>"
            )
```

and replace the `f"Модели: <b>{model_line}</b>"` line (notifier.py:253) with `f"Коллекции: <b>{blacklist_line}</b>"`.

2. In `_filter_menu_keyboard`, delete the `toggle_model_filter` conditional row and the «Редактировать модели» row (notifier.py:296-311), and replace them with:

```python
        keyboard.append(
            [
                {
                    "text": "Редактировать блеклист коллекций",
                    "callback_data": "edit_blacklisted_collections",
                }
            ]
        )
```

3. In the price row (notifier.py:312-315), remove the second button entirely:

```python
        keyboard.append(
            [{"text": "Цена мин/макс", "callback_data": "edit_min_price"}]
        )
```

- [ ] **Step 9: Update/add notifier tests**

`tests/test_notifier.py` — the existing `test_filter_menu_state_shape` (line 40) builds its state with only the four required fields by keyword and never touches model fields, so it needs NO changes. Add (states are constructed inline by keyword, four required fields first, matching existing style):

```python
    def test_filter_menu_shows_blacklist(self) -> None:
        state = FilterMenuState(
            owner_username_required=True,
            backdrop_filter_enabled=False,
            backdrop_filters=(),
            blocked_owner_username_substrings=(),
            blacklisted_collections=("Plush Pepe",),
        )
        text = BotNotifier._filter_menu_text(state)
        self.assertIn("Коллекции: <b>блеклист: <code>Plush Pepe</code></b>", text)
        self.assertNotIn("Модели", text)

    def test_filter_menu_keyboard_has_blacklist_and_no_models(self) -> None:
        state = FilterMenuState(
            owner_username_required=True,
            backdrop_filter_enabled=False,
            backdrop_filters=(),
            blocked_owner_username_substrings=(),
            blacklisted_collections=("Plush Pepe",),
        )
        keyboard = BotNotifier._filter_menu_keyboard(state)
        data = [
            button["callback_data"]
            for row in keyboard["inline_keyboard"]
            for button in row
        ]
        self.assertIn("edit_blacklisted_collections", data)
        self.assertNotIn("edit_model_filters", data)
        self.assertNotIn("toggle_model_filter", data)
        self.assertNotIn("code_noop", data)
```

- [ ] **Step 10: Run the targeted tests**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_models tests.test_notifier tests.test_monitor -v`
Expected: PASS.

- [ ] **Step 11: Run the full suite**

Run: `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v`
Expected: ALL PASS.

- [ ] **Step 12: Commit**

```bash
git add src/gift_tracking/models.py src/gift_tracking/notifier.py src/gift_tracking/monitor.py tests/test_models.py tests/test_notifier.py tests/test_monitor.py
git -c user.name=pexepo -c user.email=pexepo@MacBook-Air-pexepo.local commit -m "Replace model filters with collection blacklist"
```

---

### Task 2: Global notifications toggle in the filter menu

**Files:**
- Modify: `src/gift_tracking/notifier.py:210-256` (`_filter_menu_text`), `:258-317` (`_filter_menu_keyboard`)
- Modify: `src/gift_tracking/monitor.py:298-300` (`send_pending_notifications` early return), `:477+` callback handler (add `toggle_notifications`), `:649-659` refresh-data set
- Modify: `tests/test_monitor.py`
- Modify: `tests/test_notifier.py`

**Interfaces:**
- Consumes: `RuntimeFilters.notifications_enabled: bool = True` and `FilterMenuState.notifications_enabled: bool = True` from Task 1.
- Produces: callback `toggle_notifications` + refresh-set membership (the filter menu auto-refreshes after this toggle like other toggles).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_monitor.py`:

```python
    async def test_notifications_toggle_pauses_sending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            monitor = GiftMonitor(config)
            try:
                notifier = FakeNotifier()
                monitor.notifier = notifier
                monitor._runtime_filters = replace(
                    monitor._runtime_filters, notifications_enabled=False
                )
                for number in (1, 2):
                    monitor.storage.record_gift(event(number, number))
                await monitor.send_pending_notifications()
                self.assertEqual(len(monitor.storage.pending_notifications()), 2)
                self.assertEqual(len(notifier.status_messages), 0)
                self.assertEqual(len(notifier.sent), 0)
                monitor._runtime_filters = replace(
                    monitor._runtime_filters, notifications_enabled=True
                )
                await monitor.send_pending_notifications()
                self.assertEqual(len(monitor.storage.pending_notifications()), 0)
                self.assertEqual(len(notifier.status_messages), 2)
            finally:
                close_monitor(monitor)

    async def test_toggle_notifications_callback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            monitor = GiftMonitor(config)
            try:
                notifier = FakeNotifier()
                monitor.notifier = notifier
                await monitor._handle_callback_query(
                    {
                        "id": "cb9",
                        "data": "toggle_notifications",
                        "message": {"chat": {"id": 1}, "message_id": 77},
                    }
                )
                self.assertFalse(monitor._runtime_filters.notifications_enabled)
                self.assertEqual(
                    monitor.storage.load_runtime_filters(), monitor._runtime_filters
                )
                await monitor._handle_callback_query(
                    {
                        "id": "cb10",
                        "data": "toggle_notifications",
                        "message": {"chat": {"id": 1}, "message_id": 77},
                    }
                )
                self.assertTrue(monitor._runtime_filters.notifications_enabled)
            finally:
                close_monitor(monitor)
```

Append to `tests/test_notifier.py`:

```python
    def test_filter_menu_shows_notifications_toggle_text(self) -> None:
        state = FilterMenuState(
            owner_username_required=True,
            backdrop_filter_enabled=False,
            backdrop_filters=(),
            blocked_owner_username_substrings=(),
            notifications_enabled=False,
        )
        text = BotNotifier._filter_menu_text(state)
        self.assertIn("Автосообщения: <b>выключены</b>", text)

    def test_filter_menu_keyboard_starts_with_notifications_toggle(self) -> None:
        state = FilterMenuState(
            owner_username_required=True,
            backdrop_filter_enabled=False,
            backdrop_filters=(),
            blocked_owner_username_substrings=(),
            notifications_enabled=True,
        )
        keyboard = BotNotifier._filter_menu_keyboard(state)
        rows = keyboard["inline_keyboard"]
        self.assertEqual(
            rows[0][0]["callback_data"], "toggle_notifications"
        )
        self.assertIn(
            "вкл" if state.notifications_enabled else "выкл",
            rows[0][0]["text"],
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_monitor.MonitorTests.test_notifications_toggle_pauses_sending tests.test_monitor.MonitorTests.test_toggle_notifications_callback tests.test_notifier -v`
Expected: FAIL — toggle callback answers «Неизвестная команда», notifications still sent while disabled.

- [ ] **Step 3: Implement the toggle**

`src/gift_tracking/monitor.py`:

1. At the top of `send_pending_notifications` (before the `pending` loop, monitor.py:298-300):

```python
    async def send_pending_notifications(self) -> None:
        if not self._runtime_filters.notifications_enabled:
            return
        pending: list[GiftEvent] = []
```

2. Add a callback branch next to `toggle_owner_username` (monitor.py:610-615):

```python
        elif data == "toggle_notifications":
            self._runtime_filters = replace(
                self._runtime_filters,
                notifications_enabled=not self._runtime_filters.notifications_enabled,
            )
            self._save_runtime_filters()
```

3. Add `"toggle_notifications"` to the refresh-data set (monitor.py:649-659, the same set edited in Task 1):

```python
        elif self._pending_edit is None and data in {
            "toggle_notifications",
            "toggle_owner_username",
            "toggle_backdrop_filter",
            "refresh_filters",
            "edit_backdrop_filters",
            "edit_blocked_usernames",
            "edit_blacklisted_collections",
            "edit_min_price",
            "edit_max_price",
        }:
```

`src/gift_tracking/notifier.py`:

4. In `_filter_menu_text`, add the toggle line right after the title (notifier.py:237-238). Existing first line of the `"\n".join([...])` list is `"⚙️ <b>Фильтры Gift Tracking</b>"`, then `""` — insert between them:

```python
                "🔔 Автосообщения: <b>"
                + ("включены" if state.notifications_enabled else "выключены")
                + "</b>",
```

5. In `_filter_menu_keyboard`, prepend the toggle row (notifier.py:260-271, before the «ЛС» row):

```python
        keyboard = [
            [
                {
                    "text": (
                        "🔔 Автосообщения: вкл"
                        if state.notifications_enabled
                        else "🔔 Автосообщения: выкл"
                    ),
                    "callback_data": "toggle_notifications",
                }
            ],
            [
                {
                    "text": (
                        "ЛС: только @username"
                        if state.owner_username_required
                        else "ЛС: все владельцы"
                    ),
                    "callback_data": "toggle_owner_username",
                }
            ],
        ]
```

- [ ] **Step 4: Run the targeted tests**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_monitor tests.test_notifier -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```bash
git add src/gift_tracking/notifier.py src/gift_tracking/monitor.py tests/test_notifier.py tests/test_monitor.py
git -c user.name=pexepo -c user.email=pexepo@MacBook-Air-pexepo.local commit -m "Add notifications toggle to filter menu"
```