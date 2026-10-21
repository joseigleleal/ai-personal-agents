"""Avisos: Telegram (servidor o Mac) y notificación nativa de macOS como respaldo."""
import json
import platform
import subprocess
import time

import requests

from core.config import env

TELEGRAM_LIMIT = 4000  # Telegram corta en 4096 caracteres
ultimo_error = ""      # motivo del último envío fallido a Telegram (sin el token), para el log del agente


def _post(url: str, **kw) -> bool:
    """POST a Telegram con un reintento; si falla, deja el motivo en `ultimo_error`."""
    global ultimo_error
    for intento in range(2):
        try:
            for f in kw.get("files", {}).values():
                f.seek(0)
            r = requests.post(url, **kw)
            if r.ok:
                return True
            try:
                ultimo_error = f"HTTP {r.status_code}: {r.json().get('description', '')}"
            except ValueError:
                ultimo_error = f"HTTP {r.status_code}"
            if r.status_code < 500 and r.status_code != 429:
                return False
        except requests.RequestException as e:
            ultimo_error = type(e).__name__  # el mensaje completo incluiría la URL con el token
        if not intento:
            time.sleep(3)
    return False


def telegram(text: str, agent: str | None = None, silent: bool = False) -> bool:
    """Envía por el bot del agente (TELEGRAM_BOT_TOKEN_<AGENTE>) o, si no tiene, por el general.

    silent=True: llega sin sonido ni vibración (para avisos rutinarios).
    """
    suffix = f"_{agent.upper()}" if agent else ""
    token = env(f"TELEGRAM_BOT_TOKEN{suffix}") or env("TELEGRAM_BOT_TOKEN")
    chat_id = env(f"TELEGRAM_CHAT_ID{suffix}") or env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    return all(
        _post(f"https://api.telegram.org/bot{token}/sendMessage",
              json={"chat_id": chat_id, "text": text[i:i + TELEGRAM_LIMIT], "disable_web_page_preview": True,
                    "disable_notification": silent},
              timeout=20)
        for i in range(0, len(text), TELEGRAM_LIMIT)
    )


def telegram_estado(text: str, agent: str | None = None, message_id: int | None = None) -> int | None:
    """Mensaje de estado que se actualiza en el lugar: edita `message_id` y, si no se puede (no existe o lo
    borraste), manda uno nuevo en silencio. Devuelve el id del mensaje (guárdalo para la próxima) o None."""
    suffix = f"_{agent.upper()}" if agent else ""
    token = env(f"TELEGRAM_BOT_TOKEN{suffix}") or env("TELEGRAM_BOT_TOKEN")
    chat_id = env(f"TELEGRAM_CHAT_ID{suffix}") or env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return None
    api = f"https://api.telegram.org/bot{token}"
    text = text[:TELEGRAM_LIMIT]
    try:
        if message_id:
            r = requests.post(f"{api}/editMessageText", timeout=20, json={
                "chat_id": chat_id, "message_id": message_id, "text": text, "disable_web_page_preview": True})
            if r.ok or "not modified" in r.text:  # "message is not modified": el texto ya era ese
                return message_id
        r = requests.post(f"{api}/sendMessage", timeout=20, json={
            "chat_id": chat_id, "text": text, "disable_web_page_preview": True, "disable_notification": True})
        return r.json()["result"]["message_id"] if r.ok else None
    except (requests.RequestException, ValueError, KeyError):
        return None


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
    global ultimo_error
    try:
        with open(path, "rb") as f:
            return _post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption[:1000], "disable_notification": silent,
                      **({"reply_markup": json.dumps({"inline_keyboard": [[{"text": t, "callback_data": d}
                                                                           for t, d in botones]]})}
                         if botones else {})},
                files={"photo": f}, timeout=60,
            )
    except OSError as e:
        ultimo_error = f"no se pudo leer la imagen: {e}"
        return False


def telegram_album(paths: list, caption: str, agent: str | None = None, silent: bool = True) -> bool:
    """Envía hasta 10 imágenes en un solo mensaje (álbum), con el texto en la primera."""
    paths = list(paths)[:10]
    if len(paths) < 2:
        return bool(paths) and telegram_foto(paths[0], caption, agent, silent)
    suffix = f"_{agent.upper()}" if agent else ""
    token = env(f"TELEGRAM_BOT_TOKEN{suffix}") or env("TELEGRAM_BOT_TOKEN")
    chat_id = env(f"TELEGRAM_CHAT_ID{suffix}") or env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    global ultimo_error
    try:
        files = {f"f{i}": open(p, "rb") for i, p in enumerate(paths)}
    except OSError as e:
        ultimo_error = f"no se pudo leer la imagen: {e}"
        return False
    try:
        media = [{"type": "photo", "media": f"attach://f{i}", **({"caption": caption[:1000]} if not i else {})}
                 for i in range(len(paths))]
        return _post(f"https://api.telegram.org/bot{token}/sendMediaGroup",
                     data={"chat_id": chat_id, "media": json.dumps(media), "disable_notification": silent},
                     files=files, timeout=120)
    finally:
        for f in files.values():
            f.close()


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
