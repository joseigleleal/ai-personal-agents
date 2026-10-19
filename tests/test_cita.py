"""Pruebas del agente `cita`: memoria y ensayo de la reserva contra un sitio simulado (sin red ni certificado).

    .venv/bin/python -m unittest tests.test_cita -v

El sitio simulado imita las pantallas posteriores a Cl@ve con los ids de campos conocidos del sitio real.
Cuando el agente vea citas de verdad, guarda el HTML real en data/cita/paginas/: conviene comparar esas
páginas con PAGINAS de aquí y ajustar lo que difiera.
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs

from playwright.sync_api import sync_playwright

from agents.cita import run
from agents.cita.aprendizaje import BASE_MS, Aprendizaje

SITIO = f"https://{run.ICP_HOST}/icpplus/"


def form(accion: str, cuerpo: str, boton: str = "btnSiguiente") -> str:
    return (f"<html><body><form method='post' action='{accion}'>{cuerpo}"
            f"<input type='submit' id='{boton}' value='Siguiente'></form></body></html>")


PAGINAS = {
    "acEntrada": form("acValidarEntrada", """
        <input type='button' value='Copiar a datos solicitante'
               onclick="document.getElementById('txtIdCitado').value='Y0000000T';
                        document.getElementById('txtDesCitado').value='PERSONA DE PRUEBA'">
        <input id='txtIdCitado' name='txtIdCitado'><input id='txtDesCitado' name='txtDesCitado'>
        <select id='txtPaisNac' name='txtPaisNac'><option>ARGENTINA</option><option>PERU</option></select>""",
                      "btnEnviar"),
    "acValidarEntrada": form("acCitar", "<p>Solicitar cita</p>", "btnEnviar"),
    "acCitar": form("acVerFormulario", "<select id='idSede' name='idSede'><option value='1'>Oficina 1</option></select>"),
    "acVerFormulario": form("acOfertarCita", """
        <input id='txtTelefonoCitado' name='txtTelefonoCitado'>
        <input id='emailUNO' name='emailUNO'><input id='emailDOS' name='emailDOS'>"""),
    "acOfertarCita": form("acVerificarCita", """
        <div><input type='radio' id='cita1' name='rdbCita' value='1'> CITA 1 Día: 20/11/2026 Hora: 09:30</div>
        <div><input type='radio' id='cita2' name='rdbCita' value='2'> CITA 2 Día: 21/11/2026 Hora: 10:00</div>"""),
    "acVerificarCita": form("acGrabarCita", """
        <input type='checkbox' id='chkTotal' name='chkTotal'>
        <input type='checkbox' id='enviarCorreo' name='enviarCorreo'>
        <input id='txtCodigoVerificacion' name='txtCodigoVerificacion'>""", "btnConfirmar"),
    "acGrabarCita": "<html><body><p>Su cita ha sido CONFIRMADA.</p><p>Número de justificante: ABC123456</p></body></html>",
    # Botón que aparece tarde y botón de Cl@ve sin el id conocido
    "lento": "<html><body><form method='post' action='acCitar'></form><script>setTimeout(() => "
             "document.forms[0].insertAdjacentHTML('beforeend', \"<input type=submit id=btnSiguiente>\"), 1200)"
             "</script></body></html>",
    "clave": form("acCitar", "", "otroId").replace("value='Siguiente'", "value='Acceso Cl@ve'"),
}

CFG = {"provincia": "Prueba", "pais": "Peru", "telefono": "600000000", "email": "prueba@example.com",
       "reintentos": 2, "espera_reintento_s": 0}


class Memoria(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mem = Aprendizaje(self.tmp / "a.json")

    def test_espera_crece_con_fallos_y_baja_con_exitos(self):
        self.mem.fallo("clave", "Timeout 30000ms exceeded", por_tiempo=True)
        self.assertEqual(self.mem.timeout("clave"), BASE_MS * 1.5)
        for _ in range(10):
            self.mem.exito("clave")
        self.assertEqual(self.mem.timeout("clave"), BASE_MS)

    def test_recuerda_selector_bueno(self):
        self.mem.exito("clave", "#b")
        self.assertEqual(self.mem.orden("clave", ("#a", "#b")), ["#b", "#a"])

    def test_horas_intensas_y_ritmo(self):
        hora = datetime.now().hour
        self.mem.d["revisiones"].append({"t": f"{datetime.now():%Y-%m-%dT%H:%M}", "r": "hay_citas"})
        self.assertEqual(self.mem.horas_intensas(), {hora, (hora - 1) % 24})
        cuando = self.mem.programar_siguiente({})  # hora intensa: la mitad del ritmo inicial (10 min), ±20 %
        self.assertLessEqual(cuando, datetime.now() + timedelta(minutes=6, seconds=1))
        self.assertFalse(self.mem.toca())

    def test_ritmo_baja_con_exitos_y_solo_sube_si_confirmas(self):
        for _ in range(30):
            self.mem.revision("sin_citas")
        self.assertEqual(self.mem.intervalo(), 60)  # 600 s - 30 × 30 s, con tope en el mínimo
        self.mem.revision("error", "inicio")
        self.mem.revision("bloqueo")
        self.assertEqual((self.mem.intervalo(), self.mem.piso(), self.mem.tope_por_hora()), (60, 0, None))
        self.mem.respuesta_bloqueo(self.mem.d["ritmo"]["bloqueos"][-1]["t"], es_bloqueo=True)
        self.assertEqual(self.mem.intervalo(), 75)  # ×1,25 del ritmo al que bloqueó
        self.assertEqual(self.mem.piso(), 75)  # bloqueó a 60 s: no vuelve a bajar de 75 s en 7 días
        for _ in range(10):
            self.mem.revision("sin_citas")
        self.assertEqual(self.mem.intervalo(), 75)

    def test_pausa_se_duplica_si_bloquea_al_volver(self):
        cfg = {"pausa_bloqueo_min": 15}
        self.mem.revision("bloqueo", cfg=cfg)
        self.assertEqual(self.mem.pausa_min(cfg), 15)
        self.mem.revision("bloqueo", cfg=cfg)
        self.assertEqual(self.mem.pausa_min(cfg), 30)
        self.mem.revision("sin_citas", cfg=cfg)
        self.assertEqual(self.mem.pausa_min(cfg), 15)

    def test_tope_por_hora_y_horas_con_bloqueos(self):
        for _ in range(9):
            self.mem.revision("sin_citas")
        self.mem.revision("bloqueo", "acceso_clave", frase="too many requests")
        self.assertIsNone(self.mem.tope_por_hora())  # sin confirmar no cuenta
        self.mem.d["ritmo"]["bloqueos"][-1]["confirmado"] = True
        self.assertEqual(self.mem.tope_por_hora(), 9)  # bloqueó con 10 en la hora: máximo 9
        self.assertEqual(self.mem.d["ritmo"]["bloqueos"][-1]["paso"], "acceso_clave")
        self.assertEqual(self.mem.horas_bloqueo(), set())
        self.mem.revision("bloqueo", frase="too many requests")
        self.mem.d["ritmo"]["bloqueos"][-1]["confirmado"] = True
        self.assertEqual(self.mem.horas_bloqueo(), {datetime.now().hour})
        cuando = self.mem.programar_siguiente({})
        self.assertGreaterEqual(cuando, datetime.now() + timedelta(seconds=3600 // 9 - 1))

    def test_pausa_por_tipo_de_bloqueo(self):
        cfg = {"pausa_bloqueo_min": 15}
        for _ in range(3):  # se repite al volver: 15 → 30 → 60
            self.mem.revision("bloqueo", cfg=cfg, frase="intrusion prevention")
        self.mem.revision("sin_citas", cfg=cfg)  # con 60 funcionó
        self.assertEqual(self.mem.d["ritmo"]["pausa_por_frase"]["intrusion prevention"], 60)
        self.mem.revision("bloqueo", cfg=cfg, frase="intrusion prevention")
        self.assertEqual(self.mem.pausa_min(cfg), 45)  # parte de lo que funcionó, un 25 % menos
        self.mem.revision("sin_citas", cfg=cfg)
        self.mem.revision("bloqueo", cfg=cfg, frase="too many requests")
        self.assertEqual(self.mem.pausa_min(cfg), 15)  # otro tipo: desde la base

    def test_respuesta_no_es_bloqueo_levanta_la_pausa(self):
        self.mem.d["ritmo"]["intervalo_s"] = 240
        self.mem.revision("bloqueo", "acceso_clave", frase="too many requests")
        t = self.mem.d["ritmo"]["bloqueos"][-1]["t"]
        self.assertEqual((self.mem.intervalo(), self.mem.piso()), (240, 0))  # sin confirmar: no cambia
        self.assertIn("falsa alarma", self.mem.respuesta_bloqueo(t, es_bloqueo=False))
        self.assertEqual((self.mem.intervalo(), self.mem.piso(), self.mem.tope_por_hora()), (240, 0, None))
        self.assertTrue(self.mem.es_falsa_alarma("too many requests", "acceso_clave"))
        self.assertIsNone(self.mem.respuesta_bloqueo(t, es_bloqueo=True))  # ya no está

    def test_respuesta_si_es_bloqueo_lo_confirma(self):
        self.mem.d["ritmo"]["intervalo_s"] = 360
        self.mem.revision("bloqueo", "acceso_clave", frase="too many requests")
        t = self.mem.d["ritmo"]["bloqueos"][-1]["t"]
        self.assertIn("7.5 min", self.mem.respuesta_bloqueo(t, es_bloqueo=True))
        self.assertTrue(self.mem.d["ritmo"]["bloqueos"][-1]["confirmado"])
        self.assertEqual(self.mem.respuesta_bloqueo(t, es_bloqueo=True), "Ya estaba confirmado.")
        self.assertFalse(self.mem.es_falsa_alarma("too many requests", "acceso_clave"))

    def test_error_que_era_bloqueo(self):
        cfg = {"pausa_bloqueo_min": 5}
        self.mem.d["ritmo"]["intervalo_s"] = 240
        self.mem.revision("error", "provincia", cfg)  # un error no cambia el ritmo
        self.assertEqual(self.mem.intervalo(cfg), 240)
        t = self.mem.d["revisiones"][-1]["t"]
        self.assertIn("Aprendido como bloqueo", self.mem.error_fue_bloqueo(t, cfg))
        self.assertEqual((self.mem.intervalo(cfg), self.mem.piso(), self.mem.pausa_min(cfg)), (300, 300, 5))
        self.assertEqual(self.mem.d["revisiones"][-1]["r"], "bloqueo")
        self.assertIsNone(self.mem.error_fue_bloqueo(t, cfg))  # ya no es un error

    def test_bloqueo_viejo_caduca(self):
        self.mem.d["ritmo"]["bloqueos"] = [{"t": "2020-01-01T10:00", "cada_s": 600}]
        self.assertEqual(self.mem.piso(), 0)

    def test_aviso_sin_citas_limitado(self):
        self.assertTrue(self.mem.toca_aviso(30))
        self.assertFalse(self.mem.toca_aviso(30))
        self.assertTrue(self.mem.toca_aviso(0))

    def test_persiste(self):
        self.mem.revision("error", "clave")
        self.mem.guardar()
        self.assertEqual(json.loads((self.tmp / "a.json").read_text())["revisiones"][0]["paso"], "clave")


class SitioSimulado(unittest.TestCase):
    """Corre el agente contra páginas locales: Chromium sin ventana y peticiones interceptadas."""

    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.navegador = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.navegador.close()
        cls.pw.stop()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.enviados = {}
        for nombre, valor in (("SHOTS", self.tmp / "ultima"), ("VISTA", self.tmp / "ultima.png"),
                              ("VISTA_ENVIADA", self.tmp / "ultima_enviada"),
                              ("DONE", self.tmp / "RESERVADA"), ("PAGINAS", self.tmp / "paginas"),
                              ("BLOQUEOS", self.tmp / "bloqueos"), ("NO_ENVIADOS", self.tmp / "no_enviados.json")):
            p = mock.patch.object(run, nombre, valor)
            p.start()
            self.addCleanup(p.stop)
        (self.tmp / "ultima").mkdir()
        for nombre, valor in (("avisar", mock.Mock()), ("esperar_codigo_telegram", mock.Mock(return_value="123456"))):
            p = mock.patch.object(run, nombre, valor)
            setattr(self, nombre, p.start())
            self.addCleanup(p.stop)
        p = mock.patch.object(run.notify, "telegram")
        p.start()
        self.addCleanup(p.stop)
        self.mem = Aprendizaje(self.tmp / "a.json")
        self.contexto = self.navegador.new_context()
        self.contexto.route(SITIO + "**", self.servir)
        self.addCleanup(self.contexto.close)

    def servir(self, route):
        pagina = route.request.url.split("/")[-1].split("?")[0]
        if route.request.post_data:
            self.enviados[pagina] = parse_qs(route.request.post_data)
        route.fulfill(content_type="text/html; charset=utf-8", body=PAGINAS.get(pagina, "no existe"))

    def test_reserva_completa_tras_un_fallo_de_entrada(self):
        llamadas = []

        def entrar(flujo):  # sustituye provincia + trámite + Cl@ve (necesitan el sitio real)
            llamadas.append(1)
            if len(llamadas) == 1:
                raise run.SesionCaducada("sesión caducada de prueba")
            flujo.page.goto(SITIO + "acEntrada")

        with mock.patch.object(run.Flujo, "entrar", entrar):
            run.recorrido(self.contexto, CFG, self.mem, solo_revisar=False)

        self.assertEqual(len(llamadas), 2)
        self.assertIn("ABC123456", run.DONE.read_text())
        self.assertIn("20/11/2026", run.DONE.read_text())
        self.assertEqual(self.enviados["acValidarEntrada"]["txtPaisNac"], ["PERU"])
        self.assertEqual(self.enviados["acOfertarCita"]["txtTelefonoCitado"], ["600000000"])
        self.assertEqual(self.enviados["acOfertarCita"]["emailDOS"], ["prueba@example.com"])
        self.assertEqual(self.enviados["acVerificarCita"]["rdbCita"], ["1"])
        self.assertEqual(self.enviados["acGrabarCita"]["txtCodigoVerificacion"], ["123456"])
        self.assertEqual([r["r"] for r in self.mem.d["revisiones"]], ["hay_citas", "reservada"])
        self.assertTrue(any(run.PAGINAS.glob("*/*.html")), "debe guardar el HTML de la reserva")

    def test_boton_lento_se_reintenta_con_mas_tiempo(self):
        self.mem.d["pasos"]["oficina"] = {"ok": 0, "fallos": 0, "timeout_ms": 1000}
        page = self.contexto.new_page()
        page.goto(SITIO + "lento")
        run.Flujo(page, CFG, self.mem).pulsar("oficina", "#btnSiguiente")
        self.assertTrue(page.url.endswith("acCitar"))
        self.assertEqual((self.mem.d["pasos"]["oficina"]["fallos"], self.mem.d["pasos"]["oficina"]["ok"]), (1, 1))

    def test_boton_alternativo_y_lo_recuerda(self):
        page = self.contexto.new_page()
        page.goto(SITIO + "clave")
        run.Flujo(page, CFG, self.mem).pulsar("acceso_clave", "#btnAccesoClave", "input[value*='Cl@ve' i]",
                                              "button:has-text('Cl@ve')")
        self.assertTrue(page.url.endswith("acCitar"))
        self.assertEqual(self.mem.d["selectores"]["acceso_clave"], "input[value*='Cl@ve' i]")

    def test_no_repite_tras_confirmar(self):
        def entrar(flujo):
            flujo.page.goto(SITIO + "acEntrada")

        PAGINAS_SIN_CONFIRMAR = dict(PAGINAS, acGrabarCita="<html><body>Error del servidor</body></html>")
        with mock.patch.object(run.Flujo, "entrar", entrar), mock.patch.dict(PAGINAS, PAGINAS_SIN_CONFIRMAR):
            with self.assertRaises(run.Inesperado) as e:
                run.recorrido(self.contexto, CFG, self.mem, solo_revisar=False)
        self.assertEqual(e.exception.paso, "confirmar")
        self.assertFalse(run.DONE.exists())
        self.esperar_codigo_telegram.assert_called_once()  # un solo intento de reserva

    def test_bloqueo_guarda_su_captura_y_respeta_falsa_alarma(self):
        page = self.contexto.new_page()
        with mock.patch.dict(PAGINAS, {"rechazo": "<html><body>Too Many Requests</body></html>"}):
            page.goto(SITIO + "rechazo")
        flujo = run.Flujo(page, CFG, self.mem)
        flujo.paso = "acceso_clave"
        with self.assertRaises(run.Bloqueado) as e:
            flujo.revisar_bloqueo()
        self.assertEqual((e.exception.frase, e.exception.paso), ("too many requests", "acceso_clave"))
        self.assertEqual(len(list(run.BLOQUEOS.glob("*_bloqueo_acceso_clave.png"))), 1)
        self.mem.d["ritmo"]["falsas_alarmas"] = [{"frase": "too many requests", "paso": "acceso_clave"}]
        flujo.revisar_bloqueo()  # ya no detiene

    def test_bloqueo_se_envia_con_botones(self):
        run.VISTA.write_bytes(b"png")
        with mock.patch.object(run.notify, "telegram_foto", return_value=True) as foto:
            run.enviar_vista_si_cambio("bloqueo en «acceso_clave»", "2026-10-19T12:23")
            run.enviar_vista_si_cambio("bloqueo en «acceso_clave»", "2026-10-19T12:40")  # cada bloqueo se pregunta
        self.assertEqual(foto.call_count, 2)
        self.assertEqual(foto.call_args.kwargs["botones"][1], ("❌ No es bloqueo", "bloq:no:2026-10-19T12:40"))
        with mock.patch.object(run.notify, "telegram_foto", return_value=True) as foto:
            run.enviar_vista_si_cambio("error en «provincia»", error_t="2026-10-19T12:42")
        self.assertEqual(foto.call_args.kwargs["botones"][0], ("🚫 Fue bloqueo", "err:si:2026-10-19T12:42"))

    def test_captura_solo_si_cambia(self):
        run.VISTA.write_bytes(b"png")
        with mock.patch.object(run.notify, "telegram_foto", return_value=True) as foto:
            for estado in ("sin_citas", "sin_citas", "error en «clave»", "sin_citas"):
                run.enviar_vista_si_cambio(estado)
        self.assertEqual(foto.call_count, 3)

    def test_errores_se_explican_claro(self):
        self.assertIn("sesión había caducado", run.explicar_error(run.SesionCaducada(SITIO + "infogenerica")))
        self.assertIn("no respondió a tiempo", run.explicar_error(Exception("Locator.click: Timeout 30000ms exceeded.")))
        self.assertEqual(run.PASOS["vuelta_clave"], "volver de Cl@ve al sitio de citas")

    def test_avisos_no_enviados_se_resumen_despues(self):
        run.VISTA.write_bytes(b"png")
        run.notify.ultimo_error = "ReadTimeout"
        with mock.patch.object(run.notify, "telegram_foto", return_value=False):
            run.enviar_vista_si_cambio("error en «tramite»", error_t="2026-10-19T19:02")
        run.notify.ultimo_error = "HTTP 502"
        run.no_enviado("⏳ sin citas")
        with mock.patch.object(run.notify, "telegram", return_value=False) as tg:
            run.reportar_no_enviados()  # sigue sin conexión: se guarda para la próxima
        self.assertTrue(run.NO_ENVIADOS.exists())
        with mock.patch.object(run.notify, "telegram", return_value=True) as tg:
            run.reportar_no_enviados()
        texto = tg.call_args.args[0]
        self.assertIn("No te llegaron 2 avisos", texto)
        self.assertIn("captura «error en «tramite»»: Telegram tardó demasiado en responder", texto)
        self.assertIn("⏳ sin citas: falla en los servidores de Telegram", texto)
        self.assertFalse(run.NO_ENVIADOS.exists())

if __name__ == "__main__":
    unittest.main()
