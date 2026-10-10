"""Avisos: Telegram (servidor o Mac) y notificación nativa de macOS como respaldo."""
import platform
import subprocess

import requests

from core.config import env

TELEGRAM_LIMIT = 4000  # Telegram corta en 4096 caracteres


def telegram(text: str) -> bool:
    token, chat_id = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    try:
        for i in range(0, len(text), TELEGRAM_LIMIT):
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text[i:i + TELEGRAM_LIMIT], "disable_web_page_preview": True},
                timeout=20,
            ).raise_for_status()
        return True
    except requests.RequestException:
        return False


def macos(title: str, message: str) -> None:
    if platform.system() != "Darwin":
        return
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
            check=False, capture_output=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
