# -*- coding: utf-8 -*-
"""Pruebas de la lógica (sin red ni claves):  python -m unittest pruebas.test_logica -v"""
import io, math, struct, sys, unittest, wave
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import logica as L


def wav(segundos, amplitud, tasa=16000, ruido_hasta=0.0):
    n = int(segundos * tasa)
    muestras = [int(amplitud * 32767 * math.sin(2 * math.pi * 220 * i / tasa)) if i / tasa >= ruido_hasta else 0 for i in range(n)]
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(tasa)
        w.writeframes(struct.pack("<%dh" % n, *muestras))
    return b.getvalue()


class Privacidad(unittest.TestCase):
    def test_dni_y_telefono(self):
        t, dni, tel = L.enmascarar_pii("Mi DNI es 71234567 y llamo al 987 654 321 o al +51 987654321.")
        self.assertEqual((dni, tel), (1, 2))
        self.assertNotIn("71234567", t); self.assertNotIn("987", t)

    def test_no_toca_otros_numeros(self):
        t, dni, tel = L.enmascarar_pii("En 2024 la ley 29733 tiene 12 artículos y 1234567 no es DNI")
        self.assertEqual((dni, tel), (0, 0)); self.assertIn("29733", t)

    def test_stream_no_deja_pasar_numeros_partidos(self):
        trozos = ["Su DNI es 71", "234", "567 y fin", ". Teléfono 98", "7 654 3", "21."]
        salida = "".join(L.filtrar_pii_stream(iter(trozos)))
        self.assertNotIn("71234567", salida); self.assertNotIn("654 321", salida)
        self.assertIn("[DNI_OCULTO]", salida); self.assertIn("[TELÉFONO_OCULTO]", salida)


class VozTexto(unittest.TestCase):
    def test_limpia_markdown_y_citas(self):
        t = L.texto_para_voz("## Título\n- **Reamer** dice esto [Pág. 12].\n1. Segundo punto (et al. 2020)\n😀 Visita https://x.org")
        self.assertNotIn("#", t); self.assertNotIn("*", t); self.assertNotIn("Pág", t); self.assertNotIn("http", t); self.assertNotIn("😀", t)
        self.assertIn("y colaboradores", t)

    def test_pronunciacion_y_ocultos(self):
        t = L.texto_para_voz("Las TIC y la IA con [DNI_OCULTO]")
        self.assertIn("tics", t); self.assertIn("inteligencia artificial", t); self.assertIn("dato oculto", t)

    def test_recorta_en_frase_completa(self):
        largo = "Esta es una frase de prueba. " * 100
        t = L.texto_para_voz(largo, 200)
        self.assertLessEqual(len(t), 200); self.assertTrue(t.endswith("."))


class Audio(unittest.TestCase):
    def test_audio_corto(self):
        m = L.analizar_audio(wav(0.3, 0.5)); self.assertLess(m["dur"], L.AUDIO_MIN_S)

    def test_silencio_sin_voz(self):
        m = L.analizar_audio(wav(2.0, 0.0)); self.assertAlmostEqual(m["voz_s"], 0.0)

    def test_con_voz(self):
        m = L.analizar_audio(wav(2.0, 0.3)); self.assertGreater(m["voz_s"], 1.5)

    def test_procesar_audio_rechaza_silencio_sin_llamar_a_gemini(self):
        r = L.procesar_audio(wav(2.0, 0.0), "clave-falsa", True)
        self.assertEqual(r["estado"], "repetir")
        r = L.procesar_audio(wav(0.2, 0.5), "clave-falsa", True)
        self.assertEqual(r["estado"], "repetir")

    def test_evaluar_transcripcion(self):
        ok = {"se_entiende": True, "confianza": "alta", "texto": "¿Qué son los protocolos TIC?"}
        self.assertEqual(L.evaluar_transcripcion(ok, 3.0)[0], "ok")
        self.assertEqual(L.evaluar_transcripcion({**ok, "confianza": "media"}, 3.0)[0], "dudosa")
        self.assertEqual(L.evaluar_transcripcion({**ok, "confianza": "baja"}, 3.0)[0], "repetir")
        self.assertEqual(L.evaluar_transcripcion({**ok, "texto": "eh"}, 3.0)[0], "repetir")
        self.assertEqual(L.evaluar_transcripcion({**ok, "texto": "x" * 400}, 2.0)[0], "repetir")


class Documento(unittest.TestCase):
    def test_sin_pdf(self):
        d = L.obtener_documento()
        self.assertIn("ok", d); self.assertIn("mensaje", d)



# ───────────── Respaldo de modelos y de voz, con servicios falsos ─────────────
from types import SimpleNamespace as NS


class _Tipos:
    ThinkingConfig = staticmethod(lambda **kw: NS(**kw))
    GenerateContentConfig = staticmethod(lambda **kw: NS(**kw))


class _Modelos:
    def __init__(self, guion): self.guion, self.llamadas = guion, []
    def generate_content_stream(self, model, contents, config):
        self.llamadas.append((model, getattr(config, "thinking_config", None)))
        accion = self.guion.get(model, ["ok"]).pop(0) if self.guion.get(model) else "ok"
        if isinstance(accion, Exception):
            raise accion
        if accion == "corta":
            def g():
                yield NS(text="Hola ")
                raise RuntimeError("500 se cortó la conexión")
            return g()
        if accion == "vacio":
            return iter([NS(text="")])
        return iter([NS(text="Hola "), NS(text="mundo")])


def _preparar(guion):
    L.types = _Tipos
    L.PAUSA_REINTENTO_S = 0
    L.MODELO = "m1"; L.MODELOS_RESPALDO = ("m2", "m3")
    L.NIVEL_PENSAR = "low"
    L._estado_modelo.update(nombre=None, t=0.0, pensar=True)
    modelos = _Modelos(guion)
    L._cliente = lambda k: NS(models=modelos)
    return modelos


class Gemini(unittest.TestCase):
    def pedir(self):
        info = {}
        return "".join(L.flujo_gemini("k", [], "sis", info)), info

    def test_modelo_retirado_pasa_al_respaldo_y_se_recuerda(self):
        m = _preparar({"m1": [RuntimeError("404 NOT_FOUND model")]})
        texto, info = self.pedir()
        self.assertEqual((texto, info["modelo"]), ("Hola mundo", "m2"))
        self.pedir()  # la segunda vez va directo al que funciona
        self.assertEqual([c[0] for c in m.llamadas], ["m1", "m2", "m2"])

    def test_saturado_reintenta_y_luego_respalda(self):
        m = _preparar({"m1": [RuntimeError("503 UNAVAILABLE"), RuntimeError("503 UNAVAILABLE")]})
        texto, info = self.pedir()
        self.assertEqual(info["modelo"], "m2"); self.assertEqual([c[0] for c in m.llamadas], ["m1", "m1", "m2"])

    def test_error_de_thinking_se_desactiva_y_reintenta(self):
        m = _preparar({"m1": [RuntimeError("400 thinking_level is not supported by this model")]})
        L.MODELO = "gemini-3-flash"; L.MODELOS_RESPALDO = ()
        m.guion = {"gemini-3-flash": [RuntimeError("400 thinking_level is not supported by this model")]}
        texto, info = self.pedir()
        self.assertEqual(texto, "Hola mundo")
        self.assertIsNotNone(m.llamadas[0][1]); self.assertIsNone(m.llamadas[1][1])  # 1.º con thinking, 2.º sin él

    def test_corte_a_mitad_se_propaga_sin_duplicar_texto(self):
        _preparar({"m1": ["corta"]})
        with self.assertRaises(RuntimeError):
            self.pedir()

    def test_respuesta_vacia(self):
        _preparar({"m1": ["vacio"]})
        with self.assertRaisesRegex(RuntimeError, "vacía"):
            self.pedir()

    def test_clave_invalida_no_reintenta(self):
        m = _preparar({"m1": [RuntimeError("403 PERMISSION_DENIED api key")]})
        with self.assertRaises(RuntimeError):
            self.pedir()
        self.assertEqual(len(m.llamadas), 1)
        self.assertIn("clave", L.mensaje_error(RuntimeError("403 api key")))

    def test_thinking_por_familia(self):
        L.types = _Tipos; L.NIVEL_PENSAR = "minimal"; L._estado_modelo["pensar"] = True
        self.assertEqual(L._pensar("gemini-3.5-flash").thinking_level, "minimal")
        self.assertEqual(L._pensar("gemini-3.1-pro-preview").thinking_level, "low")   # Pro no admite «minimal»
        self.assertEqual(L._pensar("gemini-2.5-flash").thinking_budget, 0)
        self.assertIsNone(L._pensar("gemini-2.5-pro"))


class _Comunicar:
    modo = "falla"
    def __init__(self, texto, voz, rate=None): pass
    async def stream(self):
        if _Comunicar.modo == "falla":
            raise RuntimeError("403 Invalid response status")
        yield {"type": "audio", "data": b"AUDIO-EDGE"}


class VozRespaldo(unittest.TestCase):
    def setUp(self):
        L.edge_tts = NS(Communicate=_Comunicar)
        L._cache_tts.clear(); L._edge_estado.update(fallos=0, hasta=0.0)
        self.gtts_llamadas = 0
        def fake_gtts(texto, lenta): self.gtts_llamadas += 1; return b"AUDIO-GTTS"
        L._gtts = fake_gtts; L.gTTS = object()

    def test_edge_funciona(self):
        _Comunicar.modo = "ok"
        self.assertEqual(L.sintetizar("Hola.", "es-PE-AlexNeural", 1.0), (b"AUDIO-EDGE", "edge"))

    def test_edge_cae_y_usa_gtts_sin_cachear_el_fallo(self):
        _Comunicar.modo = "falla"
        self.assertEqual(L.sintetizar("Hola uno.", "v", 1.0)[1], "gtts")
        _Comunicar.modo = "ok"   # edge se recupera: la siguiente frase distinta ya sale por edge si no está en pausa
        L._edge_estado.update(fallos=0, hasta=0.0)
        self.assertEqual(L.sintetizar("Hola dos.", "v", 1.0)[1], "edge")

    def test_pausa_de_edge_tras_fallos_repetidos(self):
        _Comunicar.modo = "falla"
        L.sintetizar("a.", "v", 1.0); L.sintetizar("b.", "v", 1.0)
        self.assertGreater(L._edge_estado["hasta"], 0)       # a partir de aquí ni se intenta edge
        _Comunicar.modo = "ok"
        self.assertEqual(L.sintetizar("c.", "v", 1.0)[1], "gtts")

    def test_cache(self):
        _Comunicar.modo = "ok"
        L.sintetizar("Repetida.", "v", 1.0); _Comunicar.modo = "falla"
        self.assertEqual(L.sintetizar("Repetida.", "v", 1.0)[1], "edge")   # sale de caché, no vuelve a llamar

    def test_sin_ningun_motor(self):
        _Comunicar.modo = "falla"; L.gTTS = None
        with self.assertRaises(RuntimeError):
            L.sintetizar("Nada.", "v", 1.0)


if __name__ == "__main__":
    unittest.main()
