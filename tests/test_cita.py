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
        cuando = self.mem.programar_siguiente({})
        self.assertLessEqual(cuando, datetime.now() + timedelta(minutes=4, seconds=1))
        self.assertFalse(self.mem.toca())

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
        for nombre, valor in (("SHOTS", self.tmp / "ultima"), ("DONE", self.tmp / "RESERVADA"),
                              ("PAGINAS", self.tmp / "paginas")):
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


if __name__ == "__main__":
    unittest.main()
