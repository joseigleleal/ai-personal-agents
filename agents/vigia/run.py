"""Vigía: audita a los demás agentes desde afuera y avisa solo cuando algo anda mal.

Lee lo que cada agente ya deja en disco (memoria, log, archivos de pausa) y el estado de systemd y del servidor.
No toca a los agentes ni usa modelo de lenguaje: son reglas concretas en config/vigia.yaml.
- Un problema nuevo se avisa una vez, con sonido; cuando se arregla, llega "resuelto" en silencio.
- En cada vuelta edita un mismo mensaje (el tablero): agentes, CPU, carga, RAM, disco y procesos que más consumen.
- Si hay un pico (CPU o RAM), avisa qué lo genera con botones para detener cosas de una lista cerrada
  (`picos.acciones`); los botones se atienden en la vuelta siguiente (el timer corre cada 2 min).
- Una vez al día (`parte_diario_hora`) manda en silencio el parte de todos los agentes.
El estado (qué problemas están abiertos y desde cuándo) queda en data/vigia.json.

Uso:
    python -m agents.vigia.run            # revisa y avisa si hay algo nuevo (el timer lo lanza cada 2 min)
    python -m agents.vigia.run --ver      # muestra el parte sin avisar
    python -m agents.vigia.run --parte    # manda el parte ahora
"""
import argparse
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

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


def uso_cpu(segundos: float = 2) -> int | None:
    """% de CPU usado en los próximos `segundos` (de /proc/stat; None fuera de Linux)."""
    def leer():
        campos = [int(x) for x in Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]]
        return sum(campos), campos[3] + campos[4]  # total, ocioso (idle + iowait)
    if not Path("/proc/stat").exists():
        return None
    t1, o1 = leer()
    time.sleep(segundos)
    t2, o2 = leer()
    return round(100 * (1 - (o2 - o1) / max(t2 - t1, 1)))


def procesos_top(n: int = 3, nombres: list | None = None) -> list[tuple[str, float, float]]:
    """Los `n` procesos que más CPU usan ahora: (nombre, % CPU, GB). `nombres` traduce por patrón del comando
    ([{patron, nombre}]): así "python -m agents.cita.run" se ve como "agente cita" y no como "python"."""
    try:
        r = subprocess.run(["ps", "-eo", "pcpu,rss,args", "--sort=-pcpu", "--no-headers"],
                           capture_output=True, text=True, timeout=10)
    except OSError:
        return []
    filas = []
    for linea in r.stdout.splitlines()[:n]:
        partes = linea.split(None, 2)
        if len(partes) < 3:
            continue
        cpu, rss, args = float(partes[0]), int(partes[1]), partes[2]
        nombre = next((x["nombre"] for x in nombres or [] if x["patron"] in args),
                      Path(args.split()[0]).name)
        filas.append((nombre, cpu, rss / 1024 ** 2))
    return filas


def revisar_sistema(c: dict) -> tuple[dict, list[str], dict]:
    """CPU, carga, RAM, disco y servicios necesarios. Un pico de CPU no es problema: solo la carga de 15 min.
    Devuelve también las medidas crudas, para detectar picos."""
    problemas, lineas = {}, []
    nucleos = os.cpu_count() or 1
    cpu, (c1, c5, c15) = uso_cpu(c.get("muestra_cpu_s", 2)), os.getloadavg()
    medidas = {"cpu": cpu, "carga1": c1, "nucleos": nucleos, "ram_libre_pct": None, "top": []}
    lineas.append((f"CPU {cpu} %" if cpu is not None else "CPU ?") + f" · carga {c1:.2f} / {c5:.2f} / {c15:.2f}"
                  f" (1 / 5 / 15 min, {nucleos} núcleos)")
    if c15 / nucleos > c.get("carga_max_por_nucleo", 1.5):
        problemas["sistema:cpu"] = f"CPU saturada: carga media de 15 min {c15:.2f} con {nucleos} núcleos"
    if Path("/proc/meminfo").exists():
        mem = {l.split(":")[0]: int(l.split()[1]) for l in Path("/proc/meminfo").read_text().splitlines()}
        total, libre = mem["MemTotal"] / 1024 ** 2, mem["MemAvailable"] / 1024 ** 2
        pct_libre = medidas["ram_libre_pct"] = round(100 * libre / total)
        lineas.append(f"RAM {total - libre:.1f} de {total:.1f} GB ({pct_libre} % libre)")
        if pct_libre < c.get("ram_min_libre_pct", 10):
            problemas["sistema:ram"] = f"solo {pct_libre} % de RAM libre"
    uso = shutil.disk_usage(ROOT)
    pct_disco = round(100 * uso.used / uso.total)
    lineas.append(f"disco {uso.used / 1024 ** 3:.0f} de {uso.total / 1024 ** 3:.0f} GB ({pct_disco} %)")
    if pct_disco > c.get("disco_max_pct", 85):
        problemas["sistema:disco"] = f"disco al {pct_disco} %"
    if cpu is not None:
        medidas["top"] = procesos_top(c.get("procesos_top", 3), c.get("nombres"))
    if medidas["top"]:
        lineas.append("más CPU: " + " · ".join(f"{n} {cpu_p:.0f} % CPU, {gb:.1f} GB"
                                               for n, cpu_p, gb in medidas["top"]))
    for s in c.get("servicios", []):
        estado = systemd("is-active", s)
        if estado and estado != "active":
            problemas[f"sistema:{s}"] = f"el servicio {s} está «{estado}»"
    return problemas, lineas, medidas


# --- Picos: avisar qué los genera y ofrecer acciones de una lista cerrada ----------------------------------
def es_pico(m: dict, c: dict) -> bool:
    """CPU alta en la muestra y carga de 1 min alta (no un instante), o poca RAM libre."""
    cpu = (m.get("cpu") is not None and m["cpu"] >= c.get("cpu_pct", 90)
           and m["carga1"] / m["nucleos"] >= c.get("carga_1min_por_nucleo", 1.0))
    ram = m.get("ram_libre_pct") is not None and m["ram_libre_pct"] < c.get("ram_libre_pct", 15)
    return cpu or ram


def ollama_cargados(url: str) -> list[str]:
    try:
        return [x["name"] for x in requests.get(f"{url}/api/ps", timeout=5).json().get("models", [])]
    except (requests.RequestException, ValueError):
        return []


def pids(patron: str) -> list[int]:
    """Procesos cuyo comando contiene `patron`, sin contar shells (un bucle `bash -c "... patron ..."` no es el
    proceso a detener: matarlo cortaría todo el bucle y no solo la tanda en curso)."""
    r = subprocess.run(["pgrep", "-f", patron], capture_output=True, text=True)
    salida = []
    for pid in map(int, r.stdout.split()):
        try:
            nombre = Path(f"/proc/{pid}/comm").read_text().strip() if Path("/proc").exists() else ""
        except OSError:
            continue
        if pid != os.getpid() and nombre not in ("bash", "sh", "zsh", "dash"):
            salida.append(pid)
    return salida


def aplica(a: dict, cfg: dict) -> bool:
    """¿Tiene sentido ofrecer esta acción ahora? (hay algo que detener)"""
    if a["tipo"] == "descargar_ollama":
        return bool(ollama_cargados(a.get("url", "http://127.0.0.1:11434")))
    if a["tipo"] == "detener_proceso":
        return bool(pids(a["patron"]))
    if a["tipo"] == "pausar_cita":
        pausa = ruta(a.get("pausa", "data/cita/pausa_hasta"))
        try:
            return not (pausa.exists() and datetime.fromisoformat(pausa.read_text().strip()) > datetime.now())
        except ValueError:
            return True
    return False


def ejecutar(clave: str, cfg: dict) -> str:
    """Hace una acción de `picos.acciones` (lista cerrada en la config). Nunca ejecuta texto llegado de Telegram."""
    a = ((cfg.get("picos") or {}).get("acciones") or {}).get(clave)
    if not a:
        return "Esa acción no está en la lista permitida."
    if a["tipo"] == "descargar_ollama":
        url = a.get("url", "http://127.0.0.1:11434")
        modelos = ollama_cargados(url)
        for m in modelos:
            requests.post(f"{url}/api/generate", json={"model": m, "keep_alive": 0}, timeout=30)
        return f"Descargué de memoria: {', '.join(modelos)}." if modelos else "No había modelos cargados."
    if a["tipo"] == "detener_proceso":
        encontrados = pids(a["patron"])
        for pid in encontrados:
            try:
                os.kill(pid, 15)  # SIGTERM: que termine ordenado
            except OSError:
                pass
        return "Detenido." if encontrados else "Ya no estaba corriendo."
    if a["tipo"] == "pausar_cita":
        hasta = datetime.now() + timedelta(minutes=a.get("minutos", 60))
        ruta(a.get("pausa", "data/cita/pausa_hasta")).write_text(hasta.isoformat(timespec="minutes"))
        return f"Cita en pausa hasta las {hasta:%H:%M}."
    return "Tipo de acción desconocido."


def texto_pico(m: dict, inicio: str) -> str:
    lineas = [f"🔥 Pico en el servidor desde las {inicio[11:16]} · {datetime.now():%H:%M}",
              f"CPU {m.get('cpu')} % · carga {m['carga1']:.2f} (1 min, {m['nucleos']} núcleos)"
              + (f" · RAM {m['ram_libre_pct']} % libre" if m.get("ram_libre_pct") is not None else "")]
    if m.get("top"):
        lineas.append("Lo que más consume:")
        lineas += [f"· {n}: {cpu:.0f} % CPU, {gb:.1f} GB" for n, cpu, gb in m["top"]]
    lineas.append("¿Detengo algo? Toca un botón (solo opciones de la lista permitida).")
    return "\n".join(lineas)


def botones_pico(cfg: dict) -> dict:
    acciones = (cfg.get("picos") or {}).get("acciones") or {}
    filas = [[{"text": a["texto"], "callback_data": f"vig:{k}"}] for k, a in acciones.items() if aplica(a, cfg)]
    return {"inline_keyboard": filas}


def atender_botones(estado: dict, cfg: dict) -> list[str]:
    """Lee los botones pulsados desde la última vuelta (solo de tu chat) y ejecuta esas acciones."""
    updates = notify.telegram_api("getUpdates", {"offset": estado.get("tg_offset"), "timeout": 0,
                                                 "allowed_updates": ["callback_query"]}, "vigia") or []
    hechas, tu_chat = [], notify.chat_propio("vigia")
    for u in updates:
        estado["tg_offset"] = u["update_id"] + 1
        q = u.get("callback_query") or {}
        msg = q.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != tu_chat or not q.get("data", "").startswith("vig:"):
            continue
        clave = q["data"][4:]
        resultado = ejecutar(clave, cfg)
        hechas.append(f"{clave}: {resultado}")
        notify.telegram_api("answerCallbackQuery", {"callback_query_id": q["id"], "text": resultado[:180]}, "vigia")
        notify.telegram_api("editMessageText", {"message_id": msg.get("message_id"),
                                                "text": (msg.get("text") or "") + f"\n→ {resultado}",
                                                "reply_markup": botones_pico(cfg)}, "vigia")
    return hechas


def seguir_pico(m: dict, estado: dict, cfg: dict, ahora: datetime) -> str | None:
    """Abre, actualiza o cierra el aviso de pico. Uno por episodio: suena al empezar, después solo se edita."""
    c, pico = cfg.get("picos") or {}, estado.get("pico")
    if es_pico(m, c):
        if not pico:
            inicio = ahora.isoformat(timespec="minutes")
            r = notify.telegram_api("sendMessage", {"text": texto_pico(m, inicio),
                                                    "reply_markup": botones_pico(cfg)}, "vigia")
            if r:
                estado["pico"] = {"msg": r["message_id"], "desde": inicio, "normales": 0}
                return "pico nuevo"
        else:
            pico["normales"] = 0
            notify.telegram_api("editMessageText", {"message_id": pico["msg"], "text": texto_pico(m, pico["desde"]),
                                                    "reply_markup": botones_pico(cfg)}, "vigia")
        return None
    if pico:
        pico["normales"] = pico.get("normales", 0) + 1
        if pico["normales"] >= c.get("vueltas_para_cerrar", 2):  # dos vueltas normales seguidas: terminó
            notify.telegram_api("editMessageText", {"message_id": pico["msg"], "text":
                                f"✅ Pico terminado (de {pico['desde'][11:16]} a {ahora:%H:%M})."}, "vigia")
            del estado["pico"]
            return "pico terminado"
    return None


def auditar(cfg: dict, ahora: datetime | None = None) -> tuple[dict, dict, dict]:
    """Corre todos los chequeos. Devuelve (problemas {clave: texto}, parte {sección: [líneas]},
    medidas del servidor)."""
    ahora = ahora or datetime.now()
    problemas, parte = {}, {}
    for nombre, c in (cfg.get("agentes") or {}).items():
        p, lineas, activo = revisar_servicio(nombre, c)
        if activo:
            p2, l2 = revisar_cita(c, ahora) if c.get("tipo") == "cita" else revisar_log(nombre, c, ahora)
            p, lineas = {**p, **p2}, lineas + l2
        problemas.update(p)
        parte[nombre] = lineas
    p, lineas, medidas = revisar_sistema(cfg.get("sistema") or {})
    problemas.update(p)
    parte["servidor"] = lineas
    return problemas, parte, medidas


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
    problemas, parte, medidas = auditar(cfg, ahora)
    if args.ver:
        print(texto_parte(parte, problemas))
        return
    estado = leer_json(ESTADO)
    avisos = avisar_cambios(problemas, estado, ahora)
    if cfg.get("picos"):
        avisos += atender_botones(estado, cfg)
        cambio = seguir_pico(medidas, estado, cfg, ahora)
        if cambio:
            avisos.append(cambio)
    if cfg.get("tablero", True):  # un mensaje que se edita en cada vuelta, sin sonido
        mid = notify.telegram_estado(texto_parte(parte, problemas).replace("Parte del vigía", "Tablero del vigía"),
                                     "vigia", estado.get("tablero_msg"))
        if mid:
            estado["tablero_msg"] = mid
    hoy, hora = f"{ahora:%Y-%m-%d}", cfg.get("parte_diario_hora", 9)
    if args.parte or (hora is not None and ahora.hour >= int(hora) and estado.get("parte_dia") != hoy):
        if notify.telegram(texto_parte(parte, problemas), "vigia", silent=True) and not args.parte:
            estado["parte_dia"] = hoy
    ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=1))
    print(f"{ahora:%Y-%m-%d %H:%M} Vigía: {len(problemas)} problema(s) abierto(s)"
          + (f"; avisos: {', '.join(avisos)}" if avisos else ""))


if __name__ == "__main__":
    main()
