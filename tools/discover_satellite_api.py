"""Best-effort discovery of the Gift Satellite web API.

Usage:
  python tools/discover_satellite_api.py --api-id 123 --api-hash abc --session gift_tracking

Opens the @gift_satellite_bot web app through the MTProto session, fetches
its JS bundle and prints candidate API endpoints. This is a starting point
for adapting GiftSatelliteClient (pricing.py); the response format is then
confirmed by capturing real requests (DevTools Network tab in a web
Telegram session, or the app's own traffic).

The script never crashes: a missing or invalid session, a failed webview
request or an unreachable bundle produce a short diagnostic instead of a
traceback.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import ssl
import sys
import urllib.parse
import urllib.request

from telethon import TelegramClient
from telethon.errors import RPCError
from telethon.tl.functions.messages import (
    RequestAppWebViewRequest as RequestAppWebView,
)
from telethon.tl.types import InputBotAppShortName

BOT_USERNAME = "gift_satellite_bot"
ENDPOINT_RE = re.compile(r"https?://[a-zA-Z0-9.-]+/api/[a-zA-Z0-9/_-]+")
SCRIPT_SRC_RE = re.compile(r'<script[^>]+src="([^"]+)"')


def print_endpoints(bundle: str) -> None:
    endpoints = sorted(set(ENDPOINT_RE.findall(bundle)))
    for endpoint in endpoints:
        print(f"Endpoint: {endpoint}")
    if not endpoints:
        print("No API endpoints found in the JS bundle; inspect the bundle manually or capture requests.")


def fetch(url: str, context: ssl.SSLContext) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=30, context=context) as response:
            return response.read().decode("utf-8", "replace")
    except Exception as exc:
        print(f"Failed to fetch {url}: {exc}")
        return None


async def main(api_id: int, api_hash: str, session: str, insecure_ssl: bool) -> None:
    client = TelegramClient(session, api_id, api_hash)
    try:
        await client.start()
    except Exception as exc:
        print(f"Could not start session '{session}': {exc}")
        print("Make sure the session file exists or log in interactively first.")
        return
    try:
        bot = await client.get_entity(BOT_USERNAME)
        peer = await client.get_input_entity(bot)
    except Exception as exc:
        print(f"Could not resolve {BOT_USERNAME}: {exc}")
        return
    result = None
    for short_name in ("", "app", "gift", "web"):
        try:
            app = InputBotAppShortName(bot_id=bot, short_name=short_name)
            result = await client(
                RequestAppWebView(peer=peer, app=app, platform="android")
            )
            if result.url:
                break
        except RPCError as exc:
            print(f"RequestAppWebView (short_name={short_name or 'default'}): {exc}")
    if result is None or not result.url:
        print(
            "Webview URL not returned; open the bot in a web Telegram session "
            "and capture the app's requests in DevTools instead."
        )
        return
    parsed = urllib.parse.urlparse(result.url)
    app_url = f"{parsed.scheme}://{parsed.netloc}"
    print(f"App URL: {app_url}")
    print(f"Webview URL: {result.url}")
    context = ssl._create_unverified_context() if insecure_ssl else ssl.create_default_context()
    page = fetch(result.url, context)
    if page is None:
        return
    scripts = [
        urllib.parse.urljoin(result.url, src)
        for src in SCRIPT_SRC_RE.findall(page)
        if src
    ]
    for script in scripts:
        print(f"Script: {script}")
    if not scripts:
        scripts = [result.url]
    for script in scripts:
        bundle = fetch(script, context)
        if bundle is not None:
            print_endpoints(bundle)
            return
    print("No JS bundle could be fetched; inspect the bundle manually or capture requests.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-id", type=int, required=True)
    parser.add_argument("--api-hash", required=True)
    parser.add_argument("--session", default="gift_tracking")
    parser.add_argument("--insecure-ssl", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(main(args.api_id, args.api_hash, args.session, args.insecure_ssl))
    except KeyboardInterrupt:
        print("Aborted.")
        sys.exit(130)
    except Exception as exc:
        print(f"Discovery failed: {exc}")
        sys.exit(1)