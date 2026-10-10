"""Fuentes de ofertas. Solo canales permitidos: APIs públicas/oficiales y alertas por correo.

Nada de scraping de Upwork/Workana: sus términos lo prohíben.
"""
import email
import html
import imaplib
import re
from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser
from urllib.parse import urlsplit

import requests

from core.config import env

UA = {"User-Agent": "agentic-freelance/0.1 (personal job alerts)"}
MAX_DESC = 4000


@dataclass
class Offer:
    source: str
    external_id: str
    title: str
    url: str
    description: str
    budget: str = ""

    @property
    def id(self) -> str:
        return f"{self.source}:{self.external_id}"

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.description}".lower()


def fix_mojibake(text: str) -> str:
    """Repara textos con doble codificación (ej. 'grabaciÃ³n' -> 'grabación')."""
    if "Ã" not in text and "â€" not in text:
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def strip_html(raw: str) -> str:
    text = re.sub(r"<(br|p|li|div)[^>]*>", "\n", raw or "", flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(text)).strip()


# ---------------------------------------------------------------- RemoteOK (API pública)

def remoteok() -> list[Offer]:
    r = requests.get("https://remoteok.com/api", headers=UA, timeout=30)
    r.raise_for_status()
    offers = []
    for job in r.json()[1:]:  # el primer elemento es el aviso legal
        salary = ""
        if job.get("salary_min"):
            salary = f"{job['salary_min']}-{job.get('salary_max', '')} USD/año"
        offers.append(Offer(
            source="remoteok",
            external_id=str(job["id"]),
            title=fix_mojibake(f"{job.get('position', '')} @ {job.get('company', '')}"),
            url=job.get("url", ""),
            description=fix_mojibake(strip_html(job.get("description", "")))[:MAX_DESC],
            budget=salary,
        ))
    return offers


# ---------------------------------------------------------------- Hacker News "Freelancer? Seeking Freelancer?"

def _hn_thread(query: str, title_prefix: str) -> dict | None:
    r = requests.get(
        "https://hn.algolia.com/api/v1/search_by_date",
        params={"tags": "story", "query": query, "hitsPerPage": 10},
        timeout=30,
    )
    r.raise_for_status()
    for hit in r.json().get("hits", []):
        if hit.get("title", "").lower().startswith(title_prefix):
            return requests.get(f"https://hn.algolia.com/api/v1/items/{hit['objectID']}", timeout=60).json()
    return None


def hn_freelancer() -> list[Offer]:
    """Hilos mensuales de HN: 'Seeking freelancer' y 'Who is hiring' con trabajos por contrato."""
    threads = [
        (_hn_thread('"Seeking freelancer"', "ask hn: freelancer? seeking freelancer?"), r"SEEKING FREELANCER"),
        (_hn_thread('"Who is hiring"', "ask hn: who is hiring?"), r"\b(CONTRACT|CONTRACTOR|FREELANCE)\b"),
    ]
    offers = []
    for thread, must_match in threads:
        for c in (thread or {}).get("children", []):
            text = strip_html(c.get("text") or "")
            if not re.search(must_match, text.upper()):
                continue
            offers.append(_hn_offer(c, text))
    return offers


def _hn_offer(c: dict, text: str) -> Offer:
    first_line = text.splitlines()[0] if text else "HN freelance"
    return Offer(
        source="hn",
        external_id=str(c["id"]),
        title=first_line[:140],
        url=f"https://news.ycombinator.com/item?id={c['id']}",
        description=text[:MAX_DESC],
    )


# ---------------------------------------------------------------- Freelancer.com (API oficial)

def freelancer_com(queries: list[str]) -> list[Offer]:
    token = env("FREELANCER_TOKEN")
    if not token:
        return []
    offers = []
    for q in queries:
        r = requests.get(
            "https://www.freelancer.com/api/projects/0.1/projects/active/",
            params={"query": q, "limit": 30, "full_description": "true"},
            headers={**UA, "freelancer-oauth-v1": token},
            timeout=30,
        )
        r.raise_for_status()
        for p in r.json().get("result", {}).get("projects", []):
            budget = p.get("budget") or {}
            currency = (p.get("currency") or {}).get("code", "")
            offers.append(Offer(
                source="freelancer",
                external_id=str(p["id"]),
                title=p.get("title", ""),
                url=f"https://www.freelancer.com/projects/{p.get('seo_url', p['id'])}",
                description=(p.get("description") or p.get("preview_description") or "")[:MAX_DESC],
                budget=f"{budget.get('minimum', '?')}-{budget.get('maximum', '?')} {currency}",
            ))
    return offers


# ---------------------------------------------------------------- Alertas por correo (Upwork / Workana)

JOB_LINK_PATTERNS = {
    "upwork": re.compile(r"upwork\.com/(?:jobs|freelance-jobs/apply)/", re.I),
    "workana": re.compile(r"workana\.com/job/", re.I),
}
SENDERS = {"upwork": "upwork.com", "workana": "workana.com"}


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None


def _html_body(msg: Message) -> str:
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_type() == "text/html":
            return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
    return ""


def email_alerts() -> list[Offer]:
    """Lee correos NO leídos de alertas y extrae los links de ofertas.

    Nota: el formato de los correos cambia; si no detecta ofertas, revisar JOB_LINK_PATTERNS.
    """
    user, password = env("IMAP_USER"), env("IMAP_PASSWORD")
    if not user or not password:
        return []
    offers = []
    with imaplib.IMAP4_SSL(env("IMAP_HOST", "imap.gmail.com")) as box:
        box.login(user, password)
        box.select(env("IMAP_FOLDER", "INBOX"))
        for source, sender in SENDERS.items():
            _, data = box.search(None, f'(UNSEEN FROM "{sender}")')
            for num in data[0].split():
                _, msg_data = box.fetch(num, "(RFC822)")  # al leerlo queda marcado como leído
                msg = email.message_from_bytes(msg_data[0][1])
                parser = _LinkParser()
                parser.feed(_html_body(msg))
                for href, text in parser.links:
                    if not JOB_LINK_PATTERNS[source].search(href) or len(text) < 8:
                        continue
                    path = urlsplit(href).path.rstrip("/")
                    offers.append(Offer(
                        source=source,
                        external_id=path.split("/")[-1],
                        title=text[:200],
                        url=f"https://{urlsplit(href).netloc}{path}",
                        description=f"(Alerta por correo: {msg.get('Subject', '')}) {text}",
                    ))
    return offers


def collect(profile: dict) -> tuple[list[Offer], dict[str, str]]:
    """Recolecta de todas las fuentes. Devuelve (ofertas, errores por fuente)."""
    fetchers = {
        "remoteok": remoteok,
        "hn": hn_freelancer,
        "freelancer": lambda: freelancer_com(profile.get("busquedas", [])),
        "correo": email_alerts,
    }
    offers, errors = [], {}
    for name, fetch in fetchers.items():
        try:
            offers.extend(fetch())
        except Exception as e:  # una fuente caída no debe frenar a las demás
            errors[name] = f"{type(e).__name__}: {e}"
    return offers, errors
