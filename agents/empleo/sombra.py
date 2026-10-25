"""Modo sombra del puntuador: un modelo local (el afinado con LoRA) puntúa y uno grande (Groq) puntúa lo mismo
por detrás, para medir al local con ofertas reales antes de dejarlo solo.

- Cada oferta queda en la tabla `sombra`: respuesta de los dos, puntaje, si avisarían, señales de estafa y segundos.
- Desacuerdo = uno avisaría y el otro no. Quién manda entonces: EMPLEO_SOMBRA_MANDA=sombra (por defecto, el grande:
  no se pierde una oferta buena mientras el local aprende) o =principal (el local decide siempre).
- Realimentación: cada respuesta completa de la sombra se agrega a data/lora/etiquetas.jsonl, el mismo archivo que
  arma el experimento LoRA (experimentos/lora_mlx), así el próximo entrenamiento aprende de las ofertas nuevas.
- Si la sombra falla (tope diario de Groq, sin red): mientras manda la sombra, la oferta queda pendiente para la
  próxima corrida (el local solo, con sesgo conocido, mandaría avisos de más); con =principal decide el local.
- Una vez por semana llega por Telegram un informe con el acuerdo de los dos.
"""
import json
import time
from datetime import datetime, timedelta

from agents.empleo import scorer
from core import db, notify
from core.config import DATA_DIR, env

ETIQUETAS = DATA_DIR / "lora" / "etiquetas.jsonl"
# Mismos campos que pide SCORER_SYSTEM (y que valida experimentos/lora_mlx/dataset.py)
CAMPOS = ("main_role", "role_is_technical", "skill_match", "schedule", "weekly_hours", "hours_flexible",
          "contract_type", "work_mode", "city", "remote_only_other_country", "is_internship",
          "requires_unrestricted_permit", "red_flags", "reason")
SCHEMA = """
CREATE TABLE IF NOT EXISTS sombra (
    offer_id TEXT PRIMARY KEY, ts TEXT, titulo TEXT,
    principal TEXT, sombra TEXT,                 -- respuestas JSON del modelo (la sombra puede faltar)
    puntaje_p INTEGER, puntaje_s INTEGER,
    avisa_p INTEGER, avisa_s INTEGER,            -- 1 si el puntaje llega a umbral_aviso
    estafa_p INTEGER, estafa_s INTEGER,          -- red_flags
    segundos_p REAL, manda TEXT                  -- qué resultado se usó: principal | sombra
);
"""


class Puntuador:
    """Puntúa con el modelo principal y, si hay sombra, la compara y registra. Mismo resultado que scorer.score."""

    def __init__(self, conn, profile: dict, prefs: dict, principal, sombra=None):
        self.conn, self.profile, self.prefs = conn, profile, prefs
        self.principal, self.sombra = principal, sombra
        self.manda = env("EMPLEO_SOMBRA_MANDA", "sombra").strip().lower()
        self.umbral = prefs.get("umbral_aviso", 7)
        if sombra:
            conn.executescript(SCHEMA)

    def __call__(self, offer) -> dict:
        prompt = scorer.build_prompt(offer, self.profile)
        inicio = time.monotonic()
        answer_p = self.principal.invoke({"prompt": prompt})  # si falla, la oferta queda pendiente (como siempre)
        segundos = time.monotonic() - inicio
        res_p = scorer.result(answer_p, offer, self.prefs)
        if not self.sombra:
            return res_p
        try:
            answer_s = self.sombra.invoke({"prompt": prompt})
        except Exception as e:
            if self.manda == "sombra":  # sin la sombra no se decide: la oferta queda pendiente (como sin modelo)
                raise
            print(f"  (sombra sin respuesta: {type(e).__name__})")  # manda el local: queda sin comparar
            answer_s = None
        res_s = scorer.result(answer_s, offer, self.prefs) if answer_s else None
        avisa_p = res_p["score"] >= self.umbral
        avisa_s = res_s and res_s["score"] >= self.umbral
        usar_sombra = res_s is not None and avisa_p != avisa_s and self.manda == "sombra"
        self.conn.execute(
            "INSERT OR REPLACE INTO sombra VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (offer.id, db.now(), offer.title, json.dumps(answer_p, ensure_ascii=False),
             answer_s and json.dumps(answer_s, ensure_ascii=False), res_p["score"], res_s and res_s["score"],
             int(avisa_p), None if res_s is None else int(avisa_s), int(bool(answer_p.get("red_flags"))),
             None if res_s is None else int(bool(answer_s.get("red_flags"))), round(segundos, 1),
             "sombra" if usar_sombra else "principal"))
        self.conn.commit()
        if answer_s and all(c in answer_s for c in CAMPOS):
            guardar_etiqueta(offer.id, prompt, answer_s)
        if res_s is not None and avisa_p != avisa_s:
            print(f"  ↔ desacuerdo: local {res_p['score']} · sombra {res_s['score']} → manda {'sombra' if usar_sombra else 'local'}")
        return res_s if usar_sombra else res_p


def guardar_etiqueta(offer_id: str, prompt: str, answer: dict) -> None:
    """Agrega el ejemplo al dataset del LoRA (si la oferta no estaba ya)."""
    ETIQUETAS.parent.mkdir(parents=True, exist_ok=True)
    if ETIQUETAS.exists() and f'"id": "{offer_id}"' in ETIQUETAS.read_text():
        return
    with ETIQUETAS.open("a") as f:
        f.write(json.dumps({"id": offer_id, "prompt": prompt, "respuesta": {c: answer[c] for c in CAMPOS}},
                           ensure_ascii=False) + "\n")


def informe(conn, dias: int = 7) -> str | None:
    """Resumen de la última semana: acuerdo entre el local y la sombra. None si no hubo comparaciones."""
    conn.executescript(SCHEMA)
    desde = (datetime.now() - timedelta(days=dias)).isoformat(timespec="seconds")
    filas = conn.execute("SELECT * FROM sombra WHERE ts >= ?", (desde,)).fetchall()
    comparadas = [f for f in filas if f["puntaje_s"] is not None]
    if not filas:
        return None
    n = len(comparadas)
    lineas = [f"🧪 Puntuador local vs. Groq (últimos {dias} días)",
              f"· Puntuadas: {len(filas)} · comparadas con Groq: {n}"]
    if n:
        misma = sum(f["avisa_p"] == f["avisa_s"] for f in comparadas)
        estafa = sum(f["estafa_p"] == f["estafa_s"] for f in comparadas)
        error = sum(abs(f["puntaje_p"] - f["puntaje_s"]) for f in comparadas) / n
        solo_local = [f for f in comparadas if f["avisa_p"] and not f["avisa_s"]]
        solo_groq = [f for f in comparadas if f["avisa_s"] and not f["avisa_p"]]
        lineas += [f"· Misma decisión de avisar: {100 * misma / n:.0f} % · error medio de puntaje: {error:.2f}",
                   f"· Señales de estafa coinciden: {100 * estafa / n:.0f} %",
                   f"· Avisaría solo el local: {len(solo_local)} · solo Groq: {len(solo_groq)}"]
        for f in (solo_groq + solo_local)[:6]:
            lineas.append(f"   – [{f['puntaje_p']} vs {f['puntaje_s']}] {f['titulo'][:70]}")
    lineas.append(f"· Segundos por oferta (local): {sum(f['segundos_p'] for f in filas) / len(filas):.1f}")
    total = sum(1 for l in ETIQUETAS.read_text().split("\n") if l.strip()) if ETIQUETAS.exists() else 0
    lineas.append(f"· Ejemplos para reentrenar: {total} en total ({n} nuevos esta semana)")
    return "\n".join(lineas)


def informe_semanal(conn) -> None:
    """Manda el informe una vez por semana (la primera corrida de cada semana)."""
    texto = informe(conn)
    semana = datetime.now().strftime("%G-W%V")
    if texto and db.alert_once(conn, f"sombra:{semana}"):
        notify.telegram(texto, "empleo", silent=True)
