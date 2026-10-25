"""Compara un modelo con el maestro sobre el conjunto de prueba (ofertas que el alumno nunca vio).

    .venv/bin/python -m experimentos.lora_mlx.evaluar --nombre base                                # Qwen sin LoRA
    .venv/bin/python -m experimentos.lora_mlx.evaluar --nombre lora --adaptador data/lora/adaptador
    .venv/bin/python -m experimentos.lora_mlx.evaluar --nombre ollama --ollama empleo-puntuador   # ya exportado

Mide lo que le importa al agente, no solo la pérdida del entrenamiento:
- JSON válido y con todos los campos;
- acierto por campo (los que alimentan las reglas de puntaje);
- puntaje final (las mismas reglas de agents/empleo/scorer.py) frente al del maestro, y si coincide la decisión
  de avisar (puntaje ≥ umbral);
- segundos por oferta.
"""
import argparse
import json
import time
from statistics import mean

import requests

from agents.empleo import scorer
from agents.empleo.sources import EMAIL_MARK
from core.config import DATA_DIR, env, load_config
from experimentos.lora_mlx.dataset import CAMPOS, DATOS

MODELO_BASE = "Qwen/Qwen2.5-1.5B-Instruct"
EXACTOS = ("role_is_technical", "schedule", "hours_flexible", "contract_type", "work_mode",
           "remote_only_other_country", "is_internship", "requires_unrestricted_permit", "red_flags")


def generador_mlx(modelo: str, adaptador: str | None):
    from mlx_lm import generate, load
    from mlx_lm.sample_utils import make_sampler
    model, tokenizer = load(modelo, adapter_path=adaptador)
    sampler = make_sampler(temp=0.0)  # determinista, como el agente (temperature=0)

    def responder(mensajes: list[dict]) -> str:
        prompt = tokenizer.apply_chat_template(mensajes, add_generation_prompt=True, tokenize=False)
        return generate(model, tokenizer, prompt, max_tokens=400, sampler=sampler)
    return responder


def generador_ollama(modelo: str):
    url = env("OLLAMA_URL", "http://localhost:11434")

    def responder(mensajes: list[dict]) -> str:
        r = requests.post(f"{url}/api/chat", timeout=300, json={
            "model": modelo, "messages": mensajes, "stream": False, "format": "json",
            "options": {"temperature": 0}})
        r.raise_for_status()
        return r.json()["message"]["content"]
    return responder


def datos_oferta(prompt: str) -> tuple[str, bool]:
    """Título y si la oferta vino de una alerta por correo (pocos datos), sacados del prompt."""
    titulo = prompt.split("JOB OFFER", 1)[-1].split("\nTitle: ", 1)[1].split("\n", 1)[0]  # no el del perfil
    return titulo, prompt.split("Description:\n", 1)[-1].startswith(EMAIL_MARK)


def evaluar(responder, prefs: dict, limite: int | None) -> dict:
    ejemplos = [json.loads(l) for l in (DATOS / "test.jsonl").read_text().split("\n") if l.strip()][:limite]
    umbral = prefs.get("umbral_aviso", 7)
    validos, aciertos, dif_skill, dif_puntaje, misma_decision, tiempos = 0, {c: 0 for c in EXACTOS}, [], [], 0, []
    for i, ej in enumerate(ejemplos, 1):
        maestro = json.loads(ej["messages"][2]["content"])
        inicio = time.time()
        texto = responder(ej["messages"][:2])
        tiempos.append(time.time() - inicio)
        try:
            alumno = json.loads(texto[texto.index("{"): texto.rindex("}") + 1])
        except ValueError:
            alumno = {}
        if not all(c in alumno for c in CAMPOS):
            print(f"  {i:>3} ✗ JSON inválido o incompleto")
            continue
        validos += 1
        for c in EXACTOS:
            aciertos[c] += alumno[c] == maestro[c]
        try:
            dif_skill.append(abs(int(alumno["skill_match"]) - int(maestro["skill_match"])))
        except (TypeError, ValueError):
            dif_skill.append(10)
        titulo, poco = datos_oferta(ej["messages"][1]["content"])
        p_alumno = scorer.compute_score(alumno, prefs, poco, titulo)[0]
        p_maestro = scorer.compute_score(maestro, prefs, poco, titulo)[0]
        dif_puntaje.append(abs(p_alumno - p_maestro))
        misma_decision += (p_alumno >= umbral) == (p_maestro >= umbral)
        print(f"  {i:>3} puntaje {p_alumno:>2} vs maestro {p_maestro:>2} · {titulo[:60]}")
    n = len(ejemplos)
    return {
        "ejemplos": n,
        "json_valido_pct": round(100 * validos / n, 1),
        "acierto_por_campo_pct": {c: round(100 * v / max(validos, 1), 1) for c, v in aciertos.items()},
        "acierto_campos_promedio_pct": round(100 * sum(aciertos.values()) / max(validos * len(EXACTOS), 1), 1),
        "skill_match_error_medio": round(mean(dif_skill), 2) if dif_skill else None,
        "puntaje_error_medio": round(mean(dif_puntaje), 2) if dif_puntaje else None,
        "misma_decision_de_aviso_pct": round(100 * misma_decision / n, 1),
        "segundos_por_oferta": round(mean(tiempos), 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evalúa un modelo contra el maestro en el conjunto de prueba")
    parser.add_argument("--nombre", required=True, help="Nombre del resultado (base, lora, ollama…)")
    parser.add_argument("--modelo", default=MODELO_BASE, help="Modelo MLX (Hugging Face o carpeta local)")
    parser.add_argument("--adaptador", help="Carpeta del adaptador LoRA")
    parser.add_argument("--ollama", help="En vez de MLX, un modelo servido por Ollama (OLLAMA_URL)")
    parser.add_argument("--limite", type=int, help="Evaluar solo los primeros N ejemplos")
    args = parser.parse_args()
    responder = generador_ollama(args.ollama) if args.ollama else generador_mlx(args.modelo, args.adaptador)
    resultado = evaluar(responder, load_config("empleo"), args.limite)
    (DATA_DIR / "lora").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "lora" / f"eval-{args.nombre}.json").write_text(json.dumps(resultado, ensure_ascii=False, indent=1))
    print(json.dumps(resultado, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
