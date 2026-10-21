"""Arma el dataset de destilación: un modelo grande ("maestro") etiqueta las ofertas guardadas y esas respuestas
son lo que el modelo chico ("alumno") aprende a imitar.

    .venv/bin/python -m experimentos.lora_mlx.dataset --etiquetar     # maestro → data/lora/etiquetas.jsonl
    .venv/bin/python -m experimentos.lora_mlx.dataset --separar       # → data/lora/datos/{train,valid,test}.jsonl

--etiquetar se puede cortar y retomar: salta las ofertas ya etiquetadas. Con el plan gratis de Groq (8000 tokens
por minuto) espera solo cuando llega al límite.
"""
import argparse
import json
import random
import time

from agents.empleo import scorer
from agents.freelance.sources import Offer
from core import db, llm
from core.config import DATA_DIR, load_config

CARPETA = DATA_DIR / "lora"
ETIQUETAS = CARPETA / "etiquetas.jsonl"
DATOS = CARPETA / "datos"
MAESTRO = "openai/gpt-oss-120b"
CAMPOS = ("main_role", "role_is_technical", "skill_match", "schedule", "weekly_hours", "hours_flexible",
          "contract_type", "work_mode", "city", "remote_only_other_country", "is_internship",
          "requires_unrestricted_permit", "red_flags", "reason")


def ofertas() -> list[Offer]:
    """Ofertas guardadas por el agente `empleo`, con algo de texto (las de solo título también sirven)."""
    conn = db.connect()
    filas = conn.execute("SELECT * FROM offers WHERE length(title) > 5 ORDER BY id").fetchall()
    return [Offer(source=f["source"], external_id=f["id"].split(":", 1)[-1], title=f["title"], url=f["url"] or "",
                  description=f["description"] or "", budget=f["budget"] or "") for f in filas]


def etiquetadas() -> set[str]:
    if not ETIQUETAS.exists():
        return set()
    return {json.loads(l)["id"] for l in ETIQUETAS.read_text().splitlines() if l.strip()}


def etiquetar(maestro: str, limite: int | None) -> None:
    CARPETA.mkdir(parents=True, exist_ok=True)
    perfil, hechas = load_config("profile"), etiquetadas()
    pendientes = [o for o in ofertas() if o.id not in hechas][:limite]
    print(f"Ya etiquetadas: {len(hechas)} · por etiquetar: {len(pendientes)} (maestro {maestro})")
    for i, o in enumerate(pendientes, 1):
        prompt = scorer.build_prompt(o, perfil)
        for intento in range(5):
            try:
                respuesta = llm.groq_json(scorer.SCORER_SYSTEM, prompt, maestro)
                break
            except llm.LLMError as e:
                if "Límite" not in str(e) and "Sin conexión" not in str(e):
                    print(f"  ⚠️ {o.id}: {str(e)[:120]}")
                    respuesta = None
                    break
                time.sleep(20 * (intento + 1))  # límite por minuto o corte de red: esperar y reintentar
        else:
            print("Groq sigue limitando o sin conexión (¿tope diario?). Retomar más tarde con el mismo comando.")
            return
        if not respuesta or not all(c in respuesta for c in CAMPOS):
            continue  # respuesta incompleta: no sirve como ejemplo
        with ETIQUETAS.open("a") as f:
            f.write(json.dumps({"id": o.id, "prompt": prompt, "respuesta": {c: respuesta[c] for c in CAMPOS}},
                               ensure_ascii=False) + "\n")
        print(f"  {i}/{len(pendientes)} {o.title[:70]}")


def separar(semilla: int = 7) -> None:
    """80 % entrenamiento, 10 % validación (para ver si sobreajusta), 10 % prueba (solo para evaluar al final).
    Formato `messages` de mlx-lm: el mismo system y mensaje que usa el agente, y la respuesta del maestro."""
    filas = [json.loads(l) for l in ETIQUETAS.read_text().splitlines() if l.strip()]
    random.Random(semilla).shuffle(filas)
    n = len(filas)
    cortes = {"train": filas[: int(n * 0.8)], "valid": filas[int(n * 0.8): int(n * 0.9)], "test": filas[int(n * 0.9):]}
    DATOS.mkdir(parents=True, exist_ok=True)
    for nombre, grupo in cortes.items():
        with (DATOS / f"{nombre}.jsonl").open("w") as f:
            for r in grupo:
                f.write(json.dumps({"id": r["id"], "messages": [
                    {"role": "system", "content": scorer.SCORER_SYSTEM},
                    {"role": "user", "content": r["prompt"]},
                    {"role": "assistant", "content": json.dumps(r["respuesta"], ensure_ascii=False)},
                ]}, ensure_ascii=False) + "\n")
        print(f"{nombre}: {len(grupo)} ejemplos")


def main() -> None:
    parser = argparse.ArgumentParser(description="Dataset de destilación para el LoRA")
    parser.add_argument("--etiquetar", action="store_true", help="El maestro etiqueta las ofertas pendientes")
    parser.add_argument("--maestro", default=MAESTRO, help="Modelo de Groq que etiqueta")
    parser.add_argument("--limite", type=int, help="Máximo de ofertas a etiquetar en esta corrida")
    parser.add_argument("--separar", action="store_true", help="Separa en train/valid/test")
    args = parser.parse_args()
    if args.etiquetar:
        etiquetar(args.maestro, args.limite)
    if args.separar:
        separar()


if __name__ == "__main__":
    main()
