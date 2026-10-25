"""Pruebas del agente `empleo`: reglas del permiso y grafo completo con un modelo falso (sin red ni claves).

    .venv/bin/python -m unittest tests.test_empleo -v
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langgraph.checkpoint.sqlite import SqliteSaver

from agents.empleo import graph, report, scorer, sombra
from agents.freelance.sources import Offer
from core import db

PREFS = {
    "ciudades_presencial": ["Madrid"], "remoto": True, "preferencia": "remoto",
    "jornadas": ["parcial", "completa"], "permiso_trabajo": {"horas_max_semana": 30}, "practicas": True,
    "palabras_clave": ["python", "data"], "excluir": ["comercial"], "umbral_aviso": 7,
    "umbral_excede": 9, "max_avisos_excede": 1, "excluir_direccion": True, "priorizar_permiso": True,
}
PROFILE = {"nombre": "Test", "titulo": "Dev", "idiomas": ["es"], "habilidades": ["python"]}
BASE = {"role_is_technical": True, "skill_match": 7, "work_mode": "remote", "schedule": "full_time",
        "weekly_hours": None, "contract_type": "employee", "hours_flexible": False, "reason": "ok"}


def answer(**kw) -> str:
    return json.dumps({**BASE, **kw})


class PermitRules(unittest.TestCase):
    def fit(self, **kw):
        return scorer.permit_fit({**BASE, **kw}, PREFS)

    def test_grupos(self):
        self.assertEqual(self.fit(schedule="part_time"), "cumple")
        self.assertEqual(self.fit(weekly_hours=25), "cumple")
        self.assertEqual(self.fit(is_internship=True), "cumple")
        self.assertEqual(self.fit(contract_type="contractor"), "proyecto")
        self.assertEqual(self.fit(hours_flexible=True), "negociable")
        self.assertEqual(self.fit(schedule="unknown"), "sin_dato")
        self.assertEqual(self.fit(), "excede")
        self.assertEqual(self.fit(weekly_hours=40, contract_type="contractor"), "proyecto")

    def test_sin_limite_de_horas_todo_cumple(self):
        self.assertEqual(scorer.permit_fit(BASE, {**PREFS, "permiso_trabajo": {}}), "cumple")

    def test_puntaje_premia_lo_que_cumple(self):
        cumple, _, _ = scorer.compute_score({**BASE, "schedule": "part_time"}, PREFS)
        excede, notes, permit = scorer.compute_score(BASE, PREFS)
        self.assertEqual((cumple, excede, permit), (9, 7, "excede"))  # 7 +1 remoto ±1 permiso
        self.assertTrue(any("excede tu permiso" in n for n in notes))

    def test_direccion_y_otro_pais(self):
        self.assertLessEqual(scorer.compute_score(BASE, PREFS, title="Engineering Manager @ X")[0], 4)
        self.assertGreater(scorer.compute_score(BASE, PREFS, title="AI Platform Engineer @ X")[0], 4)
        self.assertLessEqual(scorer.compute_score({**BASE, "remote_only_other_country": True}, PREFS)[0], 2)

    def test_alerta_por_correo_no_inventa_jornada(self):
        _, _, permit = scorer.compute_score({**BASE, "schedule": "part_time"}, PREFS, low_info=True)
        self.assertEqual(permit, "sin_dato")

    def test_prefiltro_pais_en_el_titulo(self):
        o = lambda t: Offer("jobicy", "1", t, "", "python")  # noqa: E731
        self.assertFalse(scorer.prefilter(o("Data Engineer | Sweden | Remote"), PREFS))
        self.assertTrue(scorer.prefilter(o("Data Engineer | Spain | Remote"), PREFS))
        self.assertTrue(scorer.prefilter(o("Data Engineer (EMEA, UK or Spain)"), PREFS))


OFFERS = [
    Offer("jobicy", "1", "Python dev part-time", "u1", "python"),
    Offer("jobicy", "2", "Senior python full-time", "u2", "python"),
    Offer("jobicy", "3", "Python contractor", "u3", "python"),
    Offer("jobicy", "4", "Vendedor comercial", "u4", "python comercial"),   # lo saca el pre-filtro
    Offer("jobicy", "5", "Data engineer full-time", "u5", "data"),
]
ANSWERS = [answer(schedule="part_time", skill_match=8), answer(skill_match=9), answer(contract_type="contractor"),
           answer(skill_match=8)]


class Grafo(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        self.sent = []
        patches = [
            mock.patch.object(graph.sources, "collect", return_value=(list(OFFERS), {})),
            mock.patch.object(report, "REPORTS_DIR", self.tmp),
            mock.patch.object(report, "send_alerts", side_effect=lambda e: self.sent.extend(e)),
            mock.patch.object(graph, "send_alerts", side_effect=lambda e: self.sent.extend(e)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_graph(self, scorer_answers, writer_answers=("nota",), max_puntuar=60, saver=None, resume=False):
        models = iter([FakeListChatModel(responses=list(scorer_answers)), FakeListChatModel(responses=list(writer_answers))])
        with mock.patch.object(graph.llm_lc, "chat_model", side_effect=lambda *a, **k: next(models)):
            app = graph.build(PREFS, PROFILE, self.conn).compile(checkpointer=saver)
            config = {"configurable": {"thread_id": "t"}, "recursion_limit": 100}
            state = None if resume else {"max_puntuar": max_puntuar, "pendientes": []}
            return app, app.invoke(state, config, **({"durability": "sync"} if saver else {}))

    def test_corrida_completa(self):
        _, out = self.run_graph(ANSWERS)
        self.assertEqual(out["puntuadas"], 4)
        self.assertEqual([e["permit"] for e in out["elegidas"]], ["cumple", "proyecto", "excede", "excede"])
        # 🟡: solo las de puntaje ≥ 9 y máximo 1 → se avisa la de 9 (9 −1 permiso +1 remoto = 9) y no la de 8
        self.assertEqual([e["title"] for e in self.sent], ["Python dev part-time", "Python contractor", "Senior python full-time"])
        self.assertIsNone(self.sent[0].get("nota"))
        self.assertEqual(self.sent[1]["nota"], "nota")  # la que no cumple trae nota para postular
        status = dict(self.conn.execute("SELECT id, status FROM offers").fetchall())
        self.assertEqual(status["jobicy:4"], "descartada")
        self.assertIn("Python contractor", next(self.tmp.glob("empleo-*.md")).read_text())

    def test_tope_deja_pendientes_para_la_proxima(self):
        _, out = self.run_graph(ANSWERS, max_puntuar=2)
        self.assertEqual((out["puntuadas"], len(out["pendientes"])), (2, 2))

    def test_modelo_caido_no_pierde_ofertas(self):
        bad = FakeListChatModel(responses=["no es json"])
        models = iter([bad, bad])
        with mock.patch.object(graph.llm_lc, "chat_model", side_effect=lambda *a, **k: next(models)):
            out = graph.build(PREFS, PROFILE, self.conn).compile().invoke({"max_puntuar": 60, "pendientes": []})
        self.assertEqual(len(out["pendientes"]), 4)
        self.assertIn("sin_modelo", out)

    def test_retoma_tras_un_corte(self):
        real = scorer.result
        calls = {"n": 0}

        def corte(*a, **k):  # se "apaga la Mac" al puntuar la tercera oferta
            calls["n"] += 1
            if calls["n"] == 3:
                raise KeyboardInterrupt
            return real(*a, **k)

        with SqliteSaver.from_conn_string(str(self.tmp / "cp.db")) as saver:
            with mock.patch.object(graph.scorer, "result", side_effect=corte), self.assertRaises(KeyboardInterrupt):
                self.run_graph(ANSWERS, saver=saver)
            app = graph.build(PREFS, PROFILE, self.conn).compile(checkpointer=saver)
            snap = app.get_state({"configurable": {"thread_id": "t"}})
            self.assertEqual(snap.next, ("puntuar",))  # quedó guardado justo antes de puntuar
            self.assertEqual(len(snap.values["pendientes"]), 4)
            _, out = self.run_graph(ANSWERS, saver=saver, resume=True)
            self.assertEqual(graph.sources.collect.call_count, 1)  # no vuelve a consultar las fuentes
            self.assertEqual((out["puntuadas"], out["pendientes"]), (4, []))
            self.assertEqual(len(self.sent), 3)

    def run_sombra(self, local, groq, manda="sombra"):
        models = iter([FakeListChatModel(responses=local), FakeListChatModel(responses=groq), FakeListChatModel(responses=["nota"])])
        etiquetas = self.tmp / "etiquetas.jsonl"
        with mock.patch.object(graph.llm_lc, "chat_model", side_effect=lambda *a, **k: next(models)), \
                mock.patch.dict("os.environ", {"EMPLEO_SOMBRA": "groq", "EMPLEO_SOMBRA_MANDA": manda}), \
                mock.patch.object(sombra, "ETIQUETAS", etiquetas), \
                mock.patch.object(sombra.notify, "telegram", return_value=True) as tg:
            out = graph.build(PREFS, PROFILE, self.conn).compile().invoke({"max_puntuar": 60, "pendientes": []})
        filas = {f["offer_id"]: f for f in self.conn.execute("SELECT * FROM sombra")}
        return out, filas, etiquetas, tg

    def test_sombra_manda_en_desacuerdo_y_realimenta(self):
        # Local: 8 al part-time (avisa) y 2 a los demás; sombra: 2 al part-time y 9 al full-time
        local = [answer(schedule="part_time", skill_match=8), answer(skill_match=2), answer(contract_type="contractor", skill_match=2),
                 answer(skill_match=2)]
        extra = {c: None for c in sombra.CAMPOS if c not in BASE}  # respuesta completa: sirve para reentrenar
        groq = [answer(schedule="part_time", skill_match=1, **extra), answer(skill_match=9, **extra),
                answer(contract_type="contractor", skill_match=2, **extra), answer(skill_match=2)]
        out, filas, etiquetas, tg = self.run_sombra(local, groq)
        self.assertEqual(out["puntuadas"], 4)
        self.assertEqual(filas["jobicy:1"]["manda"], "sombra")       # el local avisaba y Groq no: manda Groq
        self.assertEqual(filas["jobicy:2"]["manda"], "sombra")       # Groq avisaba y el local no
        self.assertEqual(filas["jobicy:3"]["manda"], "principal")    # coinciden
        self.assertEqual([e["title"] for e in out["elegidas"]], ["Senior python full-time"])
        self.assertEqual(len(etiquetas.read_text().strip().split("\n")), 3)  # la 4.ª respuesta de Groq vino incompleta
        informe = tg.call_args[0][0]
        self.assertIn("comparadas con Groq: 4", informe)
        self.assertIn("solo Groq: 1", informe)

    def test_sin_sombra_queda_pendiente_o_decide_el_local(self):
        local = [answer(schedule="part_time", skill_match=8)] * 4
        out, filas, _, _ = self.run_sombra(local, ["no es json"])
        self.assertEqual((out["puntuadas"], len(out["pendientes"])), (0, 4))  # mientras manda Groq, sin Groq no se decide
        self.assertIn("sin_modelo", out)
        self.conn.execute("DELETE FROM offers")
        out, filas, _, _ = self.run_sombra(local, ["no es json"], manda="principal")
        self.assertEqual(out["puntuadas"], 4)
        self.assertIsNone(filas["jobicy:1"]["puntaje_s"])           # decidió el local, sin comparar

    def tearDown(self):
        self.conn.close()


if __name__ == "__main__":
    unittest.main()
