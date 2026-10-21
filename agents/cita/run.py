"""Agente de cita previa de extranjería: busca y reserva la cita de toma de huellas (TIE).

Repite el recorrido de la web de cita previa (provincia → trámite → Cl@ve con certificado) y, si hay
citas, reserva la primera disponible con los datos de config/cita.yaml. Avisa por Telegram en cada
hito: hay citas, pide código SMS, cita reservada o algo falló. Tras reservar, deja de buscar.
Si un paso falla, lo reintenta con más tiempo y, si sigue fallando, repite el recorrido completo.
Lo que aprende de cada revisión queda en data/cita/aprendizaje.json (ver aprendizaje.py).

Uso:
    python -m agents.cita.run                 # launchd lo lanza cada 1 min; revisa solo si toca
    python -m agents.cita.run --ya            # revisa ahora, sin esperar su turno
    python -m agents.cita.run --aprendizaje   # muestra lo aprendido y sale
    python -m agents.cita.run --solo-revisar  # comprueba si hay citas, pero no reserva
    python -m agents.cita.run --reiniciar     # vuelve a buscar después de una reserva

Por el chat del bot también responde comandos (/estado, /informe, /captura, /historial, /ver, /log…):
ver COMANDOS.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import threading
import time
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import certifi
import requests
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import AuthorityInformationAccessOID, ExtensionOID
from cryptography.x509.verification import PolicyBuilder, Store
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from agents.cita.aprendizaje import SEGUIDOS_PAUSA, Aprendizaje
from core import notify
from core.config import DATA_DIR, ROOT, env, load_config

ICP_HOST = "icp.administracionelectronica.gob.es"
ICP = f"https://{ICP_HOST}/icpplus/index.html"
WORK = DATA_DIR / "cita"
SHOTS = WORK / "ultima"            # capturas de la última corrida (se sobrescriben)
VISTA = WORK / "ultima.png"        # lo último que vio el navegador (se reemplaza en cada paso)
VISTA_ENVIADA = WORK / "ultima_enviada"  # resultado de la última revisión cuya captura se envió
DONE = WORK / "RESERVADA"          # existe = ya hay cita: no buscar más
PAUSE = WORK / "pausa_hasta"       # bloqueo del sitio: esperar antes de reintentar
LOCK = WORK / "corriendo"
LAST_ERROR = WORK / "ultimo_error"  # para no repetir el mismo aviso de error cada 10 min
MEMORIA = WORK / "aprendizaje.json"
LATIDO = WORK / "latido"           # se toca en cada arranque: si pasa mucho sin tocarse, la Mac dormía
PAGINAS = WORK / "paginas"         # copia de las páginas cuando hubo citas (para ensayar la reserva)
BLOQUEOS = WORK / "bloqueos"       # captura y HTML de cada bloqueo (las demás capturas se sobrescriben)
MAX_BLOQUEOS = 50                  # se guardan solo los más recientes
NO_ENVIADOS = WORK / "no_enviados.json"  # avisos que Telegram no entregó: se resumen en el siguiente arranque
MAX_NO_ENVIADOS = 20
HISTORIAL = WORK / "historial"     # capturas de cada revisión, por día (se borran tras `historial_dias`)
AVISO_FOTO = WORK / "aviso.png"    # copia de la captura que se está enviando (la de VISTA cambia en cada paso)
DATOS_EXTRA = WORK / "datos_extra.json"  # datos que el formulario pidió y respondiste por Telegram (campo → valor)
LOG = DATA_DIR / "cita.log"

NO_CITAS = "no hay citas disponibles"
BLOCKED = ("requested url was rejected", "too many requests", "demasiadas peticiones",
           "intrusion prevention")
SESION_CADUCADA = "sesión ha caducado"
# Campos visibles y vacíos de un formulario (radios, casillas y botones aparte): un dato que la config no tiene
CAMPOS_VACIOS_JS = """els => els.filter(e => e.offsetParent !== null && !e.disabled && !e.readOnly && (e.id || e.name)
    && !['hidden', 'radio', 'checkbox', 'submit', 'button', 'image', 'reset', 'file'].includes(e.type)
    && (e.tagName === 'SELECT' ? !e.value || /selecci|elija|^-+$/i.test(e.options[e.selectedIndex]?.text || '')
                               : !e.value.trim()))
  .map(e => ({sel: e.id ? '#' + CSS.escape(e.id) : `[name="${e.name}"]`, lista: e.tagName === 'SELECT',
              etiqueta: ((e.labels && e.labels[0] && e.labels[0].innerText) || e.title || e.placeholder
                         || e.name || e.id).replace(/\\s+/g, ' ').replace(/[:*]+$/, '').trim(),
              opciones: e.tagName === 'SELECT' ? [...e.options].map(o => o.text.trim())
                                                   .filter(t => t && !/selecci|elija|^-+$/i.test(t)) : []}))"""
NO_PREGUNTAR = re.compile(r"observ|comentario|txtCodigoVerificacion", re.I)  # opcionales, o el SMS (va aparte)
# Páginas de error del propio sitio: no dicen "no hay citas" y antes se confundían con que sí las había
ERRORES_SITIO = ("bad gateway", "internal server error", "service unavailable", "gateway timeout",
                 "request rejected")


class Bloqueado(Exception):
    def __init__(self, mensaje: str, frase: str = "?", paso: str = "?"):
        super().__init__(mensaje)
        self.frase, self.paso = frase, paso  # qué texto lo delató y dónde: el agente aprende de cada tipo


class Inesperado(Exception):
    pass


class SesionCaducada(Exception):
    """El sitio perdió la sesión al volver de Cl@ve: es pasajero, se reintenta el recorrido."""


class ErrorDelSitio(Exception):
    """El sitio respondió con una página de error (502, 500, rechazo…): se reintenta el recorrido."""


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def avisar(text: str, sonido: bool = False) -> None:
    log(text.replace("\n", " | "))
    if not notify.telegram(text, "cita"):
        no_enviado(text.splitlines()[0][:120])
        notify.macos("Cita extranjería", text.splitlines()[0])
    if sonido and shutil.which("afplay"):  # solo en la Mac; en el servidor no existe y cortaba la reserva
        subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"], check=False)


def avisar_con_foto(text: str) -> None:
    """Aviso con la captura de lo que el agente está viendo, enviado en paralelo para no frenar la reserva.
    Si la foto no sale, manda el texto solo."""
    try:
        shutil.copyfile(VISTA, AVISO_FOTO)
    except OSError:
        avisar(text, sonido=True)
        return
    log(text.replace("\n", " | "))

    def enviar():
        if not notify.telegram_foto(AVISO_FOTO, text, "cita"):
            avisar(text)
    threading.Thread(target=enviar).start()


def esperar_codigo_telegram(minutos: int = 8) -> str | None:
    """Espera que respondas al bot con el código SMS (solo dígitos)."""
    return esperar_respuesta_telegram(minutos, lambda t: re.fullmatch(r"\d{4,8}", t) is not None)


def esperar_respuesta_telegram(minutos: int, vale) -> str | None:
    """Espera tu próximo mensaje en el chat del bot que cumpla `vale(texto)` (los comandos /… se ignoran)."""
    token = env("TELEGRAM_BOT_TOKEN_CITA") or env("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    tu_chat = str(env("TELEGRAM_CHAT_ID_CITA") or env("TELEGRAM_CHAT_ID") or "")
    api = f"https://api.telegram.org/bot{token}/getUpdates"
    try:  # descarta mensajes anteriores
        updates = requests.get(api, timeout=20).json().get("result", [])
        offset = updates[-1]["update_id"] + 1 if updates else None
    except requests.RequestException:
        offset = None
    limit = time.time() + minutos * 60
    while time.time() < limit:
        try:
            r = requests.get(api, params={"offset": offset, "timeout": 30}, timeout=40).json()
        except requests.RequestException:
            time.sleep(5)
            continue
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            text = msg.get("text", "").strip()
            if text and not text.startswith("/") and str(msg.get("chat", {}).get("id")) == tu_chat and vale(text):
                return text
    return None


def leer_datos_extra() -> dict:
    try:
        return json.loads(DATOS_EXTRA.read_text())
    except (OSError, ValueError):
        return {}


class Flujo:
    def __init__(self, page: Page, cfg: dict, mem: Aprendizaje, n: int = 0):
        self.page, self.cfg, self.mem, self.n = page, cfg, mem, n
        self.paso = "inicio"         # último paso alcanzado (para el log y la memoria)
        self.confirmando = False     # tras pulsar Confirmar ya no se repite el recorrido

    def foto(self, nombre: str, guardar_en: Path | None = None) -> None:
        """Captura y HTML de la página: el HTML sirve para ensayar la reserva sin el sitio real.
        `guardar_en`: carpeta donde además se conserva una copia (no se sobrescribe en la próxima revisión)."""
        self.n += 1
        try:
            self.page.screenshot(path=SHOTS / f"{self.n:02d}_{nombre}.png", full_page=True)
            shutil.copyfile(SHOTS / f"{self.n:02d}_{nombre}.png", VISTA)
            if guardar_en:
                guardar_en.mkdir(parents=True, exist_ok=True)
                base = guardar_en / f"{datetime.now():%Y%m%d-%H%M%S}_{nombre}_{self.paso}"
                shutil.copyfile(SHOTS / f"{self.n:02d}_{nombre}.png", base.with_suffix(".png"))
                shutil.copyfile(SHOTS / f"{self.n:02d}_{nombre}.html", base.with_suffix(".html"))
                for viejo in sorted(guardar_en.glob("*.png"))[:-MAX_BLOQUEOS]:
                    viejo.unlink()
                    viejo.with_suffix(".html").unlink(missing_ok=True)
            (SHOTS / f"{self.n:02d}_{nombre}.html").write_text(self.page.content())
        except Exception:  # noqa: BLE001 - la captura nunca debe cortar el flujo
            pass

    def error_del_sitio(self) -> None:
        """Si la página es un error del sitio (502, 500…), lo lanza para repetir el recorrido."""
        texto = self.texto()[:500]
        frase = next((f for f in ERRORES_SITIO if f in texto), None)
        if frase:
            raise ErrorDelSitio(frase)

    def texto(self) -> str:
        try:
            return self.page.inner_text("body").lower()
        except Exception:  # noqa: BLE001 - página cerrada o navegando
            return ""

    def revisar_bloqueo(self) -> None:
        frase = next((b for b in BLOCKED if b in self.texto()), None)
        if frase and self.mem.es_falsa_alarma(frase, self.paso):  # tú dijiste que en este paso no lo es
            # no es bloqueo, pero la página es un rechazo sin salida: esperar el botón era perder hasta 90 s
            log(f"Texto «{frase}» en «{self.paso}»: lo marcaste como falsa alarma; repito el recorrido.")
            self.foto("falsa_alarma", BLOQUEOS)
            raise ErrorDelSitio(frase)
        elif frase:
            self.foto("bloqueo", BLOQUEOS)  # la página del bloqueo es la que interesa ver, y se conserva
            raise Bloqueado(f"«{frase}» en {self.page.url}", frase, self.paso)

    def pulsar(self, paso: str, *selectores: str, reintentar: bool = True) -> None:
        """Pulsa el primer botón de `selectores` que exista y espera la página siguiente.

        La espera sale de la memoria (crece si el paso suele tardar). Si se agota, reintenta una vez
        con más tiempo; si la página cambió igual (llegó tarde), lo da por bueno.
        """
        self.paso = paso
        for intento in range(2 if reintentar else 1):
            espera, antes = self.mem.timeout(paso), self.page.url
            try:
                self.page.wait_for_selector(", ".join(selectores), timeout=espera)
                sel = next((s for s in self.mem.orden(paso, selectores) if self.hay(s)), selectores[0])
                with self.page.expect_navigation(timeout=espera):
                    self.page.click(sel, timeout=espera)
                break
            except PlaywrightError as e:
                self.revisar_bloqueo()
                if self.page.url != antes:  # la página siguiente llegó después del tiempo de espera
                    sel = None
                    break
                self.mem.fallo(paso, str(e), por_tiempo="Timeout" in str(e))
                if intento or not reintentar:
                    raise
                log(f"Paso «{paso}»: sin respuesta en {espera / 1000:.0f} s; reintento con "
                    f"{self.mem.timeout(paso) / 1000:.0f} s.")
        self.page.wait_for_load_state("domcontentloaded")
        self.revisar_bloqueo()
        self.error_del_sitio()
        self.mem.exito(paso, sel)

    def hay(self, selector: str) -> bool:
        return self.page.locator(selector).count() > 0

    # --- 1. Provincia, trámite y entrada con Cl@ve -------------------------------------------
    def entrar(self) -> None:
        cfg = self.cfg
        self.paso = "inicio"
        self.page.goto(ICP, wait_until="domcontentloaded", timeout=self.mem.timeout("inicio") * 2)
        self.revisar_bloqueo()
        self.page.select_option("select#form", label=cfg["provincia"])
        self.pulsar("provincia", "#btnAceptar")
        self.foto("tramite")

        oficina = str(cfg.get("oficina", "99"))
        if self.page.input_value("select#sede") != oficina:  # cambiar la oficina recarga la página
            with self.page.expect_navigation():
                self.page.select_option("select#sede", oficina)
            self.page.wait_for_load_state("domcontentloaded")
        grupo = "tramiteGrupo[1]" if cfg.get("tramite_policia", True) else "tramiteGrupo[0]"
        sel = f"select[name='{grupo}']"
        valor = self.page.eval_on_selector_all(
            f"{sel} option", "(os, t) => (os.find(o => o.text.toUpperCase().includes(t)) || {}).value",
            cfg["tramite"].upper())
        if not valor:
            raise Inesperado(f"No encontré el trámite «{cfg['tramite']}» en {cfg['provincia']}")
        self.page.select_option(sel, valor)
        self.pulsar("tramite", "#btnAceptar")
        self.foto("info")

        self.pulsar("acceso_clave", "#btnAccesoClave", "input[value*='Cl@ve' i]", "button:has-text('Cl@ve')")
        self.foto("clave")
        self.paso = "certificado"
        boton = self.page.get_by_role("button", name=re.compile("Certificado electr", re.I))
        boton.first.click(timeout=self.mem.timeout("certificado"))
        self.paso = "vuelta_clave"
        fin = time.time() + 120
        while "icpplus/ac" not in self.page.url:  # vuelta de Cl@ve; a veces llega con la sesión caducada
            if "icpplus/infogenerica" in self.page.url and SESION_CADUCADA in self.texto():
                raise SesionCaducada(self.page.url)
            if time.time() > fin:
                raise Inesperado(f"No volvió de Cl@ve en 120 s (quedó en {self.page.url})")
            self.page.wait_for_timeout(1000)
        self.page.wait_for_load_state("domcontentloaded")
        self.revisar_bloqueo()
        self.foto("entrada")

    # --- 2. Datos del solicitante y comprobación de disponibilidad ---------------------------
    def hay_citas(self) -> bool:
        """Solo dice que hay citas si ve el formulario de datos del solicitante sin el aviso "no hay citas".
        Antes bastaba con no ver ese aviso, y una página "502 Bad Gateway" pasaba por citas disponibles."""
        self.paso = "entrada"
        if NO_CITAS in self.texto():
            return False
        self.error_del_sitio()
        if not (self.hay("#btnEnviar") or self.hay("#txtIdCitado")):
            raise Inesperado("La página de entrada no es la esperada (no dice si hay citas)")
        cfg = self.cfg
        self.paso = "datos"
        copiar = self.page.get_by_role("button", name=re.compile("Copiar a datos solicitante", re.I))
        if copiar.count():  # con Cl@ve: pasa NIE y nombre del usuario a los datos del solicitante
            copiar.first.click()
        self.rellenar("#txtIdCitado", cfg.get("nie"))
        self.rellenar("#txtDesCitado", cfg.get("nombre"))
        if self.hay("select#txtPaisNac") and cfg.get("pais"):
            self.page.select_option("select#txtPaisNac", label=cfg["pais"].upper())
        self.completar_faltantes()
        if self.hay("#btnEnviar"):
            self.pulsar("datos", "#btnEnviar")
            self.foto("validar")
        if NO_CITAS in self.texto():
            return False
        self.error_del_sitio()
        return True

    def rellenar(self, selector: str, valor: str | None) -> None:
        campo = self.page.locator(selector)
        if valor and campo.count() and campo.is_editable() and not campo.input_value():
            campo.fill(valor)

    def completar_faltantes(self) -> None:
        """Antes de enviar un formulario: si queda un campo vacío que la config no cubre (un dato nuevo que pide
        el sitio), usa lo que respondiste la vez anterior o te lo pregunta por Telegram con la captura.
        La respuesta se guarda en DATOS_EXTRA; "-" lo deja vacío y no lo vuelve a preguntar."""
        try:
            campos = self.page.eval_on_selector_all("input, select, textarea", CAMPOS_VACIOS_JS)
        except PlaywrightError:
            return
        datos = leer_datos_extra()
        for c in campos:
            if NO_PREGUNTAR.search(c["sel"] + " " + c["etiqueta"]):
                continue
            if c["sel"] not in datos:
                datos[c["sel"]] = {"etiqueta": c["etiqueta"], "valor": self.preguntar_dato(c)}
                DATOS_EXTRA.write_text(json.dumps(datos, ensure_ascii=False, indent=1))
            valor = datos[c["sel"]]["valor"]
            if valor and c["lista"]:
                self.page.select_option(c["sel"], label=valor)
            elif valor:
                self.page.fill(c["sel"], valor)

    def preguntar_dato(self, campo: dict) -> str:
        """Pregunta por Telegram el valor de un campo; en una lista, se responde con el número de la opción."""
        minutos = int(self.cfg.get("espera_dato_min", 4))
        opciones = campo["opciones"]
        self.foto("falta_dato")
        avisar_con_foto(f"📝 Para la cita piden «{campo['etiqueta']}» y no tengo ese dato. Respóndeme aquí con "
                        + ("el número de la opción (te mando la lista)" if opciones else "el valor")
                        + f"; «-» para dejarlo vacío. Tienes {minutos} min.")
        if opciones:
            avisar("\n".join(f"{i}. {o}" for i, o in enumerate(opciones, 1))[:4000])

        def vale(t: str) -> bool:
            return t == "-" or not opciones or (t.isdigit() and 1 <= int(t) <= len(opciones)) \
                or t.upper() in (o.upper() for o in opciones)
        respuesta = esperar_respuesta_telegram(minutos, vale)
        if respuesta is None:
            raise Inesperado(f"No me respondiste «{campo['etiqueta']}» a tiempo")
        if respuesta == "-":
            return ""
        if opciones:
            return opciones[int(respuesta) - 1] if respuesta.isdigit() \
                else next(o for o in opciones if o.upper() == respuesta.upper())
        return respuesta

    # --- 3. Reserva -------------------------------------------------------------------------
    def reservar(self) -> str:
        cfg, page = self.cfg, self.page
        page.on("dialog", lambda d: d.accept())

        if "acvalidarentrada" in page.url.lower() and self.hay("#btnEnviar"):   # "Solicitar cita"
            self.pulsar("solicitar", "#btnEnviar")
            self.foto("solicitar")
        if self.hay("select#idSede"):                                          # oficina, si la pide
            page.select_option("select#idSede", index=0)
            self.completar_faltantes()
            self.pulsar("oficina", "#btnSiguiente")
            self.foto("oficina")
        if self.hay("#txtTelefonoCitado"):                                     # contacto
            self.rellenar("#txtTelefonoCitado", cfg["telefono"])
            self.rellenar("#emailUNO", cfg["email"])
            self.rellenar("#emailDOS", cfg["email"])
            self.completar_faltantes()
            self.pulsar("contacto", "#btnSiguiente")
            self.foto("contacto")
        if NO_CITAS in self.texto():
            raise Inesperado("Las citas se agotaron mientras reservaba")

        self.paso = "horario"
        huecos = page.locator("input[type=radio][id^=cita], input[type=radio][name^=rdbCita]")
        if not huecos.count():
            raise Inesperado("No encontré horarios para elegir")
        detalle = self.detalle_hueco(huecos.first)
        huecos.first.check()
        self.pulsar("horario", "#btnSiguiente")
        self.foto("verificar")

        for casilla in ("#chkTotal", "#enviarCorreo"):
            if self.hay(casilla):
                page.check(casilla)
        if self.hay("#txtCodigoVerificacion"):
            self.paso = "sms"
            avisar("📱 Te llegó un SMS de la cita previa. Respóndeme aquí SOLO con el código "
                   f"(tienes unos minutos).\nHorario elegido: {detalle}", sonido=True)
            codigo = esperar_codigo_telegram()
            if not codigo:
                raise Inesperado("No llegó el código SMS a tiempo")
            page.fill("#txtCodigoVerificacion", codigo)
        self.completar_faltantes()
        self.confirmando = True  # desde aquí no se repite nada: podría duplicar la reserva
        self.pulsar("confirmar", "#btnConfirmar", reintentar=False)
        self.foto("confirmada")

        texto = page.inner_text("body")
        if "confirmada" not in texto.lower():
            raise Inesperado("Pulsé Confirmar pero no veo la cita confirmada")
        justificante = re.search(r"justificante[^\n]*?([A-Z0-9]{6,})", texto, re.I)
        return detalle + (f"\nJustificante: {justificante.group(1)}" if justificante else "")

    def detalle_hueco(self, radio) -> str:
        """Texto del bloque del horario (fecha, hora, oficina) para el aviso."""
        try:
            bloque = radio.locator("xpath=ancestor::*[self::div or self::tr][1]").inner_text()
            return " ".join(bloque.split())[:300]
        except Exception:  # noqa: BLE001
            return "(sin detalle)"


def error_nuevo(error: str, horas: int = 3) -> bool:
    """True si el error es distinto al último avisado o pasaron más de `horas` desde ese aviso."""
    clave = error.splitlines()[0][:200] if error else "?"
    if LAST_ERROR.exists() and LAST_ERROR.read_text() == clave \
            and time.time() - LAST_ERROR.stat().st_mtime < horas * 3600:
        return False
    LAST_ERROR.write_text(clave)
    return True


DOH = ("https://dns.google/resolve", "https://cloudflare-dns.com/dns-query")


def conecta(ip: str) -> bool:
    try:
        socket.create_connection((ip, 443), timeout=8).close()
        return True
    except OSError:
        return False


def ip_alcanzable(host: str) -> str | None:
    """IP de `host` que acepte conexión, o None si la del DNS local ya funciona.

    El DNS de la red local puede entregar una IP del sitio (p. ej. la de una red interna o de un
    proveedor) que deja de responder mientras la pública sigue arriba. Si pasa, se pregunta por DNS
    sobre HTTPS y se usa la primera IP que acepte conexión.
    """
    locales = {a[4][0] for a in socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)}
    if any(conecta(ip) for ip in locales):
        return None
    for url in DOH:
        try:
            r = requests.get(url, params={"name": host, "type": "A"},
                             headers={"accept": "application/dns-json"}, timeout=10)
            ips = [a["data"] for a in r.json().get("Answer", []) if a.get("type") == 1]
        except (requests.RequestException, ValueError):
            continue
        for ip in ips:
            if ip not in locales and conecta(ip):
                log(f"La IP del DNS local no responde; uso {ip} (DNS sobre HTTPS)")
                return ip
    raise ConnectionError(f"{host} no responde (ni por DNS local ni por DNS sobre HTTPS)")


def clave_verificada(host: str, ip: str | None = None) -> str:
    """Huella SPKI (sha256, base64) del certificado de `host`, solo si su cadena completa es válida.

    El servidor de citas no envía el certificado intermedio y el navegador a veces no lo consigue
    (ERR_CERT_AUTHORITY_INVALID). Aquí se descarga el intermedio que indica el propio certificado y se
    valida la cadena contra las raíces de confianza (certifi) y el nombre del sitio. Si algo no cuadra,
    lanza excepción y no se confía en nada.
    """
    lector = ssl.create_default_context()
    lector.check_hostname, lector.verify_mode = False, ssl.CERT_NONE  # solo para leerlo; se valida abajo
    with socket.create_connection((ip or host, 443), timeout=20) as s, \
            lector.wrap_socket(s, server_hostname=host) as t:
        hoja = x509.load_der_x509_certificate(t.getpeercert(binary_form=True))
    aia = hoja.extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_INFORMATION_ACCESS).value
    url = next(d.access_location.value for d in aia
               if d.access_method == AuthorityInformationAccessOID.CA_ISSUERS)
    crudo = requests.get(url, timeout=20).content
    intermedio = (x509.load_pem_x509_certificate(crudo) if crudo.startswith(b"-----")
                  else x509.load_der_x509_certificate(crudo))
    with open(certifi.where(), "rb") as f, warnings.catch_warnings():
        warnings.simplefilter("ignore")  # una raíz vieja de certifi tiene número de serie negativo
        raices = x509.load_pem_x509_certificates(f.read())
    PolicyBuilder().store(Store(raices)).build_server_verifier(x509.DNSName(host)) \
        .verify(hoja, [intermedio])
    spki = hoja.public_key().public_bytes(serialization.Encoding.DER,
                                          serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(hashlib.sha256(spki).digest()).decode()


def fijar_ip(ip: str | None) -> None:
    """Con client_certificates Playwright conecta desde su proceso Node, que no ve las reglas de
    Chromium: se le precarga fijar_ip.cjs. Debe llamarse antes de sync_playwright()."""
    if ip:
        os.environ["CITA_FIJAR_IP"] = f"{ICP_HOST}={ip}"
        os.environ["NODE_OPTIONS"] = f"--require {Path(__file__).with_name('fijar_ip.cjs')}"


def lanzar(p, cfg: dict, ip: str | None):
    opciones = {"locale": "es-ES", "viewport": {"width": 1280, "height": 900}}
    cert = cfg.get("certificado") or {}
    archivo = ROOT / cert.get("archivo", "data/cita/certificado.p12")
    if archivo.exists():
        clave = env("CITA_CERT_PASSWORD") or ""
        opciones["client_certificates"] = [
            {"origin": o, "pfxPath": str(archivo), "passphrase": clave} for o in cert.get("origenes", [])
        ]
    # Cadena ya validada: el navegador acepta solo esa clave aunque el servidor no envíe el intermedio
    args = [f"--ignore-certificate-errors-spki-list={clave_verificada(ICP_HOST, ip)}"]
    if ip:
        args.append(f"--host-resolver-rules=MAP {ICP_HOST} {ip}")
    fantasma = cfg.get("headless", True)
    if not fantasma and cfg.get("ventana_oculta", True):  # ventana real, fuera de vista (macOS a veces la trae)
        args.append("--window-position=-2400,-2400")
    # Fantasma: Chromium completo sin ventana (no el "headless shell") y con user-agent normal, porque el
    # cortafuegos del sitio rechaza el que dice "HeadlessChrome"
    navegador = p.chromium.launch(headless=fantasma, channel="chromium" if fantasma else None, args=args)
    if fantasma:
        opciones["user_agent"] = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                                  f"(KHTML, like Gecko) Chrome/{navegador.version} Safari/537.36")
    contexto = navegador.new_context(**opciones)
    return navegador, contexto


def revisar_latido(cfg: dict, mem: Aprendizaje) -> None:
    """Avisa si el agente estuvo mucho tiempo sin arrancar (Mac dormida, apagada o sin sesión)."""
    limite = max(30, mem.pausa_min(cfg) + 15)
    if LATIDO.exists():
        parado = (time.time() - LATIDO.stat().st_mtime) / 60
        if parado > limite:
            horas = f"{parado / 60:.1f} h" if parado >= 90 else f"{parado:.0f} min"
            avisar(f"⏸️ Cita: estuve {horas} sin revisar (¿Mac dormida, apagada o con la tapa cerrada?). "
                   "Retomo ahora.")
    LATIDO.touch()


def nueva_pagina(contexto) -> Page:
    page = contexto.new_page()
    page.on("requestfailed", lambda r: log(f"Pide certificado: {r.url.split('/')[2]}")
            if "CLIENT_AUTH_CERT" in (r.failure or "") else None)
    return page


PASOS = {  # qué hace cada paso, para que los avisos de error se entiendan sin leer el código
    "conexion": "conectar con el sitio de citas", "inicio": "abrir la página de inicio",
    "provincia": "elegir la provincia", "oficina": "elegir la oficina", "tramite": "elegir el trámite",
    "acceso_clave": "entrar a Cl@ve", "certificado": "identificarse con el certificado en Cl@ve",
    "vuelta_clave": "volver de Cl@ve al sitio de citas", "entrada": "ver si hay citas",
    "datos": "llenar tus datos",
    "solicitar": "pedir la cita", "contacto": "poner teléfono y correo",
    "horario": "elegir el horario", "sms": "esperar el código SMS", "confirmar": "confirmar la cita",
}


def explicar_error(e: Exception) -> str:
    """Traduce la excepción de un recorrido fallido a una frase clara."""
    if isinstance(e, SesionCaducada):
        return "al volver de Cl@ve, el sitio dijo que la sesión había caducado (falla del sitio, pasa a veces)"
    if isinstance(e, ErrorDelSitio):
        return f"el sitio respondió con una página de error «{e}» (falla del sitio, no tuya)"
    texto = str(e).splitlines()[0] if str(e) else type(e).__name__
    if "Timeout" in texto:
        return "la página no respondió a tiempo (el sitio estaba lento o no cargó)"
    if "No volvió de Cl@ve" in texto:
        return "Cl@ve no devolvió al sitio de citas en 2 minutos"
    return texto[:200]


def causa_error(e: Exception) -> str:
    """Causa corta del error; junto con el paso forma el tipo que se recuerda al responder en Telegram."""
    if isinstance(e, SesionCaducada):
        return "sesión caducada"
    if isinstance(e, ErrorDelSitio):
        return str(e)
    texto = str(e).splitlines()[0] if str(e) else type(e).__name__
    if "Timeout" in texto:
        return "no respondió a tiempo"
    if "No volvió de Cl@ve" in texto:
        return "Cl@ve no devolvió"
    return re.sub(r"\d+", "N", texto)[:60]  # sin números, para que el mismo error sea el mismo tipo


def motivo_legible(error: str) -> str:
    """Traduce el motivo técnico de un envío fallido (core.notify.ultimo_error) a algo claro."""
    if "Timeout" in error:
        return "Telegram tardó demasiado en responder"
    if error in ("ConnectionError", "SSLError", "ProxyError"):
        return "no había conexión con Telegram"
    if error.startswith("HTTP 429"):
        return "Telegram frenó los envíos por exceso de mensajes"
    if error.startswith("HTTP 5"):
        return "falla en los servidores de Telegram"
    if error.startswith("HTTP 4"):
        return f"Telegram rechazó el mensaje ({error})"
    return error or "motivo desconocido"


def no_enviado(que: str) -> None:
    """Anota en el log y en NO_ENVIADOS un aviso que Telegram no entregó (se te resume en el siguiente arranque)."""
    motivo = motivo_legible(notify.ultimo_error)
    log(f"No se pudo enviar por Telegram: {que} ({motivo}).")
    try:
        lista = json.loads(NO_ENVIADOS.read_text()) if NO_ENVIADOS.exists() else []
    except ValueError:
        lista = []
    lista.append({"t": f"{datetime.now():%H:%M}", "que": que, "motivo": motivo})
    NO_ENVIADOS.write_text(json.dumps(lista[-MAX_NO_ENVIADOS:], ensure_ascii=False))


def reportar_no_enviados() -> None:
    """Si hubo avisos que no llegaron, te manda un resumen; se borra solo cuando ese resumen sí llega."""
    if not NO_ENVIADOS.exists():
        return
    try:
        lista = json.loads(NO_ENVIADOS.read_text())
    except ValueError:
        lista = []
    if lista:
        lineas = "\n".join(f"• {a['t']} — {a['que']}: {a['motivo']}." for a in lista)
        texto = (f"📭 No te llegaron {len(lista)} aviso{'s' if len(lista) > 1 else ''} de Cita:\n{lineas}\n"
                 "El agente siguió revisando con normalidad.")
        if not notify.telegram(texto, "cita", silent=True):
            return  # sigue sin conexión: se intenta en el próximo arranque
        log(f"Resumen enviado por Telegram: {len(lista)} aviso(s) que no habían llegado.")
    NO_ENVIADOS.unlink(missing_ok=True)


def enviar_vista_si_cambio(estado: str, bloqueo_t: str | None = None, error_t: str | None = None,
                           causa: str | None = None) -> None:
    """Manda la última captura por Telegram solo si el resultado cambió desde la última enviada
    (sin citas → hay citas, error en un paso, bloqueo…). Se compara el resultado y no la imagen ni el texto:
    la misma página varía según el instante de la captura (el aviso "no hay citas" aparece un poco después).
    Los bloqueos se envían siempre, con botones para que digas si de verdad lo son; los errores, con botones
    para que digas si en realidad eran un bloqueo (un 500 o una página que no carga pueden serlo)."""
    if not VISTA.exists() or (not bloqueo_t and VISTA_ENVIADA.exists() and VISTA_ENVIADA.read_text() == estado):
        return
    botones, pregunta = None, ""
    if bloqueo_t:
        botones = [("✅ Sí es bloqueo", f"bloq:si:{bloqueo_t}"), ("❌ No es bloqueo", f"bloq:no:{bloqueo_t}")]
        pregunta = "\n¿Es un bloqueo? Si sí, sigo un poco más lento; si no, retomo ya. Sin tu respuesta no cambio el ritmo."
    elif error_t:
        botones = [("🚫 Fue bloqueo", f"err:si:{error_t}"), ("⚠️ Error normal", f"err:no:{error_t}")]
        pregunta = ("\n¿Fue un bloqueo encubierto? Si sí, pauso y sigo un poco más lento. Sin tu respuesta no "
                    "cambio el ritmo. Lo que respondas vale para este tipo de error: no te vuelvo a preguntar.")
    texto = (f"🖼️ {datetime.now():%H:%M} Cita: {estado.replace('_', ' ')}" + (f" ({causa})" if causa else "")
             + "." + pregunta)
    if notify.telegram_foto(VISTA, texto, "cita", botones=botones):
        VISTA_ENVIADA.write_text(estado)
        log(f"Captura enviada por Telegram (cambió a «{estado}»).")
    else:
        no_enviado(f"captura «{estado.replace('_', ' ')}»")


def leer_respuestas(mem: Aprendizaje, cfg: dict) -> None:
    """Lee los botones que pulsaste en Telegram ("¿es un bloqueo?") y aprende de la respuesta, y responde
    los comandos (/estado, /informe…). Solo atiende tu chat. Corre en cada arranque (cada minuto) que no
    coincida con una revisión en curso, así responde en uno o dos minutos."""
    token = env("TELEGRAM_BOT_TOKEN_CITA") or env("TELEGRAM_BOT_TOKEN")
    if not token:
        return
    api = f"https://api.telegram.org/bot{token}"
    try:
        params = {"timeout": 0, **({"offset": mem.d["tg_offset"]} if mem.d.get("tg_offset") else {})}
        updates = requests.get(f"{api}/getUpdates", params=params, timeout=20).json().get("result", [])
    except (requests.RequestException, ValueError):
        return
    tu_chat = str(env("TELEGRAM_CHAT_ID_CITA") or env("TELEGRAM_CHAT_ID") or "")
    menu = [{"command": l[1:].split()[0], "description": l.split(" — ")[1][:100]}
            for l in AYUDA.splitlines() if l.startswith("/") and " — " in l]
    if mem.d.get("menu") != menu:  # el menú que muestra Telegram al escribir "/", solo cuando cambia
        try:
            if requests.post(f"{api}/setMyCommands", json={"commands": menu}, timeout=20).ok:
                mem.d["menu"] = menu
                mem.guardar()
        except requests.RequestException:
            pass
    for u in updates:
        mem.d["tg_offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        if msg.get("text") and str(msg.get("chat", {}).get("id")) == tu_chat:
            if msg["text"].startswith("/"):
                log(f"Comando por Telegram: {msg['text'][:40]}")
                atender_comando(msg["text"], mem, cfg)
            elif not re.fullmatch(r"\d{4,8}", msg["text"].strip()):  # un código SMS fuera de tiempo se ignora
                notify.telegram(AYUDA, "cita", silent=True)
            continue
        q = u.get("callback_query") or {}
        if str(((q.get("message") or {}).get("chat") or {}).get("id")) != tu_chat:
            continue  # botones o mensajes de otro chat: se ignoran
        partes = q.get("data", "").split(":", 2)
        if len(partes) != 3 or partes[0] not in ("bloq", "err"):
            continue
        tipo, es_bloqueo, t = partes[0], partes[1] == "si", partes[2]
        if tipo == "bloq":
            respuesta = mem.respuesta_bloqueo(t, es_bloqueo, cfg) or "Ese bloqueo ya no está en la memoria."
            if not es_bloqueo and "falsa alarma" in respuesta:
                PAUSE.unlink(missing_ok=True)
                mem.programar(datetime.now())
        elif es_bloqueo:  # un error que en realidad era un bloqueo
            respuesta = mem.error_fue_bloqueo(t, cfg) or "Ese error ya no está en la memoria."
            if respuesta.startswith("Aprendido"):
                hasta = datetime.now() + timedelta(minutes=mem.pausa_min(cfg))
                PAUSE.write_text(hasta.isoformat(timespec="minutes"))
                mem.programar(hasta)
        else:
            respuesta = mem.error_normal(t) or "Ese error ya no está en la memoria."
        log(f"Respuesta en Telegram sobre el {'bloqueo' if tipo == 'bloq' else 'error'} de {t}: "
            f"{'sí' if es_bloqueo else 'no'} es bloqueo.")
        try:
            requests.post(f"{api}/answerCallbackQuery", json={"callback_query_id": q["id"]}, timeout=20)
            msg = q.get("message") or {}
            requests.post(f"{api}/editMessageCaption", timeout=20, json={
                "chat_id": msg.get("chat", {}).get("id"), "message_id": msg.get("message_id"),
                "caption": (msg.get("caption") or "").split("\n")[0] + f"\n→ {respuesta}"})
        except requests.RequestException:
            pass
    if updates:
        mem.guardar()


def archivar(r: dict, cfg: dict, vio_citas: bool = False) -> None:
    """Guarda en HISTORIAL lo que vio esta revisión, para poder pedirlo después por Telegram (/ver).
    Sin citas: solo la última pantalla (~200 KB). Otro resultado, o si vio citas aunque luego se agotaran:
    todos los pasos con su HTML (~3 MB).
    Borra los días con más de `historial_dias` (14 por defecto)."""
    try:
        estado = "hay_citas_" + r["r"] if vio_citas and r["r"] != "hay_citas" else r["r"]
        estado += f"_{r['paso']}" if r.get("paso") else ""
        destino = HISTORIAL / r["t"][:10] / f"{r['t'][11:].replace(':', '')}_{estado}"
        destino.mkdir(parents=True, exist_ok=True)
        if r["r"] == "sin_citas" and not vio_citas:
            shutil.copyfile(VISTA, destino / "ultima.png")
        else:
            shutil.copytree(SHOTS, destino, dirs_exist_ok=True)
        limite = f"{datetime.now() - timedelta(days=int(cfg.get('historial_dias', 14))):%Y-%m-%d}"
        for dia in HISTORIAL.iterdir():
            if dia.name < limite:
                shutil.rmtree(dia, ignore_errors=True)
    except OSError as e:  # el historial nunca debe cortar la revisión
        log(f"No pude guardar el historial: {e}")


def revisiones_guardadas(dia: str | None = None) -> list[Path]:
    """Carpetas del historial, de la más vieja a la más nueva (de un día AAAA-MM-DD, o de todos)."""
    dias = [HISTORIAL / dia] if dia else sorted(HISTORIAL.glob("*"))
    return [c for d in dias if d.is_dir() for c in sorted(d.iterdir()) if c.is_dir()]


AYUDA = """Comandos del agente de citas:
/estado — qué hizo en la última revisión y cuándo toca la próxima
/informe [horas] — resumen de las últimas horas (24 por defecto): resultados, errores por tipo, citas vistas
/aprendido — todo lo que aprendió: ritmo, piso, bloqueos, tipos de error
/captura — la última pantalla que vio
/historial [n] — las últimas revisiones que no fueron "sin citas" (10 por defecto)
/ver HH:MM [AAAA-MM-DD] — las capturas de esa revisión (hoy si no das fecha)
/log [n] — las últimas líneas del registro (20 por defecto)
/revisar — revisa en el próximo minuto, sin esperar su turno
/pausar [horas] — deja de revisar (1 h por defecto)
/seguir — vuelve a revisar (quita la pausa)"""


def atender_comando(texto: str, mem: Aprendizaje, cfg: dict) -> None:
    """Responde un comando escrito en el chat del bot. Los resultados son de solo lectura, salvo
    /revisar, /pausar y /seguir, que cambian cuándo toca la próxima revisión."""
    partes = texto.split()
    cmd, args = partes[0].split("@")[0].lower(), partes[1:]
    numero = int(args[0]) if args and args[0].isdigit() else None
    responder = lambda t: notify.telegram(t, "cita", silent=True)  # noqa: E731
    if cmd == "/estado":
        r = mem.d["revisiones"][-1] if mem.d["revisiones"] else None
        lineas = [f"Última revisión: {r['t'].replace('T', ' ')} → {r['r'].replace('_', ' ')}"
                  + (f" al {PASOS.get(r['paso'], r['paso'])}" if r.get("paso") else "")
                  + (f" ({r['causa']})" if r.get("causa") else "") if r else "Todavía no hay revisiones."]
        if PAUSE.exists() and datetime.now() < datetime.fromisoformat(PAUSE.read_text().strip()):
            lineas.append(f"⏸️ En pausa hasta {PAUSE.read_text().strip().replace('T', ' ')}")
        if LOCK.exists():
            lineas.append("🔄 Hay una revisión en curso.")
        lineas.append(f"Próxima: {(mem.d.get('proxima') or '?').replace('T', ' ')[:16]} · ritmo "
                      f"~{mem.intervalo(cfg) / 60:.1f} min")
        lineas.append(mem.informe(24).split("\n")[0])
        responder("\n".join(lineas))
    elif cmd == "/informe":
        responder(mem.informe(numero or 24))
    elif cmd == "/aprendido":
        responder(mem.resumen(cfg))
    elif cmd == "/captura":
        if not VISTA.exists() or not notify.telegram_foto(
                VISTA, f"Última pantalla ({datetime.fromtimestamp(VISTA.stat().st_mtime):%d/%m %H:%M}).",
                "cita", silent=True):
            responder("No tengo una captura para enviarte.")
    elif cmd == "/historial":
        otras = [c for c in revisiones_guardadas() if "sin_citas" not in c.name][-(numero or 10):]
        responder("Revisiones guardadas (pide una con /ver HH:MM AAAA-MM-DD):\n" + "\n".join(
            f"• {c.parent.name} {c.name[:2]}:{c.name[2:4]} — {c.name[5:].replace('_', ' ')}" for c in otras)
            if otras else "No hay revisiones distintas de \"sin citas\" en el historial.")
    elif cmd == "/ver":
        hora = (args[0] if args else "").replace(":", "")
        dia = args[1] if len(args) > 1 else f"{datetime.now():%Y-%m-%d}"
        carpeta = next((c for c in reversed(revisiones_guardadas(dia)) if c.name.startswith(hora)), None) \
            if re.fullmatch(r"\d{4}", hora) else None
        if not carpeta:
            responder("No encontré esa revisión. Usa /ver HH:MM [AAAA-MM-DD]; /historial muestra las guardadas.")
            return
        fotos = sorted(carpeta.glob("*.png"))
        if not notify.telegram_album(fotos[-10:], f"{dia} {hora[:2]}:{hora[2:]} — "
                                     f"{carpeta.name[5:].replace('_', ' ')} ({len(fotos)} pantallas"
                                     + (", te mando las 10 últimas" if len(fotos) > 10 else "") + ")", "cita"):
            responder(f"No pude enviar las capturas ({motivo_legible(notify.ultimo_error)}).")
    elif cmd == "/log":
        lineas = LOG.read_text(errors="replace").splitlines()[-min(numero or 20, 60):] if LOG.exists() else []
        responder("\n".join(l[11:] for l in lineas) or "El registro está vacío.")
    elif cmd == "/revisar":
        mem.programar(datetime.now())
        responder("Reviso en el próximo minuto." + (" Ojo: estoy en pausa; usa /seguir para quitarla."
                                                    if PAUSE.exists() else ""))
    elif cmd == "/pausar":
        hasta = datetime.now() + timedelta(hours=numero or 1)
        PAUSE.write_text(hasta.isoformat(timespec="minutes"))
        mem.programar(hasta)
        responder(f"⏸️ Pausado hasta {hasta:%d/%m %H:%M}. /seguir para retomar antes.")
    elif cmd == "/seguir":
        PAUSE.unlink(missing_ok=True)
        mem.programar(datetime.now())
        responder("▶️ Retomo: reviso en el próximo minuto.")
    else:
        responder(AYUDA)


def guardar_paginas() -> None:
    """Copia las capturas y el HTML de una corrida con citas: son las únicas páginas reales de la reserva."""
    destino = PAGINAS / f"{datetime.now():%Y%m%d-%H%M%S}"
    try:  # nunca debe tapar el resultado de la reserva
        shutil.copytree(SHOTS, destino, dirs_exist_ok=True)
        log(f"Páginas de la reserva guardadas en {destino}")
    except OSError as e:
        log(f"No pude guardar las páginas de la reserva: {e}")


def recorrido(contexto, cfg: dict, mem: Aprendizaje, solo_revisar: bool) -> None:
    """Entrada → comprobación → reserva. Si algo falla antes de Confirmar, repite el recorrido completo
    (sesión nueva) hasta `reintentos` veces; con citas a la vista reintenta sin esperar."""
    intentos = 1 + int(cfg.get("reintentos", 2))
    vio_citas, n, page = False, 0, None
    try:
        for intento in range(intentos):
            if page:
                page.close()
            contexto.clear_cookies()
            page = nueva_pagina(contexto)
            flujo = Flujo(page, cfg, mem, n)
            try:
                flujo.entrar()
                if not flujo.hay_citas():
                    log("Sin citas disponibles." + (" (se agotaron)" if vio_citas else ""))
                    mem.revision("sin_citas", cfg=cfg)
                    if mem.toca_aviso(cfg.get("aviso_sin_citas_min", 30)):
                        if not notify.telegram(f"⏳ {datetime.now():%H:%M} Sin citas de toma de huellas en "
                                               f"{cfg['provincia']}. Sigo revisando cada "
                                               f"~{mem.intervalo(cfg) / 60:.1f} min.", "cita"):
                            no_enviado("⏳ sin citas")
                    return
                if not vio_citas:
                    vio_citas = True
                    mem.revision("hay_citas", cfg=cfg)
                    mem.guardar()
                    avisar_con_foto("🚨 ¡HAY CITAS de toma de huellas en "
                                    f"{cfg['provincia']}! " + ("Entra ya a reservar." if solo_revisar
                                                               else "Intentando reservar…")
                                    + "\nEsta es la pantalla en que las vi.")
                if solo_revisar:
                    return
                detalle = flujo.reservar()
                mem.revision("reservada", cfg=cfg)
                DONE.write_text(f"{datetime.now():%Y-%m-%d %H:%M}\n{detalle}\n")
                avisar(f"✅ CITA RESERVADA\n{detalle}\nRevisa tu correo para el justificante. "
                       f"Capturas en data/cita/ultima/.", sonido=True)
                return
            except (Bloqueado, KeyboardInterrupt):
                raise
            except Exception as e:  # noqa: BLE001 - se reintenta; el último intento sube a main()
                flujo.foto("error")
                n = flujo.n
                # un tipo de error que ya salió 3 veces seguidas no se arregla reintentando: mejor pausar
                pausar = mem.error_repetido(flujo.paso, causa_error(e), cfg) and not vio_citas
                if flujo.confirmando or intento == intentos - 1 or pausar:
                    e.paso, e.causa, e.pausar, e.intentos = flujo.paso, causa_error(e), pausar, intento + 1
                    raise
                espera = 0 if vio_citas else cfg.get("espera_reintento_s", 15) * 2 ** intento
                log(f"Falló en «{flujo.paso}»: {str(e).splitlines()[0][:150]} — reintento "
                    f"{intento + 1}/{intentos - 1}" + (" ya (hay citas)." if vio_citas else f" en {espera} s."))
                page.wait_for_timeout(espera * 1000)
    finally:
        if vio_citas:
            guardar_paginas()


def main() -> None:
    parser = argparse.ArgumentParser(description="Agente de cita previa de extranjería")
    parser.add_argument("--ya", action="store_true", help="Revisa ahora, aunque no le toque")
    parser.add_argument("--solo-revisar", action="store_true", help="Comprueba citas sin reservar")
    parser.add_argument("--reiniciar", action="store_true", help="Vuelve a buscar tras una reserva")
    parser.add_argument("--aprendizaje", action="store_true", help="Muestra lo aprendido y sale")
    args = parser.parse_args()

    SHOTS.mkdir(parents=True, exist_ok=True)
    mem = Aprendizaje(MEMORIA)
    if args.aprendizaje:
        print(mem.resumen(load_config("cita")))
        return
    if args.reiniciar:
        DONE.unlink(missing_ok=True)
    if DONE.exists():
        if args.ya:
            log("Ya hay una cita reservada (data/cita/RESERVADA). Usa --reiniciar para buscar otra.")
        return
    cfg = load_config("cita")
    revisar_latido(cfg, mem)
    if not LOCK.exists() or time.time() - LOCK.stat().st_mtime >= 20 * 60:  # sin otra revisión escribiendo
        leer_respuestas(mem, cfg)
        reportar_no_enviados()
    if not args.ya and not mem.toca():  # launchd lanza cada 1 min; la memoria decide el ritmo
        return
    if PAUSE.exists() and datetime.now() < datetime.fromisoformat(PAUSE.read_text().strip()):
        log(f"En pausa hasta {PAUSE.read_text().strip()}")
        return
    if LOCK.exists() and time.time() - LOCK.stat().st_mtime < 20 * 60:
        log("Otra revisión sigue en curso; salgo.")
        return

    LOCK.touch()
    mem.programar(datetime.now() + timedelta(minutes=20))  # mientras corre, los arranques de launchd no entran
    mem.guardar()
    for f in SHOTS.glob("*.*"):
        f.unlink()
    inicio = f"{datetime.now():%Y-%m-%dT%H:%M}"
    log("Revisión" + (" (modo fantasma, sin ventana)" if cfg.get("headless", True) else " (con ventana)"))
    try:
        try:
            ip = ip_alcanzable(ICP_HOST)
        except OSError as e:  # sitio caído: se avisa como cualquier error, sin abrir el navegador
            log(str(e))
            mem.revision("error", "conexion", cfg)
            if error_nuevo(str(e)):
                avisar(f"⚠️ Cita: {e}. Sigo intentando.")
            return
        fijar_ip(ip)
        with sync_playwright() as p:
            navegador, contexto = lanzar(p, cfg, ip)
            try:
                recorrido(contexto, cfg, mem, args.solo_revisar)
            except Bloqueado as e:
                mem.revision("bloqueo", e.paso, cfg, frase=e.frase)  # pausa; el ritmo espera tu respuesta
                hasta = datetime.now() + timedelta(minutes=mem.pausa_min(cfg))
                PAUSE.write_text(hasta.isoformat(timespec="minutes"))
                mem.programar(hasta)
                avisar(f"⛔ El sitio de citas bloqueó el acceso ({e}). Pauso hasta {hasta:%H:%M}; "
                       "el ritmo lo ajusto cuando me confirmes si fue un bloqueo.")
            except Exception as e:  # noqa: BLE001 - cualquier fallo se avisa con su captura
                paso, causa = getattr(e, "paso", "?"), getattr(e, "causa", None) or causa_error(e)
                url = contexto.pages[-1].url if contexto.pages else "?"
                log(f"Error en «{paso}» ({url}): {e}")
                mem.revision("error", paso, cfg, causa=causa)
                reservando = any(r["r"] == "hay_citas" and r["t"] >= inicio for r in mem.d["revisiones"])
                conocido, seguidos = mem.tipo_conocido(paso, causa), mem.errores_seguidos()
                intentos = getattr(e, "intentos", 1 + int(cfg.get("reintentos", 2)))
                pausa = None
                if getattr(e, "pausar", False) and conocido != "bloqueo" and not reservando:
                    pausa = datetime.now() + timedelta(minutes=mem.pausa_error_min(cfg))
                    PAUSE.write_text(pausa.isoformat(timespec="minutes"))
                    mem.programar(pausa)
                    log(f"Error «{causa}» en «{paso}»: ya salió {SEGUIDOS_PAUSA} veces seguidas; pauso hasta "
                        f"{pausa:%H:%M} y después sigo al mismo ritmo.")
                if conocido == "bloqueo":  # ya dijiste que este tipo de error es un bloqueo encubierto
                    t = mem.d["revisiones"][-1]["t"]
                    log(f"Error «{causa}» en «{paso}»: lo marcaste como bloqueo. "
                        + (mem.error_fue_bloqueo(t, cfg) or ""))
                    hasta = datetime.now() + timedelta(minutes=mem.pausa_min(cfg))
                    PAUSE.write_text(hasta.isoformat(timespec="minutes"))
                    mem.programar(hasta)
                    avisar(f"⛔ {datetime.now():%H:%M} Cita: «{causa}» al {PASOS.get(paso, paso)}; me dijiste "
                           f"que eso es un bloqueo, así que pauso hasta {hasta:%H:%M} y sigo un poco más lento.")
                elif conocido == "normal" and not reservando and seguidos < 3:
                    log(f"Error «{causa}» en «{paso}»: lo marcaste como normal; no aviso.")
                elif error_nuevo(str(e)) or (conocido and seguidos == 3):
                    avisar(f"⚠️ {datetime.now():%H:%M} Cita: esta revisión no se pudo completar "
                           f"(probé {intentos} {'vez' if intentos == 1 else 'veces'}).\n"
                           f"Dónde: al {PASOS.get(paso, paso)}.\n"
                           f"Qué pasó: {explicar_error(e)}.\n"
                           + (f"Van {seguidos} revisiones seguidas con error.\n" if seguidos >= 3 else "")
                           + ("🚨 ¡Estaba reservando una cita! Entra tú ya: " + ICP if reservando
                              else f"Este error ya se repitió antes: pauso hasta {pausa:%H:%M} y sigo al mismo "
                              "ritmo." if pausa else "No tienes que hacer nada: sigo revisando como siempre."))
            finally:
                contexto.close()
                navegador.close()
                r = mem.d["revisiones"][-1] if mem.d["revisiones"] else {}
                if r.get("t", "") >= inicio:  # solo si esta corrida llegó a un resultado
                    archivar(r, cfg, any(x["r"] == "hay_citas" and x["t"] >= inicio for x in mem.d["revisiones"]))
                    conocido = r["r"] in ("error", "bloqueo") and mem.tipo_conocido(r.get("paso"), r.get("causa"))
                    if not conocido or mem.errores_seguidos() >= 3:  # error ya respondido: no se pregunta de nuevo
                        enviar_vista_si_cambio(r["r"] + (f" en «{r['paso']}»" if r.get("paso") else ""),
                                               r["t"] if r["r"] == "bloqueo" else None,
                                               r["t"] if r["r"] == "error" and not conocido else None,
                                               r.get("causa"))
    finally:
        if not PAUSE.exists() or datetime.now() >= datetime.fromisoformat(PAUSE.read_text().strip()):
            cuando, piso = mem.programar_siguiente(cfg), mem.piso(cfg)
            log(f"Próxima revisión: {cuando:%H:%M} (ritmo ~{mem.intervalo(cfg) / 60:.1f} min"
                + (f", piso aprendido {piso / 60:.1f} min" if piso else "") + ")")
        mem.guardar()
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
