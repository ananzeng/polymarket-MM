import logging
import os

import requests

logger = logging.getLogger(__name__)


def sendTelegram(text: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chatId = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chatId:
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chatId, "text": text},
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as e:
        logger.error("Telegram notify failed: %s", e)
