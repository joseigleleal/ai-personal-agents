"""Pruebas del vigía: reglas de auditoría y avisos una sola vez por problema (sin red ni systemd).

    .venv/bin/python -m unittest tests.test_vigia -v
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from agents.vigia import run

AHORA = datetime(2026, 10, 21, 18, 0)


def hace(minutos: int) -> str:
    return f"{AHORA - timedelta(minutes=minutos):%Y-%m-%dT%H:%M}"


class Vigia(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "data/cita").mkdir(parents=True)
        p = mock.patch.object(run, "ROOT", self.tmp)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(run.shutil, "which", return_value=None)  # sin systemd, como en la Mac
        p.start()
        self.addCleanup(p.stop)
        self.cfg = {"tipo": "cita", "memoria": "data/cita/aprendizaje.json"}

    def memoria(self, revisiones, proxima):
        (self.tmp / "data/cita/aprendizaje.json").write_text(json.dumps({"revisiones": revisiones,
                                                                          "proxima": proxima}))

    def test_cita_al_dia_no_es_problema(self):
        self.memoria([{"t": hace(m), "r": "sin_citas"} for m in (20, 15, 10, 5)], hace(-2))
        problemas, lineas = run.revisar_cita(self.cfg, AHORA)
        self.assertEqual(problemas, {})
        self.assertIn("ciclo ~5.0 min", lineas[0])

    def test_cita_quieta_errores_y_pausa(self):
        self.memoria([{"t": hace(m), "r": "error"} for m in (50, 45, 40, 35)], hace(30))
        problemas, _ = run.revisar_cita(self.cfg, AHORA)
        self.assertIn("cita:quieto", problemas)    # tenía que revisar hace 30 min
        self.assertIn("cita:errores", problemas)   # 100 % de errores en la hora
        (self.tmp / "data/cita/pausa_hasta").write_text((AHORA + timedelta(hours=3)).isoformat())
        problemas, lineas = run.revisar_cita(self.cfg, AHORA)
        self.assertNotIn("cita:quieto", problemas)  # en pausa no se le exige revisar
        self.assertIn("cita:pausa", problemas)      # pero una pausa de 3 h sí se avisa
        (self.tmp / "data/cita/RESERVADA").write_text("x")
        self.assertEqual(run.revisar_cita(self.cfg, AHORA)[0], {})

    def test_log_viejo_y_frases_de_error(self):
        log = self.tmp / "data/empleo.log"
        log.write_text("ok\n" + "  ⚠️ No se pudo puntuar: 429\n" * 3)
        problemas, _ = run.revisar_log("empleo", {"log": "data/empleo.log", "frases_error": ["No se pudo puntuar"]},
                                       datetime.now() + timedelta(hours=4))
        self.assertEqual(set(problemas), {"empleo:quieto", "empleo:fallos"})

    def test_avisa_una_vez_y_avisa_resuelto(self):
        estado = {}
        with mock.patch.object(run.notify, "telegram", return_value=True) as tg:
            self.assertEqual(run.avisar_cambios({"cita:quieto": "x"}, estado, AHORA), ["cita:quieto"])
            self.assertEqual(run.avisar_cambios({"cita:quieto": "x"}, estado, AHORA), [])  # no repite
            self.assertEqual(run.avisar_cambios({}, estado, AHORA), ["cita:quieto"])        # resuelto
        self.assertEqual(tg.call_count, 2)
        self.assertFalse(tg.call_args_list[0].kwargs.get("silent", False))  # el problema suena
        self.assertTrue(tg.call_args_list[1].kwargs["silent"])              # el "resuelto", no
        self.assertEqual(estado["abiertos"], {})

    def test_leve_no_avisa_y_va_al_parte(self):
        estado = {}
        with mock.patch.object(run.notify, "telegram", return_value=True) as tg:
            for problemas in ({"cita:errores": "60 %"}, {"cita:errores": "70 %"}, {}, {"cita:errores": "55 %"}):
                self.assertEqual(run.avisar_cambios(problemas, estado, AHORA), [])
        tg.assert_not_called()                                   # ni al aparecer ni al resolverse
        self.assertEqual(estado["leves"]["cita:errores"]["veces"], 2)  # dos apariciones, no cuatro vueltas
        self.assertIn("cita:errores: 2 veces desde el 21/10 18:00 (último: 55 %)", run.texto_leves(estado["leves"]))
        self.assertEqual(run.texto_leves({}), "")
        self.assertTrue(run.es_grave("sistema:ram", run.GRAVES))
        self.assertTrue(run.es_grave("empleo:fallo", run.GRAVES))
        self.assertFalse(run.es_grave("empleo:quieto", run.GRAVES))

    def test_disco_lleno_y_cpu_saturada(self):
        uso = mock.Mock(total=100 * 1024 ** 3, used=90 * 1024 ** 3)
        with mock.patch.object(run.shutil, "disk_usage", return_value=uso), \
                mock.patch.object(run, "uso_cpu", return_value=97), \
                mock.patch.object(run, "procesos_top", return_value=[("ollama", 180.0, 1.3)]), \
                mock.patch.object(run.os, "cpu_count", return_value=2), \
                mock.patch.object(run.os, "getloadavg", return_value=(4.0, 3.8, 3.5)):
            problemas, lineas, medidas = run.revisar_sistema({"disco_max_pct": 85})
        self.assertEqual({"sistema:disco", "sistema:cpu"}, {k for k in problemas if k != "sistema:ram"})
        self.assertIn("CPU 97 %", lineas[0])
        self.assertIn("disco 90 de 100 GB (90 %)", lineas)
        self.assertIn("más CPU: ollama", lineas[-1])
        with mock.patch.object(run.os, "getloadavg", return_value=(4.0, 1.0, 0.5)), \
                mock.patch.object(run, "uso_cpu", return_value=99):
            self.assertNotIn("sistema:cpu", run.revisar_sistema({})[0])  # un pico de 1 min no es problema

    def test_pico_avisa_una_vez_edita_y_cierra(self):
        cfg = {"picos": {"acciones": {"cita": {"texto": "Pausar cita", "tipo": "pausar_cita", "minutos": 30}}}}
        pico = {"cpu": 98, "carga1": 2.4, "nucleos": 2, "ram_libre_pct": 50, "top": [("Ollama", 180.0, 4.8)]}
        normal = {**pico, "cpu": 20, "carga1": 0.3}
        estado = {}
        with mock.patch.object(run.notify, "telegram_api", return_value={"message_id": 9}) as api:
            self.assertEqual(run.seguir_pico(pico, estado, cfg, AHORA), "pico nuevo")
            self.assertIsNone(run.seguir_pico(pico, estado, cfg, AHORA))       # sigue: solo edita
            self.assertIsNone(run.seguir_pico(normal, estado, cfg, AHORA))     # 1 vuelta normal: espera otra
            self.assertEqual(run.seguir_pico(normal, estado, cfg, AHORA), "pico terminado")
        metodos = [c.args[0] for c in api.call_args_list]
        self.assertEqual(metodos, ["sendMessage", "editMessageText", "editMessageText"])
        primer = api.call_args_list[0].args[1]
        self.assertIn("Ollama: 180 % CPU", primer["text"])
        self.assertEqual(primer["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "vig:cita")
        self.assertNotIn("pico", estado)

    def test_un_instante_de_cpu_no_es_pico(self):
        self.assertFalse(run.es_pico({"cpu": 99, "carga1": 0.4, "nucleos": 2, "ram_libre_pct": 60}, {}))
        self.assertTrue(run.es_pico({"cpu": 30, "carga1": 0.4, "nucleos": 2, "ram_libre_pct": 8}, {}))

    def test_botones_solo_ejecutan_la_lista_permitida(self):
        cfg = {"picos": {"acciones": {"cita": {"texto": "Pausar", "tipo": "pausar_cita", "minutos": 30}}}}
        updates = [
            {"update_id": 5, "callback_query": {"id": "a", "data": "vig:cita",
                                                 "message": {"chat": {"id": 42}, "message_id": 9, "text": "🔥"}}},
            {"update_id": 6, "callback_query": {"id": "b", "data": "vig:rm -rf /",
                                                 "message": {"chat": {"id": 42}, "message_id": 9, "text": "🔥"}}},
            {"update_id": 7, "callback_query": {"id": "c", "data": "vig:cita",
                                                 "message": {"chat": {"id": 666}, "message_id": 1, "text": "x"}}},
        ]
        estado = {}

        def api(metodo, datos=None, agent=None):
            return updates if metodo == "getUpdates" else {}
        with mock.patch.object(run.notify, "telegram_api", side_effect=api), \
                mock.patch.object(run.notify, "chat_propio", return_value="42"):
            hechas = run.atender_botones(estado, cfg)
        self.assertEqual(len(hechas), 2)                    # el de otro chat se ignora
        self.assertIn("Cita en pausa", hechas[0])
        self.assertIn("no está en la lista permitida", hechas[1])
        self.assertTrue((self.tmp / "data/cita/pausa_hasta").exists())
        self.assertEqual(estado["tg_offset"], 8)


if __name__ == "__main__":
    unittest.main()


class ServicioContinuo(unittest.TestCase):
    def revisar(self, estado):
        with mock.patch.object(run.shutil, "which", return_value="/bin/systemctl"), \
             mock.patch.object(run, "systemd", return_value=estado):
            return run.revisar_servicio("etiquetado", {"servicio": "lora-etiquetar", "continuo": True})

    def test_activo_sigue_con_el_log(self):
        self.assertEqual(self.revisar("active"), ({}, [], True))

    def test_inactivo_se_avisa(self):
        problemas, _, activo = self.revisar("inactive")
        self.assertIn("etiquetado:detenido", problemas)
        self.assertFalse(activo)

