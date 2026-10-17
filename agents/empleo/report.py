"""Salidas del agente `empleo`: reporte Markdown del día y avisos por Telegram."""
from datetime import datetime

from agents.empleo.scorer import PERMITS
from core import notify
from core.config import REPORTS_DIR

MODES = {"remote": "Remoto", "hybrid": "Híbrido", "onsite": "Presencial", "unknown": "Modalidad n/d"}
SCHEDULES = {"part_time": "Parcial", "full_time": "Completa", "unknown": "Jornada n/d"}


def write_report(entries: list[dict], errors: dict, pending: int = 0) -> str:
    """Agrega la corrida a data/reports/empleo-AAAA-MM-DD.md, agrupada por encaje con el permiso."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"empleo-{datetime.now():%Y-%m-%d}.md"
    lines = [] if path.exists() else [f"# Ofertas de empleo — {datetime.now():%Y-%m-%d}\n"]
    lines.append(f"\n## Corrida {datetime.now():%H:%M}\n")
    lines += [f"- ⚠️ `{name}` falló: {err}" for name, err in errors.items()]
    if pending:
        lines.append(f"- Quedan {pending} ofertas por puntuar en la próxima corrida")
    if not entries:
        lines.append("\nSin ofertas nuevas sobre el umbral.\n")
    for permit, label in PERMITS.items():
        group = [e for e in entries if e["permit"] == permit]
        if group:
            lines.append(f"\n**{label} ({len(group)})**")
        for e in group:
            lines += [f"\n### [{e['score']}/10] {e['title']}",
                      f"- {MODES.get(e['work_mode'], MODES['unknown'])} · {SCHEDULES.get(e['schedule'], SCHEDULES['unknown'])}"
                      f" · Salario: {e['budget'] or 'n/d'} · Fuente: {e['source']}",
                      f"- Link: {e['url']}",
                      f"- Por qué: {e['reason']}",
                      *[f"- {n}" for n in e["notes"]]]
            if e.get("nota"):
                lines.append(f"- Nota para postular: {e['nota']}")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return str(path)


def send_alerts(entries: list[dict]) -> None:
    sent = False
    for e in entries:
        text = (f"{PERMITS[e['permit']]}\n💼 [{e['score']}/10] {e['title']}\n"
                f"{MODES.get(e['work_mode'], MODES['unknown'])} · {SCHEDULES.get(e['schedule'], SCHEDULES['unknown'])}"
                f" · Salario: {e['budget'] or 'n/d'}\n{e['reason']}\n" + "".join(f"{n}\n" for n in e["notes"])
                + (f"\n✍️ Nota para postular:\n{e['nota']}\n\n" if e.get("nota") else "") + e["url"])
        sent = notify.telegram(text, "empleo") or sent
    if not sent:
        notify.macos("Agente empleo", f"{len(entries)} ofertas de empleo para revisar")
