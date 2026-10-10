"""Base de datos SQLite compartida: ofertas, costos e ingresos."""
import sqlite3
from datetime import datetime, timezone

from core.config import DATA_DIR, DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS offers (
    id TEXT PRIMARY KEY,
    source TEXT,
    title TEXT,
    url TEXT,
    description TEXT,
    budget TEXT,
    seen_at TEXT,
    score INTEGER,
    score_reason TEXT,
    language TEXT,
    proposal TEXT,
    status TEXT DEFAULT 'nueva'   -- nueva | descartada | puntuada | propuesta | enviada | ganada
);
CREATE TABLE IF NOT EXISTS costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    provider TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    usd REAL,
    purpose TEXT
);
CREATE TABLE IF NOT EXISTS fx_quotes (     -- tasas que el usuario ve en las apps (Global66, bancos...)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    provider TEXT,
    rate REAL,      -- COP por 1 EUR
    mid REAL        -- tasa de mercado en ese momento, para calcular el margen
);
CREATE TABLE IF NOT EXISTS alerts_sent (   -- evita repetir el mismo aviso
    key TEXT PRIMARY KEY,
    ts TEXT
);
CREATE TABLE IF NOT EXISTS income (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    source TEXT,
    usd REAL,
    note TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def offer_exists(conn: sqlite3.Connection, offer_id: str) -> bool:
    return conn.execute("SELECT 1 FROM offers WHERE id = ?", (offer_id,)).fetchone() is not None


def insert_offer(conn: sqlite3.Connection, offer, status: str = "nueva") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO offers (id, source, title, url, description, budget, seen_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (offer.id, offer.source, offer.title, offer.url, offer.description, offer.budget, now(), status),
    )
    conn.commit()


def update_offer(conn: sqlite3.Connection, offer_id: str, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE offers SET {cols} WHERE id = ?", (*fields.values(), offer_id))
    conn.commit()


def record_cost(conn, provider, model, input_tokens, output_tokens, usd, purpose) -> None:
    conn.execute(
        "INSERT INTO costs (ts, provider, model, input_tokens, output_tokens, usd, purpose) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (now(), provider, model, input_tokens, output_tokens, usd, purpose),
    )
    conn.commit()


def alert_once(conn: sqlite3.Connection, key: str) -> bool:
    """True la primera vez que se pide un aviso con esta clave; False si ya se envió."""
    cur = conn.execute("INSERT OR IGNORE INTO alerts_sent (key, ts) VALUES (?, ?)", (key, now()))
    conn.commit()
    return cur.rowcount == 1


def month_spend(conn: sqlite3.Connection) -> float:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    row = conn.execute("SELECT COALESCE(SUM(usd), 0) FROM costs WHERE ts LIKE ?", (f"{month}%",)).fetchone()
    return float(row[0])


def month_income(conn: sqlite3.Connection) -> float:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    row = conn.execute("SELECT COALESCE(SUM(usd), 0) FROM income WHERE ts LIKE ?", (f"{month}%",)).fetchone()
    return float(row[0])
