"""Filtrado de ofertas de empleo: pre-filtro por palabras (gratis) + preguntas concretas al modelo.

Como en el agente freelance, el modelo solo extrae datos; las reglas (jornada, permiso de trabajo,
modalidad y ciudad) se aplican aquí con las preferencias de config/empleo.yaml.
"""
from agents.freelance.scorer import _has_word
from agents.empleo.sources import EMAIL_MARK
from agents.freelance.sources import Offer
from core import llm
from core.config import env, profile_summary

SCORER_SYSTEM = """You evaluate a job offer in Spain for a specific candidate. Be strict and skeptical.
Never guess: if the offer does not state something, answer "unknown", null or false.
Return ONLY this JSON object:
{
  "main_role": "<the role being hired, max 8 words>",
  "role_is_technical": <true if the job is mainly software development, data, AI/ML or automation; false for sales, support, operations, HR, teaching, annotation>,
  "skill_match": <integer 0-10: how well the REQUIRED skills and seniority match the candidate. Unrelated stacks are 0-3.
                  If only the title is available, judge the title against the candidate's skills; missing details are NOT a reason for a low score>,
  "schedule": "part_time" | "full_time" | "unknown",
  "weekly_hours": <integer hours per week if stated, else null>,
  "work_mode": "remote" | "hybrid" | "onsite" | "unknown",
  "city": "<city where onsite/hybrid work happens, or empty>",
  "is_internship": <true for internships / prácticas / becas / convenio universitario>,
  "requires_unrestricted_permit": <true if it demands EU nationality or an unrestricted/permanent work permit>,
  "red_flags": <true if unpaid (except internships with stipend), scam-like, pyramid sales, or requires paying>,
  "reason": "<one short sentence in Spanish explaining the fit>"
}"""


def prefilter(offer: Offer, prefs: dict) -> bool:
    text = offer.text
    if any(_has_word(text, w) for w in prefs.get("excluir", [])):
        return False
    return any(_has_word(text, w) for w in prefs.get("palabras_clave", []))


def _same_city(city: str, cities: list[str]) -> bool:
    city = (city or "").lower()
    return any(c.lower() in city for c in cities)


LOW_INFO_MAX = 8  # con solo título y ubicación no se puede saber más; que lo revise una persona


def compute_score(a: dict, prefs: dict, low_info: bool = False) -> tuple[int, list[str], bool]:
    """Puntaje 0-10, notas para el aviso y si cumple el permiso de trabajo (≤ horas máximas, parcial,
    prácticas o jornada no indicada)."""
    notes = []
    if low_info:  # alertas por correo: lo que no aparece en el título o la ubicación, el modelo lo inventaría
        a = {**a, "schedule": "unknown", "weekly_hours": None, "requires_unrestricted_permit": False,
             "work_mode": a.get("work_mode") if a.get("work_mode") == "remote" else "unknown"}
    score = max(0, min(10, int(a.get("skill_match") or 0)))
    if not a.get("role_is_technical", False):
        score = min(score, 3)

    # Modalidad y ciudad
    mode, cities = a.get("work_mode", "unknown"), prefs.get("ciudades_presencial", [])
    if mode == "remote":
        if not prefs.get("remoto", True):
            score = min(score, 3)
    elif mode in ("hybrid", "onsite"):
        if not _same_city(a.get("city", ""), cities):
            score = min(score, 2)
            notes.append(f"{'Híbrido' if mode == 'hybrid' else 'Presencial'} fuera de {', '.join(cities) or 'tus ciudades'}")
    else:
        notes.append("Modalidad no indicada")
    preferred = {"remoto": "remote", "hibrido": "hybrid"}.get(prefs.get("preferencia", ""))
    if preferred and mode == preferred:
        score = min(10, score + 1)

    # Jornada frente al permiso de trabajo
    jornadas = prefs.get("jornadas", ["parcial", "completa"])
    max_hours = (prefs.get("permiso_trabajo") or {}).get("horas_max_semana")
    hours, schedule = a.get("weekly_hours"), a.get("schedule", "unknown")
    over_limit = bool(max_hours) and ((hours or 0) > max_hours or (hours is None and schedule == "full_time"))
    if schedule == "full_time" and "completa" not in jornadas:
        score = 0
    elif schedule == "part_time" and "parcial" not in jornadas:
        score = min(score, 3)
    elif over_limit:
        notes.append(f"⚠️ Jornada de {hours or '~40'} h: excede tu permiso de {max_hours} h (requiere ajustar jornada)")

    if a.get("is_internship") and prefs.get("practicas"):
        score = min(10, score + 1)
        notes.append("Prácticas / convenio")
    if a.get("requires_unrestricted_permit"):
        score = min(score, 3)
        notes.append("Pide nacionalidad UE o permiso sin restricciones")
    if a.get("red_flags"):
        score = 0
    if low_info:
        score = min(score, LOW_INFO_MAX)
        notes = [n for n in notes if n != "Modalidad no indicada"]
        notes.append("Pocos datos (alerta por correo): revisa modalidad, jornada y requisitos en el anuncio")
    return score, notes, bool(a.get("is_internship")) or not over_limit


def score(offer: Offer, profile: dict, prefs: dict, conn) -> dict | None:
    """Devuelve {"score", "reason", "notes", "fits_permit", "work_mode", "schedule"} o None si no hay puntuador disponible."""
    prompt = (
        f"CANDIDATE PROFILE:\n{profile_summary(profile)}\n\n"
        f"JOB OFFER ({offer.source}):\nTitle: {offer.title}\nSalary: {offer.budget or 'n/a'}\n"
        f"Description:\n{offer.description[:3000]}"
    )
    answer, error = None, None
    # Backend propio del agente (ej. groq con respaldo ollama); si no se define, el general SCORER_BACKEND
    if env("EMPLEO_SCORER_BACKEND"):
        chain = llm.backend_chain("EMPLEO_SCORER_BACKEND", "ollama", "EMPLEO_SCORER_FALLBACK")
    else:
        chain = llm.backend_chain("SCORER_BACKEND", "ollama", "SCORER_FALLBACK")
    for backend in chain:
        if not llm.is_ready(backend):
            continue
        try:
            if backend == "ollama":
                answer = llm.ollama_json(SCORER_SYSTEM, prompt)
            elif backend == "groq":
                answer = llm.groq_json(SCORER_SYSTEM, prompt, env("GROQ_SCORER_MODEL") or None)
            else:
                answer = llm.claude_json(SCORER_SYSTEM, prompt, "claude-haiku-5-5", "puntuar_empleo", conn)
            break
        except llm.LLMError as e:
            error = e  # se prueba el siguiente backend de la cadena
    if answer is None:
        if error:
            raise error
        return None
    value, notes, fits_permit = compute_score(answer, prefs, low_info=offer.description.startswith(EMAIL_MARK))
    return {"score": value, "reason": answer.get("reason", ""), "notes": notes, "fits_permit": fits_permit,
            "work_mode": answer.get("work_mode", "unknown"), "schedule": answer.get("schedule", "unknown")}
