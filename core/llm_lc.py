"""Modelos vía LangChain: la misma elección por .env que core/llm.py (Ollama | Groq | Claude), con respaldo.

Lo usa el agente `empleo` (piloto con LangGraph). Diferencias con core/llm.py:
- Cada backend es un `ChatModel` de LangChain y el respaldo se arma con `.with_fallbacks()`.
- El costo de Claude se registra con un callback y el tope mensual se revisa antes de incluir Claude en la cadena.
"""
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable, RunnableLambda

from core import db, llm
from core.config import env, env_float

CLAUDE_MODEL = "claude-haiku-5-5"


class ClaudeCost(BaseCallbackHandler):
    """Registra en la tabla `costs` lo que gasta cada llamada a Claude (las de Ollama y Groq son gratis)."""

    def __init__(self, conn, purpose: str):
        self.conn, self.purpose = conn, purpose

    def on_llm_end(self, response, **kwargs) -> None:
        for gens in response.generations:
            for gen in gens:
                msg = getattr(gen, "message", None)
                model = (getattr(msg, "response_metadata", None) or {}).get("model_name") or ""
                usage = getattr(msg, "usage_metadata", None)
                if not model.startswith("claude") or not usage:
                    continue
                price_in, price_out = llm.PRICES.get(model, llm.PRICES.get(CLAUDE_MODEL, (0, 0)))
                usd = (usage["input_tokens"] * price_in + usage["output_tokens"] * price_out) / 1_000_000
                db.record_cost(self.conn, "anthropic", model, usage["input_tokens"], usage["output_tokens"],
                               usd, self.purpose)


def _model(backend: str, json_mode: bool, temperature: float, groq_model: str | None,
           ollama_model: str | None = None) -> BaseChatModel:
    if backend == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(model=ollama_model or env("OLLAMA_MODEL", "qwen3:8b"), base_url=env("OLLAMA_URL", "http://localhost:11434"),
                          format="json" if json_mode else None, reasoning=False, temperature=temperature)
    if backend == "groq":
        from langchain_groq import ChatGroq
        kwargs = {"response_format": {"type": "json_object"}} if json_mode else {}
        # max_retries: el SDK de Groq espera y reintenta ante el límite por minuto del plan gratis
        return ChatGroq(model=groq_model or env("GROQ_MODEL", "openai/gpt-oss-120b"), temperature=temperature,
                        max_retries=3, request_timeout=120, model_kwargs=kwargs)
    from langchain_anthropic import ChatAnthropic
    return ChatAnthropic(model=CLAUDE_MODEL, temperature=temperature, max_tokens=2000, max_retries=2)


def chat_model(main_var: str, main_default: str, fallback_var: str, conn, *, json_mode: bool = False,
               temperature: float = 0.2, groq_model: str | None = None,
               ollama_model: str | None = None) -> Runnable | None:
    """Modelo principal con su respaldo (ej. EMPLEO_SCORER_BACKEND=groq, EMPLEO_SCORER_FALLBACK=ollama).

    Solo entran los backends listos (Ollama encendido, clave de Groq o Claude). Claude sale de la cadena si
    ya se llegó a MONTHLY_BUDGET_USD. Devuelve None si no queda ninguno.
    """
    chain = [b for b in llm.backend_chain(main_var, main_default, fallback_var) if llm.is_ready(b)]
    if "claude" in chain and db.month_spend(conn) >= env_float("MONTHLY_BUDGET_USD", 10):
        chain.remove("claude")
    if not chain:
        return None
    models = [_model(b, json_mode, temperature, groq_model, ollama_model) for b in chain]
    return models[0].with_fallbacks(models[1:]) if len(models) > 1 else models[0]


def json_chain(system: str, model: Runnable) -> Runnable:
    """prompt → modelo → texto → dict. El usuario se pasa como {"prompt": "..."}."""
    prompt = ChatPromptTemplate.from_messages([("system", "{system}"), ("human", "{prompt}")]).partial(system=system)
    return prompt | model | StrOutputParser() | RunnableLambda(llm._parse_json)


def text_chain(system: str, model: Runnable) -> Runnable:
    prompt = ChatPromptTemplate.from_messages([("system", "{system}"), ("human", "{prompt}")]).partial(system=system)
    return prompt | model | StrOutputParser()
