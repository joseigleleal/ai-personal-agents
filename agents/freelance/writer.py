"""Redacción de propuestas (Ollama o Groq gratis, o Claude). El agente NUNCA las envía: quedan como borrador."""
import re

from agents.freelance.sources import Offer
from core import llm
from core.config import env, profile_summary

WRITER_SYSTEM = """You write freelance proposals (cover letters) on behalf of the freelancer described below.

{profile}

Rules:
- Write in {language_name}, the same language the client used.
- Write in FIRST PERSON SINGULAR ("I" / "yo"): the freelancer works alone, never "we" / "nosotros".
- 120-200 words. Plain text, no markdown headings. NEVER use placeholders or brackets like [Name];
  greet with a simple "Hi," / "Hola," if the client's name is unknown.
- Open with the client's problem, not with "I am". Show you understood the specific job.
- Propose a concrete approach in 2-4 short steps, choosing only tools that fit THIS job
  (do not list skills just because they are in the profile).
- Mention at most one relevant past achievement from the profile.
- Never invent past clients, projects, numbers or certifications that are not in the profile.
- End with one smart clarifying question and a simple next step (short call or quick demo).
- Output ONLY the proposal text."""


def write_proposal(offer: Offer, language: str, profile: dict, conn) -> str:
    system = WRITER_SYSTEM.format(
        profile=profile_summary(profile),
        language_name="Spanish (neutral Latin American)" if language == "es" else "English",
    )
    prompt = (
        f"JOB OFFER ({offer.source})\nTitle: {offer.title}\nBudget: {offer.budget or 'n/a'}\n"
        f"URL: {offer.url}\n\nDescription:\n{offer.description}"
    )
    error = llm.LLMError("Ningún backend disponible (revisa WRITER_BACKEND, Ollama o las API keys)")
    for backend in llm.backend_chain("WRITER_BACKEND", "ollama", "WRITER_FALLBACK"):
        if not llm.is_ready(backend):
            continue
        try:
            if backend == "ollama":
                text = llm.ollama_text(system, prompt, env("OLLAMA_WRITER_MODEL") or None)
            elif backend == "groq":
                text = llm.groq_text(system, prompt, env("GROQ_WRITER_MODEL") or None)
            else:
                model = env("CLAUDE_WRITER_MODEL", "claude-sonnet-5-5")
                text = llm.claude_text(system, prompt, model, "redactar_propuesta", conn)
            return _clean(text)
        except llm.LLMError as e:
            error = e  # se prueba el siguiente backend de la cadena
    raise error


def _clean(text: str) -> str:
    """Red de seguridad para modelos locales: quita marcadores tipo [Nombre del Cliente]."""
    text = re.sub(r"\s*\[[^\]]{2,40}\]", "", text)
    return re.sub(r"[ \t]+([,.])", r"\1", text).strip()
