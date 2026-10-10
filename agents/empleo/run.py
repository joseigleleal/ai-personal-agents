"""Agente de empleo en España: recolecta → filtra → puntúa según jornada, permiso y modalidad → avisa.

Nunca postula: solo avisa por Telegram y deja el reporte en data/reports/empleo-AAAA-MM-DD.md.

Uso:
    python -m agents.empleo.run                 # corrida normal
    python -m agents.empleo.run --solo-fuentes  # solo prueba las fuentes
"""
import argparse
from datetime import datetime

from agents.empleo import scorer, sources
from core import db, llm, notify
from core.config import REPORTS_DIR, load_config, load_profile

MODES = {"remote": "Remoto", "hybrid": "Híbrido", "onsite": "Presencial", "unknown": "Modalidad n/d"}
SCHEDULES = {"part_time": "Parcial", "full_time": "Completa", "unknown": "Jornada n/d"}
# Primero las que caben en el permiso de trabajo; después las que lo exceden pero encajan con tu perfil
LABELS = {True: "✅ Cumple tu permiso", False: "🟡 Excede tu permiso, pero encaja"}


def write_report(entries: list[dict], errors: dict) -> str:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"empleo-{datetime.now():%Y-%m-%d}.md"
    lines = [] if path.exists() else [f"# Ofertas de empleo — {datetime.now():%Y-%m-%d}\n"]
    lines.append(f"\n## Corrida {datetime.now():%H:%M}\n")
    lines += [f"- ⚠️ Fuente `{name}` falló: {err}" for name, err in errors.items()]
    if not entries:
        lines.append("\nSin ofertas nuevas sobre el umbral.\n")
    for fits in (True, False):
        group = [e for e in entries if e["fits_permit"] == fits]
        if group:
            lines.append(f"\n**{LABELS[fits]} ({len(group)})**")
        for e in group:
            lines += _report_entry(e)
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return str(path)


def _report_entry(e: dict) -> list[str]:
    return [f"\n### [{e['score']}/10] {e['title']}",
            f"- {MODES[e['work_mode']]} · {SCHEDULES[e['schedule']]} · Salario: {e['budget'] or 'n/d'} · Fuente: {e['source']}",
            f"- Link: {e['url']}",
            f"- Por qué: {e['reason']}",
            *[f"- {n}" for n in e["notes"]]]


def send_alerts(entries: list[dict]) -> None:
    sent = False
    for e in entries:
        text = (f"{LABELS[e['fits_permit']]}\n💼 [{e['score']}/10] {e['title']}\n"
                f"{MODES[e['work_mode']]} · {SCHEDULES[e['schedule']]} · Salario: {e['budget'] or 'n/d'}\n"
                f"{e['reason']}\n" + "".join(f"{n}\n" for n in e["notes"]) + e["url"])
        sent = notify.telegram(text) or sent
    if not sent:
        notify.macos("Agente empleo", f"{len(entries)} ofertas de empleo para revisar")


def main() -> None:
    parser = argparse.ArgumentParser(description="Agente de búsqueda de empleo")
    parser.add_argument("--solo-fuentes", action="store_true", help="Solo recolecta y muestra conteos")
    parser.add_argument("--max-puntuar", type=int, default=60, help="Máximo de ofertas a puntuar por corrida")
    args = parser.parse_args()

    prefs, profile = load_config("empleo"), load_profile()
    conn = db.connect()
    offers, errors = sources.collect(prefs, conn, use_turns=not args.solo_fuentes)  # la prueba no gasta turnos

    unique = {}
    for o in offers:  # si la misma oferta aparece como recomendada y como postulada, gana "postulada"
        unique[o.id] = o if o.applied or o.id not in unique else unique[o.id]
    applied = [o for o in unique.values() if o.applied]
    for o in applied:  # ya postulé: se registra (o se actualiza) y nunca se avisa
        db.insert_offer(conn, o, status="postulada")
        db.update_offer(conn, o.id, status="postulada")
    new = [o for o in unique.values() if not db.offer_exists(conn, o.id)]
    by_source = {}
    for o in new:
        by_source[o.source] = by_source.get(o.source, 0) + 1
    print(f"Recolectadas: {len(unique)} · nuevas: {len(new)} · ya postuladas: {len(applied)} · por fuente: {by_source}")
    for name, err in errors.items():
        print(f"  ⚠️ {name}: {err}")
    if args.solo_fuentes:
        return

    candidates = []
    for o in new:
        if scorer.prefilter(o, prefs):
            candidates.append(o)
        else:
            db.insert_offer(conn, o, status="descartada")
    print(f"Pasan el pre-filtro: {len(candidates)}")

    threshold = prefs.get("umbral_aviso", 7)
    entries = []
    # Lo que excede el máximo queda sin registrar para puntuarlo en la próxima corrida
    for o in candidates[: args.max_puntuar]:
        try:
            result = scorer.score(o, profile, prefs, conn)
        except llm.LLMError as e:
            print(f"  ⚠️ No se pudo puntuar: {e}")
            break
        if result is None:
            print("  ⚠️ Ningún puntuador disponible (SCORER_BACKEND / SCORER_FALLBACK): no se puntúa.")
            break
        db.insert_offer(conn, o, status="puntuada")
        db.update_offer(conn, o.id, score=result["score"],
                        score_reason="; ".join([result["reason"], *result["notes"]]))
        print(f"  [{result['score']:>2}] {o.title[:80]}")
        if result["score"] >= threshold:
            entries.append({**result, "title": o.title, "url": o.url, "source": o.source, "budget": o.budget})

    entries.sort(key=lambda e: (not e["fits_permit"], -e["score"]))  # primero las que cumplen el permiso
    print(f"Reporte: {write_report(entries, errors)}")
    if entries:
        send_alerts(entries[: prefs.get("max_avisos_por_corrida", 10)])  # el reporte las tiene todas


if __name__ == "__main__":
    main()
