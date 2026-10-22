"""Memoria del agente `cita`: aprende de cada revisión y la guarda en data/cita/aprendizaje.json.

Qué aprende (sin modelo de lenguaje, solo contando lo que pasa):
- Tiempo de espera por paso: si un paso agota el tiempo, la próxima vez espera 1,5× más (tope 90 s);
  cada éxito lo baja un 10 % hacia el valor base.
- Qué selector de cada botón funcionó la última vez, para probarlo primero.
- Ritmo entre revisiones: cada revisión sin problemas lo acorta un poco (hasta 1 min). Un error o un
  bloqueo no lo cambian por sí solos: se pregunta por Telegram y solo si confirmas que fue un bloqueo
  lo alarga un poco (×1,2 por defecto). Mientras tanto, tras un bloqueo detectado pausa unos minutos.
  Los bloqueos sin confirmar no cuentan para nada de esto.
- Piso aprendido: el ritmo del último bloqueo confirmado ×1,2 queda como piso: el ritmo no baja de ahí. Si lo
  bloquean otra vez estando en el piso, el piso sube otro ×1,2 (1 → 1,2 → 1,44 min…). Cada `horas_baja_piso`
  (2) sin bloqueos confirmados baja `baja_piso_s` (12 s): 3,0 → 2,8 → 2,6 min…, así vuelve a probar el límite
  despacio en vez de saltar de golpe al mínimo. Se olvida a los `ritmo.dias_piso` días (7).
- Errores que se repiten: si un mismo tipo de error sale 3 veces seguidas (contando los reintentos), se
  aprende; desde entonces, apenas aparece, pausa `pausa_error_min` (5) en vez de reintentar, y luego sigue
  con el mismo ritmo. Se olvida si pasa 7 días sin aparecer.
- Pausa tras un bloqueo, por tipo (la frase que lo delató): si al volver lo bloquean otra vez, la siguiente
  dura el doble; la que funcionó se recuerda para ese tipo y la próxima vez prueba un 25 % menos.
- A qué horas aparecieron citas: en esas horas (y la anterior) revisa al doble de ritmo, sin bajar del piso.
- Tipos de error (paso + causa, p. ej. «vuelta_clave | 502 bad gateway»): lo que respondiste en Telegram
  ("error normal" o "fue bloqueo") se recuerda por tipo, para no volver a preguntar por el mismo.
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
PAUSA_MIN = 5          # pausa base tras un bloqueo (se sobrescribe con `pausa_bloqueo_min` del yaml)
PAUSA_ERROR_MIN = 5    # pausa ante un error que ya se repitió (se sobrescribe con `pausa_error_min` del yaml)
SEGUIDOS_PAUSA = 3     # veces seguidas que un tipo de error tiene que salir para aprender a pausar
RITMO = {"minimo_s": 60, "maximo_s": 1800, "inicial_s": 600, "baja_s": 30, "sube_bloqueo": 1.2,
         "dias_piso": 7, "horas_baja_piso": 2, "baja_piso_s": 12}  # se sobrescribe con `ritmo:` del yaml


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

    # --- Reintentos volviendo a la pantalla anterior -------------------------------------------
    def vueltas(self, paso: str, cfg: dict | None = None, con_citas: bool = False) -> int:
        """Cuántas veces volver atrás y pulsar de nuevo ante un error del sitio en este paso. Empieza en
        `reintentos_paso` (2) y aprende entre 1 y `reintentos_paso_max` (5); con citas a la vista, el tope."""
        cfg = cfg or {}
        base, tope = int(cfg.get("reintentos_paso", 2)), int(cfg.get("reintentos_paso_max", 5))
        if con_citas:
            return tope
        return min(tope, int(self.d.get("vueltas", {}).get(paso, {}).get("limite", base)))

    def resultado_vuelta(self, paso: str, usadas: int, entro: bool, cfg: dict | None = None) -> None:
        """Si entró justo con el último reintento, suma uno (estuvo cerca de no alcanzar). Si se agotaron
        3 veces seguidas sin entrar, resta uno: insistir más ahí solo cansa al sitio."""
        cfg = cfg or {}
        tope = int(cfg.get("reintentos_paso_max", 5))
        v = self.d.setdefault("vueltas", {}).setdefault(
            paso, {"limite": int(cfg.get("reintentos_paso", 2)), "entro": 0, "agotadas": 0, "seguidas": 0})
        if entro:
            v["entro"] += 1
            v["seguidas"] = 0
            if usadas >= v["limite"]:
                v["limite"] = min(tope, v["limite"] + 1)
        else:
            v["agotadas"] += 1
            v["seguidas"] += 1
            if v["seguidas"] >= SEGUIDOS_PAUSA:
                v["limite"], v["seguidas"] = max(1, v["limite"] - 1), 0

    def orden(self, paso: str, selectores: tuple[str, ...]) -> list[str]:
        """Selectores del botón, con el que funcionó la última vez primero."""
        bueno = self.d["selectores"].get(paso)
        return sorted(selectores, key=lambda s: s != bueno)

    # --- Revisiones y horario ------------------------------------------------------------------
    def revision(self, resultado: str, paso: str | None = None, cfg: dict | None = None,
                 frase: str | None = None, causa: str | None = None) -> None:
        """resultado: sin_citas | hay_citas | reservada | error | bloqueo. Ajusta el ritmo según cómo fue.
        causa: en un error, qué pasó en pocas palabras (junto con el paso forma el tipo de error)."""
        r = {"t": f"{datetime.now():%Y-%m-%dT%H:%M}", "r": resultado, "cada_s": self.intervalo(cfg)}
        if paso:
            r["paso"] = paso
        if causa:
            r["causa"] = causa
        self._ajustar_ritmo(resultado, cfg or {}, paso, frase)
        self.d["revisiones"].append(r)

    # --- Ritmo aprendido -----------------------------------------------------------------------
    @staticmethod
    def _limites(cfg: dict | None) -> dict:
        return {**RITMO, **((cfg or {}).get("ritmo") or {})}

    def intervalo(self, cfg: dict | None = None) -> int:
        """Segundos entre revisiones que se están usando ahora."""
        return int(self.d["ritmo"].get("intervalo_s", self._limites(cfg)["inicial_s"]))

    def _bloqueos(self, cfg: dict | None = None) -> list[dict]:
        """Bloqueos confirmados de los últimos `ritmo.dias_piso` días (0 = ninguno: no frenan el ritmo).
        Se guardan 7 días (para mostrarlos y esperar tu respuesta). Los que esperan tu respuesta en Telegram
        no cuentan; los anotados antes de preguntar, sí."""
        desde = f"{datetime.now() - timedelta(days=DIAS_PISO):%Y-%m-%dT%H:%M}"
        bloqueos = [b for b in self.d["ritmo"].get("bloqueos", []) if b["t"] >= desde]
        self.d["ritmo"]["bloqueos"] = bloqueos[-50:]
        dias = self._limites(cfg)["dias_piso"]
        desde = f"{datetime.now() - timedelta(days=dias):%Y-%m-%dT%H:%M}"
        return [b for b in bloqueos if b.get("confirmado", True) and dias and b["t"] >= desde]

    def piso(self, cfg: dict | None = None) -> int:
        """Ritmo mínimo seguro: el del último bloqueo confirmado × `sube_bloqueo` (un bloqueo estando en el piso
        lo sube otro escalón), menos `baja_piso_s` por cada `horas_baja_piso` que pasaron sin bloqueos."""
        ultimo = max(self._bloqueos(cfg), key=lambda b: b["t"], default=None)
        if not ultimo:
            return 0
        lim = self._limites(cfg)
        # la bajada cuenta desde el último bloqueo, o desde que se usa esta versión si es posterior (así un piso
        # aprendido antes no cae de golpe)
        desde = max(ultimo["t"], self.d["ritmo"].setdefault("piso_desde", f"{datetime.now():%Y-%m-%dT%H:%M}"))
        horas = (datetime.now() - datetime.fromisoformat(desde)).total_seconds() / 3600
        bajadas = int(horas // lim["horas_baja_piso"]) if lim["horas_baja_piso"] else 0
        return int(max(lim["minimo_s"], ultimo["cada_s"] * lim["sube_bloqueo"] - bajadas * lim["baja_piso_s"]))

    def _por_hora(self, hasta: datetime | None = None) -> int:
        """Revisiones en los 60 min previos a `hasta` (por defecto, ahora)."""
        hasta = hasta or datetime.now()
        desde, fin = f"{hasta - timedelta(hours=1):%Y-%m-%dT%H:%M}", f"{hasta:%Y-%m-%dT%H:%M}"
        return sum(1 for r in self.d["revisiones"] if desde <= r["t"] <= fin)

    def respuesta_bloqueo(self, t: str, es_bloqueo: bool, cfg: dict | None = None) -> str | None:
        """Lo que respondiste en Telegram sobre el bloqueo anotado a la hora `t`.
        Sí: cuenta para el piso y el ritmo sube un poco.
        No: se levanta la pausa y la frase en ese paso se recuerda como falsa alarma.
        Devuelve un texto para el chat, o None si ya no está ese bloqueo."""
        ritmo = self.d["ritmo"]
        b = next((b for b in ritmo.get("bloqueos", []) if b["t"] == t), None)
        if not b:
            return None
        if es_bloqueo:
            if b.get("confirmado"):
                return "Ya estaba confirmado."
            b["confirmado"] = True
            self._subir_ritmo(b["cada_s"], cfg)
            return f"Confirmado: después de la pausa reviso cada {self.intervalo(cfg) / 60:.1f} min."
        ritmo["bloqueos"].remove(b)
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
        bloqueo encubierto: se aprende como un bloqueo confirmado (piso, ritmo un poco más lento
        y pausa)."""
        cfg = cfg or {}
        r = next((r for r in reversed(self.d["revisiones"]) if r["t"] == t and r["r"] == "error"), None)
        if not r:
            return None
        r["r"] = "bloqueo"
        ritmo = self.d["ritmo"]
        self._recordar_error(r, "bloqueo")
        frase = f"error en {r.get('paso', '?')}"
        cada = r.get("cada_s", self.intervalo(cfg))
        ritmo.setdefault("bloqueos", []).append({
            "t": t, "cada_s": cada, "frase": frase, "paso": r.get("paso", "?"),
            "por_hora": self._por_hora(datetime.fromisoformat(t)), "confirmado": True})
        self._subir_ritmo(cada, cfg)
        ritmo["pausa_min"], ritmo["ultima_frase"] = cfg.get("pausa_bloqueo_min", PAUSA_MIN), frase
        return (f"Aprendido como bloqueo: pauso {ritmo['pausa_min']} min y después reviso cada "
                f"{ritmo['intervalo_s'] / 60:.1f} min.")

    # --- Tipos de error ya respondidos -----------------------------------------------------------
    @staticmethod
    def tipo_error(paso: str | None, causa: str | None) -> str:
        return f"{paso or '?'} | {causa or '?'}"

    def _recordar_error(self, r: dict, que: str) -> None:
        if r.get("causa"):  # los errores anotados antes de clasificar la causa no forman un tipo
            self.d["ritmo"].setdefault("tipos_error", {})[self.tipo_error(r.get("paso"), r["causa"])] = que

    def tipo_conocido(self, paso: str | None, causa: str | None) -> str | None:
        """"normal" o "bloqueo" si ya respondiste por este tipo de error; None si nunca preguntó."""
        return self.d["ritmo"].get("tipos_error", {}).get(self.tipo_error(paso, causa))

    def error_normal(self, t: str) -> str | None:
        """Respondiste que el error de la hora `t` es normal: no se vuelve a preguntar por ese tipo."""
        r = next((r for r in reversed(self.d["revisiones"]) if r["t"] == t and r["r"] == "error"), None)
        if not r:
            return None
        if not r.get("causa"):
            return "Ok, queda como error normal."
        self._recordar_error(r, "normal")
        return (f"Anotado: «{r['causa']}» al {r.get('paso', '?')} es un error normal. "
                "No te vuelvo a preguntar por ese tipo (salvo que se repita varias veces seguidas).")

    def error_repetido(self, paso: str | None, causa: str | None, cfg: dict | None = None) -> bool:
        """Anota un intento fallido (también los reintentos dentro de una revisión). Devuelve True si conviene
        pausar `pausa_error_min` en vez de reintentar: este tipo ya salió 3 veces seguidas alguna vez
        (en los últimos 7 días), o acaba de hacerlo."""
        ritmo, tipo = self.d["ritmo"], self.tipo_error(paso, causa)
        racha = ritmo.get("racha") or {}
        n = racha["n"] + 1 if racha.get("tipo") == tipo else 1
        ritmo["racha"] = {"tipo": tipo, "n": n}
        aprendidos = self._errores_pausa()
        if tipo in aprendidos or n >= SEGUIDOS_PAUSA:
            aprendidos[tipo] = f"{datetime.now():%Y-%m-%dT%H:%M}"
            return True
        return False

    def _errores_pausa(self) -> dict:
        """Tipos de error que aprendió a esperar (tipo → última vez); olvida los de más de 7 días."""
        desde = f"{datetime.now() - timedelta(days=DIAS_PISO):%Y-%m-%dT%H:%M}"
        ritmo = self.d["ritmo"]
        ritmo["errores_pausa"] = {t: f for t, f in ritmo.get("errores_pausa", {}).items() if f >= desde}
        return ritmo["errores_pausa"]

    def pausa_error_min(self, cfg: dict | None = None) -> int:
        return int((cfg or {}).get("pausa_error_min", PAUSA_ERROR_MIN))

    def errores_seguidos(self) -> int:
        """Revisiones seguidas, desde la última, que terminaron en error."""
        n = 0
        for r in reversed(self.d["revisiones"]):
            if r["r"] != "error":
                break
            n += 1
        return n

    def es_falsa_alarma(self, frase: str, paso: str) -> bool:
        return {"frase": frase, "paso": paso} in self.d["ritmo"].get("falsas_alarmas", [])

    def pausa_min(self, cfg: dict | None = None) -> int:
        """Minutos de pausa tras un bloqueo: la base de la config, o lo aprendido si se repitieron."""
        return int(self.d["ritmo"].get("pausa_min", (cfg or {}).get("pausa_bloqueo_min", PAUSA_MIN)))

    def _subir_ritmo(self, cada_s: int, cfg: dict | None) -> None:
        """Bloqueo confirmado: el ritmo pasa a `sube_bloqueo` × el que había al bloquear (nunca baja)."""
        lim = self._limites(cfg)
        nuevo = max(self.intervalo(cfg), cada_s * lim["sube_bloqueo"], self.piso(cfg))
        self.d["ritmo"]["intervalo_s"] = int(min(lim["maximo_s"], nuevo))

    def _ajustar_ritmo(self, resultado: str, cfg: dict, paso: str | None = None, frase: str | None = None) -> None:
        lim, ritmo = self._limites(cfg), self.d["ritmo"]
        actual = self.intervalo(cfg)
        anterior = self.d["revisiones"][-1]["r"] if self.d["revisiones"] else None
        base = cfg.get("pausa_bloqueo_min", PAUSA_MIN)
        pausas = ritmo.setdefault("pausa_por_frase", {})
        if resultado == "bloqueo":
            frase = frase or "?"
            ritmo.setdefault("bloqueos", []).append({
                "t": f"{datetime.now():%Y-%m-%dT%H:%M}", "cada_s": actual, "frase": frase, "paso": paso or "?",
                "por_hora": self._por_hora() + 1, "confirmado": False})  # el ritmo espera tu respuesta
            nuevo = actual
            if anterior == "bloqueo":  # bloqueo justo al volver de la pausa: la próxima, el doble
                ritmo["pausa_min"] = min(240, self.pausa_min(cfg) * 2)
            else:  # parte de la que funcionó la última vez con este tipo, probando un 25 % menos
                ritmo["pausa_min"] = max(base, int(pausas.get(frase, base) * 0.75))
            ritmo["ultima_frase"] = frase
        elif resultado == "error":  # no cambia: si fue un bloqueo encubierto lo dirás por Telegram
            nuevo = actual
        else:  # revisión completa: probar un poco más rápido
            nuevo = actual - lim["baja_s"]
            ritmo.pop("racha", None)
            if "pausa_min" in ritmo:  # la pausa funcionó: se recuerda para ese tipo de bloqueo
                pausas[ritmo.get("ultima_frase", "?")] = ritmo.pop("pausa_min")
        ritmo["intervalo_s"] = int(min(lim["maximo_s"], max(lim["minimo_s"], self.piso(cfg), nuevo)))

    def horas_intensas(self, extra=()) -> set[int]:
        """Horas en que aparecieron citas (últimos 60 días) y la hora anterior, más las de la config."""
        desde = f"{datetime.now() - timedelta(days=DIAS_HORAS):%Y-%m-%dT%H:%M}"
        horas = {int(r["t"][11:13]) for r in self.d["revisiones"]
                 if r["r"] == "hay_citas" and r["t"] >= desde}
        return {h % 24 for x in horas for h in (x - 1, x)} | {int(h) for h in extra}

    def ciclo_real(self, horas: float = 2) -> int | None:
        """Segundos reales entre el inicio de una revisión y el de la siguiente (mediana de las últimas `horas`).
        Es más que `intervalo`: suma lo que dura cada revisión y la espera hasta que el timer la lanza."""
        desde = f"{datetime.now() - timedelta(hours=horas):%Y-%m-%dT%H:%M}"
        ts = [datetime.fromisoformat(r["t"]) for r in self.d["revisiones"] if r["t"] >= desde]
        huecos = sorted((b - a).total_seconds() for a, b in zip(ts, ts[1:]) if b > a)
        return int(huecos[len(huecos) // 2]) if len(huecos) >= 3 else None

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
            segundos = max(self._limites(cfg)["minimo_s"], self.piso(cfg), segundos // 2)
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
        for paso, v in sorted(self.d.get("vueltas", {}).items()):
            lineas.append(f"  {paso}: vuelve atrás hasta {v['limite']} veces ante un error del sitio "
                          f"(entró {v['entro']}, se agotaron {v['agotadas']})")
        vistas = [r["t"] for r in revs if r["r"] == "hay_citas"]
        lineas.append("Citas vistas: " + (", ".join(vistas[-10:]) if vistas else "ninguna todavía"))
        intensas = sorted(self.horas_intensas((cfg or {}).get("horas_intensas") or ()))
        lineas.append("Horas intensas: " + (", ".join(f"{h:02d}h" for h in intensas) if intensas else "ninguna"))
        lim = self._limites(cfg)
        lineas.append(f"Ritmo: cada {self.intervalo(cfg) / 60:.1f} min · piso por bloqueos: "
                      + (f"{self.piso(cfg) / 60:.1f} min (baja {lim['baja_piso_s'] / 60:.1f} min cada "
                         f"{lim['horas_baja_piso']} h sin bloqueos)" if self.piso(cfg) else "ninguno")
                      + f" · pausa tras bloqueo: {self.pausa_min(cfg)} min")
        pausas = self.d["ritmo"].get("pausa_por_frase", {})
        if pausas:
            lineas.append("Pausa que funcionó por tipo: " + ", ".join(f"«{f}» {m} min" for f, m in pausas.items()))
        for f in self.d["ritmo"].get("falsas_alarmas", []):
            lineas.append(f"  falsa alarma (según tú): «{f['frase']}» en {f['paso']}")
        self._bloqueos()  # olvida los de más de 7 días
        for b in self.d["ritmo"].get("bloqueos", [])[-5:]:
            marca = " ✓" if b.get("confirmado", True) else " (sin confirmar: no cuenta)"
            lineas.append(f"  bloqueo {b['t']}{marca}: «{b.get('frase', '?')}» en {b.get('paso', '?')}, cada "
                          f"{b['cada_s'] / 60:.1f} min, "
                          f"{b.get('por_hora') or self._por_hora(datetime.fromisoformat(b['t']))} revisiones en la hora previa")
        for tipo, que in self.d["ritmo"].get("tipos_error", {}).items():
            lineas.append(f"  error «{tipo}»: {'normal, no pregunto' if que == 'normal' else 'cuenta como bloqueo'}")
        for tipo, f in self._errores_pausa().items():
            lineas.append(f"  error «{tipo}»: salió {SEGUIDOS_PAUSA} veces seguidas → al aparecer pauso "
                          f"{self.pausa_error_min(cfg)} min (última: {f.replace('T', ' ')})")
        if self.d.get("proxima"):
            lineas.append(f"Próxima revisión: {self.d['proxima']}")
        return "\n".join(lineas)

    def informe(self, horas: int = 24) -> str:
        """Qué pasó en las últimas `horas`: resultados, errores por tipo y citas vistas (para /informe)."""
        desde = f"{datetime.now() - timedelta(hours=horas):%Y-%m-%dT%H:%M}"
        revs = [r for r in self.d["revisiones"] if r["t"] >= desde]
        if not revs:
            return f"Sin revisiones en las últimas {horas} h."
        conteo = Counter(r["r"] for r in revs)
        lineas = [f"📊 Últimas {horas} h: {len(revs)} revisiones (" + ", ".join(
            f"{k.replace('_', ' ')}: {v}" for k, v in conteo.most_common()) + ")"]
        ok = conteo["sin_citas"] + conteo["hay_citas"] + conteo["reservada"]
        lineas.append(f"Completas: {ok * 100 // len(revs)} %")
        errores = Counter(self.tipo_error(r.get("paso"), r.get("causa")) for r in revs if r["r"] == "error")
        if errores:
            lineas.append("Errores por tipo:")
            for tipo, n in errores.most_common(8):
                que = self.d["ritmo"].get("tipos_error", {}).get(tipo)
                lineas.append(f"  • {tipo} ×{n}" + (f" ({que})" if que else ""))
        vistas = [r["t"][11:] for r in revs if r["r"] == "hay_citas"]
        lineas.append("Citas vistas: " + (", ".join(vistas) if vistas else "ninguna"))
        bloqueos = [r["t"][11:] for r in revs if r["r"] == "bloqueo"]
        if bloqueos:
            lineas.append("Bloqueos: " + ", ".join(bloqueos))
        return "\n".join(lineas)
