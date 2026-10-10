"""Filtrado: pre-filtro por palabras (gratis) + puntuación con Ollama (gratis) o Haiku (respaldo).

El modelo solo responde preguntas concretas; el puntaje final se calcula aquí,
así un modelo local pequeño no infla los resultados.
"""
import re

from agents.freelance.sources import Offer
from core import llm
from core.config import env, profile_summary

SCORER_SYSTEM = """You evaluate a job offer for a specific freelancer. Be strict and skeptical.
Return ONLY this JSON object:
{
  "main_role": "<the role the client is hiring for, max 8 words>",
  "role_is_hands_on_development": <true if the work is mainly writing code / building software, data or AI systems; false for managers, sales, operations, HR, design, annotation, support>,
  "skill_match": <integer 0-10: how well the REQUIRED skills match the freelancer's skills. Unrelated stacks (e.g. frontend-only, Golang-only, pricing strategy) are 0-3>,
  "engagement": "freelance" | "contract" | "part_time" | "full_time" | "unknown",
  "remote_possible": <false if the offer requires onsite or hybrid work in a specific city; true otherwise>,
  "red_flags": <true if unpaid, vague, scam-like, crypto trading, or requires paying to apply>,
  "reason": "<one short sentence in Spanish explaining the fit>"
}"""

SPANISH_HINTS = re.compile(r"(?<!\w)(el|la|los|las|de|del|para|con|una|que|proyecto|buscamos|necesitamos|experiencia)(?!\w)")
ENGLISH_HINTS = re.compile(r"(?<!\w)(the|and|for|with|we|you|are|looking|need|experience|project)(?!\w)")


def detect_language(offer: Offer) -> str:
    text = offer.text
    return "es" if len(SPANISH_HINTS.findall(text)) > len(ENGLISH_HINTS.findall(text)) else "en"


def _has_word(text: str, word: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(word.strip())}(?!\w)", text) is not None


def prefilter(offer: Offer, profile: dict) -> bool:
    text, title = offer.text, offer.title.lower()
    if any(_has_word(text, w) for w in profile.get("excluir", [])):
        return False
    return (any(_has_word(text, w) for w in profile.get("palabras_clave", []))
            or any(_has_word(title, w) for w in profile.get("palabras_debiles", [])))


def compute_score(answer: dict) -> int:
    score = max(0, min(10, int(answer.get("skill_match", 0))))
    if not answer.get("role_is_hands_on_development", False):
        score = min(score, 3)
    if answer.get("engagement") == "full_time":
        score = min(score, 5)   # empleo fijo: se registra, pero no se redacta propuesta freelance
    if answer.get("remote_possible") is False:
        score = min(score, 3)   # presencial / híbrido: no aplica trabajando remoto
    if answer.get("red_flags"):
        score = 0
    return score


def score(offer: Offer, profile: dict, conn) -> dict | None:
    """Devuelve {"score", "reason", "language", ...} o None si no hay puntuador disponible."""
    prompt = (
        f"FREELANCER PROFILE:\n{profile_summary(profile)}\n\n"
        f"JOB OFFER ({offer.source}):\nTitle: {offer.title}\nBudget: {offer.budget or 'n/a'}\n"
        f"Description:\n{offer.description[:3000]}"
    )
    answer, error = None, None
    for backend in llm.backend_chain("SCORER_BACKEND", "ollama", "SCORER_FALLBACK"):
        if not llm.is_ready(backend):
            continue
        try:
            if backend == "ollama":
                answer = llm.ollama_json(SCORER_SYSTEM, prompt)
            elif backend == "groq":
                answer = llm.groq_json(SCORER_SYSTEM, prompt, env("GROQ_SCORER_MODEL") or None)
            else:
                answer = llm.claude_json(SCORER_SYSTEM, prompt, "claude-haiku-5-5", "puntuar_oferta", conn)
            break
        except llm.LLMError as e:
            error = e  # se prueba el siguiente backend de la cadena
    if answer is None:
        if error:
            raise error
        return None
    reason = answer.get("reason", "")
    if answer.get("engagement"):
        reason = f"{reason} ({answer['engagement']})"
    return {"score": compute_score(answer), "reason": reason, "language": detect_language(offer)}
