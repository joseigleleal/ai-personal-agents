"""Memoria del agente `cita`: aprende de cada revisión y la guarda en data/cita/aprendizaje.json.

Qué aprende (sin modelo de lenguaje, solo contando lo que pasa):
- Tiempo de espera por paso: si un paso agota el tiempo, la próxima vez espera 1,5× más (tope 90 s);
  cada éxito lo baja un 10 % hacia el valor base.
- Qué selector de cada botón funcionó la última vez, para probarlo primero.
- A qué horas aparecieron citas: en esas horas (y la anterior) revisa más seguido.
- Cuándo toca la próxima revisión (launchd lanza el agente cada 2 min; el agente decide si toca).
"""
import json
import os
import random
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

BASE_MS = 30_000
MAX_MS = 90_000
MAX_REVISIONES = 3000   # historial acotado (≈ 3 semanas a 1 revisión cada 10 min)
DIAS_HORAS = 60         # citas vistas que cuentan para las horas intensas


class Aprendizaje:
    def __init__(self, ruta: Path):
        self.ruta = ruta
        try:
            self.d = json.loads(ruta.read_text())
        except (OSError, ValueError):
            self.d = {}
        for clave, vacio in (("pasos", {}), ("selectores", {}), ("revisiones", [])):
            self.d.setdefault(clave, vacio)

    def guardar(self) -> None:
        self.d["revisiones"] = self.d["revisiones"][-MAX_REVISIONES:]
        tmp = self.ruta.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.d, ensure_ascii=False, indent=1))
        os.replace(tmp, self.ruta)  # escritura atómica: nunca queda un JSON a medias

    # --- Tiempos de espera por paso ------------------------------------------------------------
    def _paso(self, paso: str) -> dict:
        return self.d["pasos"].setdefault(paso, {"ok": 0, "fallos": 0, "timeout_ms": BASE_MS})

    def timeout(self, paso: str) -> int:
        return int(self._paso(paso)["timeout_ms"])

    def exito(self, paso: str, selector: str | None = None) -> None:
        p = self._paso(paso)
        p["ok"] += 1
        p["timeout_ms"] = max(BASE_MS, int(p["timeout_ms"] * 0.9))
        if selector:
            self.d["selectores"][paso] = selector

    def fallo(self, paso: str, error: str, por_tiempo: bool) -> None:
        p = self._paso(paso)
        p["fallos"] += 1
        p["ultimo_error"] = error.splitlines()[0][:200] if error else "?"
        p["ultimo_fallo"] = f"{datetime.now():%Y-%m-%d %H:%M}"
        if por_tiempo:
            p["timeout_ms"] = min(MAX_MS, int(p["timeout_ms"] * 1.5))

    def orden(self, paso: str, selectores: tuple[str, ...]) -> list[str]:
        """Selectores del botón, con el que funcionó la última vez primero."""
        bueno = self.d["selectores"].get(paso)
        return sorted(selectores, key=lambda s: s != bueno)

    # --- Revisiones y horario ------------------------------------------------------------------
    def revision(self, resultado: str, paso: str | None = None) -> None:
        """resultado: sin_citas | hay_citas | reservada | error | bloqueo."""
        r = {"t": f"{datetime.now():%Y-%m-%dT%H:%M}", "r": resultado}
        if paso:
            r["paso"] = paso
        self.d["revisiones"].append(r)

    def horas_intensas(self, extra=()) -> set[int]:
        """Horas en que aparecieron citas (últimos 60 días) y la hora anterior, más las de la config."""
        desde = f"{datetime.now() - timedelta(days=DIAS_HORAS):%Y-%m-%dT%H:%M}"
        horas = {int(r["t"][11:13]) for r in self.d["revisiones"]
                 if r["r"] == "hay_citas" and r["t"] >= desde}
        return {h % 24 for x in horas for h in (x - 1, x)} | {int(h) for h in extra}

    def toca(self) -> bool:
        proxima = self.d.get("proxima")
        return not proxima or datetime.now() >= datetime.fromisoformat(proxima)

    def programar(self, cuando: datetime) -> None:
        self.d["proxima"] = cuando.isoformat(timespec="seconds")

    def programar_siguiente(self, cfg: dict) -> datetime:
        """Próxima revisión: 2-4 min en horas intensas, 8-12 min el resto (configurable)."""
        normal = cfg.get("intervalo_min", [8, 12])
        intenso = cfg.get("intervalo_intenso_min", [2, 4])
        intensas = self.horas_intensas(cfg.get("horas_intensas") or ())
        rango = intenso if (datetime.now() + timedelta(minutes=normal[0])).hour in intensas \
            or datetime.now().hour in intensas else normal
        cuando = datetime.now() + timedelta(minutes=random.uniform(*rango))
        self.programar(cuando)
        return cuando

    def resumen(self, cfg: dict | None = None) -> str:
        revs = self.d["revisiones"]
        conteo = Counter(r["r"] for r in revs)
        lineas = [f"Revisiones guardadas: {len(revs)} ({', '.join(f'{k}: {v}' for k, v in conteo.items())})"]
        fallos = Counter(r.get("paso", "?") for r in revs if r["r"] == "error")
        if fallos:
            lineas.append("Pasos que más fallan: " + ", ".join(f"{p} ×{n}" for p, n in fallos.most_common(5)))
        for paso, p in sorted(self.d["pasos"].items()):
            if p["fallos"]:
                lineas.append(f"  {paso}: ok {p['ok']}, fallos {p['fallos']}, espera {p['timeout_ms'] / 1000:.0f} s"
                              f" — último: {p.get('ultimo_error', '')}")
        vistas = [r["t"] for r in revs if r["r"] == "hay_citas"]
        lineas.append("Citas vistas: " + (", ".join(vistas[-10:]) if vistas else "ninguna todavía"))
        intensas = sorted(self.horas_intensas((cfg or {}).get("horas_intensas") or ()))
        lineas.append("Horas intensas: " + (", ".join(f"{h:02d}h" for h in intensas) if intensas else "ninguna"))
        if self.d.get("proxima"):
            lineas.append(f"Próxima revisión: {self.d['proxima']}")
        return "\n".join(lineas)
