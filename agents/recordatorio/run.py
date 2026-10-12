"""Recordatorio diario por Telegram de los pendientes que faltan configurar.

Lee la sección "Pendientes del usuario" de las notas locales (memoria.md, fuera de git): cada pendiente es un
título "### N. [ ] ..." seguido de sus pasos. Los que dependen de una clave del .env se dan por hechos
cuando la clave ya está completa, aunque no se hayan marcado [x]. Envía la lista y los pasos del primero.

Uso:
    python -m agents.recordatorio.run           # un aviso por día como máximo
    python -m agents.recordatorio.run --forzar  # envía aunque ya se haya avisado hoy
    python -m agents.recordatorio.run --ver     # solo muestra el mensaje
"""
import argparse
import re
from datetime import date
from pathlib import Path

from core import db, notify
from core.config import ROOT, env

NOTES = Path(env("PENDIENTES_FILE") or ROOT / "memoria.md")
SECTION = re.compile(r"^## [^\n]*Pendientes del usuario[^\n]*$(.*?)(?=^## |\Z)", re.M | re.S)
ITEM = re.compile(r"^### \d+\. \[ \] (.+?)$(.*?)(?=^### |\Z)", re.M | re.S)
# Palabra del título → variable del .env que lo resuelve
DONE_WHEN_SET = {"IMAP": "IMAP_USER", "Groq": "GROQ_API_KEY", "Adzuna": "ADZUNA_APP_ID",
                 "InfoJobs API": "INFOJOBS_CLIENT_ID", "Claude API": "ANTHROPIC_API_KEY"}
MAX_STEPS_CHARS = 3000  # Telegram admite 4000 por mensaje


def pending_items() -> list[tuple[str, str]]:
    if not NOTES.exists():
        return []
    section = SECTION.search(NOTES.read_text(encoding="utf-8"))
    if not section:
        return []
    items = []
    for title, steps in ITEM.findall(section.group(1)):
        if any(word in title and env(var) for word, var in DONE_WHEN_SET.items()):
            continue
        items.append((title.replace("`", "").strip(), steps.strip()))
    return items


def build_message(items: list[tuple[str, str]]) -> str:
    title, steps = items[0]
    steps = re.sub(r"`|\*\*", "", steps)[:MAX_STEPS_CHARS]
    lines = [f"📋 Pendientes de Personal Agents ({len(items)})", ""]
    lines += [f"{i}. {t}" for i, (t, _) in enumerate(items, 1)]
    lines += ["", f"👉 Siguiente: {title}", steps, "", "Estado completo: ./scripts/estado.sh"]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Recordatorio diario de pendientes")
    parser.add_argument("--forzar", action="store_true", help="Envía aunque ya se haya avisado hoy")
    parser.add_argument("--ver", action="store_true", help="Solo muestra el mensaje, no lo envía")
    args = parser.parse_args()

    items = pending_items()
    if not items:
        print("Sin pendientes abiertos: no se envía nada.")
        return
    message = build_message(items)
    if args.ver:
        print(message)
        return
    conn = db.connect()
    if not args.forzar and not db.alert_once(conn, f"recordatorio:{date.today()}"):
        print("Ya se envió el recordatorio de hoy.")
        return
    if not notify.telegram(message, "recordatorio"):
        notify.macos("Pendientes", f"{len(items)} pendientes: {items[0][0]}")
    print(f"Recordatorio enviado: {len(items)} pendientes.")


if __name__ == "__main__":
    main()
