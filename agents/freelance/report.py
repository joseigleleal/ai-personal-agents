"""Reporte diario en Markdown + avisos (Telegram / macOS)."""
from datetime import datetime

from core import notify
from core.config import REPORTS_DIR


def append_to_report(entries: list[dict], spend: float, budget: float, errors: dict) -> str:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"{datetime.now():%Y-%m-%d}.md"
    lines = []
    if not path.exists():
        lines.append(f"# Ofertas freelance — {datetime.now():%Y-%m-%d}\n")
    lines.append(f"\n## Corrida {datetime.now():%H:%M}\n")
    lines.append(f"Gasto del mes: {spend:.2f} / {budget:.2f} USD\n")
    for name, err in errors.items():
        lines.append(f"- ⚠️ Fuente `{name}` falló: {err}")
    if not entries:
        lines.append("\nSin ofertas nuevas sobre el umbral.\n")
    for e in entries:
        lines.append(f"\n### [{e['score']}/10] {e['title']}")
        lines.append(f"- Fuente: {e['source']} · Presupuesto: {e['budget'] or 'n/d'} · Idioma: {e['language']}")
        lines.append(f"- Link: {e['url']}")
        lines.append(f"- Por qué: {e['reason']}")
        if e.get("proposal"):
            lines.append(f"\n**Borrador de propuesta** (revisar antes de enviar):\n\n{e['proposal']}\n")
        elif e.get("note"):
            lines.append(f"- {e['note']}")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return str(path)


def send_alerts(entries: list[dict], proposals: int) -> None:
    """Un mensaje por oferta buena (con su borrador) a Telegram; si no hay Telegram, aviso de macOS."""
    sent = False
    for e in entries:
        text = f"[{e['score']}/10] {e['title']}\n{e['url']}\nPresupuesto: {e['budget'] or 'n/d'}\n{e['reason']}"
        if e.get("proposal"):
            text += f"\n\n--- Borrador ---\n{e['proposal']}"
        sent = notify.telegram(text) or sent
    if not sent:
        notify.macos("Agente freelance", f"{len(entries)} ofertas buenas, {proposals} borradores listos")
