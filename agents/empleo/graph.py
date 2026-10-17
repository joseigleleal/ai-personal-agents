"""Flujo del agente `empleo` como grafo de LangGraph.

    recolectar ─▶ prefiltrar ─▶ puntuar ⟲ (lotes de 10) ─▶ priorizar ─▶ [redactar_notas] ─▶ reportar

- Estado: un diccionario tipado (EmpleoState) que pasa de nodo en nodo.
- Checkpointer SQLite (data/empleo_graph.db): tras cada nodo se guarda el estado. Si una corrida se corta
  (sin red, Groq caído, Mac dormida), la siguiente la retoma desde el último nodo en vez de empezar de cero.
- Ofertas que no alcanzaron a puntuarse (tope por corrida o sin modelo) quedan en `pendientes` y se puntúan
  en la próxima corrida, aunque esa corrida no vuelva a consultar la API de donde salieron.
"""
from dataclasses import asdict
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from agents.empleo import scorer, sources
from agents.freelance.sources import Offer
from agents.empleo.report import send_alerts, write_report
from core import db, llm_lc
from core.config import env

BATCH = 10  # ofertas por paso de `puntuar`: el estado se guarda después de cada lote

WRITER_SYSTEM = """You help a candidate apply to a job offer. The candidate is legally allowed to work in Spain
up to {hours} hours per week (as an employee or self-employed), because of a student residence permit.
Write a short note (max 3 sentences) the candidate can paste in the application form or send to the recruiter:
honest, confident, no apologies. State the {hours} h/week availability up front and propose one concrete
arrangement that fits this offer (e.g. 4 days x 7.5 h, mornings, or a contract/per-project basis).
Do not invent experience or facts. No greeting, no signature. Write in the same language as the offer title."""


class EmpleoState(TypedDict, total=False):
    max_puntuar: int           # tope de ofertas a puntuar en esta corrida
    nuevas: list[dict]         # ofertas nuevas (Offer como dict, para que el checkpointer las guarde)
    errores: dict              # fuente → error
    pendientes: list[dict]     # pasaron el pre-filtro y faltan por puntuar (se arrastran entre corridas)
    puntuadas: int             # cuántas se puntuaron en esta corrida
    elegidas: list[dict]       # puntuadas sobre el umbral: lo que va al reporte
    sin_modelo: str            # motivo si no se pudo puntuar (ningún modelo disponible o todos fallaron)
    avisos: list[dict]         # las que se avisan por Telegram
    reporte: str               # ruta del reporte


def build(prefs: dict, profile: dict, conn) -> StateGraph:
    """Arma el grafo. Las dependencias (preferencias, perfil, base de datos) quedan en el cierre de cada nodo,
    no en el estado: así el checkpointer solo guarda datos serializables."""
    # Backend propio del agente (ej. groq con respaldo ollama); si no se define, el general SCORER_BACKEND
    prefix = "EMPLEO_SCORER" if env("EMPLEO_SCORER_BACKEND") else "SCORER"
    scorer_model = llm_lc.chat_model(f"{prefix}_BACKEND", "ollama", f"{prefix}_FALLBACK", conn, json_mode=True, temperature=0,
                                     groq_model=env("GROQ_SCORER_MODEL"))
    score_chain = scorer_model and llm_lc.json_chain(scorer.SCORER_SYSTEM, scorer_model)
    if score_chain:
        score_chain = score_chain.with_config(callbacks=[llm_lc.ClaudeCost(conn, "puntuar_empleo")])

    def recolectar(state: EmpleoState) -> dict:
        offers, errors = sources.collect(prefs, conn)
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
        return {"nuevas": [asdict(o) for o in new], "errores": errors}

    def prefiltrar(state: EmpleoState) -> dict:
        pending = {o["source"] + ":" + o["external_id"]: o for o in state.get("pendientes", [])}
        for data in state["nuevas"]:
            o = Offer(**data)
            if o.id in pending:
                continue
            if scorer.prefilter(o, prefs):
                pending[o.id] = data
            else:
                db.insert_offer(conn, o, status="descartada")
        print(f"Para puntuar: {len(pending)} (incluye las que quedaron de corridas anteriores)")
        return {"pendientes": list(pending.values()), "nuevas": [], "puntuadas": 0, "elegidas": []}

    def puntuar(state: EmpleoState) -> dict:
        if not score_chain:
            return {"sin_modelo": "ningún puntuador disponible (EMPLEO_SCORER_BACKEND / SCORER_BACKEND)"}
        pending, chosen, done = list(state["pendientes"]), list(state["elegidas"]), state["puntuadas"]
        threshold = prefs.get("umbral_aviso", 7)
        for data in pending[: min(BATCH, state["max_puntuar"] - done)]:
            o = Offer(**data)
            try:
                result = scorer.score(o, profile, prefs, score_chain)
            except Exception as e:  # fallaron el modelo principal y el respaldo: lo que falta queda pendiente
                print(f"  ⚠️ No se pudo puntuar: {type(e).__name__}: {str(e)[:200]}")
                return {"pendientes": pending, "elegidas": chosen, "puntuadas": done, "sin_modelo": str(e)[:200]}
            pending.remove(data)
            done += 1
            db.insert_offer(conn, o, status="puntuada")
            db.update_offer(conn, o.id, score=result["score"],
                            score_reason="; ".join([scorer.PERMITS[result["permit"]], result["reason"], *result["notes"]]))
            print(f"  [{result['score']:>2}] {scorer.PERMITS[result['permit']][:1]} {o.title[:80]}")
            if result["score"] >= threshold:
                chosen.append({**result, "id": o.id, "title": o.title, "url": o.url, "source": o.source, "budget": o.budget})
        return {"pendientes": pending, "elegidas": chosen, "puntuadas": done}

    def seguir_puntuando(state: EmpleoState) -> str:
        if state.get("sin_modelo") or not state["pendientes"] or state["puntuadas"] >= state["max_puntuar"]:
            return "priorizar"
        return "puntuar"

    def priorizar(state: EmpleoState) -> dict:
        order = list(scorer.PERMITS)
        chosen = sorted(state["elegidas"], key=lambda e: (order.index(e["permit"]), -e["score"]))
        # Lo que excede el permiso solo se avisa si es excelente y en pocas unidades; el reporte lo tiene todo
        top_over, max_over = prefs.get("umbral_excede", 9), prefs.get("max_avisos_excede", 2)
        alerts, over = [], 0
        for e in chosen:
            if e["permit"] == "excede":
                if e["score"] < top_over or over >= max_over:
                    continue
                over += 1
            alerts.append(e)
        return {"elegidas": chosen, "avisos": alerts[: prefs.get("max_avisos_por_corrida", 10)]}

    def hay_que_redactar(state: EmpleoState) -> str:
        return "redactar_notas" if any(e["permit"] != "cumple" for e in state["avisos"]) else "reportar"

    def redactar_notas(state: EmpleoState) -> dict:
        model = llm_lc.chat_model("WRITER_BACKEND", "ollama", "WRITER_FALLBACK", conn, temperature=0.5)
        if not model:
            return {}
        hours = (prefs.get("permiso_trabajo") or {}).get("horas_max_semana") or 30
        chain = llm_lc.text_chain(WRITER_SYSTEM.format(hours=hours), model).with_config(
            callbacks=[llm_lc.ClaudeCost(conn, "nota_empleo")])
        alerts = []
        for e in state["avisos"]:
            if e["permit"] != "cumple" and not e.get("nota"):
                try:
                    e = {**e, "nota": chain.invoke({"prompt": f"Offer: {e['title']}\nWhy it fits: {e['reason']}"})}
                except Exception as err:  # sin nota el aviso igual sirve
                    print(f"  ⚠️ No se pudo redactar la nota: {type(err).__name__}")
            alerts.append(e)
        return {"avisos": alerts}

    def reportar(state: EmpleoState) -> dict:
        errors = dict(state.get("errores") or {})
        if state.get("sin_modelo"):
            errors["puntuador"] = state["sin_modelo"]
        notes = {e["id"]: e.get("nota") for e in state["avisos"]}
        entries = [{**e, "nota": notes.get(e["id"])} for e in state["elegidas"]]
        path = write_report(entries, errors, len(state["pendientes"]))
        print(f"Reporte: {path}")
        if state["avisos"]:
            send_alerts(state["avisos"])
        return {"reporte": path}

    graph = StateGraph(EmpleoState)
    for node in (recolectar, prefiltrar, puntuar, priorizar, redactar_notas, reportar):
        graph.add_node(node.__name__, node)
    graph.add_edge(START, "recolectar")
    graph.add_edge("recolectar", "prefiltrar")
    graph.add_edge("prefiltrar", "puntuar")
    graph.add_conditional_edges("puntuar", seguir_puntuando, ["puntuar", "priorizar"])
    graph.add_conditional_edges("priorizar", hay_que_redactar, ["redactar_notas", "reportar"])
    graph.add_edge("redactar_notas", "reportar")
    graph.add_edge("reportar", END)
    return graph
