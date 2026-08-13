# Design: Blacklist of Collections and Global Notification Toggle

Date: 2026-08-14
Status: Approved by user (brainstorming)

## Motivation

- Пользователь хочет вместо фильтрации по моделям («Редактировать модели» / «Модели: вкл/выкл») иметь блеклист коллекций — список названий, для которых уведомления не отправляются.
- Пользователь хочет глобальный тумбл «Автосообщения: вкл/выкл» в меню «Фильтры», приостанавливающий все уведомления (админу и ЛС владельцам).

## Changes

### 1. Data model (`src/gift_tracking/models.py`)

`RuntimeFilters` (frozen dataclass, `to_dict`/`from_dict`):

- Removed: `model_filter_enabled: bool`, `model_filters: tuple[str, ...]`
- Added:
  - `notifications_enabled: bool = True` — default True (old stored rows without the key read as True)
  - `blacklisted_collections: tuple[str, ...] = ()`

`from_dict`:

- `notifications_enabled=bool(data.get("notifications_enabled", True))`
- `blacklisted_collections=tuple(data.get("blacklisted_collections", []))`
- Old rows containing `model_filter_enabled`/`model_filters` are ignored (fields no longer exist).

`FilterMenuState` (`src/gift_tracking/notifier.py`): same two field changes (removed models, added `notifications_enabled`, `blacklisted_collections`), default `notifications_enabled=True`.

### 2. Filter menu UI (`src/gift_tracking/notifier.py`)

Keyboard rows (in order):

1. `🔔 Автосообщения: вкл/выкл` — `toggle_notifications` (always present, topmost)
2. Existing: ЛС: только @username (`toggle_owner_username`)
3. Existing: фоны toggle / «Редактировать фоны»
4. Existing: «Редактировать blacklist username»
5. **Replaces** the model buttons: «Редактировать блеклист коллекций» — `edit_blacklisted_collections`
6. Existing: Цена мин/макс (`edit_min_price`) — the leftover «…» (`code_noop`) button in this row is **removed**
7. Existing: Обновить (`refresh_filters`)

Menu text adds two lines:

- `🔔 Автосообщения: <b>включены/выключены</b>` (first line, after title)
- `Коллекции: <b>не задан | блеклист: <code>...</code></b>` (replaces the «Модели» line)

### 3. Monitor behavior (`src/gift_tracking/monitor.py`)

- `send_pending_notifications` returns early when `notifications_enabled` is False — nothing is sent, nothing is marked notified (backlog preserved until re-enabled; monitoring continues).
- `_matches_models` (and the `model_filter_enabled`/`model_filters` branches) are replaced by a blacklist check: skip (mark notified, log `Пропуск %s: коллекция в блеклисте`) when any blacklisted phrase is a case-insensitive substring of `event.title`.
- Callback handlers: `toggle_notifications` toggles `notifications_enabled`; `edit_blacklisted_collections` replaces the `edit_model_filters` pending-edit with kind `blacklisted_collections` (same «через запятую / none — очистить» parsing; `none`/empty clears; matching `_parse_price` style).
- `_pending_edit` kind strings updated consistently (`blocked_usernames`, `backdrop_filters` unchanged).

### 4. Tests (`tests/test_monitor.py`, `tests/test_models.py`)

- `test_model_filter_skips_non_matching` → `test_blacklist_skips_collection` (substring, case-insensitive).
- Toggle-preservation test updated: seeds `blacklisted_collections` instead of model fields.
- New: `test_notifications_toggle_pauses_sending` — disabled → pending gifts remain pending, nothing sent; re-enabled → notifications flow.
- `test_models.py`: round-trip with new fields; `from_dict` backward compatible (old row → default True, empty blacklist).

## Out of scope

- Model filters return (feature removed per user decision).
- Whitelist option for collections.
- Per-chat menu state.