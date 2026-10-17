"""Filtrado de ofertas de empleo: pre-filtro por palabras (gratis) + preguntas concretas al modelo.

Como en el agente freelance, el modelo solo extrae datos; las reglas (jornada, permiso de trabajo,
modalidad, ciudad y nivel) se aplican aquí con las preferencias de config/empleo.yaml.
"""
import re

from agents.empleo.sources import EMAIL_MARK
from agents.freelance.scorer import _has_word
from agents.freelance.sources import Offer
from core.config import profile_summary

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
  "hours_flexible": <true only if the offer says hours are flexible or negotiable, offers reduced hours / 4-day week, or accepts part-time>,
  "contract_type": "employee" | "contractor" | "internship" | "unknown",
                   <"contractor" = freelance, B2B, autónomo, per project or per hour invoicing>,
  "work_mode": "remote" | "hybrid" | "onsite" | "unknown",
  "city": "<city where onsite/hybrid work happens, or empty>",
  "remote_only_other_country": <true if it is remote but only for residents of a specific country that is not Spain>,
  "is_internship": <true for internships / prácticas / becas / convenio universitario>,
  "requires_unrestricted_permit": <true ONLY if the text explicitly demands EU nationality, citizenship or an unrestricted/permanent work permit; false otherwise>,
  "red_flags": <true if unpaid (except internships with stipend), scam-like, pyramid sales, or requires paying>,
  "reason": "<one short sentence in Spanish explaining the fit>"
}"""

# Encaje con el permiso de trabajo (ej. estancia por estudios: hasta 30 h/semana).
# Orden en avisos y reporte: primero lo que se puede aceptar tal cual.
PERMITS = {
    "cumple": "✅ Cumple tu permiso",
    "proyecto": "🟢 Por proyecto / freelance",
    "negociable": "🤝 Jornada negociable",
    "sin_dato": "❔ Jornada sin indicar",
    "excede": "🟡 Excede tu permiso",
}
# Título que nombra un país concreto (ej. "Engineering Manager | Sweden | Remote") sin abrir a España o Europa
OTHER_COUNTRY = re.compile(r"\b(uk|united kingdom|sweden|germany|france|poland|netherlands|ireland|italy|usa|us only|"
                           r"united states|canada|brazil|india|mexico|argentina|colombia|chile|australia|latam)\b", re.I)
OPEN_TO_SPAIN = re.compile(r"spain|españa|europe|emea|\beu\b|worldwide|anywhere", re.I)
# Dirección de equipos, por el título (el modelo la confundía con puestos técnicos senior)
MANAGEMENT = re.compile(r"\b(manager|head of|director|directora|vp|vice president|chief|cto|cio|jefe|jefa|gerente)\b", re.I)


def prefilter(offer: Offer, prefs: dict) -> bool:
    text = offer.text
    if any(_has_word(text, w) for w in prefs.get("excluir", [])):
        return False
    if OTHER_COUNTRY.search(offer.title) and not OPEN_TO_SPAIN.search(offer.title):
        return False  # remoto pero solo para residentes de otro país
    return any(_has_word(text, w) for w in prefs.get("palabras_clave", []))


def _same_city(city: str, cities: list[str]) -> bool:
    city = (city or "").lower()
    return any(c.lower() in city for c in cities)


LOW_INFO_MAX = 8  # con solo título y ubicación no se puede saber más; que lo revise una persona


def permit_fit(a: dict, prefs: dict) -> str:
    """Clave de PERMITS según jornada, horas y tipo de contrato frente a `permiso_trabajo.horas_max_semana`."""
    max_hours = (prefs.get("permiso_trabajo") or {}).get("horas_max_semana")
    hours, schedule = a.get("weekly_hours"), a.get("schedule", "unknown")
    if not max_hours or a.get("is_internship") or a.get("contract_type") == "internship":
        return "cumple"
    if hours and hours <= max_hours:
        return "cumple"
    if schedule == "part_time" and not hours:
        return "cumple"
    if a.get("contract_type") == "contractor":
        return "proyecto"  # facturando por horas o proyecto se puede pactar la dedicación
    if a.get("hours_flexible"):
        return "negociable"
    if schedule == "unknown" and not hours:
        return "sin_dato"
    return "excede"


def compute_score(a: dict, prefs: dict, low_info: bool = False, title: str = "") -> tuple[int, list[str], str]:
    """Puntaje 0-10, notas para el aviso y encaje con el permiso de trabajo (clave de PERMITS)."""
    notes = []
    if low_info:  # alertas por correo: lo que no aparece en el título o la ubicación, el modelo lo inventaría
        a = {**a, "schedule": "unknown", "weekly_hours": None, "requires_unrestricted_permit": False,
             "hours_flexible": False, "contract_type": "unknown",
             "work_mode": a.get("work_mode") if a.get("work_mode") == "remote" else "unknown"}
    score = max(0, min(10, int(a.get("skill_match") or 0)))
    if not a.get("role_is_technical", False):
        score = min(score, 3)

    # Modalidad y ciudad
    mode, cities = a.get("work_mode", "unknown"), prefs.get("ciudades_presencial", [])
    if mode == "remote":
        if not prefs.get("remoto", True):
            score = min(score, 3)
        if a.get("remote_only_other_country"):
            score = min(score, 2)
            notes.append("Remoto solo para residentes de otro país")
    elif mode in ("hybrid", "onsite"):
        if not _same_city(a.get("city", ""), cities):
            score = min(score, 2)
            notes.append(f"{'Híbrido' if mode == 'hybrid' else 'Presencial'} fuera de {', '.join(cities) or 'tus ciudades'}")
    else:
        notes.append("Modalidad no indicada")
    preferred = {"remoto": "remote", "hibrido": "hybrid"}.get(prefs.get("preferencia", ""))
    if preferred and mode == preferred:
        score = min(10, score + 1)

    # Dirección de equipos: casi nunca es a tiempo parcial
    if prefs.get("excluir_direccion") and MANAGEMENT.search(title):
        score = min(score, 4)
        notes.append("Puesto de dirección de equipos")

    # Jornada frente al permiso de trabajo
    jornadas = prefs.get("jornadas", ["parcial", "completa"])
    max_hours = (prefs.get("permiso_trabajo") or {}).get("horas_max_semana")
    schedule, permit = a.get("schedule", "unknown"), permit_fit(a, prefs)
    if schedule == "full_time" and "completa" not in jornadas:
        score = 0
    elif schedule == "part_time" and "parcial" not in jornadas:
        score = min(score, 3)
    if permit == "cumple" and prefs.get("priorizar_permiso", True) and not low_info:
        score = min(10, score + 1)
    elif permit == "proyecto":
        notes.append(f"Por proyecto: pacta una dedicación de ≤ {max_hours} h/semana")
    elif permit == "negociable":
        notes.append(f"Horario flexible: propón {max_hours} h/semana")
    elif permit == "excede":
        if prefs.get("priorizar_permiso", True):
            score = max(0, score - 1)
        notes.append(f"⚠️ Jornada de {a.get('weekly_hours') or '~40'} h: excede tu permiso de {max_hours} h")

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
    return score, notes, permit


def score(offer: Offer, profile: dict, prefs: dict, chain) -> dict:
    """Puntúa con la cadena de LangChain (core.llm_lc.json_chain). Devuelve
    {"score", "reason", "notes", "permit", "work_mode", "schedule"}. Si fallan todos los modelos, lanza la excepción."""
    prompt = (
        f"CANDIDATE PROFILE:\n{profile_summary(profile)}\n\n"
        f"JOB OFFER ({offer.source}):\nTitle: {offer.title}\nSalary: {offer.budget or 'n/a'}\n"
        f"Description:\n{offer.description[:3000]}"
    )
    answer = chain.invoke({"prompt": prompt})
    value, notes, permit = compute_score(answer, prefs, offer.description.startswith(EMAIL_MARK), offer.title)
    return {"score": value, "reason": answer.get("reason", ""), "notes": notes, "permit": permit,
            "work_mode": answer.get("work_mode", "unknown"), "schedule": answer.get("schedule", "unknown")}
