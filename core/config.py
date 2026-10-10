"""Configuración compartida: rutas, variables de entorno y perfil."""
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REPORTS_DIR = DATA_DIR / "reports"
DB_PATH = DATA_DIR / "agentic.db"

load_dotenv(ROOT / ".env")


def env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def env_float(name: str, default: float) -> float:
    return float(env(name, str(default)))


def env_int(name: str, default: int) -> int:
    return int(env(name, str(default)))


def load_config(name: str) -> dict:
    """Usa config/<name>.yaml (datos reales, fuera de git); si no existe, config/<name>.example.yaml."""
    path = ROOT / "config" / f"{name}.yaml"
    if not path.exists():
        path = ROOT / "config" / f"{name}.example.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_profile() -> dict:
    """Perfil profesional (habilidades, experiencia): config/profile.yaml o el de ejemplo."""
    return load_config("profile")


def profile_summary(profile: dict) -> str:
    """Resumen en texto del perfil, para incluir en los prompts."""
    lines = [
        f"Name: {profile.get('nombre')}",
        f"Title: {profile.get('titulo')}",
        f"Languages: {', '.join(profile.get('idiomas', []))}",
        "Skills: " + "; ".join(profile.get("habilidades", [])),
    ]
    if profile.get("anios_experiencia"):
        lines.append(f"Years of experience: {profile['anios_experiencia']}+")
    if profile.get("tarifa_hora_usd"):
        lines.append(f"Hourly rate: {profile['tarifa_hora_usd']} USD")
    if profile.get("presupuesto_minimo_usd"):
        lines.append(f"Minimum project budget: {profile['presupuesto_minimo_usd']} USD")
    if profile.get("experiencia_destacada"):
        lines.append("Real past work: " + "; ".join(profile["experiencia_destacada"]))
    return "\n".join(lines)
