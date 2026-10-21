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

    def test_disco_lleno(self):
        uso = mock.Mock(total=100, used=90)
        with mock.patch.object(run.shutil, "disk_usage", return_value=uso):
            problemas, _ = run.revisar_sistema({"disco_max_pct": 85})
        self.assertIn("sistema:disco", problemas)


if __name__ == "__main__":
    unittest.main()
