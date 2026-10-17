"""Agente de empleo en España: recolecta → filtra → puntúa según jornada, permiso y modalidad → avisa.

El flujo es un grafo de LangGraph (agents/empleo/graph.py) con su estado guardado en data/empleo_graph.db:
si una corrida se corta, la siguiente la retoma. Nunca postula: solo avisa por Telegram y deja el reporte
en data/reports/empleo-AAAA-MM-DD.md.

Uso:
    python -m agents.empleo.run                 # corrida normal
    python -m agents.empleo.run --solo-fuentes  # solo prueba las fuentes (no gasta turnos ni avisa)
    python -m agents.empleo.run --grafo         # imprime el grafo en Mermaid
    python -m agents.empleo.run --reiniciar     # descarta una corrida a medias y empieza de cero
    python -m agents.empleo.run --estado        # una línea: corrida a medias y ofertas pendientes
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone

from langgraph.checkpoint.sqlite import SqliteSaver

from agents.empleo import graph, sources
from core import db
from core.config import DATA_DIR, load_config, load_profile

CHECKPOINTS = DATA_DIR / "empleo_graph.db"
THREAD = {"configurable": {"thread_id": "empleo"}, "recursion_limit": 100}
RESUME_MAX_AGE = timedelta(hours=24)  # una corrida a medias más vieja que esto se descarta (no se reintenta siempre)


def status() -> str:
    """Estado guardado del grafo, sin correr nada (lo usa scripts/estado.sh)."""
    if not CHECKPOINTS.exists():
        return "sin corridas con LangGraph todavía"
    with SqliteSaver.from_conn_string(str(CHECKPOINTS)) as saver:
        tup = saver.get_tuple(THREAD)
    if not tup:
        return "sin estado guardado"
    values = tup.checkpoint["channel_values"]
    pending = len(values.get("pendientes") or [])
    state = "última corrida completa" if "reporte" in values else "corrida a medias: se retoma en la próxima"
    return f"{state} · {pending} oferta(s) pendientes de puntuar"


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)  # que el log de launchd se vea mientras corre
    parser = argparse.ArgumentParser(description="Agente de búsqueda de empleo (LangGraph)")
    parser.add_argument("--solo-fuentes", action="store_true", help="Solo recolecta y muestra conteos")
    parser.add_argument("--max-puntuar", type=int, default=60, help="Máximo de ofertas a puntuar por corrida")
    parser.add_argument("--grafo", action="store_true", help="Imprime el grafo en formato Mermaid y termina")
    parser.add_argument("--reiniciar", action="store_true", help="Descarta la corrida a medias y las pendientes")
    parser.add_argument("--estado", action="store_true", help="Muestra si hay una corrida a medias y cuántas pendientes")
    args = parser.parse_args()
    if args.estado:
        print(status())
        return

    prefs, profile, conn = load_config("empleo"), load_profile(), db.connect()
    if args.solo_fuentes:
        offers, errors = sources.collect(prefs, conn, use_turns=False)
        by_source = {}
        for o in offers:
            by_source[o.source] = by_source.get(o.source, 0) + 1
        print(f"Recolectadas: {len(offers)} · por fuente: {by_source}")
        for name, err in errors.items():
            print(f"  ⚠️ {name}: {err}")
        return

    app_graph = graph.build(prefs, profile, conn)
    if args.grafo:
        print(app_graph.compile().get_graph().draw_mermaid())
        return

    with SqliteSaver.from_conn_string(str(CHECKPOINTS)) as saver:
        app = app_graph.compile(checkpointer=saver)
        snap = app.get_state(THREAD)
        started = datetime.fromisoformat(snap.created_at) if snap.created_at else None
        fresh = started and datetime.now(timezone.utc) - started < RESUME_MAX_AGE
        if snap.next and fresh and not args.reiniciar:
            print(f"Retomando la corrida anterior desde: {', '.join(snap.next)}")
            app.invoke(None, THREAD, durability="sync")
            return
        # Corrida nueva: se arrastran las ofertas que quedaron sin puntuar y se borra el historial anterior
        leftover = [] if args.reiniciar else (snap.values or {}).get("pendientes", [])
        saver.delete_thread("empleo")
        app.invoke({"max_puntuar": args.max_puntuar, "pendientes": leftover}, THREAD, durability="sync")


if __name__ == "__main__":
    main()
