"""Pruebas de eco sin modelo: turnos (mensajes seguidos = una respuesta), perfil de estilo y búsqueda de ejemplos."""
import unittest

import numpy as np

from agents.eco import datos, estilo, run

PARES = [
    {"chat": "Ana", "fecha": "2026-10-01T10:00", "contexto": "¿vienes hoy?", "respuesta": "Sii"},
    {"chat": "Ana", "fecha": "2026-10-01T10:01", "contexto": "", "respuesta": "jajaja a las 8"},
    {"chat": "Ana", "fecha": "2026-10-01T12:00", "contexto": "", "respuesta": "Ya salí"},      # pausa larga: otro turno
    {"chat": "Ana", "fecha": "2026-10-01T12:05", "contexto": "", "respuesta": "<enlace>"},    # solo un enlace: fuera
    {"chat": "Luis", "fecha": "2026-10-01T12:06", "contexto": "", "respuesta": "Bueno 😂"},  # otro chat: otro turno
]


class Eco(unittest.TestCase):
    def test_turnos_juntan_mensajes_seguidos(self):
        turnos = datos.turnos(PARES)
        self.assertEqual([t["mensajes"] for t in turnos], [["Sii", "jajaja a las 8"], ["Ya salí"], ["Bueno 😂"]])
        self.assertEqual(turnos[0]["contexto"], "¿vienes hoy?")

    def test_perfil(self):
        p = estilo.perfil(datos.turnos(PARES))
        self.assertEqual(p["mensajes"], 4)
        self.assertEqual(p["risa"], ["jajaja"])
        self.assertEqual(p["emojis"], ["😂"])
        self.assertEqual(p["termina_punto"], 0)
        self.assertIn("jajaja", estilo.describir(p))

    def test_parecidos_prefiere_el_mismo_chat_y_excluye(self):
        turnos = [{"chat": "Ana"}, {"chat": "Luis"}, {"chat": "Luis"}]
        vectores = np.array([[1, 0], [0.98, 0.2], [0, 1]], dtype=np.float32)
        consulta = np.array([1, 0], dtype=np.float32)
        self.assertIs(run.parecidos(consulta, turnos, vectores)[0], turnos[0])
        self.assertIs(run.parecidos(consulta, turnos, vectores, chat="luis")[0], turnos[1])
        self.assertIs(run.parecidos(consulta, turnos, vectores, excluir=0)[0], turnos[1])


if __name__ == "__main__":
    unittest.main()
