"""Fuentes de ofertas de empleo. Solo canales permitidos: APIs oficiales (con o sin clave) y alertas por correo.

LinkedIn, Indeed y Tecnoempleo prohíben el scraping: de ellos solo se leen los correos de alerta.
"""
import email
import html
import imaplib
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import certifi
import requests

from agents.freelance.sources import MAX_DESC, UA, Offer, _html_body, _LinkParser, strip_html
from core import db
from core.config import DATA_DIR, ROOT, env

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


# ---------------------------------------------------------------- Bolsas remotas con API pública (sin clave)

# Solo se guardan ofertas que se pueden hacer desde España: las que exigen vivir en otro país se descartan aquí.
REMOTE_FROM_SPAIN = re.compile(r"worldwide|anywhere|europe|emea|spain|españa|\bcet\b|\beu\b", re.I)
PUBLIC_MAX_DAYS = 14  # las APIs devuelven también ofertas viejas: solo las de las últimas 2 semanas
TECH_AREA = re.compile(r"software|devops|data|machine|artificial|\bai\b|developer|programming", re.I)  # áreas de Jobicy


def _recent(published: datetime) -> bool:
    return published >= datetime.now(published.tzinfo) - timedelta(days=PUBLIC_MAX_DAYS)


def getonboard(prefs: dict) -> list[Offer]:
    """Get on Board (tech en LatAm y España). Solo 100 % remoto desde cualquier país o con España entre los países."""
    offers, seen = [], set()
    for q in prefs.get("busquedas", []):
        r = requests.get("https://www.getonbrd.com/api/v0/search/jobs",
                         params={"query": q, "per_page": 50, "expand": '["company"]'}, headers=UA, timeout=30)
        r.raise_for_status()
        for job in r.json().get("data", []):
            a = job["attributes"]
            countries = a.get("countries") or []
            if job["id"] in seen or not _recent(datetime.fromtimestamp(a.get("published_at") or 0)):
                continue
            if a.get("remote_modality") != "fully_remote" and not {"Spain", "España"} & set(countries):
                continue
            seen.add(job["id"])
            company = ((a.get("company") or {}).get("data") or {}).get("attributes", {}).get("name", "")
            salary = ""
            if a.get("min_salary"):
                salary = f"{a['min_salary']}-{a.get('max_salary') or a['min_salary']} USD/mes"
            offers.append(Offer(
                source="getonboard",
                external_id=job["id"],
                title=f"{a.get('title', '')} @ {company}".strip(" @"),
                url=(job.get("links") or {}).get("public_url", f"https://www.getonbrd.com/jobs/{job['id']}"),
                description=(f"Modalidad: {a.get('remote_modality')} · Países: {', '.join(countries) or 'n/d'}\n"
                             f"{strip_html(a.get('description', ''))}\n{strip_html(a.get('functions', ''))}")[:MAX_DESC],
                budget=salary,
            ))
    return offers


def remotive(prefs: dict) -> list[Offer]:
    """Remotive (remoto internacional). Una sola consulta por corrida: pide no abusar de su API."""
    r = requests.get("https://remotive.com/api/remote-jobs", headers=UA, timeout=60)
    r.raise_for_status()
    offers = []
    for job in r.json().get("jobs", []):
        location = job.get("candidate_required_location", "")
        if not REMOTE_FROM_SPAIN.search(location):
            continue
        if not _recent(datetime.fromisoformat(job.get("publication_date", "1970-01-01T00:00:00"))):
            continue
        offers.append(Offer(
            source="remotive",
            external_id=str(job["id"]),
            title=f"{job.get('title', '')} @ {job.get('company_name', '')}".strip(" @"),
            url=job.get("url", ""),
            description=(f"Modalidad: remoto · Ubicación aceptada: {location} · Jornada: {job.get('job_type') or 'n/d'}\n"
                         f"Categoría: {job.get('category', '')}\n{strip_html(job.get('description', ''))}")[:MAX_DESC],
            budget=job.get("salary", ""),
        ))
    return offers


def jobicy(prefs: dict) -> list[Offer]:
    """Jobicy (remoto). El filtro geo=spain ya deja solo ofertas abiertas a España."""
    r = requests.get("https://jobicy.com/api/v2/remote-jobs", params={"count": 100, "geo": "spain"}, headers=UA, timeout=30)
    r.raise_for_status()
    offers = []
    for job in r.json().get("jobs", []):
        if not _recent(datetime.fromisoformat(job.get("pubDate", "1970-01-01T00:00:00+00:00"))):
            continue
        if not TECH_AREA.search(" ".join(job.get("jobIndustry") or [])):
            continue  # ventas, soporte, marketing...: sus descripciones nombran "data" o "AI" y pasarían el pre-filtro
        offers.append(Offer(
            source="jobicy",
            external_id=str(job["id"]),
            title=f"{job.get('jobTitle', '')} @ {job.get('companyName', '')}".strip(" @"),
            url=job.get("url", ""),
            description=(f"Modalidad: remoto · Ubicación aceptada: {job.get('jobGeo', '')} · "
                         f"Jornada: {', '.join(job.get('jobType') or []) or 'n/d'} · Nivel: {job.get('jobLevel', 'n/d')}\n"
                         f"{strip_html(job.get('jobDescription') or job.get('jobExcerpt', ''))}")[:MAX_DESC],
        ))
    return offers


# ---------------------------------------------------------------- Salamanca: USAL (RSS) y ECYL (datos abiertos)

# empleo.usal.es no envía el certificado intermedio (GEANT TLS RSA 1): se suma al almacén de certifi solo para ese sitio.
USAL_FEEDS = {"empleo": "https://empleo.usal.es/trabajo/sipoerss", "practicas": "https://empleo.usal.es/practicas/sipcprss"}
USAL_ID = re.compile(r"[?&]id=(\d+)")


def _usal_ca_bundle() -> str:
    path = DATA_DIR / "ca-bundle-usal.pem"
    if not path.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(Path(certifi.where()).read_text() + (ROOT / "certs" / "geant-tls-rsa-1.pem").read_text())
    return str(path)


def usal(prefs: dict) -> list[Offer]:
    """Ofertas de empleo y convocatorias de prácticas vigentes del servicio de empleo de la USAL (SIPPE).
    Para postular hay que entrar en e-SIPPE con la cuenta de estudiante."""
    offers = []
    for kind, url in USAL_FEEDS.items():
        r = requests.get(url, headers=UA, timeout=30, verify=_usal_ca_bundle())
        r.raise_for_status()
        for item in ET.fromstring(r.content).iter("item"):
            link = item.findtext("link", "")
            match = USAL_ID.search(link)
            if not match:
                continue
            path = "trabajo/oferta" if kind == "empleo" else "practicas/convocatorias-practicas"  # el RSS de prácticas trae el link roto
            offers.append(Offer(
                source="usal",
                external_id=f"{kind}-{match.group(1)}",
                title=strip_html(item.findtext("title", ""))[:200],
                url=f"https://empleo.usal.es/{path}?id={match.group(1)}",
                description=(f"{'Prácticas (USAL)' if kind == 'practicas' else 'Empleo (bolsa USAL)'} · "
                             f"Publicada: {item.findtext('pubDate', 'n/d')}\n"
                             f"{strip_html(html.unescape(item.findtext('description', '')))}")[:MAX_DESC],
            ))
    return offers


# El CSV de origen se cargó sin cabecera: los nombres de columna son los valores de la primera oferta.
# Si la Junta lo corrige, estos nombres cambian y la fuente avisa del error (no inventa datos).
ECYL_API = "https://analisis.datosabiertos.jcyl.es/api/explore/v2.1/catalog/datasets/ofertas-de-empleo/records"
ECYL_FIELDS = {
    "title": "cuidadores_de_personas_con_discapacidad_y_o_dependencia_en_instituciones",
    "province": "burgos",
    "town": "villalmanzo",
    "date": "20260928",
    "description": "p_puesto_solicitado_gerocultora_en_residencia_privada_localidad_villamanzo_burgos_se_ofrece_contrato",
    "origin": "ecyl",
    "id": "1285683981477",
    "url": "https_empleo_jcyl_es_web_jcyl_empleo_es_plantilla100detalle_1284216458054_ofertasempleo_128568398147",
}


def ecyl(prefs: dict) -> list[Offer]:
    """Ofertas del ECYL (y otras que publica la Junta) en tus ciudades, vía datos abiertos (se actualiza a diario)."""
    f = ECYL_FIELDS
    since = (datetime.now() - timedelta(days=PUBLIC_MAX_DAYS)).strftime("%Y%m%d")
    offers = []
    for province in prefs.get("ciudades_presencial", []):
        for offset in range(0, 1000, 100):
            r = requests.get(ECYL_API, params={"where": f'{f["province"]}="{province}" and `{f["date"]}` >= {since}',
                                               "limit": 100, "offset": offset}, headers=UA, timeout=30)
            r.raise_for_status()
            rows = r.json().get("results", [])
            if rows and not set(f.values()) <= set(rows[0]):
                raise ValueError("cambió el formato del dataset de ofertas del ECYL: revisar ECYL_FIELDS")
            for row in rows:
                town = row[f["town"]] or row[f["province"]]
                offers.append(Offer(
                    source="ecyl",
                    external_id=str(row[f["id"]]),
                    title=f"{str(row[f['title']]).capitalize()} @ {town}",
                    url=row[f["url"]] or "",
                    description=(f"Ubicación: {town} ({row[f['province']]}) · Origen: {row[f['origin']]} · "
                                 f"Publicada: {row[f['date']]}\n{strip_html(row[f['description']] or '')}")[:MAX_DESC],
                ))
            if len(rows) < 100:
                break
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
PUBLIC_APIS = {"getonboard", "remotive", "jobicy", "usal", "ecyl"}


def _api_turn(conn, name: str, every_hours: int) -> bool:
    """True una vez por bloque de `every_hours` horas: cuida el límite gratis de las APIs (Adzuna ~250 pedidos/día)."""
    now = datetime.now()
    return db.alert_once(conn, f"fuente:{name}:{now:%Y-%m-%d}:{now.hour // every_hours}")


def collect(prefs: dict, conn, use_turns: bool = True) -> tuple[list[Offer], dict[str, str]]:
    """Recolecta de todas las fuentes. Devuelve (ofertas, errores por fuente).

    Los correos se leen en cada corrida; las APIs (con o sin clave) solo cada `cada_horas_api` horas.
    Con use_turns=False (prueba de fuentes) se consultan todas sin gastar el turno.
    """
    every = int(prefs.get("cada_horas_api", 6))
    fetchers = {
        "adzuna": lambda: adzuna(prefs),
        "infojobs": lambda: infojobs(prefs),
        "getonboard": lambda: getonboard(prefs),
        "remotive": lambda: remotive(prefs),
        "jobicy": lambda: jobicy(prefs),
        "usal": lambda: usal(prefs),
        "ecyl": lambda: ecyl(prefs),
        "correo": email_alerts,
    }
    offers, errors = [], {}
    for name, fetch in fetchers.items():
        if name in API_KEYS and not (env(API_KEYS[name]) and (not use_turns or _api_turn(conn, name, every))):
            continue  # sin clave, o ya se consultó en este bloque de horas
        if name in PUBLIC_APIS and use_turns and not _api_turn(conn, name, every):
            continue  # APIs públicas: también cada `cada_horas_api` horas, por cortesía
        try:
            offers.extend(fetch())
        except Exception as e:  # una fuente caída no debe frenar a las demás
            errors[name] = f"{type(e).__name__}: {e}"
    return offers, errors
