"""Fuentes de ofertas de empleo. Solo canales permitidos: APIs oficiales y alertas por correo.

LinkedIn, Indeed y Tecnoempleo prohíben el scraping: de ellos solo se leen los correos de alerta.
"""
import email
import imaplib
import re
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import requests

from agents.freelance.sources import MAX_DESC, UA, Offer, _html_body, _LinkParser, strip_html
from core import db
from core.config import env

# ---------------------------------------------------------------- Adzuna (API oficial, clave gratis)


def adzuna(prefs: dict) -> list[Offer]:
    app_id, app_key = env("ADZUNA_APP_ID"), env("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        return []
    country = prefs.get("pais", "es")
    wheres = list(prefs.get("ciudades_presencial", []))
    if prefs.get("remoto", True):
        wheres.append("")  # todo el país; el puntuador descarta lo presencial fuera de tus ciudades
    offers = []
    for what in prefs.get("busquedas", []):
        for where in wheres:
            query = what if where or not prefs.get("remoto") else f"{what} remoto"
            r = requests.get(
                f"https://api.adzuna.com/v1/api/jobs/{country}/search/1",
                params={"app_id": app_id, "app_key": app_key, "what": query, "where": where,
                        "results_per_page": 50, "max_days_old": 3, "content-type": "application/json"},
                headers=UA, timeout=30,
            )
            r.raise_for_status()
            for job in r.json().get("results", []):
                salary = ""
                if job.get("salary_min"):
                    salary = f"{int(job['salary_min'])}-{int(job.get('salary_max') or job['salary_min'])} EUR/año"
                company = (job.get("company") or {}).get("display_name", "")
                location = (job.get("location") or {}).get("display_name", "")
                contract = " ".join(filter(None, [job.get("contract_time"), job.get("contract_type")]))
                offers.append(Offer(
                    source="adzuna",
                    external_id=str(job["id"]),
                    title=f"{job.get('title', '')} @ {company}".strip(" @"),
                    url=job.get("redirect_url", ""),
                    description=(f"Ubicación: {location}\nContrato: {contract or 'n/d'}\n"
                                 f"{strip_html(job.get('description', ''))}")[:MAX_DESC],
                    budget=salary,
                ))
    return offers


# ---------------------------------------------------------------- InfoJobs (API oficial, app registrada)


def infojobs(prefs: dict) -> list[Offer]:
    client_id, secret = env("INFOJOBS_CLIENT_ID"), env("INFOJOBS_CLIENT_SECRET")
    if not client_id or not secret:
        return []
    provinces = [c.lower() for c in prefs.get("ciudades_presencial", [])]
    if prefs.get("remoto", True):
        provinces.append("")  # sin provincia: todo el país
    offers = []
    for q in prefs.get("busquedas", []):
        for province in provinces:
            params = {"q": q, "maxResults": 50, "sinceDate": "_7_DAYS"}
            if province:
                params["province"] = province
            r = requests.get("https://api.infojobs.net/api/9/offer", params=params,
                             auth=(client_id, secret), headers=UA, timeout=30)
            r.raise_for_status()
            for o in r.json().get("offers", []):
                value = lambda key: (o.get(key) or {}).get("value", "")  # noqa: E731
                salary = " - ".join(filter(None, [value("salaryMin"), value("salaryMax")]))
                if salary:
                    salary = f"{salary} {value('salaryPeriod')}".strip()
                offers.append(Offer(
                    source="infojobs",
                    external_id=str(o["id"]),
                    title=f"{o.get('title', '')} @ {(o.get('author') or {}).get('name', '')}".strip(" @"),
                    url=o.get("link", ""),
                    description=(f"Ubicación: {o.get('city', '')} ({value('province')})\n"
                                 f"Modalidad: {value('teleworking') or 'n/d'}\n"
                                 f"Jornada: {value('workDay') or 'n/d'} · Contrato: {value('contractType') or 'n/d'}\n"
                                 f"Requisitos: {o.get('requirementMin', '')}")[:MAX_DESC],
                    budget=salary,
                ))
    return offers


# ---------------------------------------------------------------- Alertas por correo (LinkedIn, Indeed...)

# remitente → patrón del link de la oferta (el grupo 1 es el id). Si cambia el formato, ajustar aquí.
EMAIL_SOURCES = {
    "linkedin": ("linkedin.com", re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)", re.I)),
    "infojobs": ("infojobs.net", re.compile(r"infojobs\.net/[^?#]*/of-i([0-9a-f]{20,})", re.I)),
    "tecnoempleo": ("tecnoempleo.com", re.compile(r"tecnoempleo\.com/[^?#]*/rf-([0-9a-z]+)", re.I)),
    "indeed": ("indeed.com", re.compile(r"indeed\.com/(?:rc/clk|viewjob|pagead/clk)[^\s\"']*[?&]jk=([0-9a-f]+)", re.I)),
}
EMAIL_DAYS = 3
EMAIL_MARK = "(Alerta por correo"  # las ofertas de correo traen pocos datos; el puntuador lo tiene en cuenta

# LinkedIn, texto plano: bloques "título / empresa / ubicación / [adornos] / Ver anuncio de empleo: <url>"
LI_SEPARATOR = re.compile(r"-{10,}")
LI_APPLIED = re.compile(r"se ha enviado tu solicitud|your application was sent", re.I)
LI_NOISE = re.compile(r"^(solicitar|solicitud sencilla|easy apply|promocionad|promoted|hace \d|\d+ (persona|people|alumni|solicitud|applicant)"
                      r"|sé de los primeros|be an early|contratando activamente|actively recruiting|responde|ver anuncio|view job"
                      r"|solicitado el|ahora sigue|descubre otros|se ha enviado|your application)", re.I)


def _text_body(msg) -> str:
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_type() == "text/plain":
            return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
    return ""


def _linkedin_offers(msg, pattern) -> list[Offer]:
    """Ofertas de un correo de LinkedIn con título, empresa y ubicación. Las de "se ha enviado tu solicitud"
    se marcan como ya postuladas (no se avisan; quedan registradas)."""
    offers = []
    for block in LI_SEPARATOR.split(_text_body(msg)):
        match = pattern.search(block)
        if not match:
            continue
        lines = [l.strip() for l in block[:match.start()].splitlines()]
        info = [l for l in lines if l and not LI_NOISE.search(l) and "http" not in l][-3:]
        if not info:
            continue
        title, company, location = (info + ["", ""])[:3]
        offers.append(Offer(
            source="linkedin",
            external_id=match.group(1),
            title=f"{title} @ {company}".strip(" @"),
            url=f"https://www.linkedin.com/jobs/view/{match.group(1)}",
            description=f"{EMAIL_MARK}: solo título, empresa y ubicación)\nUbicación: {location or 'n/d'}",
            applied=bool(LI_APPLIED.search(block)),
        ))
    return offers


def _link_offers(source: str, msg, pattern) -> list[Offer]:
    """Ofertas a partir de los links del HTML (InfoJobs, Tecnoempleo, Indeed): solo el título."""
    parser = _LinkParser()
    parser.feed(_html_body(msg))
    offers, seen = [], set()
    for href, text in parser.links:
        match = pattern.search(href)
        if not match or len(text) < 5 or match.group(1) in seen:
            continue
        seen.add(match.group(1))
        parts = urlsplit(href)
        offers.append(Offer(
            source=source,
            external_id=match.group(1),
            title=text[:200],
            url=f"https://{parts.netloc}{parts.path}" + (f"?{parts.query}" if source == "indeed" else ""),
            description=f"{EMAIL_MARK}: solo título) {msg.get('Subject', '')}",
        ))
    return offers


def email_alerts() -> list[Offer]:
    """Lee los correos de los últimos días SIN marcarlos como leídos (los duplicados los frena la base)."""
    user, password = env("IMAP_USER"), env("IMAP_PASSWORD")
    if not user or not password:
        return []
    since = (datetime.now() - timedelta(days=EMAIL_DAYS)).strftime("%d-%b-%Y")
    offers = []
    with imaplib.IMAP4_SSL(env("IMAP_HOST", "imap.gmail.com")) as box:
        box.login(user, password)
        box.select(env("IMAP_FOLDER", "INBOX"), readonly=True)
        for source, (sender, pattern) in EMAIL_SOURCES.items():
            _, data = box.search(None, f'(FROM "{sender}" SINCE {since})')
            for num in data[0].split():
                _, msg_data = box.fetch(num, "(BODY.PEEK[])")
                msg = email.message_from_bytes(msg_data[0][1])
                if source == "linkedin":
                    offers.extend(_linkedin_offers(msg, pattern))
                else:
                    offers.extend(_link_offers(source, msg, pattern))
    return offers


API_KEYS = {"adzuna": "ADZUNA_APP_ID", "infojobs": "INFOJOBS_CLIENT_ID"}


def _api_turn(conn, name: str, every_hours: int) -> bool:
    """True una vez por bloque de `every_hours` horas: cuida el límite gratis de las APIs (Adzuna ~250 pedidos/día)."""
    now = datetime.now()
    return db.alert_once(conn, f"fuente:{name}:{now:%Y-%m-%d}:{now.hour // every_hours}")


def collect(prefs: dict, conn) -> tuple[list[Offer], dict[str, str]]:
    """Recolecta de todas las fuentes. Devuelve (ofertas, errores por fuente).

    Los correos se leen en cada corrida; las APIs solo cada `cada_horas_api` horas.
    """
    every = int(prefs.get("cada_horas_api", 6))
    fetchers = {
        "adzuna": lambda: adzuna(prefs),
        "infojobs": lambda: infojobs(prefs),
        "correo": email_alerts,
    }
    offers, errors = [], {}
    for name, fetch in fetchers.items():
        if name in API_KEYS and not (env(API_KEYS[name]) and _api_turn(conn, name, every)):
            continue  # sin clave, o ya se consultó en este bloque de horas
        try:
            offers.extend(fetch())
        except Exception as e:  # una fuente caída no debe frenar a las demás
            errors[name] = f"{type(e).__name__}: {e}"
    return offers, errors
