"""Agente freelance: recolecta → filtra → puntúa → redacta borradores → reporta.

Uso:
    python -m agents.freelance.run                 # corrida normal
    python -m agents.freelance.run --sin-propuestas  # solo puntúa, no redacta
    python -m agents.freelance.run --solo-fuentes  # solo prueba las fuentes
"""
import argparse

from agents.freelance import report, scorer, sources, writer
from core import db, llm
from core.config import env_float, env_int, load_profile


def main() -> None:
    parser = argparse.ArgumentParser(description="Agente freelance asistido")
    parser.add_argument("--sin-propuestas", action="store_true", help="Solo puntúa, no redacta propuestas")
    parser.add_argument("--solo-fuentes", action="store_true", help="Solo recolecta y muestra conteos")
    parser.add_argument("--max-puntuar", type=int, default=40, help="Máximo de ofertas a puntuar por corrida")
    args = parser.parse_args()

    profile = load_profile()
    conn = db.connect()
    offers, errors = sources.collect(profile)

    # Quitar duplicados de esta corrida y ofertas ya vistas
    unique = {o.id: o for o in offers}
    new = [o for o in unique.values() if not db.offer_exists(conn, o.id)]
    by_source = {}
    for o in new:
        by_source[o.source] = by_source.get(o.source, 0) + 1
    print(f"Recolectadas: {len(unique)} · nuevas: {len(new)} · por fuente: {by_source}")
    for name, err in errors.items():
        print(f"  ⚠️ {name}: {err}")
    if args.solo_fuentes:
        return

    candidates = []
    for o in new:
        if scorer.prefilter(o, profile):
            candidates.append(o)
        else:
            db.insert_offer(conn, o, status="descartada")
    print(f"Pasan el pre-filtro: {len(candidates)}")

    threshold = profile.get("umbral_propuesta", 7)
    max_proposals = env_int("MAX_PROPOSALS_PER_RUN", 5)
    entries, proposals = [], 0

    # Lo que excede el máximo queda sin registrar para puntuarlo en la próxima corrida
    for o in candidates[: args.max_puntuar]:
        try:
            result = scorer.score(o, profile, conn)
        except llm.LLMError as e:
            print(f"  ⚠️ No se pudo puntuar: {e}")
            break
        if result is None:
            print("  ⚠️ Ningún puntuador disponible (SCORER_BACKEND / SCORER_FALLBACK): no se puntúa.")
            break
        db.insert_offer(conn, o, status="puntuada")
        db.update_offer(conn, o.id, score=result["score"], score_reason=result["reason"],
                        language=result["language"])
        print(f"  [{result['score']:>2}] {o.title[:80]}")
        if result["score"] < threshold:
            continue

        entry = {**result, "title": o.title, "url": o.url, "source": o.source, "budget": o.budget}
        if args.sin_propuestas:
            entry["note"] = "Propuesta no redactada (modo --sin-propuestas)."
        elif proposals >= max_proposals:
            entry["note"] = "Propuesta no redactada (máximo por corrida alcanzado)."
        else:
            try:
                entry["proposal"] = writer.write_proposal(o, result["language"], profile, conn)
                db.update_offer(conn, o.id, proposal=entry["proposal"], status="propuesta")
                proposals += 1
            except llm.LLMError as e:
                entry["note"] = f"Propuesta no redactada: {e}"
        entries.append(entry)

    spend, budget = db.month_spend(conn), env_float("MONTHLY_BUDGET_USD", 10)
    path = report.append_to_report(entries, spend, budget, errors)
    print(f"Reporte: {path} · gasto del mes: {spend:.4f} USD")
    if entries:
        report.send_alerts(entries, proposals)


if __name__ == "__main__":
    main()
