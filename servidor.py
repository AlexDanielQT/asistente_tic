# -*- coding: utf-8 -*-
"""
Asistente de Protocolos TIC · Trabajo Social · UNA Puno — SERVIDOR

Ejecutar:   python servidor.py            →  http://localhost:8000
Publicar:   python servidor.py --host 0.0.0.0 --port 8000     (o variables HOST / PORT)

Usa solo la librería estándar de Python para el servidor web (no hace falta Streamlit, FastAPI ni Flask).
Dependencias de IA y voz: ver requirements.txt.

API (la usa static/app.js):
  GET  /api/config       estado, voces, documento base
  POST /api/chat         pregunta → respuesta de Gemini en streaming (Server-Sent Events)
  POST /api/tts          frase → audio MP3 (se pide frase por frase mientras Gemini aún escribe)
  POST /api/transcribir  audio WAV → texto (con las compuertas anti-invención de logica.py)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import logica as L

log = logging.getLogger("tic_servidor")
STATIC = L.BASE_DIR / "static"
TIPOS = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".webp": "image/webp",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}

# Peticiones por minuto y por dirección IP (protege la clave de Gemini si la app está en internet).
# Una red universitaria comparte una sola IP pública: los límites son holgados a propósito.
LIMITES = {"chat": 60, "tts": 400, "transcribir": 40}
MAX_JSON = 300_000
MAX_AUDIO = 8_000_000  # 120 s de WAV mono a 16 kHz ≈ 3,8 MB
MAX_TEXTO_TTS = 1200


class Limitador:
    def __init__(self) -> None:
        self.registro: dict[tuple[str, str], list[float]] = {}
        self.lock = threading.Lock()

    def permitir(self, ip: str, grupo: str, ventana: float = 60.0) -> bool:
        ahora = time.time()
        with self.lock:
            q = [t for t in self.registro.get((ip, grupo), []) if ahora - t < ventana]
            ok = len(q) < LIMITES[grupo]
            if ok:
                q.append(ahora)
            self.registro[(ip, grupo)] = q
            if len(self.registro) > 5000:  # limpieza ocasional
                self.registro = {k: v for k, v in self.registro.items() if v and ahora - v[-1] < ventana}
            return ok


limitador = Limitador()


class Manejador(BaseHTTPRequestHandler):
    server_version = "AsistenteTIC/2.0"

    def log_message(self, formato, *args):  # registro discreto
        log.debug("%s - %s", self.address_string(), formato % args)

    # ── utilidades ──
    def _ip(self) -> str:
        if os.environ.get("TIC_PROXY") == "1":  # detrás de un proxy inverso (Render, Nginx…)
            xff = self.headers.get("X-Forwarded-For", "")
            if xff:
                return xff.split(",")[0].strip()
        return self.client_address[0]

    def _json(self, codigo: int, obj: dict) -> None:
        cuerpo = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(cuerpo)

    def _cuerpo(self, maximo: int) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            raise ValueError("Petición vacía.")
        if n > maximo:
            raise OverflowError("La petición es demasiado grande.")
        return self.rfile.read(n)

    def _sse(self, obj: dict) -> None:
        self.wfile.write(b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n\n")
        self.wfile.flush()

    # ── GET: interfaz y estado ──
    def do_GET(self) -> None:
        ruta = urlparse(self.path).path
        if ruta == "/api/config":
            return self._json(200, L.estado_general())
        if ruta == "/api/salud":
            return self._json(200, {"ok": True})
        if ruta == "/":
            ruta = "/index.html"
        nombre = ruta.removeprefix("/static/").lstrip("/")
        archivo = (STATIC / nombre).resolve()
        try:
            archivo.relative_to(STATIC.resolve())
        except ValueError:
            return self._json(404, {"error": "No encontrado."})
        if not archivo.is_file():
            return self._json(404, {"error": "No encontrado."})
        datos = archivo.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", TIPOS.get(archivo.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(datos)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(datos)

    # ── POST ──
    def do_POST(self) -> None:
        url = urlparse(self.path)
        grupos = {"/api/chat": "chat", "/api/tts": "tts", "/api/transcribir": "transcribir"}
        grupo = grupos.get(url.path)
        if grupo is None:
            return self._json(404, {"error": "No encontrado."})
        if not limitador.permitir(self._ip(), grupo):
            return self._json(429, {"error": "Demasiadas solicitudes seguidas. Espera un momento e inténtalo de nuevo."})
        try:
            if grupo == "chat":
                self._chat()
            elif grupo == "tts":
                self._tts()
            else:
                self._transcribir(parse_qs(url.query))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # la persona cerró la pestaña o cortó la petición
        except OverflowError as e:
            self._json(413, {"error": str(e)})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
            log.info("Petición inválida: %r", e)
            self._json(400, {"error": "Petición inválida."})
        except Exception as e:
            log.exception("Error en %s", url.path)
            try:
                self._json(500, {"error": L.mensaje_error(e)})
            except Exception:
                pass

    # ── Chat con streaming ──
    def _chat(self) -> None:
        d = json.loads(self._cuerpo(MAX_JSON))
        pii = bool(d.get("pii", True))
        pregunta = str(d.get("pregunta", "")).strip()[: L.MAX_CONSULTA]
        if not pregunta:
            return self._json(400, {"error": "Escribe una pregunta."})
        if not L.GENAI_OK:
            return self._json(503, {"error": "El asistente no está disponible: falta instalar google-genai en el servidor."})
        api_key = L.clave_api()
        if not api_key:
            return self._json(503, {"error": "El asistente aún no está activado: falta la clave GEMINI_API_KEY."})

        pregunta, n_dni, n_tel = L.enmascarar_pii(pregunta) if pii else (pregunta, 0, 0)
        doc = L.obtener_documento()
        sistema = L.construir_sistema(doc["texto"], pii)
        contenido = L.construir_contenido(d.get("historial") or [], pregunta, pii)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")  # Nginx: no retener el streaming
        self.send_header("Connection", "close")
        self.end_headers()
        self._sse({"t": "meta", "pregunta": pregunta, "dni": n_dni, "tel": n_tel})

        info: dict = {}
        flujo = L.flujo_gemini(api_key, contenido, sistema, info)
        if pii:
            flujo = L.filtrar_pii_stream(flujo)
        try:
            for trozo in flujo:
                self._sse({"t": "d", "x": trozo})
            self._sse({"t": "fin", "modelo": info.get("modelo", ""), "principal": L.MODELO})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            flujo.close()  # la persona se fue: se deja de pedir texto a Gemini
        except Exception as e:
            log.warning("Fallo en la consulta: %r", e)
            try:
                self._sse({"t": "error", "mensaje": L.mensaje_error(e), "detalle": f"{type(e).__name__}: {e}"})
            except Exception:
                pass

    # ── Voz: una frase → MP3 ──
    def _tts(self) -> None:
        d = json.loads(self._cuerpo(MAX_JSON))
        texto = str(d.get("texto", ""))[:MAX_TEXTO_TTS]
        if bool(d.get("pii", True)):
            texto = L.enmascarar_pii(texto)[0]
        hablado = L.texto_para_voz(texto)
        if not hablado:
            self.send_response(204)
            self.end_headers()
            return
        voz = L.VOCES.get(str(d.get("voz", "")), L.VOCES[L.VOZ_POR_DEFECTO])
        velocidad = min(1.5, max(0.6, float(d.get("velocidad", 1.0))))
        t0 = time.time()
        try:
            audio, motor = L.sintetizar(hablado, voz, velocidad)
        except Exception as e:
            log.warning("No se pudo generar la voz: %s", e)
            return self._json(502, {"error": "No se pudo generar la voz."})
        log.info("voz %s · %d caracteres · %.1f s", motor, len(hablado), time.time() - t0)
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(audio)))
        self.send_header("X-Motor-Voz", motor)
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        self.wfile.write(audio)

    # ── Micrófono: WAV → texto ──
    def _transcribir(self, consulta: dict) -> None:
        datos = self._cuerpo(MAX_AUDIO)
        pii = consulta.get("pii", ["1"])[0] != "0"
        if not L.GENAI_OK or not L.clave_api():
            return self._json(503, {"error": "El asistente aún no está activado: falta la clave del servicio de IA."})
        self._json(200, L.procesar_audio(datos, L.clave_api(), pii))


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # consolas de Windows
    except Exception:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    L.cargar_entorno()

    en_nube = bool(os.environ.get("PORT"))
    ap = argparse.ArgumentParser(description="Asistente de Protocolos TIC · UNA Puno")
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0" if en_nube else "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = ap.parse_args()

    threading.Thread(target=L.obtener_documento, daemon=True).start()  # el PDF se lee mientras arranca el servidor

    servidor = ThreadingHTTPServer((args.host, args.port), Manejador)
    servidor.daemon_threads = True
    print("=" * 64)
    print(" Asistente de Protocolos TIC · Trabajo Social · UNA Puno")
    print(f" Abre en el navegador:  http://localhost:{args.port}")
    print(f" Modelo de IA:          {L.MODELO}  (pensamiento: {L.NIVEL_PENSAR or 'por defecto'})")
    print(f" Clave GEMINI_API_KEY:  {'configurada' if L.clave_api() else 'FALTA (ver LEEME.txt)'}")
    print(f" Componente de IA:      {'listo' if L.GENAI_OK else 'FALTA: pip install google-genai'}")
    print(f" Voz del servidor:      edge-tts {'sí' if L.edge_tts else 'no'} · gTTS {'sí' if L.gTTS else 'no'}")
    pdf = L.localizar_pdf()
    print(f" Documento base:        {pdf.name if pdf else 'FALTA ' + L.PDF_NOMBRE}")
    print(" Para detener: Ctrl+C")
    print("=" * 64)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido.")
    finally:
        servidor.server_close()


if __name__ == "__main__":
    main()
