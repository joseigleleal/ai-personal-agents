"""Agente `eco`: sugiere cómo responderías tú un mensaje, aprendido de tus propios chats. Todo local (Ollama).

    python -m agents.eco.run --preparar               # turnos + perfil de estilo + índice de búsqueda
    python -m agents.eco.run --sugerir "¿vienes hoy?" [--chat Ana]
    python -m agents.eco.run --probar 10              # turnos reales al azar: lo que dijiste vs. lo que sugiere
    python -m agents.eco.run --dataset                # train/valid para afinar con LoRA (experimentos/lora_mlx)

Nunca envía nada: solo sugiere. Los chats y el índice quedan en data/eco.nosync/ (fuera de git e iCloud) y no salen
de la máquina: el modelo y los embeddings corren en Ollama local.
"""
import argparse
import json
import random

import numpy as np
import requests

from agents.eco import datos, estilo
from core import llm
from core.config import env

TURNOS = datos.CARPETA / "turnos.jsonl"
VECTORES = datos.CARPETA / "vectores.npy"
PERFIL = datos.CARPETA / "perfil.json"
EJEMPLOS = 8

SISTEMA = """You ghost-write WhatsApp replies exactly as one specific person writes them. The previous turns of
this conversation are real examples of how they replied to messages like the next one.
Their style, measured on thousands of their real messages:
{perfil}

Reply ONLY with what they would send: same language, dialect, slang, spelling habits and length; usually
{mensajes} short message(s), one per line. Never repeat the other person's message, no quotes, no explanations.
Do not invent concrete facts (times, places, names, yes/no to a plan): if the reply depends on that, keep it
vague or ask back, the way they would."""
MAX_LINEAS = 5


def embeber(textos: list[str]) -> np.ndarray:
    """Embeddings normalizados con un modelo local multilingüe (ECO_EMBED, por defecto bge-m3)."""
    url = f"{env('OLLAMA_URL', 'http://localhost:11434')}/api/embed"
    vectores = []
    for i in range(0, len(textos), 64):
        r = requests.post(url, json={"model": env("ECO_EMBED", "bge-m3"), "input": textos[i:i + 64]}, timeout=300)
        if r.status_code == 404:
            raise llm.LLMError(f"Falta el modelo de embeddings. Ejecuta: ollama pull {env('ECO_EMBED', 'bge-m3')}")
        r.raise_for_status()
        vectores += r.json()["embeddings"]
    m = np.array(vectores, dtype=np.float32)
    return m / np.linalg.norm(m, axis=1, keepdims=True)


def preparar() -> str:
    filas = datos.pares()
    if not filas:
        return f"No hay chats exportados en {datos.origen()} (ECO_DATOS)."
    turnos = datos.turnos(filas)
    datos.CARPETA.mkdir(parents=True, exist_ok=True)
    perfil = estilo.perfil(turnos)
    PERFIL.write_text(json.dumps(perfil, ensure_ascii=False, indent=1))
    con_contexto = [t for t in turnos if t["contexto"]]
    TURNOS.write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in con_contexto))
    np.save(VECTORES, embeber([t["contexto"] for t in con_contexto]))
    return (f"Listo: {len(filas)} mensajes → {len(turnos)} turnos ({len(con_contexto)} con mensaje previo, "
            f"indexados). Perfil en {PERFIL}.")


def cargar() -> tuple[list[dict], np.ndarray, dict]:
    if not TURNOS.exists():
        raise SystemExit("Primero: python -m agents.eco.run --preparar")
    turnos = [json.loads(l) for l in TURNOS.read_text().split("\n") if l.strip()]
    return turnos, np.load(VECTORES), json.loads(PERFIL.read_text())


def parecidos(vector: np.ndarray, turnos: list[dict], vectores: np.ndarray, chat: str = "",
              excluir: int | None = None) -> list[dict]:
    """Los turnos cuyo mensaje previo más se parece; los del mismo chat suben un poco (el trato cambia por persona)."""
    puntaje = vectores @ vector
    if chat:
        puntaje = puntaje + 0.05 * np.array([chat.lower() in t["chat"].lower() for t in turnos])
    if excluir is not None:
        puntaje[excluir] = -1
    return [turnos[i] for i in np.argsort(-puntaje)[:EJEMPLOS]]


def sugerir(mensaje: str, chat: str = "", indice=None, excluir: int | None = None) -> str:
    """Pocos ejemplos como conversación previa (mensaje del otro → tu respuesta real) y el mensaje nuevo al final."""
    turnos, vectores, perfil = indice or cargar()
    ejemplos = parecidos(embeber([mensaje])[0], turnos, vectores, chat, excluir)
    sistema = SISTEMA.format(perfil=estilo.describir(perfil), mensajes=round(perfil.get("mensajes_por_turno", 2)))
    conversacion = [{"role": "system", "content": sistema}]
    for t in reversed(ejemplos):  # el más parecido queda justo antes del mensaje nuevo
        conversacion += [{"role": "user", "content": t["contexto"]},
                         {"role": "assistant", "content": "\n".join(t["mensajes"])}]
    conversacion.append({"role": "user", "content": mensaje})
    r = requests.post(f"{env('OLLAMA_URL', 'http://localhost:11434')}/api/chat", timeout=300, json={
        "model": env("ECO_MODEL", "qwen3:8b"), "messages": conversacion, "stream": False, "think": False,
        "options": {"temperature": 0.6, "num_predict": 120}})
    r.raise_for_status()
    lineas = [l.strip() for l in r.json()["message"]["content"].split("\n") if l.strip()]
    return "\n".join(lineas[:MAX_LINEAS])


def probar(n: int) -> None:
    """Turnos reales al azar (sin usarlos como ejemplo de sí mismos): lo que dijiste vs. lo que sugiere eco."""
    indice = cargar()
    turnos = indice[0]
    elegidos = random.Random(7).sample(range(len(turnos)), min(n, len(turnos)))
    reales, sugeridas = [], []
    for i in elegidos:
        t = turnos[i]
        sugerida = sugerir(t["contexto"], t["chat"], indice, excluir=i)
        reales.append(" ".join(t["mensajes"]))
        sugeridas.append(sugerida.replace("\n", " "))
        print(f"\n💬 {t['contexto'][:200]}\n   tú:  {' / '.join(t['mensajes'])[:200]}\n   eco: {sugerida.replace(chr(10), ' / ')[:200]}")
    parecido = (embeber(reales) * embeber(sugeridas)).sum(axis=1)
    print(f"\nParecido de significado promedio (0-1, embeddings): {parecido.mean():.2f}")


def dataset() -> str:
    """train/valid en formato de chat para mlx_lm (mismo pipeline que experimentos/lora_mlx)."""
    turnos = cargar()[0]
    filas = [{"messages": [{"role": "system", "content": "Reply as this person would on WhatsApp."},
                           {"role": "user", "content": t["contexto"]},
                           {"role": "assistant", "content": "\n".join(t["mensajes"])}]} for t in turnos]
    random.Random(7).shuffle(filas)
    corte = int(len(filas) * 0.95)
    destino = datos.CARPETA / "lora"
    destino.mkdir(parents=True, exist_ok=True)
    for nombre, parte in (("train", filas[:corte]), ("valid", filas[corte:])):
        (destino / f"{nombre}.jsonl").write_text("".join(json.dumps(f, ensure_ascii=False) + "\n" for f in parte))
    return f"Dataset LoRA: {corte} de entrenamiento y {len(filas) - corte} de validación en {destino}."


def main() -> None:
    parser = argparse.ArgumentParser(description="eco: sugiere respuestas con tu estilo (local)")
    parser.add_argument("--preparar", action="store_true", help="Lee los chats exportados y arma perfil e índice")
    parser.add_argument("--sugerir", metavar="MENSAJE", help="Mensaje que te mandaron")
    parser.add_argument("--chat", default="", help="Con quién hablas (opcional: da más peso a ese chat)")
    parser.add_argument("--probar", type=int, metavar="N", help="Compara con N respuestas reales tuyas")
    parser.add_argument("--dataset", action="store_true", help="Exporta train/valid para LoRA")
    args = parser.parse_args()
    if args.preparar:
        print(preparar())
    elif args.sugerir:
        print(sugerir(args.sugerir, args.chat))
    elif args.probar:
        probar(args.probar)
    elif args.dataset:
        print(dataset())
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
