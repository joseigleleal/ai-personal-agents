"""Fuentes públicas y sin credenciales para la tasa EUR → COP.

- TRM oficial (USD/COP) del Banco de la República, vía datos.gov.co.
- EUR/USD de referencia del BCE, vía Frankfurter.
- EUR/COP de open.er-api.com, como control cruzado.

EUR/COP de mercado = TRM × EUR/USD. Es la tasa "media": las plataformas (Global66, bancos)
venden euros algo más caros. Esa diferencia es su margen.
"""
from datetime import date, datetime, timedelta

import requests

TIMEOUT = 20
TRM_URL = "https://www.datos.gov.co/resource/32sa-8pi3.json"
FRANKFURTER_URL = "https://api.frankfurter.dev/v1"
ER_API_URL = "https://open.er-api.com/v6/latest/EUR"


def _day(value: str) -> date:
    return datetime.fromisoformat(value[:10]).date()


def trm_history(since: date) -> dict[date, float]:
    """TRM por día. Un valor cubre su rango de vigencia (fines de semana y festivos)."""
    params = {"$where": f"vigenciadesde >= '{since.isoformat()}'", "$order": "vigenciadesde ASC", "$limit": 1000}
    r = requests.get(TRM_URL, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    out = {}
    for row in r.json():
        d, until = _day(row["vigenciadesde"]), _day(row["vigenciahasta"])
        while d <= until:
            out[d] = float(row["valor"])
            d += timedelta(days=1)
    return out


def eurusd_history(since: date) -> dict[date, float]:
    """EUR/USD del BCE (solo días hábiles)."""
    r = requests.get(f"{FRANKFURTER_URL}/{since.isoformat()}..", params={"base": "EUR", "symbols": "USD"},
                     timeout=TIMEOUT)
    r.raise_for_status()
    return {date.fromisoformat(d): v["USD"] for d, v in r.json()["rates"].items()}


def eurcop_history(days: int = 365) -> dict[date, float]:
    """EUR/COP diario = TRM × EUR/USD. En días sin dato del BCE se usa el último disponible."""
    since = date.today() - timedelta(days=days)
    trm, eurusd = trm_history(since), eurusd_history(since)
    out, last_eurusd = {}, None
    d, end = (min(eurusd) if eurusd else since), min(max(trm, default=since), date.today())
    while d <= end:
        last_eurusd = eurusd.get(d, last_eurusd)
        if d in trm and last_eurusd:
            out[d] = round(trm[d] * last_eurusd, 2)
        d += timedelta(days=1)
    return out


def eurcop_er_api() -> float | None:
    """EUR/COP de open.er-api.com (se actualiza una vez al día). None si falla."""
    try:
        r = requests.get(ER_API_URL, timeout=TIMEOUT)
        r.raise_for_status()
        return float(r.json()["rates"]["COP"])
    except (requests.RequestException, KeyError, ValueError):
        return None
