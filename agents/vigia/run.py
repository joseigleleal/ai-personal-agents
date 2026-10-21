"""Vigía: audita a los demás agentes desde afuera y avisa solo cuando algo anda mal.

Lee lo que cada agente ya deja en disco (memoria, log, archivos de pausa) y el estado de systemd y del servidor.
No toca a los agentes ni usa modelo de lenguaje: son reglas concretas en config/vigia.yaml.
- Un problema nuevo se avisa una vez, con sonido; cuando se arregla, llega "resuelto" en silencio.
- Una vez al día (`parte_diario_hora`) manda en silencio el parte de todos los agentes.
El estado (qué problemas están abiertos y desde cuándo) queda en data/vigia.json.

Uso:
    python -m agents.vigia.run            # revisa y avisa si hay algo nuevo (el timer lo lanza cada 30 min)
    python -m agents.vigia.run --ver      # muestra el parte sin avisar
    python -m agents.vigia.run --parte    # manda el parte ahora
"""
import argparse
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from core import notify
from core.config import DATA_DIR, ROOT, load_config

ESTADO = DATA_DIR / "vigia.json"


def systemd(*args: str) -> str:
    """Respuesta de systemctl ("active", "failed", "enabled"…); "" donde no hay systemd (la Mac)."""
    if not shutil.which("systemctl"):
        return ""
    r = subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=20)
    return r.stdout.strip()


def leer_json(ruta: Path) -> dict:
    try:
        return json.loads(ruta.read_text())
    except (OSError, ValueError):
        return {}


def ruta(texto: str) -> Path:
    return ROOT / texto


# --- Chequeos: cada uno devuelve (problemas {clave: texto}, líneas para el parte) ----------------------------
def revisar_servicio(nombre: str, c: dict) -> tuple[dict, list[str], bool]:
    """Timer apagado = agente detenido a propósito (no es problema). Servicio "failed" sí lo es."""
    servicio = c.get("servicio")
    if not servicio or not shutil.which("systemctl"):
        return {}, [], True
    if systemd("is-enabled", f"{servicio}.timer") != "enabled":
        return {}, ["⏹️ detenido (timer apagado)"], False
    if systemd("is-failed", f"{servicio}.service") == "failed":
        return {f"{nombre}:fallo": f"el servicio {servicio} quedó en estado «failed»"}, [], True
    return {}, [], True


def revisar_cita(c: dict, ahora: datetime) -> tuple[dict, list[str]]:
    problemas, lineas = {}, []
    if ruta(c.get("reservada", "data/cita/RESERVADA")).exists():
        return {}, ["✅ cita reservada: ya no busca"]
    mem = leer_json(ruta(c.get("memoria", "data/cita/aprendizaje.json")))
    revs = mem.get("revisiones", [])
    pausa = ruta(c.get("pausa", "data/cita/pausa_hasta"))
    hasta = None
    if pausa.exists():
        try:
            hasta = datetime.fromisoformat(pausa.read_text().strip())
        except ValueError:
            pass
    if hasta and hasta > ahora:
        lineas.append(f"⏸️ en pausa hasta {hasta:%H:%M}")
        if hasta - ahora > timedelta(minutes=c.get("pausa_max_min", 90)):
            problemas["cita:pausa"] = f"en pausa hasta {hasta:%d/%m %H:%M} (más de {c.get('pausa_max_min', 90)} min)"
    proxima = mem.get("proxima")
    if proxima and not (hasta and hasta > ahora):
        atraso = (ahora - datetime.fromisoformat(proxima)).total_seconds() / 60
        if atraso > c.get("atraso_max_min", 15):
            problemas["cita:quieto"] = (f"tenía que revisar a las {proxima[11:16]} y lleva {atraso:.0f} min sin "
                                        "hacerlo (¿timer detenido o revisión colgada?)")
    desde = f"{ahora - timedelta(hours=1):%Y-%m-%dT%H:%M}"
    hora = [r for r in revs if r["t"] >= desde]
    errores = sum(r["r"] in ("error", "bloqueo") for r in hora)
    if len(hora) >= c.get("min_revisiones", 4):
        pct = 100 * errores // len(hora)
        if pct > c.get("max_errores_pct", 50):
            problemas["cita:errores"] = f"{pct} % de las revisiones de la última hora terminaron en error o bloqueo"
    ts = [datetime.fromisoformat(r["t"]) for r in hora]
    huecos = sorted((b - a).total_seconds() / 60 for a, b in zip(ts, ts[1:]) if b > a)
    ciclo = f" · ciclo ~{huecos[len(huecos) // 2]:.1f} min" if len(huecos) >= 3 else ""
    ultima = revs[-1] if revs else {}
    lineas.append(f"última hora: {len(hora)} revisiones, {errores} con error o bloqueo{ciclo}")
    if ultima:
        lineas.append(f"última: {ultima['t'][11:16]} → {ultima['r'].replace('_', ' ')}")
    vistas = [r["t"][11:16] for r in revs if r["r"] == "hay_citas" and r["t"] >= f"{ahora:%Y-%m-%d}"]
    if vistas:
        lineas.append("🚨 citas vistas hoy: " + ", ".join(vistas))
    return problemas, lineas


def revisar_log(nombre: str, c: dict, ahora: datetime) -> tuple[dict, list[str]]:
    """Agentes con corridas periódicas (como `empleo`): el log tiene que haber crecido hace poco."""
    log = ruta(c["log"])
    if not log.exists():
        return {f"{nombre}:sin_log": f"no existe {c['log']}"}, []
    edad = (ahora - datetime.fromtimestamp(log.stat().st_mtime)).total_seconds() / 60
    problemas = {}
    if edad > c.get("log_max_min", 180):
        problemas[f"{nombre}:quieto"] = (f"no corre hace {edad / 60:.1f} h "
                                         f"(máximo esperado: {c.get('log_max_min', 180) / 60:.0f} h)")
    ultimas = log.read_text(errors="replace").splitlines()[-40:]
    fallos = sum(any(p in l for p in c.get("frases_error", [])) for l in ultimas)
    if fallos >= c.get("max_frases_error", 3):
        problemas[f"{nombre}:fallos"] = f"{fallos} líneas con error en la última corrida (ver {c['log']})"
    return problemas, [f"última corrida hace {edad:.0f} min"]


def revisar_sistema(c: dict) -> tuple[dict, list[str]]:
    problemas = {}
    uso = shutil.disk_usage(ROOT)
    pct_disco = 100 * uso.used // uso.total
    if pct_disco > c.get("disco_max_pct", 85):
        problemas["sistema:disco"] = f"disco al {pct_disco} %"
    lineas = [f"disco {pct_disco} %"]
    if Path("/proc/meminfo").exists():
        mem = {l.split(":")[0]: int(l.split()[1]) for l in Path("/proc/meminfo").read_text().splitlines()}
        libre = 100 * mem["MemAvailable"] // mem["MemTotal"]
        if libre < c.get("ram_min_libre_pct", 10):
            problemas["sistema:ram"] = f"solo {libre} % de RAM libre"
        lineas.append(f"RAM libre {libre} %")
    carga = os.getloadavg()[0] / (os.cpu_count() or 1)
    lineas.append(f"carga {carga:.2f} por núcleo")
    for s in c.get("servicios", []):
        estado = systemd("is-active", s)
        if estado and estado != "active":
            problemas[f"sistema:{s}"] = f"el servicio {s} está «{estado}»"
    return problemas, lineas


def auditar(cfg: dict, ahora: datetime | None = None) -> tuple[dict, dict]:
    """Corre todos los chequeos. Devuelve (problemas {clave: texto}, parte {sección: [líneas]})."""
    ahora = ahora or datetime.now()
    problemas, parte = {}, {}
    for nombre, c in (cfg.get("agentes") or {}).items():
        p, lineas, activo = revisar_servicio(nombre, c)
        if activo:
            p2, l2 = revisar_cita(c, ahora) if c.get("tipo") == "cita" else revisar_log(nombre, c, ahora)
            p, lineas = {**p, **p2}, lineas + l2
        problemas.update(p)
        parte[nombre] = lineas
    p, lineas = revisar_sistema(cfg.get("sistema") or {})
    problemas.update(p)
    parte["servidor"] = lineas
    return problemas, parte


def texto_parte(parte: dict, problemas: dict) -> str:
    lineas = [f"🛰️ Parte del vigía · {datetime.now():%d/%m %H:%M}"]
    for nombre, items in parte.items():
        lineas.append(f"\n{nombre}:" + "".join(f"\n  · {i}" for i in items))
    lineas.append("\n" + ("⚠️ Abierto: " + "; ".join(problemas.values()) if problemas else "Todo en orden."))
    return "\n".join(lineas)


def avisar_cambios(problemas: dict, estado: dict, ahora: datetime) -> list[str]:
    """Avisa los problemas nuevos (con sonido) y los resueltos (en silencio). Devuelve lo que se avisó."""
    abiertos, avisos = estado.setdefault("abiertos", {}), []
    for clave, texto in problemas.items():
        if clave not in abiertos:
            if notify.telegram(f"⚠️ Vigía · {clave.split(':')[0]}: {texto}", "vigia"):
                abiertos[clave] = ahora.isoformat(timespec="minutes")
                avisos.append(clave)
    for clave in [c for c in abiertos if c not in problemas]:
        if notify.telegram(f"✅ Vigía · {clave.split(':')[0]}: resuelto (desde {abiertos[clave][11:16]})",
                           "vigia", silent=True):
            del abiertos[clave]
            avisos.append(clave)
    return avisos


def main() -> None:
    parser = argparse.ArgumentParser(description="Vigía: audita a los demás agentes")
    parser.add_argument("--ver", action="store_true", help="Muestra el parte sin avisar")
    parser.add_argument("--parte", action="store_true", help="Manda el parte ahora")
    args = parser.parse_args()

    cfg, ahora = load_config("vigia"), datetime.now()
    problemas, parte = auditar(cfg, ahora)
    if args.ver:
        print(texto_parte(parte, problemas))
        return
    estado = leer_json(ESTADO)
    avisos = avisar_cambios(problemas, estado, ahora)
    hoy, hora = f"{ahora:%Y-%m-%d}", cfg.get("parte_diario_hora", 9)
    if args.parte or (hora is not None and ahora.hour >= int(hora) and estado.get("parte_dia") != hoy):
        if notify.telegram(texto_parte(parte, problemas), "vigia", silent=True) and not args.parte:
            estado["parte_dia"] = hoy
    ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=1))
    print(f"{ahora:%Y-%m-%d %H:%M} Vigía: {len(problemas)} problema(s) abierto(s)"
          + (f"; avisos: {', '.join(avisos)}" if avisos else ""))


if __name__ == "__main__":
    main()
