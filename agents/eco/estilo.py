"""Perfil de estilo medido en código (sin modelo): cómo escribes, en números y ejemplos.

Lo que un modelo no ve en 8 ejemplos y sí se ve contando miles de mensajes: cuántos mensajes mandas por turno,
qué tan largos, si empiezas con mayúscula o cierras con punto, cómo te ríes, qué emojis y muletillas usas.
"""
import re
import statistics
from collections import Counter

EMOJI = re.compile("[\U0001F300-\U0001F3FA\U0001F400-\U0001FAFF☀-➿]")  # sin los tonos de piel
RISA = re.compile(r"\b(?:ja){2,}j?\b|\b(?:je){2,}\b|\bjsjs\w*", re.I)
PALABRA = re.compile(r"[a-záéíóúñü]+", re.I)
# Palabras demasiado comunes para decir algo del estilo
COMUNES = set("""de la que el en y a los se del las un por con no una su para es al lo como más mas pero sus le ya o
fue este ha si sí porque esta son entre cuando muy sin sobre también me hasta hay donde quien desde todo nos durante
todos uno les ni contra otros ese eso ante ellos e esto mí antes algunos qué unos yo otro otras otra él tanto esa
estos mucho quienes nada muchos cual poco ella estar estas algunas algo nosotros mi mis tú te ti tu tus ellas
nosotras vosotros os mío mía tuyo tuya suyo suya nuestro vuestro esos esas estoy estás está estamos están
he has hemos han soy eres somos fui era tengo tiene tienen tenemos voy va vamos van hace bien q pa x d""".split())


def perfil(turnos: list[dict]) -> dict:
    mensajes = [m for t in turnos for m in t["mensajes"]]
    if not mensajes:
        return {}
    risas = Counter(r.lower() for m in mensajes for r in RISA.findall(m))
    emojis = Counter(e for m in mensajes for e in EMOJI.findall(m))
    palabras = Counter(p.lower() for m in mensajes for p in PALABRA.findall(m) if p.lower() not in COMUNES)
    cortos = Counter(m.strip() for m in mensajes if len(m) <= 20)
    con_letra = [m for m in mensajes if m[:1].isalpha()]
    return {
        "turnos": len(turnos), "mensajes": len(mensajes),
        "mensajes_por_turno": round(statistics.mean(len(t["mensajes"]) for t in turnos), 1),
        "largo_mediano": int(statistics.median(len(m) for m in mensajes)),
        "empieza_mayuscula": round(sum(m[0].isupper() for m in con_letra) / max(len(con_letra), 1), 2),
        "termina_punto": round(sum(m.rstrip().endswith(".") for m in mensajes) / len(mensajes), 2),
        "usa_apertura": round(sum(("¿" in m or "¡" in m) for m in mensajes)
                              / max(sum(("?" in m or "!" in m) for m in mensajes), 1), 2),
        "risa": [r for r, _ in risas.most_common(3)],
        "con_risa": round(sum(bool(RISA.search(m)) for m in mensajes) / len(mensajes), 2),
        "emojis": [e for e, _ in emojis.most_common(8)],
        "con_emoji": round(sum(bool(EMOJI.search(m)) for m in mensajes) / len(mensajes), 2),
        "palabras": [p for p, _ in palabras.most_common(30)],
        "respuestas_cortas": [c for c, _ in cortos.most_common(20)],
    }


def describir(p: dict) -> str:
    """El perfil en instrucciones para el modelo."""
    if not p:
        return ""
    pct = lambda x: f"{round(100 * x)} %"
    return "\n".join([
        f"- Sends about {p['mensajes_por_turno']} short messages per turn (separate them with a new line); "
        f"typical message length: {p['largo_mediano']} characters.",
        f"- Starts with a capital letter in {pct(p['empieza_mayuscula'])} of messages; ends with a period in "
        f"{pct(p['termina_punto'])}. Uses ¿ or ¡ in {pct(p['usa_apertura'])} of questions/exclamations.",
        f"- Laughs like: {', '.join(p['risa']) or 'rarely'} ({pct(p['con_risa'])} of messages).",
        f"- Emojis: {' '.join(p['emojis']) or 'almost none'} ({pct(p['con_emoji'])} of messages).",
        f"- Frequent words: {', '.join(p['palabras'][:20])}.",
        f"- Frequent short replies: {' | '.join(p['respuestas_cortas'][:15])}.",
    ])
