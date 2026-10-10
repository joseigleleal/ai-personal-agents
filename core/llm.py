"""Clientes de modelos: Ollama (local, gratis), Groq (API, plan gratis) y Claude (API, con registro de costo)."""
import json
import re
import time

import anthropic
import requests

from core import db
from core.config import env, env_float

# USD por millón de tokens (entrada, salida). Referencia: precios publicados de Anthropic.
PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-opus-5-5": (4.00, 20.00),
}
# Modelos que aceptan el fallback del servidor ante rechazos (Haiku no lo tiene).
SUPPORTS_FALLBACKS = {"claude-sonnet-5-5", "claude-opus-5-5"}


class LLMError(RuntimeError):
    pass


class BudgetExceeded(LLMError):
    pass


def _parse_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        raise LLMError(f"Respuesta sin JSON: {text[:200]}")
    return json.loads(match.group(0))


# ---------------------------------------------------------------- Ollama

def ollama_available() -> bool:
    try:
        requests.get(f"{env('OLLAMA_URL', 'http://localhost:11434')}/api/tags", timeout=3).raise_for_status()
        return True
    except requests.RequestException:
        return False


def _ollama_chat(system: str, prompt: str, model: str | None, json_mode: bool, temperature: float) -> str:
    payload = {
        "model": model or env("OLLAMA_MODEL", "qwen3:8b"),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "stream": False,
        "think": False,
        "options": {"temperature": temperature},
    }
    if json_mode:
        payload["format"] = "json"
    url = f"{env('OLLAMA_URL', 'http://localhost:11434')}/api/chat"
    r = requests.post(url, json=payload, timeout=180)
    if r.status_code == 400 and "think" in r.text:
        # Modelos sin modo "thinking" rechazan el parámetro
        payload.pop("think")
        r = requests.post(url, json=payload, timeout=180)
    if r.status_code == 404:
        raise LLMError(f"Modelo de Ollama no descargado. Ejecuta: ollama pull {payload['model']}")
    r.raise_for_status()
    return re.sub(r"<think>.*?</think>", "", r.json()["message"]["content"], flags=re.S).strip()


def ollama_json(system: str, prompt: str, model: str | None = None) -> dict:
    """Pide al modelo local una respuesta en JSON."""
    return _parse_json(_ollama_chat(system, prompt, model, json_mode=True, temperature=0.2))


def ollama_text(system: str, prompt: str, model: str | None = None) -> str:
    """Texto libre con el modelo local (ej. redactar propuestas gratis)."""
    return _ollama_chat(system, prompt, model, json_mode=False, temperature=0.6)


# ---------------------------------------------------------------- Claude

_client: anthropic.Anthropic | None = None


def _claude() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not env("ANTHROPIC_API_KEY"):
            raise LLMError("Falta ANTHROPIC_API_KEY en .env")
        _client = anthropic.Anthropic()
    return _client


def claude_text(system: str, prompt: str, model: str, purpose: str, conn,
                effort: str = "medium", max_tokens: int = 8000) -> str:
    """Llama a Claude, registra el costo y respeta el tope mensual."""
    budget = env_float("MONTHLY_BUDGET_USD", 10)
    spent = db.month_spend(conn)
    if spent >= budget:
        raise BudgetExceeded(f"Tope mensual alcanzado: {spent:.2f} / {budget:.2f} USD")

    kwargs = dict(
        model=model,
        max_tokens=max_tokens,
        system=system,
        output_config={"effort": effort},
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        if model in SUPPORTS_FALLBACKS:
            # Si el modelo rechaza la solicitud, el servidor reintenta con otro modelo.
            response = _claude().beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
            )
        else:
            response = _claude().messages.create(**kwargs)
    except anthropic.RateLimitError as e:
        raise LLMError(f"Límite de uso de la API: {e.message}") from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"Error de la API ({e.status_code}): {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LLMError("Sin conexión con la API de Claude") from e

    used_model = response.model
    price_in, price_out = PRICES.get(used_model, PRICES.get(model, (0, 0)))
    usd = (response.usage.input_tokens * price_in + response.usage.output_tokens * price_out) / 1_000_000
    db.record_cost(conn, "anthropic", used_model, response.usage.input_tokens,
                   response.usage.output_tokens, usd, purpose)

    if response.stop_reason == "refusal":
        raise LLMError("El modelo rechazó la solicitud")
    return "".join(b.text for b in response.content if b.type == "text").strip()


def claude_json(system: str, prompt: str, model: str, purpose: str, conn) -> dict:
    return _parse_json(claude_text(system, prompt, model, purpose, conn, effort="low", max_tokens=2000))


# ---------------------------------------------------------------- Groq

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def groq_available() -> bool:
    return bool(env("GROQ_API_KEY"))


def _groq_chat(system: str, prompt: str, model: str | None, json_mode: bool, temperature: float) -> str:
    if not groq_available():
        raise LLMError("Falta GROQ_API_KEY en .env")
    payload = {
        "model": model or env("GROQ_MODEL", "openai/gpt-oss-120b"),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {env('GROQ_API_KEY')}"}
    for attempt in range(3):
        try:
            r = requests.post(GROQ_URL, json=payload, headers=headers, timeout=120)
        except requests.RequestException as e:
            raise LLMError("Sin conexión con la API de Groq") from e
        if r.status_code != 429:
            break
        # Plan gratis: límite por minuto. Espera lo que pide el servidor (máx. 60 s) y reintenta.
        wait = min(float(r.headers.get("retry-after", 20)), 60)
        if attempt == 2 or wait >= 60:
            raise LLMError("Límite de uso de Groq alcanzado (plan gratis)")
        time.sleep(wait)
    if r.status_code >= 400:
        raise LLMError(f"Error de la API de Groq ({r.status_code}): {r.text[:200]}")
    content = r.json()["choices"][0]["message"]["content"] or ""
    return re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()


def groq_json(system: str, prompt: str, model: str | None = None) -> dict:
    return _parse_json(_groq_chat(system, prompt, model, json_mode=True, temperature=0.2))


def groq_text(system: str, prompt: str, model: str | None = None) -> str:
    return _groq_chat(system, prompt, model, json_mode=False, temperature=0.6)


# ---------------------------------------------------------------- Selección de backend

BACKENDS = ("ollama", "groq", "claude")


def backend_chain(main_var: str, main_default: str, fallback_var: str) -> list[str]:
    """Backends a probar en orden, según el .env (ej. SCORER_BACKEND=groq, SCORER_FALLBACK=ollama)."""
    chain = []
    for name in (env(main_var, main_default), env(fallback_var, "skip")):
        name = name.strip().lower()
        if name in BACKENDS and name not in chain:
            chain.append(name)
    return chain


def is_ready(backend: str) -> bool:
    if backend == "ollama":
        return ollama_available()
    if backend == "groq":
        return groq_available()
    return bool(env("ANTHROPIC_API_KEY"))
