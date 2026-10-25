"""Datos de `eco`: tus turnos de conversación, sacados de los chats exportados (pares contexto → respuesta).

La exportación la hace otra herramienta (ej. mac-remote-agent: «exporta …» deja data/whatsapp/chat-*.jsonl con
{"chat", "fecha", "contexto", "respuesta"}). Aquí se juntan en turnos: varios mensajes tuyos seguidos (el típico
«Bueno» · «Dale» · «Nos vemos») son una sola respuesta al último mensaje de la otra persona.
"""
import json
import re
from datetime import datetime
from pathlib import Path

from core.config import DATA_DIR, env

CARPETA = DATA_DIR / "eco.nosync"     # nada de esto va a git ni a iCloud
SOLO_MARCAS = re.compile(r"^(\s*<(enlace|correo|teléfono)>\s*)+$")
PAUSA_MAX = 10 * 60                   # mensajes tuyos con más de 10 min entre sí son turnos distintos


def origen() -> Path:
    return Path(env("ECO_DATOS", str(Path.home() / "mac-remote-agent/data/whatsapp"))).expanduser()


def pares(carpeta: Path | None = None) -> list[dict]:
    salida = []
    for archivo in sorted((carpeta or origen()).glob("chat-*.jsonl")):
        salida += [json.loads(l) for l in archivo.read_text().split("\n") if l.strip()]
    return salida


def turnos(filas: list[dict]) -> list[dict]:
    """[{chat, fecha, contexto, mensajes: [...]}]: cada turno empieza con un mensaje de la otra persona (o sin
    contexto) y junta tus mensajes seguidos hasta el siguiente mensaje suyo o una pausa larga."""
    salida: list[dict] = []
    for p in filas:
        texto = p["respuesta"].strip()
        if not texto or SOLO_MARCAS.match(texto):
            continue
        fecha = datetime.fromisoformat(p["fecha"])
        previo = salida[-1] if salida else None
        sigue = (previo and previo["chat"] == p["chat"] and not p["contexto"]
                 and (fecha - previo["_ultima"]).total_seconds() <= PAUSA_MAX)
        if sigue:
            previo["mensajes"].append(texto)
            previo["_ultima"] = fecha
        else:
            salida.append({"chat": p["chat"], "fecha": p["fecha"], "contexto": p["contexto"].strip(),
                           "mensajes": [texto], "_ultima": fecha})
    for t in salida:
        t.pop("_ultima")
    return salida
