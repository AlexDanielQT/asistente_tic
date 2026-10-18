/* Asistente de Protocolos TIC · UNA Puno — interfaz
   Flujo clave: mientras Gemini escribe, cada frase completa se manda a /api/tts y se reproduce en cuanto llega,
   así el gato empieza a hablar en ~1-2 s en vez de esperar a que termine toda la respuesta. */
(() => {
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const NS = 'http://www.w3.org/2000/svg';
const esperar = ms => new Promise(r => setTimeout(r, ms));
const hora = () => new Date().toLocaleTimeString('es-PE', { hour: '2-digit', minute: '2-digit', hour12: false });
const EJEMPLOS = [
  '¿Qué dice Reamer sobre la privacidad digital?',
  '¿Cuáles son los 3 niveles de la brecha digital?',
  '¿Cómo evitar la deshumanización al usar IA?',
];
const SALUDO_VOZ = '¡Hola! Soy tu asistente virtual. Estoy aquí para ayudarte con los protocolos TIC en Trabajo Social. Puedes escribir tu consulta o usar el micrófono.';

function el(tag, clase, texto) {
  const e = document.createElement(tag);
  if (clase) e.className = clase;
  if (texto != null) e.textContent = texto;
  return e;
}
function icono(id, clase = 'ic') {
  const s = document.createElementNS(NS, 'svg');
  s.setAttribute('class', clase);
  s.setAttribute('aria-hidden', 'true');
  const u = document.createElementNS(NS, 'use');
  u.setAttribute('href', '#' + id);
  s.appendChild(u);
  return s;
}
const esc = s => s.replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* Markdown mínimo y seguro: se escapa todo el HTML y solo se reconocen negritas, cursivas, código, listas y títulos. */
function md(texto) {
  const inline = s => s
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*\w])\*(?!\s)([^*]+?)\*(?!\*)/g, '$1<em>$2</em>')
    .replace(/`([^`]+)`/g, '<code>$1</code>');
  let html = '', lista = null;
  const cerrar = () => { if (lista) { html += `</${lista}>`; lista = null; } };
  for (const l of esc(texto).split('\n')) {
    let m;
    if ((m = l.match(/^\s*[-*•]\s+(.*)/))) { if (lista !== 'ul') { cerrar(); html += '<ul>'; lista = 'ul'; } html += `<li>${inline(m[1])}</li>`; }
    else if ((m = l.match(/^\s*\d+[.)]\s+(.*)/))) { if (lista !== 'ol') { cerrar(); html += '<ol>'; lista = 'ol'; } html += `<li>${inline(m[1])}</li>`; }
    else if ((m = l.match(/^\s{0,3}#{1,6}\s+(.*)/))) { cerrar(); html += `<p><strong>${inline(m[1])}</strong></p>`; }
    else if (!l.trim()) cerrar();
    else { cerrar(); html += `<p>${inline(l)}</p>`; }
  }
  cerrar();
  return html;
}

/* Misma regla que el servidor (sin «lookbehind», para que funcione también en iPhone/Safari antiguos). */
function mascarar(t) {
  let dni = 0, tel = 0;
  t = t.replace(/(^|\D)((?:\+?51[\s-]?)?\d{3}[\s-]?\d{3}[\s-]?\d{3})(?!\d)/g, (m, p) => { tel++; return p + '[TELÉFONO_OCULTO]'; });
  t = t.replace(/(^|\D)(\d{8})(?!\d)/g, (m, p) => { dni++; return p + '[DNI_OCULTO]'; });
  return { texto: t, dni, tel };
}

/* ═════════════════════════ Configuración de la persona ═════════════════════════ */
const cfg = { voz: '', vel: 'Normal', leer: true, pii: true };
let servidor = { listo: true, problemas: [], velocidades: { Normal: 1 }, voces: [], max_chars_voz: 3500, audio_max_s: 120 };
const NAVEGADOR = 'navegador';

function guardarCfg() { try { localStorage.setItem('tic.cfg', JSON.stringify(cfg)); } catch (_) { /* modo privado */ } }
function leerCfgGuardada() { try { return JSON.parse(localStorage.getItem('tic.cfg') || '{}'); } catch (_) { return {}; } }

/* ═════════════════════════ Voz: audio por frases + análisis para la boca ═════════════════════════ */
const ABREV = /(?:\bet al|\b(?:p[áa]gs?|pp|vol|cap|ed|dr|dra|sr|sra|lic|art|n[úu]m|fig|cf|aprox|ej))$/i;

function esAbreviatura(previo) {
  return ABREV.test(previo) || /(?:^|[\s(])\p{L}$/u.test(previo); // «Pág.», «et al.», iniciales como «N.»
}
function limpiarNavegador(t) {
  return t.replace(/\[P[áa]g\.?[^\]]*\]/g, '').replace(/https?:\/\/\S+/g, '').replace(/\[(?:DNI|TELÉFONO)_OCULTO\]/g, 'dato oculto')
    .replace(/[*_`#~|>]+/g, ' ').replace(/\s+/g, ' ').trim();
}

class Voz {
  constructor(alCambiar) {
    this.alCambiar = alCambiar;
    this.estado = 'reposo';           // reposo | cargando | hablando
    this.ctx = null; this.analizador = null; this.datos = null;
    this.gen = 0; this.navId = 0;
    this.cola = []; this.fuentes = new Set(); this.abortos = new Set();
    this.tFin = 0; this.bombeando = false; this.nav = false;
    this.buf = ''; this.pend = ''; this.primero = true; this.total = 0; this.finalizado = true;
    this.opts = { voz: '', velocidad: 1, pii: true };
    this.motorFalla = false; this.bloqueado = false;
  }

  /* Debe llamarse dentro de un gesto de la persona (clic, tecla, toque): los navegadores lo exigen para dar sonido. */
  desbloquear() {
    if (!this.ctx) {
      const C = window.AudioContext || window.webkitAudioContext;
      if (!C) return;
      this.ctx = new C();
      this.analizador = this.ctx.createAnalyser();
      this.analizador.fftSize = 1024;
      this.analizador.smoothingTimeConstant = 0.4;
      this.datos = new Uint8Array(this.analizador.fftSize);
      this.analizador.connect(this.ctx.destination);
    }
    if (this.ctx.state !== 'running') {
      this.ctx.resume().then(() => { this.bloqueado = false; this._recalcular(); }).catch(() => {});
      try { // iOS: un sonido mudo inicial termina de abrir el audio
        const s = this.ctx.createBufferSource();
        s.buffer = this.ctx.createBuffer(1, 1, 22050);
        s.connect(this.ctx.destination); s.start(0);
      } catch (_) { /* nada */ }
    }
  }

  nivel() {
    if (this.estado !== 'hablando') return 0;
    if (this.nav) { const t = performance.now() / 1000; return Math.max(0, 0.07 + 0.07 * Math.sin(t * 11) + 0.04 * Math.sin(t * 4.3)); }
    if (!this.analizador || !this.fuentes.size) return 0;
    this.analizador.getByteTimeDomainData(this.datos);
    let s = 0, n = 0;
    for (let i = 0; i < this.datos.length; i += 2) { const v = (this.datos[i] - 128) / 128; s += v * v; n++; }
    return Math.sqrt(s / n);
  }

  /* Prepara una nueva lectura (corta la anterior). Después: alimentar(texto)… y finalizar(). */
  nuevo(opts) {
    this.detener();
    this.desbloquear();
    Object.assign(this.opts, opts || {});
    this.finalizado = false; this.primero = true; this.total = 0; this.limite = false;
    this._recalcular();
  }
  hablarTexto(texto) { this.nuevo(); this.alimentar(texto + '\n'); this.finalizar(); }

  alimentar(delta) {
    if (this.finalizado || this.limite) return;
    this.buf += delta;
    this._extraer(false);
  }
  finalizar() {
    if (this.finalizado) return;
    this._extraer(true);
    this.finalizado = true;
    this._recalcular();
  }

  /* Corta el texto recibido en frases completas (sin partir «Pág. 12», «et al.», «2.5»…). */
  _extraer(final) {
    const re = /([.!?…]+["')\]»”]*)(?=\s)|\n+/g;
    let m, ult = 0;
    while ((m = re.exec(this.buf))) {
      if (m[0][0] === '.' && esAbreviatura(this.buf.slice(ult, m.index))) continue;
      const fin = m.index + m[0].length;
      this._acumular(this.buf.slice(ult, fin));
      ult = fin;
    }
    this.buf = this.buf.slice(ult);
    if (this.primero && !final) {        // solo la 1.ª porción: si la frase es larga, empieza en la primera coma (tras ≥40 letras)
      const c = /^([\s\S]{40,}?[,;:])\s/.exec(this.buf);
      if (c) { this._acumular(c[1]); this.buf = this.buf.slice(c[1].length); }
    }
    if (final) { this._acumular(this.buf); this.buf = ''; this._vaciar(true); }
  }
  _acumular(frase) {
    this.pend += frase;
    const minimo = this.primero ? 28 : 110;   // la 1.ª frase sale enseguida; las demás se agrupan para sonar más natural
    if (this.pend.trim().length >= minimo) this._vaciar(false);
  }
  _vaciar() {
    let t = this.pend.trim(); this.pend = '';
    while (t) {
      if (this.total + t.length > servidor.max_chars_voz) { this.limite = true; return; }
      let corte = t.length;
      if (t.length > 450) {                    // frase muy larga: se parte en una coma o punto y coma
        const c = Math.max(t.lastIndexOf(', ', 450), t.lastIndexOf('; ', 450));
        corte = c > 150 ? c + 1 : 450;
      }
      this._encolar(t.slice(0, corte).trim());
      t = t.slice(corte).trim();
    }
  }
  _encolar(texto) {
    if (!texto) return;
    this.primero = false;
    this.total += texto.length;
    this.cola.push({ texto, p: this._pedir(texto) });
    this._bombear();
    this._recalcular();
  }

  async _pedir(texto) {
    if (this.opts.voz === NAVEGADOR || this.motorFalla) return { tipo: 'nav' };
    const ac = new AbortController();
    this.abortos.add(ac);
    try {
      const r = await fetch('/api/tts', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: ac.signal,
        body: JSON.stringify({ texto, voz: this.opts.voz, velocidad: this.opts.velocidad, pii: this.opts.pii }),
      });
      if (r.status === 204) return { tipo: 'vacio' };
      if (!r.ok) throw new Error('voz del servidor: HTTP ' + r.status);
      const buf = await this.ctx.decodeAudioData(await r.arrayBuffer());
      return { tipo: 'buf', buf };
    } catch (e) {
      if (e.name === 'AbortError') return { tipo: 'abort' };
      console.warn('Voz del servidor no disponible, se usa la del dispositivo:', e);
      this.motorFalla = true;           // no se vuelve a esperar al servidor en esta sesión
      this.alFallar && this.alFallar();
      return { tipo: 'nav' };
    } finally {
      this.abortos.delete(ac);
    }
  }

  async _bombear() {
    if (this.bombeando) return;
    this.bombeando = true;
    const gen = this.gen;
    while (this.cola.length && gen === this.gen) {
      const item = this.cola[0];
      const r = await item.p;
      if (gen !== this.gen) return;
      this.cola.shift();
      if (r.tipo === 'buf') this._programar(r.buf);
      else if (r.tipo === 'nav') {
        await esperar(Math.max(0, (this.tFin - this.ctx.currentTime) * 1000)); // que termine lo ya programado
        if (gen !== this.gen) return;
        await this._hablarNavegador(item.texto, gen);
      }
      this._recalcular();
    }
    if (gen === this.gen) { this.bombeando = false; this._recalcular(); }
  }

  _programar(buf) {
    const ctx = this.ctx;
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this.analizador);
    const t0 = Math.max(ctx.currentTime + 0.03, this.tFin);   // una frase empieza justo cuando termina la anterior
    src.start(t0);
    this.tFin = t0 + buf.duration;
    this.fuentes.add(src);
    src.onended = () => { this.fuentes.delete(src); this._recalcular(); };
    if (ctx.state !== 'running') {      // el navegador aún no permite sonido: se avisa y arranca al primer toque
      this.bloqueado = true;
      ctx.resume().then(() => { this.bloqueado = false; this._recalcular(); }).catch(() => {});
    }
    this._recalcular();
  }

  _hablarNavegador(texto, gen) {
    return new Promise(resolve => {
      const sintesis = window.speechSynthesis;
      const limpio = limpiarNavegador(texto);
      if (!sintesis || !limpio) return resolve();
      const id = ++this.navId;
      const u = new SpeechSynthesisUtterance(limpio);
      const voces = sintesis.getVoices();
      const v = ['es-PE', 'es-419', 'es-MX', 'es-US', 'es-ES'].map(l => voces.find(x => x.lang.replace('_', '-') === l)).find(Boolean)
        || voces.find(x => x.lang.toLowerCase().startsWith('es'));
      if (v) u.voice = v;
      u.lang = v ? v.lang : 'es-PE';
      u.rate = this.opts.velocidad;
      const fin = () => { clearTimeout(seguro); if (this.navId === id) { this.nav = false; this._recalcular(); } resolve(); };
      const seguro = setTimeout(fin, limpio.length * 110 + 5000);
      u.onstart = () => { if (this.navId === id && gen === this.gen) { this.nav = true; this._recalcular(); } };
      u.onend = fin; u.onerror = fin;
      sintesis.speak(u);
    });
  }

  detener() {
    this.gen++; this.navId++;
    this.cola.length = 0; this.buf = ''; this.pend = '';
    this.abortos.forEach(a => a.abort()); this.abortos.clear();
    this.fuentes.forEach(s => { s.onended = null; try { s.stop(); } catch (_) { /* ya terminó */ } });
    this.fuentes.clear(); this.tFin = 0;
    try { window.speechSynthesis && window.speechSynthesis.cancel(); } catch (_) { /* nada */ }
    this.nav = false; this.bombeando = false; this.finalizado = true; this.bloqueado = false;
    this._recalcular();
  }

  _recalcular() {
    const sonando = this.fuentes.size > 0 || this.nav;
    const pendiente = this.cola.length > 0 || !this.finalizado || this.bombeando;
    const nuevo = sonando ? 'hablando' : (pendiente ? 'cargando' : 'reposo');
    if (nuevo !== this.estado) { this.estado = nuevo; document.body.dataset.voz = nuevo; }
    this.alCambiar(nuevo);
  }
}

/* ═════════════════════════ Avatar: la boca sigue el nivel real del audio ═════════════════════════ */
const Avatar = (() => {
  // Borde inferior de los labios del gato (coordenadas de la imagen de 340×510)
  const LABIO = [[126.9, 215.9], [129, 216.6], [131.3, 216.9], [133.5, 216.4], [135.6, 215.7], [138, 214.2], [140, 212.7],
    [141.3, 212.4], [143, 213.9], [145, 216], [147.5, 217.9], [150, 219.5], [152.5, 221.6]];
  const lipY = x => {
    for (let i = 1; i < LABIO.length; i++) {
      if (x <= LABIO[i][0]) { const [xa, ya] = LABIO[i - 1], [xb, yb] = LABIO[i]; return ya + (yb - ya) * (x - xa) / (xb - xa); }
    }
    return LABIO[LABIO.length - 1][1];
  };
  let grupo, boca, recorte, lengua, h = 0, forzada = null, voz = null;

  // Línea base de la mandíbula: de la comisura izquierda (127,216) a la derecha (153,221,6), así la «U» no copia el zigzag del labio
  const base = x => 216 + (x - 127.4) / 25.2 * 5.6;
  const fondo = (x, alto) => {
    const y = lipY(x), b = Math.max(y, base(x));
    return y + (b - y) * Math.min(1, alto / 4);      // al cerrarse, el borde inferior se pega al labio (sin saltos)
  };
  function trazar(alto) {
    const x0 = 127.4, x1 = 152.6, N = 16, arriba = [], abajo = [];
    for (let i = 0; i <= N; i++) {
      const t = i / N, x = x0 + (x1 - x0) * t;
      const d = Math.sqrt(Math.max(0, 1 - Math.pow(2 * t - 1, 4)));   // forma de «U» con esquinas redondeadas
      arriba.push(`${x.toFixed(1)},${(lipY(x) - 1.8).toFixed(1)}`);
      abajo.push(`${x.toFixed(1)},${(fondo(x, alto) + alto * d).toFixed(1)}`);
    }
    return 'M' + arriba.join(' L') + ' L' + abajo.reverse().join(' L') + ' Z';
  }
  function dibujar(alto) {
    if (alto < 0.9) { grupo.setAttribute('opacity', '0'); return; }
    const d = trazar(alto);
    boca.setAttribute('d', d); recorte.setAttribute('d', d);
    grupo.setAttribute('opacity', '1');
    lengua.setAttribute('cy', (fondo(140.2, alto) + alto - 0.3 * alto - 1).toFixed(1));
    lengua.setAttribute('rx', Math.min(8.5, 3.6 + alto * 0.3).toFixed(1));
    lengua.setAttribute('ry', Math.max(0.8, alto * 0.22).toFixed(1));
    lengua.setAttribute('opacity', alto > 5 ? '.95' : '0');
  }
  function cuadro() {
    let objetivo = forzada != null ? forzada : Math.min(17, Math.max(0, (voz.nivel() - 0.018) * 110));
    h += (objetivo - h) * (objetivo > h ? 0.6 : 0.28);
    if (h < 0.15 && objetivo === 0) h = 0;
    dibujar(h);
    requestAnimationFrame(cuadro);
  }
  return {
    iniciar(v) {
      voz = v;
      grupo = $('#boca-abierta'); boca = $('#boca-d'); recorte = $('#c-boca-d'); lengua = $('#lengua');
      requestAnimationFrame(cuadro);
    },
    forzar(valor) { forzada = valor; },   // solo para pruebas visuales
  };
})();

/* ═════════════════════════ Micrófono: graba, pasa a WAV de 16 kHz y lo manda a transcribir ═════════════════════════ */
function reducir(datos, desde, hacia) {
  if (desde <= hacia) return datos;
  const razon = desde / hacia, n = Math.floor(datos.length / razon), sal = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const a = Math.floor(i * razon), b = Math.min(datos.length, Math.floor((i + 1) * razon));
    let s = 0; for (let j = a; j < b; j++) s += datos[j];
    sal[i] = s / Math.max(1, b - a);
  }
  return sal;
}
function codificarWav(muestras, tasa) {
  const buf = new ArrayBuffer(44 + muestras.length * 2), v = new DataView(buf);
  const txt = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  txt(0, 'RIFF'); v.setUint32(4, 36 + muestras.length * 2, true); txt(8, 'WAVE'); txt(12, 'fmt ');
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, tasa, true); v.setUint32(28, tasa * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  txt(36, 'data'); v.setUint32(40, muestras.length * 2, true);
  for (let i = 0; i < muestras.length; i++) {
    const x = Math.max(-1, Math.min(1, muestras[i]));
    v.setInt16(44 + i * 2, x < 0 ? x * 0x8000 : x * 0x7fff, true);
  }
  return new Blob([buf], { type: 'audio/wav' });
}
const Grabador = {
  activo: false, stream: null, ctx: null, proc: null, partes: [], t0: 0,
  async iniciar() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) throw new Error('sin-soporte');
    this.stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    const C = window.AudioContext || window.webkitAudioContext;
    this.ctx = new C();
    if (this.ctx.state === 'suspended') await this.ctx.resume();
    const fuente = this.ctx.createMediaStreamSource(this.stream);
    this.proc = this.ctx.createScriptProcessor(4096, 1, 1);
    this.partes = [];
    this.proc.onaudioprocess = e => { this.partes.push(new Float32Array(e.inputBuffer.getChannelData(0))); };
    const mudo = this.ctx.createGain(); mudo.gain.value = 0;     // el procesador debe estar conectado, pero sin sonar
    fuente.connect(this.proc); this.proc.connect(mudo); mudo.connect(this.ctx.destination);
    this.activo = true; this.t0 = Date.now();
  },
  segundos() { return this.activo ? (Date.now() - this.t0) / 1000 : 0; },
  _cerrar() {
    this.activo = false;
    try { this.proc.disconnect(); } catch (_) { /* nada */ }
    if (this.stream) this.stream.getTracks().forEach(t => t.stop());
    const tasa = this.ctx ? this.ctx.sampleRate : 48000;
    if (this.ctx) this.ctx.close().catch(() => {});
    return tasa;
  },
  detener() {
    const tasa = this._cerrar();
    const total = this.partes.reduce((n, p) => n + p.length, 0), todo = new Float32Array(total);
    let o = 0; for (const p of this.partes) { todo.set(p, o); o += p.length; }
    this.partes = [];
    return codificarWav(reducir(todo, tasa, 16000), Math.min(tasa, 16000));
  },
  cancelar() { this._cerrar(); this.partes = []; },
};

/* ═════════════════════════ Aplicación ═════════════════════════ */
const voz = new Voz(() => actualizarGlobo());
const ui = { pensando: false, grabando: false, transcribiendo: false, hecho: false };
let mensajes = [];       // historial: { role, content, voz, hora }
let ocupado = false;
let abortarChat = null;
let ultima = null;       // última consulta enviada (para «Reintentar»)
let aviso = null;        // tarjeta de aviso visible en el chat (confirmar / repetir audio)

const registro = $('#registro'), contMensajes = $('#mensajes'), entrada = $('#texto'), form = $('#form');
const btnEnviar = $('#btn-enviar'), sugerencias = $('#sugerencias');

function actualizarGlobo() {
  let t;
  if (ui.grabando) t = 'Te escucho… 🎧';
  else if (ui.transcribiendo) t = 'Entendiendo\ntu audio…';
  else if (voz.bloqueado) t = 'Toca aquí para\nactivar el sonido 🔊';
  else if (voz.estado === 'hablando') t = 'Te cuento… 🗣️';
  else if (ui.pensando) t = 'Estoy pensando… 🤔';
  else if (voz.estado === 'cargando') t = 'Preparando\nmi voz…';
  else t = ui.hecho ? '¿Qué más quieres\nconsultar? 😊' : '¡Hola! 😉\nEstoy aquí para ayudarte.';
  const nodo = $('#globo-texto');
  if (nodo.textContent !== t) nodo.textContent = t;
  $('#gato-panel').classList.toggle('habla', voz.estado === 'hablando');
}
voz.alFallar = () => {
  ui.hecho = false;
  anunciar('La voz del servidor no respondió: uso la voz del dispositivo.');
};
function anunciar(t) { const a = $('#anuncio'); a.textContent = ''; setTimeout(() => { a.textContent = t; }, 50); }

function alFondo(forzar) {
  const cerca = registro.scrollHeight - registro.scrollTop - registro.clientHeight < 140;
  if (forzar || cerca) registro.scrollTop = registro.scrollHeight;
}

/* ── Mensajes ── */
function pintarMensaje(m) {
  const fila = el('div', 'msg ' + (m.role === 'user' ? 'usuario' : 'asistente'));
  if (m.role !== 'user') { const a = el('span', 'avatar-mini'); a.setAttribute('aria-hidden', 'true'); fila.append(a); }
  const burbuja = el('div', 'burbuja');
  const cuerpo = el('div', 'texto-msg');
  const nota = el('span', 'nota-msg'); nota.hidden = true;
  const pie = el('div', 'pie-msg');
  pie.append(el('span', null, m.hora || hora()));
  if (m.role === 'user') pie.append(icono('i-doble'));
  else {
    const b = el('button', 'btn-eco');
    b.type = 'button'; b.title = 'Escuchar este mensaje'; b.setAttribute('aria-label', 'Escuchar este mensaje');
    b.append(icono('i-altavoz'));
    b.addEventListener('click', () => { if (m.content) hablarMensaje(m.content); });
    pie.append(b);
  }
  burbuja.append(cuerpo, nota, pie);
  fila.append(burbuja);
  contMensajes.append(fila);
  const api = {
    fila, burbuja,
    texto(t) { if (m.role === 'user') cuerpo.textContent = (m.voz ? '🎤 ' : '') + t; else cuerpo.innerHTML = md(t); },
    pensando() { cuerpo.innerHTML = '<span class="escribiendo" aria-label="Escribiendo"><i></i><i></i><i></i></span>'; pie.hidden = true; },
    listo() { pie.hidden = false; },
    nota(t) { nota.textContent = t; nota.hidden = !t; },
    cuerpo,
  };
  if (m.role === 'user') api.texto(m.content);
  return api;
}

function quitarAviso() { if (aviso) { aviso.remove(); aviso = null; } }
function pintarAviso(tipo, contenido, botones = []) {
  quitarAviso();
  const fila = el('div', 'msg asistente');
  const a = el('span', 'avatar-mini'); a.setAttribute('aria-hidden', 'true'); fila.append(a);
  const b = el('div', 'burbuja ' + tipo);
  b.append(...contenido);
  if (botones.length) {
    const cont = el('div', 'botones');
    for (const [texto, clase, fn] of botones) {
      const x = el('button', clase, texto); x.type = 'button'; x.addEventListener('click', fn); cont.append(x);
    }
    b.append(cont);
  }
  fila.append(b); contMensajes.append(fila); aviso = fila;
  alFondo(true);
}
const parrafo = (t, fuerte) => { const p = el('p'); if (fuerte) { const s = el('strong', null, fuerte); p.append(s, ' '); } p.append(t); return p; };

/* ── Un turno de chat ── */
async function enviar(texto, { voz: esVoz = false } = {}) {
  texto = (texto || '').trim();
  if (!texto || ocupado || !servidor.listo) return;
  voz.desbloquear();
  quitarAviso();
  ocupado = true; btnEnviar.disabled = true;
  sugerencias.hidden = true;

  const enmascarado = cfg.pii ? mascarar(texto) : { texto, dni: 0, tel: 0 };
  const pregunta = enmascarado.texto;
  ultima = { texto: pregunta, voz: esVoz };
  const mu = { role: 'user', content: pregunta, voz: esVoz, hora: hora() };
  const U = pintarMensaje(mu);
  if (enmascarado.dni || enmascarado.tel) U.nota(`🔒 Filtro de privacidad: ${enmascarado.dni} DNI y ${enmascarado.tel} teléfono(s) ocultados antes de enviar.`);
  const ma = { role: 'assistant', content: '', hora: hora() };
  const A = pintarMensaje(ma);
  A.pensando();
  alFondo(true);

  if (cfg.leer) voz.nuevo({ voz: cfg.voz, velocidad: servidor.velocidades[cfg.vel] || 1, pii: cfg.pii });
  else voz.detener();
  ui.pensando = true; ui.hecho = false; actualizarGlobo();

  abortarChat = new AbortController();
  let respuesta = '', error = null, modelo = '', principal = '';
  try {
    const r = await fetch('/api/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: abortarChat.signal,
      body: JSON.stringify({ pregunta, pii: cfg.pii, historial: mensajes.slice(-6).map(({ role, content }) => ({ role, content })) }),
    });
    if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.error || ('Error ' + r.status)); }
    const lector = r.body.getReader(), dec = new TextDecoder();
    let pend = '';
    for (;;) {
      const { value, done } = await lector.read();
      if (done) break;
      pend += dec.decode(value, { stream: true });
      let i;
      while ((i = pend.indexOf('\n\n')) >= 0) {
        const bloque = pend.slice(0, i); pend = pend.slice(i + 2);
        const linea = bloque.split('\n').find(x => x.startsWith('data:'));
        if (!linea) continue;
        const ev = JSON.parse(linea.slice(5));
        if (ev.t === 'd') {
          if (!respuesta) { ui.pensando = false; A.listo(); }
          respuesta += ev.x;
          A.texto(respuesta); alFondo(false);
          if (cfg.leer) voz.alimentar(ev.x);
        } else if (ev.t === 'fin') { modelo = ev.modelo; principal = ev.principal; }
        else if (ev.t === 'error') { error = new Error(ev.mensaje); }
      }
    }
  } catch (e) {
    if (e.name === 'AbortError') { ocupado = false; btnEnviar.disabled = false; ui.pensando = false; return; }
    error = e;
  }

  ui.pensando = false; abortarChat = null;
  respuesta = respuesta.trim();
  if (cfg.leer) { if (error && !respuesta) voz.detener(); else voz.finalizar(); }

  if (error && !respuesta) {                       // nada que mostrar: error claro y reintento con un clic
    A.fila.remove(); U.fila.remove();
    const msg = error.message && !/^(TypeError|Failed to fetch|NetworkError)/i.test(error.message) ? error.message : 'No se pudo conectar con el servidor. Revisa tu conexión e inténtalo de nuevo.';
    pintarAviso('error', [parrafo(msg, 'No pude responder.'), parrafo('Tu pregunta no se perdió: «' + pregunta + '»')],
      [['Reintentar', 'btn-sec', () => { const u = ultima; quitarAviso(); enviar(u.texto, { voz: u.voz }); }]]);
    const bt = $('.botones .btn-sec', aviso); if (bt) bt.prepend(icono('i-reintentar'));
  } else {
    ma.content = respuesta;
    mensajes.push(mu, ma);
    A.listo(); A.texto(respuesta);
    if (error) A.nota('⚠️ La respuesta se interrumpió antes de terminar. ' + error.message);
    else if (modelo && principal && modelo !== principal) A.nota(`ℹ️ Respondió ${modelo}: el modelo principal estaba saturado o no disponible.`);
    ui.hecho = !error;
    anunciar('Respuesta lista.');
  }
  ocupado = false; btnEnviar.disabled = !servidor.listo;
  actualizarGlobo(); alFondo(true);
  if (!matchMedia('(pointer: coarse)').matches) entrada.focus();
}

function hablarMensaje(texto) {
  voz.nuevo({ voz: cfg.voz, velocidad: servidor.velocidades[cfg.vel] || 1, pii: cfg.pii });
  voz.alimentar(texto + '\n');
  voz.finalizar();
}
function ultimaRespuesta() {
  for (let i = mensajes.length - 1; i >= 0; i--) if (mensajes[i].role === 'assistant' && mensajes[i].content) return mensajes[i].content;
  return SALUDO_VOZ;
}

/* ── Micrófono ── */
async function alternarMicrofono() {
  if (Grabador.activo) return terminarGrabacion();
  voz.detener(); voz.desbloquear(); quitarAviso();
  mostrarVista('chat');
  try { await Grabador.iniciar(); }
  catch (e) {
    const sin = e && e.message === 'sin-soporte';
    pintarAviso('aviso', [parrafo(sin
      ? 'Este navegador no permite grabar. Prueba con Chrome, Edge o Safari actualizados, y abre la página con https o desde localhost.'
      : 'Revisa el permiso del micrófono en tu navegador y vuelve a intentarlo. También puedes escribir tu consulta.', 'No pude acceder al micrófono.')]);
    return;
  }
  ui.grabando = true; fijarGrabacion(true); actualizarGlobo();
  const t = setInterval(() => {
    if (!Grabador.activo) return clearInterval(t);
    const s = Math.floor(Grabador.segundos());
    $('#rec-tiempo').textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
    if (s >= servidor.audio_max_s - 2) terminarGrabacion();
  }, 250);
}
function fijarGrabacion(on) {
  $('#grabando').hidden = !on;
  document.body.classList.toggle('hay-grabacion', on);
  for (const id of ['#btn-mic', '#btn-hablar']) $(id).setAttribute('aria-pressed', String(on));
  if (on) $('#rec-tiempo').textContent = '0:00';
}
function cancelarGrabacion() {
  if (Grabador.activo) Grabador.cancelar();
  ui.grabando = false; fijarGrabacion(false); actualizarGlobo();
}
async function terminarGrabacion() {
  if (!Grabador.activo) return;
  const wav = Grabador.detener();
  ui.grabando = false; ui.transcribiendo = true; fijarGrabacion(false); actualizarGlobo();
  let r;
  try {
    const resp = await fetch('/api/transcribir?pii=' + (cfg.pii ? '1' : '0'), { method: 'POST', headers: { 'Content-Type': 'audio/wav' }, body: wav });
    r = await resp.json();
    if (!resp.ok) r = { estado: 'error', motivo: r.error || 'No se pudo transcribir tu audio.' };
  } catch (_) {
    r = { estado: 'error', motivo: 'No se pudo conectar con el servidor para transcribir tu audio.' };
  }
  ui.transcribiendo = false; actualizarGlobo();
  if (r.estado === 'ok') return enviar(r.texto, { voz: true });
  if (r.estado === 'dudosa') {
    return pintarAviso('aviso', [
      parrafo('«' + r.texto + '»', 'Creo que dijiste:'),
      parrafo('No estoy del todo seguro de haberte entendido bien. ¿Es correcto?'),
    ], [
      ['Sí, responder', 'btn-pri', () => { quitarAviso(); enviar(r.texto, { voz: true }); }],
      ['No, grabaré de nuevo', 'btn-sec', () => quitarAviso()],
    ]);
  }
  const partes = [parrafo(r.estado === 'error' ? r.motivo : 'Prefiero no adivinar tu pregunta. ¿Puedes grabarlo de nuevo? Acércate al micrófono, habla con calma y, si puedes, busca un lugar con menos ruido. También puedes escribir tu consulta.',
    r.estado === 'error' ? 'No pude transcribir tu audio.' : 'No logré entender bien tu audio.')];
  if (r.estado !== 'error' && r.oido) partes.push(parrafo(`Lo que alcancé a captar: «${r.oido}» (no es suficiente para responder con seguridad).`));
  if (r.estado !== 'error' && r.motivo) partes.push(parrafo(r.motivo, 'Motivo:'));
  pintarAviso('aviso', partes);
}

/* ── Vistas y menú ── */
function mostrarVista(nombre) {
  $$('.vista').forEach(v => { const on = v.id === 'v-' + nombre; v.classList.toggle('activa', on); v.hidden = !on; });
  $$('.item[data-vista]').forEach(b => {
    const on = b.dataset.vista === nombre; b.classList.toggle('activo', on);
    if (on) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
  });
  cerrarMenu();
}
const lateral = $('#lateral'), velo = $('#velo'), btnMenu = $('#btn-menu'), mq = matchMedia('(max-width: 900px)');
function sincronizarMenu() {
  const abierto = lateral.classList.contains('abierto');
  if (mq.matches && !abierto) lateral.setAttribute('inert', ''); else lateral.removeAttribute('inert');
}
function abrirMenu() { lateral.classList.add('abierto'); velo.hidden = false; btnMenu.setAttribute('aria-expanded', 'true'); sincronizarMenu(); }
function cerrarMenu() { lateral.classList.remove('abierto'); velo.hidden = true; btnMenu.setAttribute('aria-expanded', 'false'); sincronizarMenu(); }

function nuevaConversacion() {
  if (abortarChat) abortarChat.abort();
  cancelarGrabacion(); voz.detener(); quitarAviso();
  mensajes = []; contMensajes.replaceChildren(); ocupado = false; btnEnviar.disabled = !servidor.listo;
  ui.pensando = ui.transcribiendo = ui.hecho = false; ultima = null;
  sugerencias.hidden = false; actualizarGlobo();
  mostrarVista('chat'); registro.scrollTop = 0;
}

/* ── Configuración ── */
function poblarConfig() {
  const sel = $('#c-voz'); sel.replaceChildren();
  if (servidor.voz_servidor !== false) for (const v of servidor.voces) sel.add(new Option(v, v));
  sel.add(new Option('Voz del dispositivo (instantánea)', NAVEGADOR));
  const vel = $('#c-vel'); vel.replaceChildren();
  for (const k of Object.keys(servidor.velocidades)) vel.add(new Option(k, k));
  const g = leerCfgGuardada();
  cfg.pii = typeof g.pii === 'boolean' ? g.pii : servidor.pii_inicial !== false;
  cfg.leer = typeof g.leer === 'boolean' ? g.leer : servidor.leer_inicial !== false;
  cfg.voz = [...sel.options].some(o => o.value === g.voz) ? g.voz
    : (servidor.voz_servidor === false ? NAVEGADOR : servidor.voz_por_defecto);
  cfg.vel = g.vel in servidor.velocidades ? g.vel : servidor.velocidad_inicial;
  $('#c-pii').checked = cfg.pii; $('#c-leer').checked = cfg.leer; sel.value = cfg.voz; vel.value = cfg.vel;
  $('#c-pii').addEventListener('change', e => { cfg.pii = e.target.checked; guardarCfg(); });
  $('#c-leer').addEventListener('change', e => { cfg.leer = e.target.checked; guardarCfg(); if (!cfg.leer) voz.detener(); });
  sel.addEventListener('change', e => { cfg.voz = e.target.value; voz.motorFalla = false; guardarCfg(); });
  vel.addEventListener('change', e => { cfg.vel = e.target.value; guardarCfg(); });
}

/* ═════════════════════════ Arranque ═════════════════════════ */
async function iniciar() {
  Avatar.iniciar(voz);
  EJEMPLOS.forEach(q => {
    const b = el('button', 'chip', q); b.type = 'button';
    b.addEventListener('click', () => enviar(q));
    sugerencias.append(b);
  });

  try {
    const r = await fetch('/api/config');
    servidor = Object.assign(servidor, await r.json());
  } catch (_) {
    servidor.listo = false;
    servidor.problemas = ['No se pudo conectar con el servidor. ¿Está en marcha «python servidor.py»?'];
  }
  poblarConfig();

  const d = servidor.documento || {};
  $('#doc-nombre').textContent = d.nombre || 'HERRAMIENTAS_TIC_TRABAJO_SOCIAL.pdf';
  $('#d-nombre').textContent = d.nombre || '—';
  $('#d-estado').textContent = d.ok ? `Cargado · ${d.paginas} páginas` : (d.mensaje || 'No cargado');

  if (!servidor.listo) {
    entrada.disabled = true; btnEnviar.disabled = true; $$('.chip').forEach(c => { c.disabled = true; });
    pintarAviso('error', servidor.problemas.map(p => parrafo(p)));
  } else if (d && d.ok === false) {
    pintarAviso('aviso', [parrafo(d.mensaje, 'Sin documento base.')]);
  }

  /* Los navegadores solo dan sonido tras un gesto: cualquier toque o tecla abre el audio. */
  ['pointerdown', 'keydown', 'touchend'].forEach(ev => document.addEventListener(ev, () => voz.desbloquear(), { capture: true, passive: true }));

  form.addEventListener('submit', e => { e.preventDefault(); const t = entrada.value; entrada.value = ''; enviar(t); });
  $('#btn-mic').addEventListener('click', alternarMicrofono);
  $('#btn-hablar').addEventListener('click', alternarMicrofono);
  $('#rec-enviar').addEventListener('click', terminarGrabacion);
  $('#rec-cancelar').addEventListener('click', cancelarGrabacion);
  $('#btn-escuchar').addEventListener('click', () => { mostrarVista('chat'); hablarMensaje(ultimaRespuesta()); });
  $('#btn-parar').addEventListener('click', () => voz.detener());
  $('#gato').addEventListener('click', () => {
    if (voz.estado !== 'reposo' && !voz.bloqueado) voz.detener(); else hablarMensaje(ultimaRespuesta());
  });
  $('#btn-probar').addEventListener('click', () => hablarMensaje('Hola, soy tu asistente de protocolos TIC. Así suena mi voz con esta configuración.'));
  $('#btn-nueva').addEventListener('click', nuevaConversacion);
  $$('.item[data-vista]').forEach(b => b.addEventListener('click', () => mostrarVista(b.dataset.vista)));
  btnMenu.addEventListener('click', () => (lateral.classList.contains('abierto') ? cerrarMenu() : abrirMenu()));
  velo.addEventListener('click', cerrarMenu);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') { cerrarMenu(); if (Grabador.activo) cancelarGrabacion(); } });
  mq.addEventListener('change', () => { cerrarMenu(); });
  window.addEventListener('beforeunload', () => { voz.detener(); if (Grabador.activo) Grabador.cancelar(); });
  window.speechSynthesis && window.speechSynthesis.getVoices();   // algunos navegadores cargan las voces con retraso

  sincronizarMenu(); actualizarGlobo();
  if (!matchMedia('(pointer: coarse)').matches && servidor.listo) entrada.focus();
  window.__tic = { voz, avatar: Avatar, cfg, estado: () => ({ mensajes, ocupado }) };   // utilidades para pruebas
}

iniciar();
})();
