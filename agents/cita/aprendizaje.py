"""Memoria del agente `cita`: aprende de cada revisión y la guarda en data/cita/aprendizaje.json.

Qué aprende (sin modelo de lenguaje, solo contando lo que pasa):
- Tiempo de espera por paso: si un paso agota el tiempo, la próxima vez espera 1,5× más (tope 90 s);
  cada éxito lo baja un 10 % hacia el valor base.
- Qué selector de cada botón funcionó la última vez, para probarlo primero.
- Ritmo entre revisiones: cada revisión sin problemas lo acorta un poco (hasta 1 min); un error del sitio
  lo alarga ×1,5 y un bloqueo ×2. El ritmo al que hubo un bloqueo queda como "piso" durante 7 días
  (+25 %): no vuelve a bajar de ahí hasta que caduca, y entonces lo prueba de nuevo.
- Consultas por hora: al bloquear, anota cuántas revisiones llevaba en la última hora; no vuelve a pasar de
  esa cantidad menos una (7 días), aunque el intervalo sea más largo que el piso.
- Horas con bloqueos: si en una hora del día hubo 2+ bloqueos en la semana, en esa hora revisa 1,5× más lento.
- Pausa tras un bloqueo, por tipo (la frase que lo delató): si al volver lo bloquean otra vez, la siguiente
  dura el doble; la que funcionó se recuerda para ese tipo y la próxima vez prueba un 25 % menos.
- A qué horas aparecieron citas: en esas horas (y la anterior) revisa al doble de ritmo, sin bajar del piso.
- Cuándo toca la próxima revisión (launchd lanza el agente cada 1 min; el agente decide si toca).
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
DIAS_PISO = 7           # cuánto se recuerda el ritmo al que hubo un bloqueo
RITMO = {"minimo_s": 60, "maximo_s": 1800, "inicial_s": 600, "baja_s": 30}  # se sobrescribe con `ritmo:` del yaml


class Aprendizaje:
    def __init__(self, ruta: Path):
        self.ruta = ruta
        try:
            self.d = json.loads(ruta.read_text())
        except (OSError, ValueError):
            self.d = {}
        for clave, vacio in (("pasos", {}), ("selectores", {}), ("revisiones", []), ("ritmo", {})):
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
    def revision(self, resultado: str, paso: str | None = None, cfg: dict | None = None,
                 frase: str | None = None) -> None:
        """resultado: sin_citas | hay_citas | reservada | error | bloqueo. Ajusta el ritmo según cómo fue."""
        r = {"t": f"{datetime.now():%Y-%m-%dT%H:%M}", "r": resultado, "cada_s": self.intervalo(cfg)}
        if paso:
            r["paso"] = paso
        self._ajustar_ritmo(resultado, cfg or {}, paso, frase)
        self.d["revisiones"].append(r)

    # --- Ritmo aprendido -----------------------------------------------------------------------
    @staticmethod
    def _limites(cfg: dict | None) -> dict:
        return {**RITMO, **((cfg or {}).get("ritmo") or {})}

    def intervalo(self, cfg: dict | None = None) -> int:
        """Segundos entre revisiones que se están usando ahora."""
        return int(self.d["ritmo"].get("intervalo_s", self._limites(cfg)["inicial_s"]))

    def _bloqueos(self) -> list[dict]:
        """Bloqueos de los últimos 7 días (los más viejos se olvidan para volver a probar)."""
        desde = f"{datetime.now() - timedelta(days=DIAS_PISO):%Y-%m-%dT%H:%M}"
        bloqueos = [b for b in self.d["ritmo"].get("bloqueos", []) if b["t"] >= desde]
        self.d["ritmo"]["bloqueos"] = bloqueos[-50:]
        return bloqueos

    def piso(self) -> int:
        """Ritmo mínimo seguro: el más lento al que hubo un bloqueo en los últimos 7 días, +25 %."""
        return int(max((b["cada_s"] for b in self._bloqueos()), default=0) * 1.25)

    def _por_hora(self, hasta: datetime | None = None) -> int:
        """Revisiones en los 60 min previos a `hasta` (por defecto, ahora)."""
        hasta = hasta or datetime.now()
        desde, fin = f"{hasta - timedelta(hours=1):%Y-%m-%dT%H:%M}", f"{hasta:%Y-%m-%dT%H:%M}"
        return sum(1 for r in self.d["revisiones"] if desde <= r["t"] <= fin)

    def tope_por_hora(self) -> int | None:
        """Máximo de revisiones por hora: una menos que las que llevaba en el bloqueo más exigente (7 días).
        Los bloqueos anotados antes de contar (sin `por_hora`) se calculan con el historial."""
        cuentas = [b.get("por_hora") or self._por_hora(datetime.fromisoformat(b["t"])) for b in self._bloqueos()]
        return max(1, min(cuentas) - 1) if cuentas else None

    def respuesta_bloqueo(self, t: str, es_bloqueo: bool) -> str | None:
        """Lo que respondiste en Telegram sobre el bloqueo anotado a la hora `t`.
        Sí: queda confirmado. No: se deshace lo aprendido (piso, tope, ritmo, pausa) y la frase en ese paso
        se recuerda como falsa alarma. Devuelve un texto para el chat, o None si ya no está ese bloqueo."""
        ritmo = self.d["ritmo"]
        b = next((b for b in ritmo.get("bloqueos", []) if b["t"] == t), None)
        if not b:
            return None
        if es_bloqueo:
            b["confirmado"] = True
            return "Confirmado: lo tengo en cuenta para el ritmo."
        ritmo["bloqueos"].remove(b)
        ritmo["intervalo_s"] = b["cada_s"]
        ritmo.pop("pausa_min", None)
        falsa = {"frase": b.get("frase", "?"), "paso": b.get("paso", "?")}
        if falsa not in ritmo.setdefault("falsas_alarmas", []):
            ritmo["falsas_alarmas"].append(falsa)
        for r in self.d["revisiones"]:
            if r["t"] == t and r["r"] == "bloqueo":
                r["r"] = "falsa_alarma"
        return (f"Anotado como falsa alarma: «{falsa['frase']}» en «{falsa['paso']}» ya no detiene al agente. "
                f"Vuelvo a revisar cada {b['cada_s'] / 60:.1f} min, sin pausa.")

    def error_fue_bloqueo(self, t: str, cfg: dict | None = None) -> str | None:
        """Respondiste en Telegram que el error de la hora `t` (p. ej. un 500 o un paso que no carga) era un
        bloqueo encubierto: se aprende como un bloqueo (piso, tope por hora, ritmo ×2 y pausa)."""
        cfg = cfg or {}
        r = next((r for r in self.d["revisiones"] if r["t"] == t and r["r"] == "error"), None)
        if not r:
            return None
        r["r"] = "bloqueo"
        ritmo, lim = self.d["ritmo"], self._limites(cfg)
        frase = f"error en {r.get('paso', '?')}"
        cada = r.get("cada_s", self.intervalo(cfg))
        ritmo.setdefault("bloqueos", []).append({
            "t": t, "cada_s": cada, "frase": frase, "paso": r.get("paso", "?"),
            "por_hora": self._por_hora(datetime.fromisoformat(t)), "confirmado": True})
        ritmo["intervalo_s"] = int(min(lim["maximo_s"], max(self.intervalo(cfg), cada * 2, self.piso())))
        ritmo["pausa_min"], ritmo["ultima_frase"] = cfg.get("pausa_bloqueo_min", 15), frase
        return (f"Aprendido como bloqueo: pauso {ritmo['pausa_min']} min y después reviso cada "
                f"{ritmo['intervalo_s'] / 60:.1f} min.")

    def es_falsa_alarma(self, frase: str, paso: str) -> bool:
        return {"frase": frase, "paso": paso} in self.d["ritmo"].get("falsas_alarmas", [])

    def horas_bloqueo(self) -> set[int]:
        """Horas del día con 2 o más bloqueos en los últimos 7 días."""
        horas = Counter(int(b["t"][11:13]) for b in self._bloqueos())
        return {h for h, n in horas.items() if n >= 2}

    def pausa_min(self, cfg: dict | None = None) -> int:
        """Minutos de pausa tras un bloqueo: la base de la config, o lo aprendido si se repitieron."""
        return int(self.d["ritmo"].get("pausa_min", (cfg or {}).get("pausa_bloqueo_min", 15)))

    def _ajustar_ritmo(self, resultado: str, cfg: dict, paso: str | None = None, frase: str | None = None) -> None:
        lim, ritmo = self._limites(cfg), self.d["ritmo"]
        actual = self.intervalo(cfg)
        anterior = self.d["revisiones"][-1]["r"] if self.d["revisiones"] else None
        base = cfg.get("pausa_bloqueo_min", 15)
        pausas = ritmo.setdefault("pausa_por_frase", {})
        if resultado == "bloqueo":
            frase = frase or "?"
            ritmo.setdefault("bloqueos", []).append({
                "t": f"{datetime.now():%Y-%m-%dT%H:%M}", "cada_s": actual, "frase": frase, "paso": paso or "?",
                "por_hora": self._por_hora() + 1})
            nuevo = actual * 2
            if anterior == "bloqueo":  # bloqueo justo al volver de la pausa: la próxima, el doble
                ritmo["pausa_min"] = min(240, self.pausa_min(cfg) * 2)
            else:  # parte de la que funcionó la última vez con este tipo, probando un 25 % menos
                ritmo["pausa_min"] = max(base, int(pausas.get(frase, base) * 0.75))
            ritmo["ultima_frase"] = frase
        elif resultado == "error":
            nuevo = actual * 1.5
        else:  # revisión completa: probar un poco más rápido
            nuevo = actual - lim["baja_s"]
            if "pausa_min" in ritmo:  # la pausa funcionó: se recuerda para ese tipo de bloqueo
                pausas[ritmo.get("ultima_frase", "?")] = ritmo.pop("pausa_min")
        ritmo["intervalo_s"] = int(min(lim["maximo_s"], max(lim["minimo_s"], self.piso(), nuevo)))

    def horas_intensas(self, extra=()) -> set[int]:
        """Horas en que aparecieron citas (últimos 60 días) y la hora anterior, más las de la config."""
        desde = f"{datetime.now() - timedelta(days=DIAS_HORAS):%Y-%m-%dT%H:%M}"
        horas = {int(r["t"][11:13]) for r in self.d["revisiones"]
                 if r["r"] == "hay_citas" and r["t"] >= desde}
        return {h % 24 for x in horas for h in (x - 1, x)} | {int(h) for h in extra}

    def toca_aviso(self, minutos: int) -> bool:
        """El aviso ⏳ sin citas sale como mucho cada `minutos` (0 = en cada revisión)."""
        ultimo = self.d.get("ultimo_aviso")
        if minutos and ultimo and datetime.now() < datetime.fromisoformat(ultimo) + timedelta(minutes=minutos):
            return False
        self.d["ultimo_aviso"] = datetime.now().isoformat(timespec="seconds")
        return True

    def toca(self) -> bool:
        proxima = self.d.get("proxima")
        return not proxima or datetime.now() >= datetime.fromisoformat(proxima)

    def programar(self, cuando: datetime) -> None:
        self.d["proxima"] = cuando.isoformat(timespec="seconds")

    def programar_siguiente(self, cfg: dict) -> datetime:
        """Próxima revisión según el ritmo aprendido (±20 % al azar para no ser regular).
        En horas intensas, al doble de ritmo, pero nunca bajo el mínimo ni bajo el piso de bloqueos."""
        segundos = self.intervalo(cfg)
        intensas = self.horas_intensas(cfg.get("horas_intensas") or ())
        if datetime.now().hour in intensas or (datetime.now() + timedelta(seconds=segundos)).hour in intensas:
            segundos = max(self._limites(cfg)["minimo_s"], self.piso(), segundos // 2)
        if datetime.now().hour in self.horas_bloqueo():
            segundos = int(segundos * 1.5)
        tope = self.tope_por_hora()
        if tope:  # repartir las revisiones permitidas en la hora
            segundos = max(segundos, 3600 // tope)
        cuando = datetime.now() + timedelta(seconds=segundos * random.uniform(0.8, 1.2))
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
        lineas.append(f"Ritmo: cada {self.intervalo(cfg) / 60:.1f} min · piso por bloqueos: "
                      + (f"{self.piso() / 60:.1f} min" if self.piso() else "ninguno")
                      + f" · pausa tras bloqueo: {self.pausa_min(cfg)} min")
        tope, horas = self.tope_por_hora(), sorted(self.horas_bloqueo())
        lineas.append("Tope por hora: " + (f"{tope} revisiones" if tope else "ninguno")
                      + " · horas con bloqueos: " + (", ".join(f"{h:02d}h" for h in horas) if horas else "ninguna"))
        pausas = self.d["ritmo"].get("pausa_por_frase", {})
        if pausas:
            lineas.append("Pausa que funcionó por tipo: " + ", ".join(f"«{f}» {m} min" for f, m in pausas.items()))
        for f in self.d["ritmo"].get("falsas_alarmas", []):
            lineas.append(f"  falsa alarma (según tú): «{f['frase']}» en {f['paso']}")
        for b in self._bloqueos()[-5:]:
            lineas.append(f"  bloqueo {b['t']}{' ✓' if b.get('confirmado') else ''}: «{b.get('frase', '?')}» en {b.get('paso', '?')}, cada "
                          f"{b['cada_s'] / 60:.1f} min, "
                          f"{b.get('por_hora') or self._por_hora(datetime.fromisoformat(b['t']))} revisiones en la hora previa")
        if self.d.get("proxima"):
            lineas.append(f"Próxima revisión: {self.d['proxima']}")
        return "\n".join(lineas)
