# -*- coding: utf-8 -*-
"""
Asistente de Protocolos TIC · Trabajo Social · UNA Puno — LÓGICA (sin servidor web)

Este módulo no depende de ningún framework web: lo usa servidor.py y se puede probar por separado.
Aquí vive lo que antes estaba en app2_avatar.py, sin Streamlit:
  · filtro de privacidad (DNI y teléfonos)        · lectura del PDF base
  · chat con Gemini en streaming                  · micrófono: voz → texto sin inventar la pregunta
  · texto → voz (edge-tts, gTTS de respaldo)

Lo técnico se cambia SOLO en la sección CONFIGURACIÓN. Nada de esto aparece en la interfaz.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import sys
import threading
import time
import wave
from array import array
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path

try:
    from pypdf import PdfReader
except Exception:  # la app avisa en pantalla
    PdfReader = None

try:
    from google import genai
    from google.genai import types

    GENAI_OK = True
except Exception:  # SDK no instalado: la app avisa en pantalla
    GENAI_OK = False

try:
    import edge_tts
except Exception:
    edge_tts = None
try:
    from gtts import gTTS
except Exception:
    gTTS = None
VOZ_OK = edge_tts is not None or gTTS is not None

log = logging.getLogger("tic_trabajo_social")
BASE_DIR = Path(__file__).resolve().parent

# ═══════════════════════════ CONFIGURACIÓN (BACKEND) ═══════════════════════════

# ── Servicio de IA (Gemini) ──
# La clave NO va en el código: variable de entorno GEMINI_API_KEY, archivo .env o .streamlit/secrets.toml
MODELO = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
# Se prueban en orden si el modelo elegido está saturado (503/429) o ya no existe (404).
MODELOS_RESPALDO = ("gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite")
REINTENTOS_POR_MODELO = 2
PAUSA_REINTENTO_S = 1.2
RECORDAR_MODELO_S = 600  # el modelo que funcionó pasa primero durante 10 min (evita esperar a los que fallan)
TIMEOUT_GEMINI_MS = 60_000
# Los modelos Gemini 3 «piensan» antes de responder (por defecto en nivel alto): eso retrasa la 1.ª palabra.
# Valores: minimal | low | medium | high | vacío (= no tocar). Para un chat con voz, «low» es lo adecuado.
NIVEL_PENSAR = os.environ.get("GEMINI_THINKING", "low").strip().lower()
HISTORIAL_MENSAJES = 6  # mensajes previos que se reenvían al modelo como contexto
MAX_CONSULTA = 1500  # caracteres máximos por pregunta escrita

# ── Documento base ──
PDF_NOMBRE = "HERRAMIENTAS_TIC_TRABAJO_SOCIAL.pdf"
MAX_CONTEXTO = 120_000  # caracteres del PDF enviados al modelo

# ── Privacidad ──
FILTRO_PII_INICIAL = True

# ── Voz de salida (texto → voz) ──
VOZ_INICIAL = True  # lectura automática activada al abrir la app
VOCES = {  # nombre en pantalla → voz neuronal de Microsoft Edge (edge-tts)
    "Alex · hombre (Perú)": "es-PE-AlexNeural",
    "Camila · mujer (Perú)": "es-PE-CamilaNeural",
    "Jorge · hombre (México)": "es-MX-JorgeNeural",
    "Dalia · mujer (México)": "es-MX-DaliaNeural",
    "Álvaro · hombre (España)": "es-ES-AlvaroNeural",
    "Elvira · mujer (España)": "es-ES-ElviraNeural",
}
VOZ_POR_DEFECTO = "Alex · hombre (Perú)"
VOZ_VELOCIDADES = {"Lenta": 0.85, "Normal": 1.0, "Rápida": 1.25}
VOZ_VELOCIDAD_INICIAL = "Normal"
GTTS_TLD = "com.mx"  # acento del respaldo gTTS (com.mx = latinoamericano, es = España)
MAX_CHARS_VOZ = 3500  # una respuesta más larga se lee solo hasta aquí (lo aplica la interfaz)
MAX_CHARS_TROZO = 900  # máximo de caracteres por petición de voz
TTS_TIMEOUT_S = 12  # edge-tts: si tarda más, se pasa al respaldo (antes eran 45 s por intento)
TTS_CONCURRENCIA = 3  # peticiones de voz simultáneas a Microsoft/Google
TTS_PAUSA_TRAS_FALLOS_S = 120  # tras 2 fallos seguidos de edge-tts se usa gTTS directamente durante este tiempo
TTS_CACHE_MAX = 200
# Cómo debe sonar lo que la voz suele leer mal (expresión regular → texto leído).
PRONUNCIACION = {
    r"\bet al\.": "y colaboradores",
    r"\bN\.\s?[°º]": "número",
    r"\bNASW\b": "N.A.S.W.",
    r"\bTICs?\b": "tics",
    r"\bIA\b": "inteligencia artificial",
    r"\bDNI\b": "D N I",
}

# ── Micrófono (voz → texto) ──
AUDIO_MIN_S = 0.8  # menos que esto: demasiado corto para ser una pregunta
AUDIO_MAX_S = 120.0
AUDIO_VOZ_MIN_S = 0.5  # segundos mínimos con señal de voz; menos = silencio o solo ruido
AUDIO_UMBRAL_RMS = 0.008  # volumen mínimo (0–1) de un tramo de 50 ms para contarlo como «con voz»
CONFIRMAR_SI_DUDA = True  # confianza «media» → pedir confirmación; False = responder directamente

# ── Textos institucionales ──
FACULTAD = "Facultad de Trabajo Social - UNA Puno"
# ═════════════════════════════════════════════════════════════════════════════════


def cargar_entorno() -> None:
    """Lee .env (y, por compatibilidad, .streamlit/secrets.toml) sin pisar variables ya definidas."""
    env = BASE_DIR / ".env"
    if env.is_file():
        for linea in env.read_text(encoding="utf-8-sig").splitlines():
            linea = linea.strip()
            if not linea or linea.startswith("#") or "=" not in linea:
                continue
            k, v = linea.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    if not os.environ.get("GEMINI_API_KEY"):
        secretos = BASE_DIR / ".streamlit" / "secrets.toml"
        try:
            import tomllib

            if secretos.is_file():
                valor = tomllib.loads(secretos.read_text(encoding="utf-8-sig")).get("GEMINI_API_KEY", "")
                if valor:
                    os.environ["GEMINI_API_KEY"] = str(valor)
        except Exception as e:
            log.warning("No se pudo leer secrets.toml: %s", e)


def clave_api() -> str:
    return os.environ.get("GEMINI_API_KEY", "").strip()


# ═══════════════════════════ Privacidad (PII) ═══════════════════════════
RE_TEL = re.compile(r"(?<!\d)(?:\+?51[\s-]?)?\d{3}[\s-]?\d{3}[\s-]?\d{3}(?!\d)")  # 9 dígitos
RE_DNI = re.compile(r"(?<!\d)\d{8}(?!\d)")  # 8 dígitos


def enmascarar_pii(texto: str) -> tuple[str, int, int]:
    """Reemplaza teléfonos y DNI. Devuelve (texto, n_dni, n_telefonos)."""
    if not texto:
        return texto, 0, 0
    texto, n_tel = RE_TEL.subn("[TELÉFONO_OCULTO]", texto)
    texto, n_dni = RE_DNI.subn("[DNI_OCULTO]", texto)
    return texto, n_dni, n_tel


def filtrar_pii_stream(flujo):
    """Filtro PII sobre el streaming sin dejar pasar números partidos entre trozos.

    Se retiene la cola numérica (dígitos, espacios, guiones) hasta saber si el número terminó.
    """
    pendiente = ""
    for trozo in flujo:
        pendiente += trozo
        cola = re.search(r"[\d+][\d \-]*$", pendiente)
        seguro, pendiente = (pendiente[: cola.start()], pendiente[cola.start():]) if cola else (pendiente, "")
        if seguro:
            yield enmascarar_pii(seguro)[0]
    if pendiente:
        yield enmascarar_pii(pendiente)[0]


# ═══════════════════════════ Documento base (PDF) ═══════════════════════════
def extraer_texto_pdf(datos: bytes) -> tuple[str, int]:
    lector = PdfReader(io.BytesIO(datos))
    paginas = []
    for i, pagina in enumerate(lector.pages, 1):
        try:
            t = pagina.extract_text() or ""
        except Exception:
            t = ""
        t = re.sub(r"[ \t]+", " ", t).strip()
        if t:
            paginas.append(f"[Pág. {i}]\n{t}")
    return "\n\n".join(paginas), len(lector.pages)


def localizar_pdf() -> Path | None:
    exacto = BASE_DIR / PDF_NOMBRE
    if exacto.is_file():
        return exacto
    candidatos = sorted(p for p in BASE_DIR.glob("*.pdf") if "HERRAMIENTAS" in p.name.upper())
    return candidatos[0] if candidatos else None


_doc_cache: dict = {"firma": None, "valor": None}
_doc_lock = threading.Lock()


def obtener_documento() -> dict:
    """{'ok': bool, 'nombre': str, 'paginas': int, 'texto': str, 'mensaje': str}. Se relee solo si el archivo cambia."""
    ruta = localizar_pdf()
    if ruta is None:
        return dict(ok=False, nombre=PDF_NOMBRE, paginas=0, texto="", mensaje="Falta el documento base: el asistente responderá solo con conocimiento general.")
    with _doc_lock:
        firma = (str(ruta), ruta.stat().st_mtime)
        if _doc_cache["firma"] == firma:
            return _doc_cache["valor"]
        if PdfReader is None:
            valor = dict(ok=False, nombre=ruta.name, paginas=0, texto="", mensaje="Falta instalar pypdf (pip install pypdf).")
        else:
            try:
                texto, paginas = extraer_texto_pdf(ruta.read_bytes())
                if texto.strip():
                    valor = dict(ok=True, nombre=ruta.name, paginas=paginas, texto=texto, mensaje=f"{ruta.name} · {paginas} págs.")
                else:
                    valor = dict(ok=False, nombre=ruta.name, paginas=paginas, texto="", mensaje="El documento base no tiene texto que se pueda leer (¿es un escaneo?).")
            except Exception as e:
                log.warning("No se pudo leer el PDF: %s", e)
                valor = dict(ok=False, nombre=ruta.name, paginas=0, texto="", mensaje="No se pudo leer el documento base.")
        _doc_cache.update(firma=firma, valor=valor)
        return valor


# ═══════════════════════════ Gemini: chat con streaming ═══════════════════════════
def construir_sistema(contexto: str, pii: bool) -> str:
    doc = contexto[:MAX_CONTEXTO] if contexto else "(No se cargó el documento base: avisa a la persona de que respondes solo con conocimiento general.)"
    privacidad = (
        "El filtro ético de privacidad está ACTIVO: nunca repitas DNI ni teléfonos; usa [DNI_OCULTO] y [TELÉFONO_OCULTO]."
        if pii
        else "Recuerda a la persona que no debe compartir datos reales de casos."
    )
    return f"""Eres el «Asistente de Protocolos TIC» de la Escuela Profesional de Trabajo Social de la UNA Puno.
Responde en español, con tono profesional, claro y accesible (máximo ~250 palabras salvo que pidan más).
Reglas:
1. Basa tus respuestas en el DOCUMENTO BASE y cita a los autores tal como aparecen (Reamer, Castells, van Dijk, Gómez Navarro et al., NASW et al. 2017, FITS 2018, Hwang 2024, etc.).
2. Si algo no está en el documento (p. ej. la Ley N.° 29733), indícalo como conocimiento general y pide verificar la norma vigente.
3. Las TIC complementan, no sustituyen, la empatía, la escucha activa ni el juicio ético profesional.
4. Considera siempre confidencialidad, consentimiento informado y brecha digital (zonas rurales de Puno, lenguas quechua y aimara).
5. {privacidad}
6. Tu respuesta se leerá en voz alta: escribe frases completas y no demasiado largas, y evita tablas.
7. Si la pregunta es ambigua o está incompleta, pide una aclaración breve en lugar de suponer lo que la persona quiso decir.

=== DOCUMENTO BASE ===
{doc}
=== FIN DEL DOCUMENTO ==="""


def construir_contenido(historial: list, pregunta: str, pii: bool) -> list:
    """Historial reciente (que manda el navegador) + la pregunta actual → contenido para Gemini."""
    previos = [
        m for m in (historial or [])[-HISTORIAL_MENSAJES:]
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") and str(m.get("content", "")).strip()
    ]
    while previos and previos[0]["role"] != "user":
        previos = previos[1:]

    def limpio(t: str) -> str:
        t = str(t)[:6000]
        return enmascarar_pii(t)[0] if pii else t

    contenido = [
        types.Content(role="user" if m["role"] == "user" else "model", parts=[types.Part.from_text(text=limpio(m["content"]))])
        for m in previos
    ]
    contenido.append(types.Content(role="user", parts=[types.Part.from_text(text=pregunta)]))
    return contenido


def _es(texto: str, claves: tuple[str, ...]) -> bool:
    return any(k in texto for k in claves)


_CLAVES_RETIRADO = ("404", "not_found", "no longer available")
_CLAVES_SATURADO = ("503", "unavailable", "overloaded", "high demand", "429", "resource_exhausted", "deadline", "timed out", "timeout")

_estado_modelo: dict = {"nombre": None, "t": 0.0, "pensar": True}


def _candidatos() -> list[str]:
    base = [MODELO] + [m for m in MODELOS_RESPALDO if m != MODELO]
    n = _estado_modelo["nombre"]
    if n in base and time.time() - _estado_modelo["t"] < RECORDAR_MODELO_S:
        base.remove(n)
        base.insert(0, n)
    return base


def _recordar(modelo: str) -> None:
    _estado_modelo.update(nombre=modelo, t=time.time())


@lru_cache(maxsize=4)
def _cliente(api_key: str):
    try:
        return genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=TIMEOUT_GEMINI_MS))
    except Exception:  # versiones del SDK sin ese parámetro
        return genai.Client(api_key=api_key)


def _pensar(modelo: str):
    """Configuración de «pensamiento» apropiada al modelo (None = no tocar)."""
    if not NIVEL_PENSAR or not _estado_modelo["pensar"]:
        return None
    m = modelo.lower()
    try:
        if m.startswith("gemini-3"):
            nivel = "low" if (NIVEL_PENSAR == "minimal" and "pro" in m) else NIVEL_PENSAR
            return types.ThinkingConfig(thinking_level=nivel)
        if m.startswith("gemini-2.5") and "pro" not in m:
            return types.ThinkingConfig(thinking_budget=0)
    except Exception as e:  # SDK antiguo sin thinking_level
        log.info("Sin control de pensamiento: %s", e)
    return None


def _config(modelo: str, sistema: str, **extra):
    kw = dict(system_instruction=sistema, **extra)
    pensar = _pensar(modelo)
    if pensar is not None:
        kw["thinking_config"] = pensar
    try:
        return types.GenerateContentConfig(**kw)
    except Exception:
        kw.pop("thinking_config", None)
        return types.GenerateContentConfig(**kw)


def flujo_gemini(api_key: str, contenido, sistema: str, info: dict):
    """Generador: entrega el texto de Gemini a medida que se produce (streaming).

    Si el modelo está saturado o retirado ANTES de emitir texto, reintenta y pasa al respaldo.
    Si falla a mitad de la respuesta no se puede cambiar de modelo: se propaga el error.
    `info['modelo']` queda con el modelo que respondió.
    """
    cliente = _cliente(api_key)
    ultimo_error: Exception | None = None
    for modelo in _candidatos():
        for intento in range(REINTENTOS_POR_MODELO):
            emitido = False
            try:
                for trozo in cliente.models.generate_content_stream(model=modelo, contents=contenido, config=_config(modelo, sistema)):
                    texto = getattr(trozo, "text", None) or ""
                    if not texto:
                        continue
                    if not emitido:
                        emitido = True
                        info["modelo"] = modelo
                        _recordar(modelo)
                    yield texto
            except Exception as e:
                if emitido:
                    raise  # ya se mostró texto: no se puede reintentar sin duplicarlo
                t = str(e).lower()
                if "thinking" in t and _estado_modelo["pensar"]:
                    _estado_modelo["pensar"] = False  # el modelo no acepta ese parámetro: se desactiva y se reintenta
                    ultimo_error = e
                    continue
                if _es(t, _CLAVES_RETIRADO):
                    ultimo_error = e
                    break
                if _es(t, _CLAVES_SATURADO):
                    ultimo_error = e
                    if intento < REINTENTOS_POR_MODELO - 1:
                        time.sleep(PAUSA_REINTENTO_S)
                    continue
                raise
            if not emitido:
                raise RuntimeError("La respuesta llegó vacía (posible bloqueo de seguridad). Reformula la pregunta.")
            return
    raise ultimo_error or RuntimeError("No hay un modelo de Gemini disponible.")


def llamar_con_respaldo(hacer):
    """Para llamadas SIN streaming: hacer(modelo) → resultado, con los mismos reintentos y respaldos."""
    ultimo_error: Exception | None = None
    for modelo in _candidatos():
        for intento in range(REINTENTOS_POR_MODELO):
            try:
                resultado = hacer(modelo)
                _recordar(modelo)
                return resultado
            except Exception as e:
                t = str(e).lower()
                if "thinking" in t and _estado_modelo["pensar"]:
                    _estado_modelo["pensar"] = False
                    ultimo_error = e
                    continue
                if _es(t, _CLAVES_RETIRADO):
                    ultimo_error = e
                    break
                if _es(t, _CLAVES_SATURADO):
                    ultimo_error = e
                    if intento < REINTENTOS_POR_MODELO - 1:
                        time.sleep(PAUSA_REINTENTO_S)
                    continue
                raise
    raise ultimo_error or RuntimeError("No hay un modelo de Gemini disponible.")


def mensaje_error(e: Exception) -> str:
    """Mensaje en lenguaje claro: qué pasó y qué puede hacer la persona."""
    t = str(e).lower()
    if _es(t, ("api key", "api_key", "401", "403", "permission", "unauthenticated")):
        return "El servicio de IA no está bien configurado (la clave no es válida o no tiene permisos). Avisa a quien administra la aplicación."
    if _es(t, ("429", "quota", "resource_exhausted")):
        return "Se alcanzó el límite de uso del servicio. Espera un momento e inténtalo de nuevo."
    if _es(t, ("503", "unavailable", "high demand", "overloaded")):
        return "Los modelos de IA están saturados en este momento. Espera unos segundos y vuelve a intentarlo."
    if _es(t, _CLAVES_RETIRADO):
        return "El modelo de IA configurado ya no está disponible. Quien administra la aplicación debe actualizarlo (variable GEMINI_MODEL o logica.py)."
    if "vacía" in t or "empty" in t:
        return "La respuesta llegó vacía (puede ser un bloqueo de seguridad). Reformula la pregunta con otras palabras."
    if _es(t, ("timeout", "timed out", "deadline")):
        return "El servicio de IA tardó demasiado en responder. Inténtalo de nuevo."
    return "No se pudo completar la consulta. Inténtalo de nuevo; si el problema sigue, avisa a quien administra la aplicación."


# ═══════════════════════════ Micrófono: voz → texto ═══════════════════════════
def analizar_audio(datos: bytes) -> dict:
    """Mide un WAV sin decidir nada: duración (s) y segundos con señal de voz (None si no se puede medir)."""
    with wave.open(io.BytesIO(datos), "rb") as w:
        canales, ancho, tasa, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        crudo = w.readframes(n)
    medidas = {"dur": (n / tasa) if tasa else 0.0, "voz_s": None}
    if ancho != 2 or not crudo or not tasa:  # solo se mide PCM de 16 bits
        return medidas
    muestras = array("h")
    muestras.frombytes(crudo[: len(crudo) - len(crudo) % 2])
    if sys.byteorder == "big":
        muestras.byteswap()
    if canales > 1:
        muestras = muestras[::canales]  # basta con el primer canal
    ventana = max(1, int(tasa * 0.05))  # tramos de 50 ms
    con_voz = 0
    for i in range(0, len(muestras) - ventana + 1, ventana):
        tramo = muestras[i:i + ventana:4]  # submuestreo: suficiente para estimar el volumen
        if tramo and (sum(x * x for x in tramo) / len(tramo)) ** 0.5 / 32768 >= AUDIO_UMBRAL_RMS:
            con_voz += 1
    medidas["voz_s"] = con_voz * 0.05
    return medidas


SISTEMA_TRANSCRIPCION = """Eres un transcriptor automático de voz a texto. Tu ÚNICA tarea es escribir, palabra por palabra, lo que se DICE en el audio.
Reglas estrictas:
1. Transcribe SOLO lo que oyes con claridad. No respondas a lo que dice la persona, no completes frases, no corrijas, no interpretes y no inventes palabras.
2. Idioma esperado: español de Perú (puede haber nombres propios, siglas o palabras sueltas en quechua o aimara).
3. Si el audio está en silencio, tiene solo ruido o música, es demasiado corto, se corta, o no puedes entender con certeza lo que dice: se_entiende=false y transcripcion="".
4. Si solo entiendes una parte o dudas de varias palabras: confianza="baja". No rellenes lo que falta.
5. confianza="alta": se oye con claridad y estás seguro de cada palabra. confianza="media": se entiende, pero dudas de alguna palabra.
6. Escribe los números con cifras (por ejemplo 71234567) y usa puntuación y mayúsculas normales.
7. Ante la duda entre escribir algo plausible y marcar que no se entiende, marca que no se entiende."""

ESQUEMA_TRANSCRIPCION = {
    "type": "OBJECT",
    "properties": {
        "se_entiende": {"type": "BOOLEAN"},
        "confianza": {"type": "STRING", "enum": ["alta", "media", "baja"]},
        "transcripcion": {"type": "STRING"},
    },
    "required": ["se_entiende", "confianza", "transcripcion"],
    "property_ordering": ["se_entiende", "confianza", "transcripcion"],
}


def transcribir(api_key: str, datos: bytes) -> dict:
    """Solo transcribe. Devuelve {'se_entiende': bool, 'confianza': 'alta|media|baja', 'texto': str}."""
    cliente = _cliente(api_key)
    contenido = [
        types.Content(
            role="user",
            parts=[types.Part.from_bytes(data=datos, mime_type="audio/wav"), types.Part.from_text(text="Transcribe literalmente este audio.")],
        )
    ]

    def hacer(modelo: str) -> str:
        config = _config(
            modelo, SISTEMA_TRANSCRIPCION,
            temperature=0.0, response_mime_type="application/json", response_schema=ESQUEMA_TRANSCRIPCION,
        )
        return cliente.models.generate_content(model=modelo, contents=contenido, config=config).text or ""

    bruto = re.sub(r"^```(?:json)?\s*|\s*```$", "", llamar_con_respaldo(hacer).strip())
    try:
        d = json.loads(bruto)
    except Exception:
        log.warning("Transcripción con formato inesperado: %r", bruto[:200])
        return {"se_entiende": False, "confianza": "baja", "texto": ""}
    confianza = str(d.get("confianza", "baja")).lower()
    return {
        "se_entiende": bool(d.get("se_entiende")),
        "confianza": confianza if confianza in ("alta", "media", "baja") else "baja",
        "texto": str(d.get("transcripcion", "")).strip(),
    }


RELLENOS = {"eh", "ehh", "ah", "ahh", "mm", "mmm", "hm", "hmm", "um", "uh", "oh", "ajá", "aja"}


def evaluar_transcripcion(r: dict, dur: float | None) -> tuple[str, str]:
    """Decide qué hacer con una transcripción: ('ok' | 'dudosa' | 'repetir', motivo)."""
    texto = (r.get("texto") or "").strip()
    palabras = re.findall(r"[a-záéíóúñü0-9]+", texto.lower())
    if not texto or not r.get("se_entiende") or r.get("confianza") == "baja":
        return "repetir", "No se entendió con claridad lo que dijiste."
    if sum(len(p) for p in palabras) < 3 or all(p in RELLENOS for p in palabras):
        return "repetir", "Solo se captaron sonidos sueltos, no una pregunta."
    if dur and len(texto) > dur * 35 + 25:  # imposible hablar tan rápido: el texto no sale del audio
        return "repetir", "La transcripción no cuadra con la duración del audio (posible ruido)."
    if r.get("confianza") != "alta" and CONFIRMAR_SI_DUDA:
        return "dudosa", ""
    return "ok", ""


def procesar_audio(datos: bytes, api_key: str, pii: bool) -> dict:
    """Audio WAV → {'estado': 'ok'|'dudosa'|'repetir', 'texto', 'motivo', 'oido'}. Nunca inventa la pregunta."""
    try:
        medidas = analizar_audio(datos)
    except Exception as e:  # formato WAV inesperado: no bloquear, la transcripción también valida
        log.warning("No se pudo medir el audio: %r", e)
        medidas = None
    dur = medidas["dur"] if medidas else None
    if medidas:
        if medidas["dur"] < AUDIO_MIN_S:
            return dict(estado="repetir", texto="", oido="", motivo=f"El audio dura {medidas['dur']:.1f} s: es demasiado corto.")
        if medidas["dur"] > AUDIO_MAX_S:
            return dict(estado="repetir", texto="", oido="", motivo=f"El audio dura más de {int(AUDIO_MAX_S)} s: haz preguntas más breves.")
        if medidas["voz_s"] is not None and medidas["voz_s"] < AUDIO_VOZ_MIN_S:
            return dict(estado="repetir", texto="", oido="", motivo="Casi no se escucha voz en la grabación (silencio o solo ruido). Revisa que el micrófono esté activo.")

    try:
        r = transcribir(api_key, datos)
    except Exception as e:
        log.warning("Fallo al transcribir: %r", e)
        return dict(estado="error", texto="", oido="", motivo=mensaje_error(e))

    texto = enmascarar_pii(r["texto"])[0] if pii else r["texto"]
    estado, motivo = evaluar_transcripcion(r, dur)
    if estado == "repetir":
        return dict(estado="repetir", texto="", oido=texto, motivo=motivo)
    return dict(estado=estado, texto=texto, oido="", motivo="")


# ═══════════════════════════ Lectura en voz alta: texto → voz ═══════════════════════════
_RE_EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200d]")


def texto_para_voz(texto: str, maximo: int = MAX_CHARS_TROZO) -> str:
    """Quita Markdown, enlaces, emojis y símbolos para que la voz lea solo lenguaje natural."""
    t = re.sub(r"```.*?```", " ", texto, flags=re.S)
    t = re.sub(r"\[([^\]]+)\]\(https?://[^)]*\)", r"\1", t)  # [texto](url) → texto
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"\[P[áa]g\.?\s*\d+(?:\s*[-–,]\s*\d+)*\]", "", t)  # marcas [Pág. 12] del documento base
    t = t.replace("[DNI_OCULTO]", "dato oculto").replace("[TELÉFONO_OCULTO]", "dato oculto")
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)  # títulos
    t = re.sub(r"^\s*[-*•]\s+", "", t, flags=re.M)  # viñetas
    t = re.sub(r"^\s*\d+[.)]\s+", "", t, flags=re.M)  # numeración «1. »
    t = re.sub(r"[*_`~|]+", "", t)  # énfasis y tablas
    t = _RE_EMOJI.sub("", t)
    t = re.sub(r"<[^<>\n]{1,25}>", " ", t)  # <br>…: no deben leerse
    t = t.replace("<", " ").replace(">", " ")
    for patron, lectura in PRONUNCIACION.items():
        t = re.sub(patron, lectura, t)
    t = re.sub(r"(?<!\.)\.\.(?!\.)", ".", t)  # «N.A.S.W.» + punto final no debe quedar con doble punto
    lineas = []
    for linea in t.splitlines():
        linea = re.sub(r"\s+([.,;:!?])", r"\1", re.sub(r"\s+", " ", linea)).strip()
        if linea:  # una pausa natural al final de cada línea
            lineas.append(linea if linea[-1] in ".,;:!?…)" else linea + ".")
    t = "\n".join(lineas)
    if len(t) > maximo:  # se corta en una frase completa
        corte = max(t.rfind(". ", 0, maximo), t.rfind("\n", 0, maximo))
        t = t[: corte + 1] if corte > maximo // 2 else t[:maximo]
    return t


async def _edge_async(texto: str, voz: str, rate: str) -> bytes:
    audio = bytearray()
    async for trozo in edge_tts.Communicate(texto, voz, rate=rate).stream():
        if trozo["type"] == "audio":
            audio.extend(trozo["data"])
    if not audio:
        raise RuntimeError("edge-tts no devolvió audio")
    return bytes(audio)


async def _edge_con_limite(texto: str, voz: str, rate: str) -> bytes:
    return await asyncio.wait_for(_edge_async(texto, voz, rate), TTS_TIMEOUT_S)


def _gtts(texto: str, lenta: bool) -> bytes:
    buf = io.BytesIO()
    gTTS(text=texto, lang="es", tld=GTTS_TLD, slow=lenta).write_to_fp(buf)
    return buf.getvalue()


_cache_tts: OrderedDict = OrderedDict()
_cache_lock = threading.Lock()
_sem_tts = threading.Semaphore(TTS_CONCURRENCIA)
_edge_estado = {"fallos": 0, "hasta": 0.0}


def sintetizar(texto: str, voz: str, velocidad: float) -> tuple[bytes, str]:
    """Texto → MP3. Devuelve (audio, motor). Prueba edge-tts (2 intentos) y luego gTTS. Los fallos no se guardan en caché."""
    clave = hashlib.sha1(f"{voz}|{velocidad:.2f}|{texto}".encode("utf-8")).hexdigest()
    with _cache_lock:
        if clave in _cache_tts:
            _cache_tts.move_to_end(clave)
            return _cache_tts[clave]

    def guardar(audio: bytes, motor: str):
        with _cache_lock:
            _cache_tts[clave] = (audio, motor)
            while len(_cache_tts) > TTS_CACHE_MAX:
                _cache_tts.popitem(last=False)
        return audio, motor

    rate = f"{round((velocidad - 1) * 100):+d}%"
    errores: list[str] = []
    with _sem_tts:
        if edge_tts is not None and time.time() >= _edge_estado["hasta"]:
            for intento in range(2):
                try:
                    audio = asyncio.run(_edge_con_limite(texto, voz, rate))
                    _edge_estado["fallos"] = 0
                    return guardar(audio, "edge")
                except Exception as e:
                    errores.append(f"edge-tts: {type(e).__name__}: {e}")
                    if intento == 0:
                        time.sleep(0.3)
            _edge_estado["fallos"] += 1
            if _edge_estado["fallos"] >= 2:  # servicio caído o bloqueado: no seguir esperando en cada frase
                _edge_estado["hasta"] = time.time() + TTS_PAUSA_TRAS_FALLOS_S
                log.warning("edge-tts falla de forma repetida; se usa gTTS durante %s s", TTS_PAUSA_TRAS_FALLOS_S)
        if gTTS is not None:
            try:
                return guardar(_gtts(texto, velocidad < 0.95), "gtts")
            except Exception as e:
                errores.append(f"gTTS: {type(e).__name__}: {e}")
    raise RuntimeError(" | ".join(errores) or "No hay ninguna librería de voz instalada (pip install edge-tts gTTS).")


# ═══════════════════════════ Estado para la interfaz ═══════════════════════════
def estado_general() -> dict:
    doc = obtener_documento()
    problemas = []
    if not GENAI_OK:
        problemas.append("Falta instalar el componente de IA en el servidor (pip install google-genai).")
    if not clave_api():
        problemas.append("Falta la clave del servicio de IA (GEMINI_API_KEY).")
    return {
        "listo": not problemas,
        "problemas": problemas,
        "documento": {k: doc[k] for k in ("ok", "nombre", "paginas", "mensaje")},
        "voces": list(VOCES),
        "voz_por_defecto": VOZ_POR_DEFECTO,
        "velocidades": VOZ_VELOCIDADES,
        "velocidad_inicial": VOZ_VELOCIDAD_INICIAL,
        "voz_servidor": VOZ_OK,
        "leer_inicial": VOZ_INICIAL,
        "pii_inicial": FILTRO_PII_INICIAL,
        "max_consulta": MAX_CONSULTA,
        "max_chars_voz": MAX_CHARS_VOZ,
        "audio_max_s": AUDIO_MAX_S,
        "facultad": FACULTAD,
    }
