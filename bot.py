"""
Minimal Telegram bot wrapper for the Encar parser.

Behavior:
  - user sends an Encar URL (or a message containing it)
  - bot replies with the same JSON payload as `python main.py <url>`

Run (polling):
  set TELEGRAM_BOT_TOKEN and execute:
      python bot.py
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from parser.encar_parser import ParseError, parse_encar_url


log = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _extract_first_url(text: str) -> str | None:
    if not text:
        return None
    m = _URL_RE.search(text)
    return m.group(0) if m else None


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Отправь ссылку на объявление Encar (например:\n"
        "https://fem.encar.com/cars/detail/41453346?listAdvType=share)\n\n"
        "В ответ пришлю JSON с распарсенными полями.",
        disable_web_page_preview=True,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Команды:\n"
        "/start — описание\n"
        "/help — помощь\n\n"
        "Также можно просто отправить ссылку Encar в сообщении.",
        disable_web_page_preview=True,
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    url = _extract_first_url(update.message.text or "")
    if not url:
        await update.message.reply_text(
            "Не нашёл URL в сообщении. Пришли ссылку Encar.",
            disable_web_page_preview=True,
        )
        return

    try:
        payload: Dict[str, Any] = parse_encar_url(url)
    except ParseError as e:
        payload = {"error": e.message}
    except Exception as e:  # noqa: BLE001
        payload = {"error": f"Unexpected error: {type(e).__name__}: {e}"}

    # Telegram message limit ~4096 chars; our payload is small.
    await update.message.reply_text(
        f"<pre>{_dump(payload)}</pre>",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN env var is required")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()

