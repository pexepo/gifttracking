from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import __version__
from .config import Config, ConfigError
from .monitor import GiftMonitor
from .notifier import BotNotifier, NotificationError


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="gift-tracking",
        description="Мониторинг новых уникальных подарков Telegram",
    )
    parser.add_argument(
        "--test-notification",
        action="store_true",
        help="отправить тестовое сообщение через Bot API и завершиться",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    if args.test_notification:
        notifier = BotNotifier(
            config.bot_token,
            config.notify_chat_id,
            config.timezone,
            ca_file=config.bot_api_ca_file,
            insecure_ssl=config.bot_api_insecure_ssl,
        )
        try:
            asyncio.run(
                notifier.send_text("✅ <b>Gift Tracking:</b> тестовое уведомление")
            )
        except NotificationError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(1) from exc
        print("Тестовое уведомление отправлено")
        return
    try:
        asyncio.run(GiftMonitor(config).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
