"""Avisos: Telegram (servidor o Mac) y notificación nativa de macOS como respaldo."""
import json
import platform
import subprocess

import requests

from core.config import env

TELEGRAM_LIMIT = 4000  # Telegram corta en 4096 caracteres


def telegram(text: str, agent: str | None = None, silent: bool = False) -> bool:
    """Envía por el bot del agente (TELEGRAM_BOT_TOKEN_<AGENTE>) o, si no tiene, por el general.

    silent=True: llega sin sonido ni vibración (para avisos rutinarios).
    """
    suffix = f"_{agent.upper()}" if agent else ""
    token = env(f"TELEGRAM_BOT_TOKEN{suffix}") or env("TELEGRAM_BOT_TOKEN")
    chat_id = env(f"TELEGRAM_CHAT_ID{suffix}") or env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    try:
        for i in range(0, len(text), TELEGRAM_LIMIT):
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text[i:i + TELEGRAM_LIMIT], "disable_web_page_preview": True,
                      "disable_notification": silent},
                timeout=20,
            ).raise_for_status()
        return True
    except requests.RequestException:
        return False


def telegram_foto(path, caption: str, agent: str | None = None, silent: bool = False,
                  botones: list[tuple[str, str]] | None = None) -> bool:
    """Envía una imagen (p. ej. una captura) por el mismo bot que telegram().

    botones: [(texto, dato)] en una fila; al pulsar uno, el bot recibe un callback_query con ese dato.
    """
    suffix = f"_{agent.upper()}" if agent else ""
    token = env(f"TELEGRAM_BOT_TOKEN{suffix}") or env("TELEGRAM_BOT_TOKEN")
    chat_id = env(f"TELEGRAM_CHAT_ID{suffix}") or env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    try:
        with open(path, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption[:1000], "disable_notification": silent,
                      **({"reply_markup": json.dumps({"inline_keyboard": [[{"text": t, "callback_data": d}
                                                                           for t, d in botones]]})}
                         if botones else {})},
                files={"photo": f}, timeout=60,
            ).raise_for_status()
        return True
    except (OSError, requests.RequestException):
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
