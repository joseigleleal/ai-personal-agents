"""Agente de cita previa de extranjería: busca y reserva la cita de toma de huellas (TIE).

Repite el recorrido de la web de cita previa (provincia → trámite → Cl@ve con certificado) y, si hay
citas, reserva la primera disponible con los datos de config/cita.yaml. Avisa por Telegram en cada
hito: hay citas, pide código SMS, cita reservada o algo falló. Tras reservar, deja de buscar.

Uso:
    python -m agents.cita.run                 # una revisión (lo que hace launchd cada ~10 min)
    python -m agents.cita.run --ya            # sin la espera aleatoria inicial
    python -m agents.cita.run --solo-revisar  # comprueba si hay citas, pero no reserva
    python -m agents.cita.run --reiniciar     # vuelve a buscar después de una reserva
"""
import argparse
import base64
import hashlib
import random
import re
import socket
import ssl
import subprocess
import time
import warnings
from datetime import datetime, timedelta

import certifi
import requests
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import AuthorityInformationAccessOID, ExtensionOID
from cryptography.x509.verification import PolicyBuilder, Store
from playwright.sync_api import Page, sync_playwright

from core import notify
from core.config import DATA_DIR, ROOT, env, load_config

ICP_HOST = "icp.administracionelectronica.gob.es"
ICP = f"https://{ICP_HOST}/icpplus/index.html"
WORK = DATA_DIR / "cita"
SHOTS = WORK / "ultima"            # capturas de la última corrida (se sobrescriben)
DONE = WORK / "RESERVADA"          # existe = ya hay cita: no buscar más
PAUSE = WORK / "pausa_hasta"       # bloqueo del sitio: esperar antes de reintentar
LOCK = WORK / "corriendo"
LAST_ERROR = WORK / "ultimo_error"  # para no repetir el mismo aviso de error cada 10 min

NO_CITAS = "no hay citas disponibles"
BLOCKED = ("requested url was rejected", "too many requests", "demasiadas peticiones",
           "intrusion prevention")
SESION_CADUCADA = "sesión ha caducado"


class Bloqueado(Exception):
    pass


class Inesperado(Exception):
    pass


class SesionCaducada(Exception):
    """El sitio perdió la sesión al volver de Cl@ve: es pasajero, se reintenta una vez."""


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def avisar(text: str, sonido: bool = False) -> None:
    log(text.replace("\n", " | "))
    if not notify.telegram(text, "cita"):
        notify.macos("Cita extranjería", text.splitlines()[0])
    if sonido:
        subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"], check=False)


def esperar_codigo_telegram(minutos: int = 8) -> str | None:
    """Espera que respondas al bot con el código SMS (solo dígitos)."""
    token = env("TELEGRAM_BOT_TOKEN_CITA") or env("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
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
            text = (u.get("message") or {}).get("text", "").strip()
            if re.fullmatch(r"\d{4,8}", text):
                return text
    return None


class Flujo:
    def __init__(self, page: Page, cfg: dict):
        self.page, self.cfg, self.n = page, cfg, 0

    def foto(self, nombre: str) -> None:
        self.n += 1
        try:
            self.page.screenshot(path=SHOTS / f"{self.n:02d}_{nombre}.png", full_page=True)
        except Exception:  # noqa: BLE001 - la captura nunca debe cortar el flujo
            pass

    def texto(self) -> str:
        try:
            return self.page.inner_text("body").lower()
        except Exception:  # noqa: BLE001 - página cerrada o navegando
            return ""

    def revisar_bloqueo(self) -> None:
        if any(b in self.texto() for b in BLOCKED):
            raise Bloqueado(self.page.url)

    def pulsar(self, selector: str, timeout: int = 60_000) -> None:
        with self.page.expect_navigation(timeout=timeout):
            self.page.click(selector)
        self.page.wait_for_load_state("domcontentloaded")
        self.revisar_bloqueo()

    def hay(self, selector: str) -> bool:
        return self.page.locator(selector).count() > 0

    # --- 1. Provincia, trámite y entrada con Cl@ve -------------------------------------------
    def entrar(self) -> None:
        cfg = self.cfg
        self.page.goto(ICP, wait_until="domcontentloaded")
        self.revisar_bloqueo()
        self.page.select_option("select#form", label=cfg["provincia"])
        self.pulsar("#btnAceptar")
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
        self.pulsar("#btnAceptar")
        self.foto("info")

        self.pulsar("#btnAccesoClave")
        self.foto("clave")
        boton = self.page.get_by_role("button", name=re.compile("Certificado electr", re.I))
        boton.first.click()
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
        if NO_CITAS in self.texto():
            return False
        cfg = self.cfg
        copiar = self.page.get_by_role("button", name=re.compile("Copiar a datos solicitante", re.I))
        if copiar.count():  # con Cl@ve: pasa NIE y nombre del usuario a los datos del solicitante
            copiar.first.click()
        self.rellenar("#txtIdCitado", cfg.get("nie"))
        self.rellenar("#txtDesCitado", cfg.get("nombre"))
        if self.hay("select#txtPaisNac") and cfg.get("pais"):
            self.page.select_option("select#txtPaisNac", label=cfg["pais"].upper())
        if self.hay("#btnEnviar"):
            self.pulsar("#btnEnviar")
            self.foto("validar")
        return NO_CITAS not in self.texto()

    def rellenar(self, selector: str, valor: str | None) -> None:
        campo = self.page.locator(selector)
        if valor and campo.count() and campo.is_editable() and not campo.input_value():
            campo.fill(valor)

    # --- 3. Reserva -------------------------------------------------------------------------
    def reservar(self) -> str:
        cfg, page = self.cfg, self.page
        page.on("dialog", lambda d: d.accept())

        if "acvalidarentrada" in page.url.lower() and self.hay("#btnEnviar"):   # "Solicitar cita"
            self.pulsar("#btnEnviar")
            self.foto("solicitar")
        if self.hay("select#idSede"):                                          # oficina, si la pide
            page.select_option("select#idSede", index=0)
            self.pulsar("#btnSiguiente")
            self.foto("oficina")
        if self.hay("#txtTelefonoCitado"):                                     # contacto
            self.rellenar("#txtTelefonoCitado", cfg["telefono"])
            self.rellenar("#emailUNO", cfg["email"])
            self.rellenar("#emailDOS", cfg["email"])
            self.pulsar("#btnSiguiente")
            self.foto("contacto")
        if NO_CITAS in self.texto():
            raise Inesperado("Las citas se agotaron mientras reservaba")

        huecos = page.locator("input[type=radio][id^=cita], input[type=radio][name^=rdbCita]")
        if not huecos.count():
            raise Inesperado("No encontré horarios para elegir")
        detalle = self.detalle_hueco(huecos.first)
        huecos.first.check()
        self.pulsar("#btnSiguiente")
        self.foto("verificar")

        for casilla in ("#chkTotal", "#enviarCorreo"):
            if self.hay(casilla):
                page.check(casilla)
        if self.hay("#txtCodigoVerificacion"):
            avisar("📱 Te llegó un SMS de la cita previa. Respóndeme aquí SOLO con el código "
                   f"(tienes unos minutos).\nHorario elegido: {detalle}", sonido=True)
            codigo = esperar_codigo_telegram()
            if not codigo:
                raise Inesperado("No llegó el código SMS a tiempo")
            page.fill("#txtCodigoVerificacion", codigo)
        self.pulsar("#btnConfirmar")
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


def clave_verificada(host: str) -> str:
    """Huella SPKI (sha256, base64) del certificado de `host`, solo si su cadena completa es válida.

    El servidor de citas no envía el certificado intermedio y el navegador a veces no lo consigue
    (ERR_CERT_AUTHORITY_INVALID). Aquí se descarga el intermedio que indica el propio certificado y se
    valida la cadena contra las raíces de confianza (certifi) y el nombre del sitio. Si algo no cuadra,
    lanza excepción y no se confía en nada.
    """
    lector = ssl.create_default_context()
    lector.check_hostname, lector.verify_mode = False, ssl.CERT_NONE  # solo para leerlo; se valida abajo
    with socket.create_connection((host, 443), timeout=20) as s, \
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


def lanzar(p, cfg: dict):
    opciones = {"locale": "es-ES", "viewport": {"width": 1280, "height": 900}}
    cert = cfg.get("certificado") or {}
    archivo = ROOT / cert.get("archivo", "data/cita/certificado.p12")
    if archivo.exists():
        clave = env("CITA_CERT_PASSWORD") or ""
        opciones["client_certificates"] = [
            {"origin": o, "pfxPath": str(archivo), "passphrase": clave} for o in cert.get("origenes", [])
        ]
    # Cadena ya validada: el navegador acepta solo esa clave aunque el servidor no envíe el intermedio
    args = [f"--ignore-certificate-errors-spki-list={clave_verificada(ICP_HOST)}"]
    if cfg.get("ventana_oculta", True):  # el modo headless lo bloquea un cortafuegos: ventana real, fuera de vista
        args.append("--window-position=-2400,-2400")
    navegador = p.chromium.launch(headless=cfg.get("headless", False), args=args)
    contexto = navegador.new_context(**opciones)
    return navegador, contexto


def main() -> None:
    parser = argparse.ArgumentParser(description="Agente de cita previa de extranjería")
    parser.add_argument("--ya", action="store_true", help="Sin espera aleatoria inicial")
    parser.add_argument("--solo-revisar", action="store_true", help="Comprueba citas sin reservar")
    parser.add_argument("--reiniciar", action="store_true", help="Vuelve a buscar tras una reserva")
    args = parser.parse_args()

    SHOTS.mkdir(parents=True, exist_ok=True)
    if args.reiniciar:
        DONE.unlink(missing_ok=True)
    if DONE.exists():
        log("Ya hay una cita reservada (data/cita/RESERVADA). Usa --reiniciar para buscar otra.")
        return
    if PAUSE.exists() and datetime.now() < datetime.fromisoformat(PAUSE.read_text().strip()):
        log(f"En pausa por bloqueo del sitio hasta {PAUSE.read_text().strip()}")
        return
    if LOCK.exists() and time.time() - LOCK.stat().st_mtime < 20 * 60:
        log("Otra revisión sigue en curso; salgo.")
        return

    cfg = load_config("cita")
    if not args.ya:  # launchd corre cada 8 min; con esto queda entre 8 y 12 (≈10 ± 2)
        espera = random.uniform(0, 240)
        log(f"Revisión programada: empieza en {espera:.0f} s")
        time.sleep(espera)

    LOCK.touch()
    for f in SHOTS.glob("*.png"):
        f.unlink()
    try:
        with sync_playwright() as p:
            navegador, contexto = lanzar(p, cfg)
            page = contexto.new_page()
            page.on("requestfailed", lambda r: log(f"Pide certificado: {r.url.split('/')[2]}")
                    if "CLIENT_AUTH_CERT" in (r.failure or "") else None)
            flujo = Flujo(page, cfg)
            try:
                try:
                    flujo.entrar()
                except SesionCaducada:
                    log("Sesión caducada al volver de Cl@ve; reintento una vez.")
                    flujo.foto("caducada")
                    flujo.entrar()
                if not flujo.hay_citas():
                    log("Sin citas disponibles.")
                    notify.telegram(f"⏳ {datetime.now():%H:%M} Sin citas de toma de huellas en "
                                    f"{cfg['provincia']}. Sigo revisando.", "cita")
                    return
                avisar("🚨 ¡HAY CITAS de toma de huellas en "
                       f"{cfg['provincia']}! " + ("Entra ya a reservar." if args.solo_revisar
                                                  else "Intentando reservar…"), sonido=True)
                if args.solo_revisar:
                    return
                detalle = flujo.reservar()
                DONE.write_text(f"{datetime.now():%Y-%m-%d %H:%M}\n{detalle}\n")
                avisar(f"✅ CITA RESERVADA\n{detalle}\nRevisa tu correo para el justificante. "
                       f"Capturas en data/cita/ultima/.", sonido=True)
            except Bloqueado as e:
                hasta = datetime.now() + timedelta(minutes=cfg.get("pausa_bloqueo_min", 15))
                PAUSE.write_text(hasta.isoformat(timespec="minutes"))
                flujo.foto("bloqueo")
                avisar(f"⛔ El sitio de citas bloqueó el acceso ({e}). Pauso hasta {hasta:%H:%M}.")
            except Exception as e:  # noqa: BLE001 - cualquier fallo se avisa con su captura
                flujo.foto("error")
                log(f"Error en {page.url}: {e}")
                if NO_CITAS not in flujo.texto() and error_nuevo(str(e)):
                    avisar(f"⚠️ Cita: algo falló en {page.url}\n{str(e).splitlines()[0][:300]}\n"
                           "Si estaba reservando, entra tú ya. Capturas en data/cita/ultima/.")
            finally:
                contexto.close()
                navegador.close()
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
