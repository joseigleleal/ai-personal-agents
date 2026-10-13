"""Agente de tasa EUR → COP: avisa cuando comprar euros con pesos está barato frente al historial.

No predice el futuro: compara la tasa de hoy con los últimos 30/90/365 días.

Uso:
    python -m agents.fx.run                          # revisa y avisa solo si hay señal
    python -m agents.fx.run --resumen                # envía el resumen aunque no haya señal
    python -m agents.fx.run --cotizacion global66 3720   # registra la tasa que ves en una app
"""
import argparse
import sys
from datetime import date
from statistics import mean

import requests

from agents.fx import sources
from core import db, notify
from core.config import env_float

# Percentil de la tasa de hoy dentro de los últimos 90 días (0 = la más barata)
BUENA, CARA = 20, 80


def cop(value: float) -> str:
    return f"{value:,.0f}".replace(",", ".")


def parse_rate(text: str) -> float:
    """Acepta 3720, 3.720, 3.720,50, 3720,5 o 3720.5."""
    if "," in text:
        return float(text.replace(".", "").replace(",", "."))
    head, _, tail = text.partition(".")
    return float(head + tail) if len(tail) == 3 else float(text)


def percentile(value: float, values: list[float]) -> int:
    return round(100 * sum(v < value for v in values) / len(values))


def analyze(history: dict[date, float]) -> dict:
    days = sorted(history)
    today, rate = days[-1], history[days[-1]]
    window = lambda n: [history[d] for d in days[-n:]]  # noqa: E731
    w30, w90, w365 = window(30), window(90), window(365)
    week_ago = history[days[-8]] if len(days) > 8 else rate
    pct = percentile(rate, w90)
    if rate <= min(w90):
        level = "excelente"
    elif pct <= BUENA:
        level = "buena"
    elif pct >= CARA:
        level = "cara"
    else:
        level = "normal"
    return {
        "date": today, "rate": rate, "pct90": pct, "level": level,
        "avg30": mean(w30), "avg90": mean(w90), "min90": min(w90), "max90": max(w90),
        "min365": min(w365), "max365": max(w365), "change7": (rate / week_ago - 1) * 100,
    }


def provider_margins(conn) -> dict[str, float]:
    """Margen promedio de cada app sobre la tasa de mercado, según las cotizaciones registradas."""
    rows = conn.execute(
        "SELECT provider, AVG(rate / mid - 1) FROM fx_quotes WHERE mid > 0 GROUP BY provider"
    ).fetchall()
    return {p: m for p, m in rows}


def summary(a: dict, margins: dict[str, float], amount_eur: float, cross: float | None) -> str:
    icon = {"excelente": "🟢", "buena": "🟢", "normal": "🟡", "cara": "🔴"}[a["level"]]
    advice = {
        "excelente": "La más barata de los últimos 90 días. Buen momento para comprar.",
        "buena": "Está entre el 20 % más barato de los últimos 90 días. Buen momento.",
        "normal": "Está en un nivel normal. Si no tienes apuro, puedes esperar.",
        "cara": "Está entre el 20 % más caro de los últimos 90 días. Si puedes, espera.",
    }[a["level"]]
    trend = "bajando" if a["change7"] < -0.3 else "subiendo" if a["change7"] > 0.3 else "estable"
    saving = (a["avg90"] - a["rate"]) * amount_eur
    lines = [
        f"{icon} EUR → COP ({a['date']:%d/%m}): {cop(a['rate'])} COP por euro (mercado)",
        advice,
        "",
        f"• Percentil 90 días: {a['pct90']} (0 = la más barata)",
        f"• Promedio 30 d: {cop(a['avg30'])} · 90 d: {cop(a['avg90'])}",
        f"• Rango 90 d: {cop(a['min90'])} – {cop(a['max90'])} · 1 año: {cop(a['min365'])} – {cop(a['max365'])}",
        f"• Última semana: {a['change7']:+.1f} % ({trend})",
        f"• {cop(amount_eur)} € hoy vs. promedio 90 d: "
        + (f"ahorras ~{cop(saving)} COP" if saving >= 0 else f"pagas ~{cop(-saving)} COP más"),
    ]
    if cross and abs(cross / a["rate"] - 1) > 0.02:
        lines.append(f"• Ojo: otra fuente da {cop(cross)} (difiere más de 2 %)")
    if margins:
        lines += ["", "Precio estimado en las apps hoy (según tus cotizaciones):"]
        for p, m in sorted(margins.items(), key=lambda x: x[1]):
            lines.append(f"• {p}: ~{cop(a['rate'] * (1 + m))} (margen {m * 100:.1f} %)")
    else:
        lines += ["", "Tip: registra la tasa de Global66 para ver su margen real:",
                  "python -m agents.fx.run --cotizacion global66 <tasa>"]
    return "\n".join(lines)


def send(text: str, title: str) -> None:
    print(text)
    if not notify.telegram(text, "fx"):
        notify.macos(title, text.splitlines()[0])


def main() -> None:
    parser = argparse.ArgumentParser(description="Agente de tasa EUR → COP")
    parser.add_argument("--resumen", action="store_true", help="Envía el resumen aunque no haya señal")
    parser.add_argument("--cotizacion", nargs=2, metavar=("PROVEEDOR", "TASA"),
                        help="Registra la tasa (COP por 1 EUR) que ves en una app")
    args = parser.parse_args()

    conn = db.connect()
    try:
        history = sources.eurcop_history(400)
    except requests.RequestException as e:
        sys.exit(f"Fuente de tasas no disponible tras reintentar ({e.__class__.__name__}); se intenta en la próxima corrida.")
    a = analyze(history)

    if args.cotizacion:
        provider, rate = args.cotizacion[0].lower(), parse_rate(args.cotizacion[1])
        conn.execute("INSERT INTO fx_quotes (ts, provider, rate, mid) VALUES (?, ?, ?, ?)",
                     (db.now(), provider, rate, a["rate"]))
        conn.commit()
        print(f"Registrado: {provider} {cop(rate)} · mercado {cop(a['rate'])} · margen {(rate / a['rate'] - 1) * 100:.1f} %")
        return

    text = summary(a, provider_margins(conn), env_float("FX_MONTO_EUR", 1000), sources.eurcop_er_api())
    threshold = env_float("FX_ALERTA_EUR_COP", 0)
    day = a["date"].isoformat()

    reasons = []
    if a["level"] in ("excelente", "buena") and db.alert_once(conn, f"fx:{a['level']}:{day}"):
        reasons.append("señal de compra")
    if threshold and a["rate"] <= threshold and db.alert_once(conn, f"fx:umbral:{day}"):
        reasons.append(f"bajó de tu umbral de {cop(threshold)}")
    weekly = date.today().weekday() == 6 and db.alert_once(conn, f"fx:semanal:{date.today()}")

    if args.resumen or reasons or weekly:
        header = f"🔔 Aviso: {', '.join(reasons)}\n\n" if reasons else ""
        send(header + text, "EUR → COP")
    else:
        print(text)
        print("\n(Sin señal nueva o ya avisada hoy: no se envía aviso.)")


if __name__ == "__main__":
    main()
