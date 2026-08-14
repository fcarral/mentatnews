/* MentatNews — comportamiento de la interfaz. Vanilla JS, sin dependencias. */
'use strict';

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const CST_OFFSET_MS = -6 * 3600 * 1000;   // CST fijo, sin horario de verano

// ── Utilidades ────────────────────────────────────────────────────────

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/** Fecha desplazada a CST para poder leer sus componentes en UTC. */
function enCST(iso) { return new Date(new Date(iso).getTime() + CST_OFFSET_MS); }
function hoyCST() { return new Date(Date.now() + CST_OFFSET_MS); }
const dosDig = n => String(n).padStart(2, '0');
const MESES = ['ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic'];

function fmtHora(iso) {
  if (!iso) return '';
  const d = enCST(iso), hoy = hoyCST();
  if (d.toISOString().slice(0, 10) === hoy.toISOString().slice(0, 10))
    return `${dosDig(d.getUTCHours())}:${dosDig(d.getUTCMinutes())}`;
  const anio = d.getUTCFullYear() === hoy.getUTCFullYear() ? '' : ` ${d.getUTCFullYear()}`;
  return `${d.getUTCDate()} ${MESES[d.getUTCMonth()]}${anio}`;
}

function fmtCompleta(iso) {
  if (!iso) return '';
  const d = enCST(iso);
  return `${dosDig(d.getUTCDate())}/${dosDig(d.getUTCMonth() + 1)}/${d.getUTCFullYear()} ` +
         `${dosDig(d.getUTCHours())}:${dosDig(d.getUTCMinutes())} CST`;
}

/** Etiqueta del separador de día: Hoy, Ayer, o la fecha. */
function etiquetaDia(iso) {
  if (!iso) return 'Sin fecha';
  const d = enCST(iso), hoy = hoyCST();
  const dia = d.toISOString().slice(0, 10);
  const hoyStr = hoy.toISOString().slice(0, 10);
  const ayer = new Date(hoy.getTime() - 86400000).toISOString().slice(0, 10);
  if (dia === hoyStr) return 'Hoy';
  if (dia === ayer) return 'Ayer';
  const anio = d.getUTCFullYear() === hoy.getUTCFullYear() ? '' : ` de ${d.getUTCFullYear()}`;
  return `${d.getUTCDate()} de ${['enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre'][d.getUTCMonth()]}${anio}`;
}
const claveDia = iso => iso ? enCST(iso).toISOString().slice(0, 10) : 'sin-fecha';

let toastTimer;
function toast(msg, accion) {
  const t = $('#toast');
  t.textContent = msg;
  if (accion) {
    const b = document.createElement('button');
    b.className = 'toast-action';
    b.textContent = accion.texto;
    b.addEventListener('click', () => { t.hidden = true; accion.fn(); });
    t.appendChild(b);
  }
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, accion ? 9000 : 3400);
}

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: opts.body ? { 'Content-Type': 'application/json' } : {},
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { const j = await r.json(); msg = j.detail || j.error || j.warning || msg; } catch {}
    throw new Error(msg);
  }
  return r.json();
}

// ── Estado ────────────────────────────────────────────────────────────

const state = {
  view: { type: 'unread' },
  articulos: [],
  cursor: null,
  fin: false,
  cargando: false,
  generacion: 0,        // token que invalida las cargas de una vista abandonada
  peticion: null,       // AbortController de la carga en curso
  actual: null,
  verFull: false,
  enHistorial: false,   // el lector metió una entrada en el historial
  feeds: [],
  folders: [],
  ultimoDia: null,
};

const prefs = {
  densidad: localStorage.getItem('mn_densidad') || 'comoda',
  escala: +(localStorage.getItem('mn_escala') || 100),
  tam: +(localStorage.getItem('mn_tam') || 16),
  ancho: +(localStorage.getItem('mn_ancho') || 68),
  familia: localStorage.getItem('mn_familia') || 'sans',
  scrollLeido: localStorage.getItem('mn_scroll_leido') === '1',
};

function aplicarPrefs() {
  document.body.dataset.densidad = prefs.densidad;
  const raiz = document.documentElement.style;
  raiz.setProperty('--escala', (prefs.escala / 100).toFixed(2));
  raiz.setProperty('--lectura-tam', prefs.tam + 'px');
  raiz.setProperty('--lectura-ancho', prefs.ancho + 'ch');
  raiz.setProperty('--lectura-fam', prefs.familia === 'serif'
    ? 'Georgia, "Times New Roman", serif' : "'IBM Plex Sans', system-ui, sans-serif");
  $$('.dens-btn').forEach(b => b.classList.toggle('activa', b.dataset.densidad === prefs.densidad));
  // Otra letra cambia las alturas: los breves puede que ya no vayan donde iban.
  const portada = $('#portada');
  if (portada && !portada.hidden) equilibrarPortada(portada);
}
function guardarPrefs() {
  localStorage.setItem('mn_densidad', prefs.densidad);
  localStorage.setItem('mn_escala', prefs.escala);
  localStorage.setItem('mn_tam', prefs.tam);
  localStorage.setItem('mn_ancho', prefs.ancho);
  localStorage.setItem('mn_familia', prefs.familia);
  localStorage.setItem('mn_scroll_leido', prefs.scrollLeido ? '1' : '0');
}

// ── Fuentes (barra lateral) ───────────────────────────────────────────

// Con nueve temas y casi noventa fuentes, el árbol abierto de par en par no se
// puede recorrer. La primera vez se pliega todo; a partir de ahí manda lo que
// el usuario haya dejado abierto.
const carpetasCerradas = new Set(JSON.parse(localStorage.getItem('mn_cerradas') || '[]'));
let plegadoInicialPendiente = localStorage.getItem('mn_cerradas') === null;

function iconoFeed(f) {
  const src = f.favicon_url || '/static/logo-mentat.svg';
  return `<img class="favicon" src="${esc(src)}" alt="" loading="lazy" ` +
         `onerror="this.src='/static/logo-mentat.svg'">`;
}

async function cargarLateral() {
  const [fd, st] = await Promise.all([api('/api/feeds'), api('/api/stats')]);
  state.feeds = fd.feeds; state.folders = fd.folders;
  $('#badge-unread').textContent = st.unread || '';
  $('#badge-today').textContent = st.today || '';
  $('#badge-saved').textContent = st.saved || '';
  $('#badge-alertas').textContent = st.alertas || '';
  $('#side-foot').innerHTML =
    `${st.feeds} fuentes · ${st.articles} artículos` +
    (st.feeds_error ? `<br><span style="color:var(--accent)">${st.feeds_error} con error</span>` : '');

  if (plegadoInicialPendiente && state.folders.length > 4) {
    state.folders.forEach(f => carpetasCerradas.add(f.id));
    localStorage.setItem('mn_cerradas', JSON.stringify([...carpetasCerradas]));
    plegadoInicialPendiente = false;
  }

  const arbol = $('#tree');
  arbol.innerHTML = '';
  const porCarpeta = new Map();
  for (const f of state.feeds) {
    const k = f.folder_id ?? 0;
    if (!porCarpeta.has(k)) porCarpeta.set(k, []);
    porCarpeta.get(k).push(f);
  }

  const filaFeed = f => {
    const b = document.createElement('button');
    b.className = 'side-item';
    b.dataset.view = 'feed'; b.dataset.id = f.id;
    b.innerHTML = iconoFeed(f) +
      `<span class="si-label">${esc(f.title || f.url)}</span>` +
      (f.last_status === 'error' ? '<span class="err-dot" title="error al descargar"></span>' : '') +
      `<span class="badge">${f.unread || ''}</span>`;
    b.addEventListener('click', () =>
      irA({ type: 'feed', id: f.id, title: f.title || f.url }));
    return b;
  };

  for (const carpeta of state.folders) {
    const feeds = porCarpeta.get(carpeta.id) || [];
    if (!feeds.length) continue;
    const sinLeer = feeds.reduce((n, f) => n + (f.unread || 0), 0);
    const cerrada = carpetasCerradas.has(carpeta.id);

    const cab = document.createElement('button');
    cab.className = 'folder-head' + (cerrada ? ' cerrada' : '');
    cab.setAttribute('aria-expanded', String(!cerrada));
    cab.innerHTML = `<span class="tw" aria-hidden="true">▾</span>` +
      `<span class="si-label">${esc(carpeta.name)}</span>` +
      `<span class="badge">${sinLeer || ''}</span>`;
    const caja = document.createElement('div');
    caja.className = 'folder-feeds' + (cerrada ? ' oculta' : '');
    feeds.forEach(f => caja.appendChild(filaFeed(f)));

    cab.addEventListener('click', e => {
      // El contador abre el tema; el resto de la cabecera pliega
      if (e.target.closest('.badge')) {
        irA({ type: 'folder', id: carpeta.id, title: carpeta.name });
        return;
      }
      const ahoraCerrada = !cab.classList.contains('cerrada');
      cab.classList.toggle('cerrada', ahoraCerrada);
      caja.classList.toggle('oculta', ahoraCerrada);
      cab.setAttribute('aria-expanded', String(!ahoraCerrada));
      ahoraCerrada ? carpetasCerradas.add(carpeta.id) : carpetasCerradas.delete(carpeta.id);
      localStorage.setItem('mn_cerradas', JSON.stringify([...carpetasCerradas]));
    });
    cab.addEventListener('dblclick', () =>
      irA({ type: 'folder', id: carpeta.id, title: carpeta.name }));

    arbol.appendChild(cab); arbol.appendChild(caja);
  }
  (porCarpeta.get(0) || []).forEach(f => arbol.appendChild(filaFeed(f)));

  marcarActiva();
  pintarFeedsIA().catch(() => {});
  $('#folder-list').innerHTML = state.folders.map(f => `<option value="${esc(f.name)}">`).join('');
}

/** Refresca solo los contadores, sin reconstruir el árbol (evita saltos de scroll). */
async function refrescarContadores() {
  try {
    const st = await api('/api/stats');
    $('#badge-unread').textContent = st.unread || '';
    $('#badge-today').textContent = st.today || '';
    $('#badge-saved').textContent = st.saved || '';
    $('#badge-alertas').textContent = st.alertas || '';
  } catch {}
}

function marcarActiva() {
  $$('.side-item').forEach(el => {
    const v = el.dataset.view;
    const on = v === state.view.type &&
      (!el.dataset.id || Number(el.dataset.id) === state.view.id);
    el.classList.toggle('activa', on);
  });
  $$('.tab-btn').forEach(el =>
    el.classList.toggle('activa', el.dataset.view === state.view.type));
}

// ── Vistas y lista ────────────────────────────────────────────────────

function consultaVista() {
  const v = state.view, p = new URLSearchParams();
  if (v.type === 'alertas') p.set('alertas', 1);
  if (v.type === 'aifeed') p.set('ai_feed', v.id);
  if (v.type === 'unread') p.set('unread', 1);
  if (v.type === 'today') p.set('today', 1);
  if (v.type === 'saved') p.set('saved', 1);
  if (v.type === 'feed') p.set('feed_id', v.id);
  if (v.type === 'folder') p.set('folder_id', v.id);
  if (v.type === 'search') p.set('q', v.q);
  if (v.type === 'silenciados') { p.set('incluir_silenciados', 1); p.set('solo_silenciados', 1); }
  if (state.cursor) p.set('before_id', state.cursor);
  p.set('limit', 50);
  return p;
}

function tituloVista() {
  const v = state.view;
  return { unread: 'Sin leer', today: 'Hoy', all: 'Todo', saved: 'Guardados',
           portada: 'Portada', alertas: 'Alertas' }[v.type]
    || (v.type === 'search' ? `Búsqueda: ${v.q}` : v.title || '');
}

function esqueletos(n = 6) {
  return Array.from({ length: n }, () =>
    `<div class="esqueleto"><div class="esq-linea corta"></div>` +
    `<div class="esq-linea media"></div><div class="esq-linea larga"></div></div>`).join('');
}

async function irA(v) {
  state.generacion++;
  if (state.peticion) state.peticion.abort();
  state.view = v;
  state.articulos = []; state.cursor = null; state.fin = false;
  state.cargando = false; state.ultimoDia = null;

  $('#lista-titulo').textContent = tituloVista();
  $('#lista').innerHTML = esqueletos();
  $('#lista').setAttribute('aria-busy', 'true');
  $('#lista-fin').hidden = true;
  $('#btn-feed-edit').hidden = v.type !== 'feed';
  $('#btn-mark-read').hidden = v.type === 'portada';
  cerrarLateralMovil();
  marcarActiva();
  cerrarLector();
  $('#panel-lista').scrollTop = 0;

  // La Portada vive en los temas: en una carpeta va encabezando su lista.
  const portada = $('#portada');
  portada.hidden = true; portada.innerHTML = '';
  $('#portada-temas').hidden = true;

  if (v.type === 'portada') {
    $('#lista').innerHTML = '';
    $('#lista').setAttribute('aria-busy', 'false');
    await portadaConSelector();
    return;
  }

  const mia = state.generacion;
  await cargarMas();
  if (mia !== state.generacion) return;

  if (v.type === 'folder') pintarPortada(v.id, v.title);
}

async function cargarMas() {
  if (state.cargando || state.fin) return;
  const mia = state.generacion;
  state.cargando = true;
  state.peticion = new AbortController();
  try {
    const r = await fetch('/api/articles?' + consultaVista(), { signal: state.peticion.signal });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const { articles } = await r.json();
    if (mia !== state.generacion) return;   // cambiaron de vista mientras cargaba

    if (state.articulos.length === 0) $('#lista').innerHTML = '';
    if (articles.length < 50) { state.fin = true; }
    if (articles.length) state.cursor = articles[articles.length - 1].id;
    state.articulos.push(...articles);

    const caja = $('#lista');
    for (const a of articles) {
      const dia = claveDia(a.published_at || a.fetched_at);
      if (dia !== state.ultimoDia) {
        state.ultimoDia = dia;
        const sep = document.createElement('div');
        sep.className = 'dia-sep';
        sep.textContent = etiquetaDia(a.published_at || a.fetched_at);
        caja.appendChild(sep);
      }
      caja.appendChild(pintarFila(a));
    }
    if (!state.articulos.length) pintarVacio();
    $('#lista-fin').hidden = !state.fin || !state.articulos.length;
  } catch (e) {
    if (e.name === 'AbortError') return;
    if (mia !== state.generacion) return;
    $('#lista').innerHTML =
      `<div class="vacio"><h2>No se pudo cargar la lista</h2><p>${esc(e.message)}. ` +
      `Vuelve a intentarlo con el botón de refrescar.</p></div>`;
  } finally {
    if (mia === state.generacion) {
      state.cargando = false;
      $('#lista').setAttribute('aria-busy', 'false');
    }
  }
}

function pintarVacio() {
  const v = state.view;
  const textos = {
    unread: ['Todo leído', 'No te queda nada pendiente. Vuelve más tarde o entra a Todo para releer.'],
    today: ['Nada nuevo hoy', 'Tus fuentes no han publicado nada en el día. Prueba con Sin leer.'],
    saved: ['Aún no guardas nada', 'Pulsa la estrella en cualquier artículo y aparecerá aquí.'],
    search: ['Sin resultados', 'No hay artículos que coincidan con esa búsqueda.'],
  };
  const [tit, txt] = textos[v.type] || ['Aquí no hay nada', 'Esta vista está vacía por ahora.'];
  $('#lista').innerHTML =
    `<div class="vacio"><div class="vacio-marca" aria-hidden="true">✓</div>` +
    `<h2>${esc(tit)}</h2><p>${esc(txt)}</p></div>`;
}

function extracto(html) {
  const d = document.createElement('div');
  d.innerHTML = html || '';
  return d.textContent.trim();
}

function pintarFila(a) {
  const el = document.createElement('article');
  el.className = 'arow' + (a.read ? ' leido' : '');
  el.dataset.id = a.id;
  el.tabIndex = 0;
  el.setAttribute('role', 'article');
  const img = a.image_url
    ? `<img class="a-img" src="${esc(a.image_url)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">` : '';
  el.innerHTML =
    img + iconoFeed(a) +
    `<span class="a-src"><span class="fname">${esc(a.feed_title || '')}</span>` +
      (a.saved ? '<span class="a-estrella" aria-label="Guardado">★</span>' : '') +
      (a.duplicados ? `<span class="a-mas">+${a.duplicados}</span>` : '') +
    `</span>` +
    `<span class="a-hora">${fmtHora(a.published_at || a.fetched_at)}</span>` +
    `<h2 class="a-titulo">${esc(a.title)}</h2>` +
    `<p class="a-resumen">${esc((a.summary_limpio ?? extracto(a.summary)).slice(0, 240))}</p>`;
  el.addEventListener('click', () => abrirArticulo(a.id));
  el.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); abrirArticulo(a.id); }
  });
  return el;
}

$('#panel-lista').addEventListener('scroll', e => {
  const el = e.target;
  if (el.scrollTop + el.clientHeight > el.scrollHeight - 700) cargarMas();
  if (prefs.scrollLeido) marcarLeidosAlPasar();
});

/** Marca leído lo que ya quedó por encima del borde superior. */
let pendientesScroll = new Set(), timerScroll;
function marcarLeidosAlPasar() {
  const panel = $('#panel-lista');
  const borde = panel.getBoundingClientRect().top + 60;
  for (const fila of $$('.arow:not(.leido)', panel)) {
    if (fila.getBoundingClientRect().bottom < borde) {
      const id = Number(fila.dataset.id);
      fila.classList.add('leido');
      pendientesScroll.add(id);
      const a = state.articulos.find(x => x.id === id);
      if (a) a.read = 1;
    }
  }
  clearTimeout(timerScroll);
  timerScroll = setTimeout(async () => {
    if (!pendientesScroll.size) return;
    const ids = [...pendientesScroll]; pendientesScroll.clear();
    await Promise.all(ids.map(id =>
      api(`/api/articles/${id}`, { method: 'PATCH', body: { read: true } }).catch(() => {})));
    refrescarContadores();
  }, 700);
}

// ── La Portada ────────────────────────────────────────────────────────

async function pintarPortada(folderId, tema, { anexar = false } = {}) {
  const caja = $('#portada');
  const mia = state.generacion;
  caja.hidden = false;
  if (!anexar) {
    caja.innerHTML = `<div class="portada-cargando">` +
      `<span class="mono">Armando la portada de ${esc(tema)}…</span></div>`;
  }
  try {
    const r = await api(`/api/portada?folder_id=${folderId}`);
    if (mia !== state.generacion) return;
    const html = htmlPortada(r, tema);
    if (anexar) caja.insertAdjacentHTML('beforeend', html);
    else caja.innerHTML = html;
    engancharPortada(caja);
  } catch (e) {
    if (mia !== state.generacion) return;
    if (anexar) return;
    caja.hidden = true;
  }
}

/** Sin imagen, la nota principal se apoya en un extracto del propio artículo. */
function extractoPortada(a) {
  const t = extracto(a.summary || '');
  return t.length > 60 ? t.slice(0, 260).trim() + (t.length > 260 ? '…' : '') : '';
}

function htmlPortada(r, tema) {
  const p = r.portada || {};
  const arts = r.articulos || {};
  if (!p.principal || !arts[p.principal.id]) return '';

  const art = id => arts[id];
  const fuente = a => `${esc(a.feed_title || '')} · ${fmtHora(a.published_at || a.fetched_at)}`;
  const pr = art(p.principal.id);

  const secundarias = (p.secundarias || []).filter(s => art(s.id)).map(s => {
    const a = art(s.id);
    return `<button class="pp-sec" data-id="${a.id}">
      <div class="pp-sec-titulo">${esc(a.title)}</div>
      ${s.motivo ? `<div class="pp-sec-motivo">${esc(s.motivo)}</div>` : ''}
      <div class="pp-fuente">${fuente(a)}</div>
    </button>`;
  }).join('');

  const breves = (p.breves || []).filter(id => art(id)).map(id => {
    const a = art(id);
    return `<button class="pp-breve" data-id="${a.id}">
      <span class="punto" aria-hidden="true">▸</span>
      <span class="pp-breve-txt">
        <span class="pp-breve-tit">${esc(a.title)}</span>
        <span class="fuente">${esc(a.feed_title || '')}</span>
      </span>
    </button>`;
  }).join('');

  const hoy = hoyCST();
  const fecha = `${hoy.getUTCDate()} ${MESES[hoy.getUTCMonth()]} · ${dosDig(hoy.getUTCHours())}:${dosDig(hoy.getUTCMinutes())} CST`;

  // Orden de prensa: primero el titular, después la foto que lo ilustra. Al
  // revés la imagen se come la jerarquía y el titular queda de pie de foto.
  return `
  <div class="portada-bloque">
    <div class="portada-cintillo">
      <span class="portada-kicker">Portada</span>
      <span class="portada-tema">${esc(tema)}</span>
      <span class="portada-fecha mono">${fecha}</span>
    </div>
    ${p.resumen ? `<p class="portada-resumen">${esc(p.resumen)}</p>` : ''}
    <div class="portada-rejilla">
      <button class="pp-principal" data-id="${pr.id}">
        <h2 class="pp-titulo">${esc(pr.title)}</h2>
        ${/* Una sola entradilla. Cuando la principal no traía foto salían el
              motivo (en español, de la edición) y el extracto del feed (a
              menudo en inglés) diciendo lo mismo, uno debajo del otro. */
          p.principal.motivo
            ? `<p class="pp-motivo">${esc(p.principal.motivo)}</p>`
            : (extractoPortada(pr) ? `<p class="pp-extracto">${esc(extractoPortada(pr))}</p>` : '')}
        ${pr.image_url
          ? `<img class="pp-img" src="${esc(pr.image_url)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">`
          : ''}
        <div class="pp-fuente">${fuente(pr)}</div>
      </button>
      ${secundarias ? `<div class="pp-secundarias">
        <div class="pp-rotulo">Lo siguiente</div>${secundarias}</div>` : ''}
      ${breves ? `<div class="pp-breves">
        <div class="pp-rotulo">También</div>
        <div class="pp-breves-lista">${breves}</div>
      </div>` : ''}
    </div>
    ${p.modo === 'cronologica'
      ? `<div class="portada-pie">Orden cronológico: la edición automática no estuvo disponible.</div>` : ''}
  </div>`;
}

// ── Publicaciones incrustadas ─────────────────────────────────────────
// Los medios incrustan tuits con un <blockquote> más el script de X, que aquí
// no se carga: es de terceros, la política de seguridad lo bloquea y de paso
// le diría a X quién lee qué. Sin él el tuit queda como una cita descuadrada
// —a veces un enlace vacío y nada más—, que es lo que se veía roto.
//
// Se resuelve en dos tiempos: primero se arma la tarjeta con lo que ya trae el
// artículo, para que no haya hueco; después el servidor devuelve el tuit de
// verdad (texto completo, autor, foto, vídeo) y la tarjeta se completa sola.

const RE_TUIT = /(?:twitter|x)\.com\/[^/]+\/status\/(\d+)/i;
const RE_MEDIO_X = /^https?:\/\/(?:pic\.twitter\.com|t\.co)\//i;

/** "— C5 Estado de México (@C5Edomex) August 14, 2026" → nombre y cuenta. */
function autorDelTuit(texto) {
  const m = texto.match(/—\s*([^(]{2,60})\(@([A-Za-z0-9_]{1,20})\)/);
  return m ? { nombre: m[1].trim(), cuenta: '@' + m[2] } : null;
}

function cabezaDeX(autor) {
  return `<div class="x-cabeza">
    <span class="x-logo" aria-hidden="true">𝕏</span>
    ${autor ? `<span class="x-nombre">${esc(autor.nombre)}</span>
               <span class="x-cuenta">${esc(autor.cuenta)}</span>`
            : `<span class="x-nombre">Publicación en X</span>`}
  </div>`;
}

function pieDeX(destino) {
  return `<a class="x-ir" href="${esc(destino)}" target="_blank" rel="noopener noreferrer">
            Ver la publicación en X ↗</a>`;
}

/** Tarjeta provisional con lo que el propio artículo trae del tuit. */
function tarjetaDeX(cita) {
  const enlaces = [...cita.querySelectorAll('a')];
  const alTuit = enlaces.find(a => RE_TUIT.test(a.href));
  const alMedio = enlaces.find(a => RE_MEDIO_X.test(a.href));
  if (!alTuit && !alMedio) return null;

  const autor = autorDelTuit(cita.textContent);
  const parrafo = cita.querySelector('p');
  const cuerpo = document.createElement('div');
  cuerpo.className = 'x-cuerpo';
  if (parrafo) {
    cuerpo.innerHTML = parrafo.innerHTML;
    // Hashtags y menciones se quedan en texto: sacan de la aplicación y no
    // aportan nada dentro del artículo.
    cuerpo.querySelectorAll('a').forEach(a => {
      if (RE_TUIT.test(a.href) || RE_MEDIO_X.test(a.href)) { a.remove(); return; }
      a.replaceWith(document.createTextNode(a.textContent));
    });
  } else {
    cuerpo.textContent = cita.textContent.split('—')[0].trim();
  }

  const destino = (alTuit || alMedio).href;
  const tarjeta = document.createElement('figure');
  tarjeta.className = 'x-tarjeta';
  const id = alTuit ? (alTuit.href.match(RE_TUIT) || [])[1] : null;
  if (id) tarjeta.dataset.tuit = id;
  tarjeta.innerHTML = cabezaDeX(autor);
  tarjeta.appendChild(cuerpo);
  tarjeta.insertAdjacentHTML('beforeend', pieDeX(destino));
  return tarjeta;
}

function mediosDeX(t) {
  if (t.video) {
    return `<div class="x-medio">
      <video class="x-video" controls preload="none" playsinline
             poster="${esc(t.video.poster)}" style="aspect-ratio:${esc(t.video.aspecto)}">
        <source src="${esc(t.video.mp4)}" type="video/mp4">
      </video></div>`;
  }
  const fotos = (t.medios || []).filter(m => m.tipo === 'foto');
  if (!fotos.length) return '';
  return `<div class="x-medio${fotos.length > 1 ? ' x-medio-varias' : ''}">
    ${fotos.slice(0, 4).map(f =>
      `<img src="${esc(f.url)}" alt="${esc(f.alt)}" loading="lazy">`).join('')}
  </div>`;
}

/** Sustituye la tarjeta provisional por el tuit tal cual está en X. */
function pintarTuit(tarjeta, t) {
  if (!t || t.error) return;
  const avatar = t.avatar
    ? `<img class="x-avatar" src="${esc(t.avatar)}" alt="" loading="lazy">` : '';
  tarjeta.innerHTML =
    `<div class="x-cabeza">
       ${avatar}
       <span class="x-quien">
         <span class="x-nombre">${esc(t.autor)}</span>
         <span class="x-cuenta">${esc(t.cuenta)}</span>
       </span>
       <span class="x-logo" aria-hidden="true">𝕏</span>
     </div>
     <div class="x-cuerpo">${esc(t.texto).replace(/\n/g, '<br>')}</div>
     ${mediosDeX(t)}
     <div class="x-pie">
       ${t.fecha ? `<time class="x-fecha mono">${esc(fmtCompleta(t.fecha))}</time>` : ''}
       ${pieDeX(t.url)}
     </div>`;
  tarjeta.classList.add('x-lista');
}

/** Pide al servidor los tuits que cita el artículo y completa sus tarjetas. */
async function completarTuits(raiz, mia) {
  const pendientes = [...raiz.querySelectorAll('.x-tarjeta[data-tuit]')];
  if (!pendientes.length) return;
  const ids = [...new Set(pendientes.map(t => t.dataset.tuit))].slice(0, 12);
  try {
    const r = await api('/api/x?ids=' + ids.join(','));
    if (mia !== undefined && mia !== state.generacion) return;
    pendientes.forEach(t => pintarTuit(t, r.tuits[t.dataset.tuit]));
  } catch { /* la tarjeta provisional ya es legible */ }
}

/** Deja presentables los tuits y los enlaces sueltos a X del artículo. */
function mejorarIncrustados(raiz) {
  raiz.querySelectorAll('blockquote').forEach(cita => {
    const tarjeta = tarjetaDeX(cita);
    if (tarjeta) cita.replaceWith(tarjeta);
  });
  // Enlaces a un tuit que quedaron sueltos en su propio párrafo
  raiz.querySelectorAll('p > a:only-child').forEach(a => {
    if (!RE_TUIT.test(a.href) && !RE_MEDIO_X.test(a.href)) return;
    if (a.closest('.x-tarjeta')) return;
    const id = (a.href.match(RE_TUIT) || [])[1];
    if (id) {
      const tarjeta = document.createElement('figure');
      tarjeta.className = 'x-tarjeta';
      tarjeta.dataset.tuit = id;
      tarjeta.innerHTML = cabezaDeX(null) +
        `<div class="x-cuerpo"></div>` + pieDeX(a.href);
      a.parentElement.replaceWith(tarjeta);
      return;
    }
    const suelto = document.createElement('p');
    suelto.innerHTML = `<a class="x-ir suelto" href="${esc(a.href)}"
        target="_blank" rel="noopener noreferrer">Ver la publicación en X ↗</a>`;
    a.parentElement.replaceWith(suelto);
  });
  completarTuits(raiz, state.generacion);
}

/** Sondea hasta que la portada recién montada sustituya a la que se mostró. */
async function esperarPortadaNueva(folderId, tema, mia, intento = 0) {
  const caja = $('#portada');
  const bloque = $('.portada-bloque', caja);
  if (bloque && !$('.portada-actualizando', bloque)) {
    bloque.insertAdjacentHTML('beforeend',
      `<div class="portada-actualizando mono">Actualizando esta portada…</div>`);
  }
  if (intento > 12) { $('.portada-actualizando')?.remove(); return; }
  await new Promise(r => setTimeout(r, 5000));
  if (mia !== state.generacion) return;
  try {
    const r = await api(`/api/portada?folder_id=${folderId}`);
    if (mia !== state.generacion) return;
    if (r.renovando) return esperarPortadaNueva(folderId, tema, mia, intento + 1);
    const html = htmlPortada(r, tema);
    if (html) {
      caja.innerHTML = html;
      engancharPortada(caja);
      toast(`Portada de ${tema} actualizada`);
    }
  } catch { $('.portada-actualizando')?.remove(); }
}

/** Coloca los breves donde dejen las dos columnas más parejas.
 *
 * Con una principal grande —sobre todo si trae foto— la columna de al lado se
 * quedaba corta y sobraba medio palmo de blanco a la derecha; con una principal
 * escueta pasaba lo contrario. En vez de adivinarlo, se prueban las dos
 * disposiciones y se mide. Son dos reflujos y ninguno llega a pintarse.
 */
function equilibrarPortada(caja) {
  const rejilla = $('.portada-rejilla', caja);
  const principal = $('.pp-principal', caja);
  const secundarias = $('.pp-secundarias', caja);
  const breves = $('.pp-breves', caja);
  if (!rejilla || !principal || !secundarias || !breves) return;
  if (!window.matchMedia('(min-width: 900px)').matches) {
    rejilla.classList.remove('faldon');
    return;
  }

  const desnivel = () => {
    const izquierda = rejilla.classList.contains('faldon')
      ? principal.getBoundingClientRect().bottom
      : breves.getBoundingClientRect().bottom;
    return Math.abs(izquierda - secundarias.getBoundingClientRect().bottom);
  };

  rejilla.classList.remove('faldon');
  const debajo = desnivel();
  rejilla.classList.add('faldon');
  if (desnivel() > debajo) rejilla.classList.remove('faldon');
}

function engancharPortada(caja) {
  equilibrarPortada(caja);
  $$('[data-id]', caja).forEach(b => {
    if (b.dataset.enganchado) return;
    b.dataset.enganchado = '1';
    b.addEventListener('click', () => abrirArticulo(Number(b.dataset.id)));
  });
}

/** Vista Portada: un tema a la vez. Las portadas nunca se apilan en una sola. */
async function portadaConSelector() {
  const pestanas = $('#portada-temas');
  const caja = $('#portada');
  const conSinLeer = state.folders.filter(f =>
    state.feeds.some(x => x.folder_id === f.id && x.unread > 0));
  const temas = conSinLeer.length ? conSinLeer : state.folders;

  if (!temas.length) {
    pestanas.hidden = true; caja.hidden = true;
    $('#lista').innerHTML =
      `<div class="vacio"><h2>Todavía no hay temas</h2>` +
      `<p>Añade un tema listo desde el botón Fuente y aquí verás su portada.</p></div>`;
    return;
  }

  // El tema que se estaba viendo la última vez, si sigue existiendo
  const guardado = +(localStorage.getItem('mn_tema_portada') || 0);
  let activo = temas.find(t => t.id === guardado) || temas[0];

  pestanas.hidden = false;
  pestanas.innerHTML = temas.map(t => {
    const sinLeer = state.feeds
      .filter(f => f.folder_id === t.id)
      .reduce((n, f) => n + (f.unread || 0), 0);
    return `<button class="ptab${t.id === activo.id ? ' activa' : ''}" role="tab"
      aria-selected="${t.id === activo.id}" data-id="${t.id}" data-nombre="${esc(t.name)}">
      ${esc(t.name)}${sinLeer ? `<span class="ptab-n">${sinLeer}</span>` : ''}</button>`;
  }).join('');

  // La tira arranca en el tema que se estaba viendo: si no, en el teléfono la
  // pestaña marcada quedaba fuera de pantalla y no se sabía qué se está leyendo.
  $('.ptab.activa', pestanas)?.scrollIntoView({ block: 'nearest', inline: 'center' });

  $$('.ptab', pestanas).forEach(b => b.addEventListener('click', () => {
    const id = Number(b.dataset.id);
    localStorage.setItem('mn_tema_portada', id);
    $$('.ptab', pestanas).forEach(x => {
      const on = x === b;
      x.classList.toggle('activa', on);
      x.setAttribute('aria-selected', String(on));
    });
    b.scrollIntoView({ block: 'nearest', inline: 'center' });
    mostrarPortadaDe(id, b.dataset.nombre);
  }));

  await mostrarPortadaDe(activo.id, activo.name);
}

async function mostrarPortadaDe(folderId, tema) {
  const caja = $('#portada');
  const mia = state.generacion;
  caja.hidden = false;
  caja.innerHTML = `<div class="portada-cargando"><span class="mono">Armando la portada de ${esc(tema)}…</span></div>`;
  $('#panel-lista').scrollTop = 0;
  try {
    const r = await api(`/api/portada?folder_id=${folderId}`);
    if (mia !== state.generacion) return;
    const html = htmlPortada(r, tema);
    caja.innerHTML = html || '';
    if (!html) {
      caja.hidden = true;
      $('#lista').innerHTML =
        `<div class="vacio"><div class="vacio-marca" aria-hidden="true">✓</div>` +
        `<h2>Nada que destacar en ${esc(tema)}</h2><p>No hay artículos sin leer en este tema.</p></div>`;
      return;
    }
    $('#lista').innerHTML = '';
    engancharPortada(caja);

    // El servidor sirvió una portada anterior y está montando la nueva por
    // detrás: se avisa y se recoge sola en cuanto esté, sin recargar nada.
    if (r.renovando) esperarPortadaNueva(folderId, tema, mia);
  } catch (e) {
    if (mia !== state.generacion) return;
    caja.innerHTML = `<div class="vacio"><h2>No se pudo armar la portada</h2>` +
      `<p>${esc(e.message)}. Puedes seguir leyendo el tema desde la barra lateral.</p></div>`;
  }
}

// ── Lector ────────────────────────────────────────────────────────────

async function abrirArticulo(id) {
  try {
    const a = await api(`/api/articles/${id}`);
    state.actual = a; state.verFull = false;
    document.body.classList.add('leyendo');
    $('#lector-cuerpo').hidden = false;
    $('#lector').scrollTop = 0;
    $('#lector-meta').textContent = a.feed_title || '';
    $('#lector-titulo').textContent = a.title;
    $('#lector-sub').textContent =
      [a.author, fmtCompleta(a.published_at || a.fetched_at)].filter(Boolean).join(' · ');
    $('#lector-contenido').innerHTML = a.content || a.summary || '<p>(sin contenido)</p>';
    mejorarIncrustados($('#lector-contenido'));
    $('#btn-open').href = a.url || a.site_url || '#';
    $('#btn-full').classList.remove('on');
    actualizarBotonesLector();
    // Una sola entrada de historial por sesión de lectura: saltar de artículo
    // en artículo con j/k no debe obligar a pulsar atrás quince veces.
    if (!state.enHistorial) {
      history.pushState({ mn: 'lector' }, '');
      state.enHistorial = true;
    }
    if (!a.read) await marcarArticulo(id, { read: true });
    $$('.arow').forEach(r => r.classList.toggle('activa', Number(r.dataset.id) === id));

    // El feed casi nunca trae el artículo entero: si viene corto, lo traemos completo.
    if (a.url && $('#lector-contenido').textContent.trim().length < 900) traerTextoCompleto(id);
  } catch (e) { toast('No se pudo abrir el artículo: ' + e.message); }
}

function cerrarLector({ desdeHistorial = false } = {}) {
  state.actual = null;
  document.body.classList.remove('leyendo');
  const cuerpo = $('#lector-cuerpo');
  cuerpo.hidden = true;
  cuerpo.style.transform = '';
  $('#lector').style.transition = '';
  // Si el lector metió una entrada en el historial, se retira al cerrarlo a
  // mano; cuando el cierre viene del propio historial (gesto de volver del
  // teléfono o botón atrás), el navegador ya la quitó.
  if (state.enHistorial) {
    state.enHistorial = false;
    if (!desdeHistorial) history.back();
  }
}

// El gesto de volver del teléfono y el botón atrás del navegador cierran lo que
// esté abierto, en vez de sacarte de la aplicación.
window.addEventListener('popstate', () => {
  if (!$('#modal-cmd').hidden) { cerrarPaleta(); return; }
  const modal = $$('.modal-wrap:not([hidden])')[0];
  if (modal) { modal.hidden = true; return; }
  if (state.actual) { cerrarLector({ desdeHistorial: true }); return; }
  if ($('#sidebar').classList.contains('abierta')) cerrarLateralMovil();
});

function actualizarBotonesLector() {
  const a = state.actual; if (!a) return;
  $('#btn-save').classList.toggle('on', !!a.saved);
  $('#btn-toggle-read').classList.toggle('on', !a.read);
}

async function marcarArticulo(id, flags) {
  await api(`/api/articles/${id}`, { method: 'PATCH', body: flags });
  const enLista = state.articulos.find(x => x.id === id);
  if (enLista) Object.assign(enLista, flags);
  if (state.actual?.id === id) Object.assign(state.actual, flags);
  const fila = $(`.arow[data-id="${id}"]`);
  if (fila && 'read' in flags) fila.classList.toggle('leido', !!flags.read);
  if (fila && 'saved' in flags) {
    const src = $('.a-src', fila);
    const est = $('.a-estrella', fila);
    if (flags.saved && !est) src.insertAdjacentHTML('beforeend', '<span class="a-estrella">★</span>');
    if (!flags.saved && est) est.remove();
  }
  actualizarBotonesLector();
  refrescarContadores();
}

async function traerTextoCompleto(id, forzar = false) {
  const el = $('#lector-contenido');
  const nota = document.createElement('p');
  nota.className = 'ft-nota';
  nota.textContent = 'Trayendo el artículo completo…';
  el.prepend(nota);
  try {
    const r = await api(`/api/articles/${id}/fulltext${forzar ? '?refresh=1' : ''}`);
    if (state.actual?.id !== id) return;
    el.innerHTML = r.html;
    mejorarIncrustados(el);
    state.verFull = true;
    $('#btn-full').classList.add('on');
  } catch (e) {
    nota.textContent = `No se pudo extraer el texto completo (${e.message}). Se muestra el resumen del feed.`;
    setTimeout(() => nota.remove(), 6000);
  }
}

$('#btn-full').addEventListener('click', () => {
  const a = state.actual; if (!a) return;
  if (state.verFull) {
    $('#lector-contenido').innerHTML = a.content || a.summary || '<p>(sin contenido)</p>';
    mejorarIncrustados($('#lector-contenido'));
    state.verFull = false;
    $('#btn-full').classList.remove('on');
  } else traerTextoCompleto(a.id);
});
$('#btn-back').addEventListener('click', cerrarLector);
$('#btn-save').addEventListener('click', () => {
  const a = state.actual; if (a) marcarArticulo(a.id, { saved: !a.saved });
});
$('#btn-toggle-read').addEventListener('click', () => {
  const a = state.actual; if (a) marcarArticulo(a.id, { read: !a.read });
});

// ── Gesto de volver ───────────────────────────────────────────────────
// Arrastrar desde el borde izquierdo cierra el artículo, como en cualquier app
// del teléfono. En Safari el gesto nativo ya funciona gracias al historial;
// esto es para cuando la app está instalada en la pantalla de inicio, donde ese
// gesto no existe.

const BORDE_GESTO = 34;      // desde dónde cuenta como "arrastre de volver"
const CIERRA_EN = 92;        // cuánto hay que arrastrar para que cierre

let gesto = null;

function iniciarGesto(e) {
  if (!state.actual) return;
  const t = e.touches[0];
  if (t.clientX > BORDE_GESTO) return;
  gesto = { x0: t.clientX, y0: t.clientY, dx: 0, decidido: false, arrastrando: false };
}

function moverGesto(e) {
  if (!gesto || !state.actual) return;
  const t = e.touches[0];
  const dx = t.clientX - gesto.x0;
  const dy = t.clientY - gesto.y0;

  // Hasta saber si el dedo va en horizontal o en vertical no se toca nada: si
  // no, el gesto se comería el scroll de la lectura.
  if (!gesto.decidido) {
    if (Math.abs(dx) < 10 && Math.abs(dy) < 10) return;
    gesto.decidido = true;
    gesto.arrastrando = Math.abs(dx) > Math.abs(dy);
    if (gesto.arrastrando) $('#lector').style.transition = 'none';
  }
  if (!gesto.arrastrando) return;

  e.preventDefault();
  gesto.dx = Math.max(0, dx);
  const lector = $('#lector');
  lector.style.transform = `translateX(${gesto.dx}px)`;
  lector.style.boxShadow = `-14px 0 34px rgba(20,50,80,${Math.max(0, .22 - gesto.dx / 1800)})`;
}

function soltarGesto() {
  if (!gesto || !gesto.arrastrando) { gesto = null; return; }
  const lector = $('#lector');
  const cierra = gesto.dx > CIERRA_EN;
  lector.style.transition = 'transform .2s ease, box-shadow .2s ease';
  if (cierra) {
    lector.style.transform = 'translateX(100%)';
    setTimeout(() => {
      lector.style.transition = ''; lector.style.transform = ''; lector.style.boxShadow = '';
      cerrarLector();
    }, 190);
  } else {
    lector.style.transform = '';
    lector.style.boxShadow = '';
    setTimeout(() => { lector.style.transition = ''; }, 210);
  }
  gesto = null;
}

const lectorEl = $('#lector');
lectorEl.addEventListener('touchstart', iniciarGesto, { passive: true });
lectorEl.addEventListener('touchmove', moverGesto, { passive: false });
lectorEl.addEventListener('touchend', soltarGesto);
lectorEl.addEventListener('touchcancel', soltarGesto);

// ── Cabecera y navegación ─────────────────────────────────────────────

$$('.side-item[data-view]').forEach(el =>
  el.addEventListener('click', () => irA({ type: el.dataset.view })));
$$('.tab-btn').forEach(el =>
  el.addEventListener('click', () => irA({ type: el.dataset.view })));

$$('.dens-btn').forEach(b => b.addEventListener('click', () => {
  prefs.densidad = b.dataset.densidad; aplicarPrefs(); guardarPrefs();
}));

// En el teléfono el tamaño de letra tiene que estar a un toque, no enterrado en
// ajustes: el botón Aa va rotando por los tamaños y vuelve a empezar.
const PASOS_LETRA = [100, 115, 130, 145];
$('#btn-letra').addEventListener('click', () => {
  const i = PASOS_LETRA.findIndex(p => p >= prefs.escala);
  prefs.escala = PASOS_LETRA[(i + 1) % PASOS_LETRA.length];
  $('#set-escala').value = prefs.escala;
  $('#set-escala-val').textContent = `${prefs.escala} %`;
  aplicarPrefs(); guardarPrefs();
  toast(`Letra al ${prefs.escala} %`);
});

$('#btn-mark-read').addEventListener('click', async () => {
  const v = state.view, body = {};
  if (v.type === 'feed') body.feed_id = v.id;
  if (v.type === 'folder') body.folder_id = v.id;
  if (v.type === 'saved') body.saved = 1;
  if (v.type === 'today') body.today = 1;
  if (v.type === 'search') body.q = v.q;
  const { marked } = await api('/api/articles/mark_read', { method: 'POST', body });
  if (!marked) { toast('No había nada sin leer aquí'); return; }
  toast(`${marked} marcados como leídos`, {
    texto: 'Deshacer',
    fn: async () => {
      const { restored } = await api('/api/articles/mark_read/undo', { method: 'POST' });
      toast(`${restored} devueltos a no leídos`);
      irA(state.view); cargarLateral();
    },
  });
  irA(state.view); cargarLateral();
});

$('#btn-refresh').addEventListener('click', async () => {
  const btn = $('#btn-refresh');
  btn.disabled = true;
  toast('Refrescando tus fuentes…');
  try {
    const r = await api('/api/refresh', { method: 'POST' });
    if (r.job || r.en_curso !== undefined) {   // refresco en segundo plano
      vigilarRefresco();
    } else {
      toast(`Listo: ${r.new} artículos nuevos` + (r.errors?.length ? ` · ${r.errors.length} con error` : ''));
      cargarLateral(); irA(state.view); btn.disabled = false;
    }
  } catch (e) { toast('Error al refrescar: ' + e.message); btn.disabled = false; }
});

async function vigilarRefresco() {
  const btn = $('#btn-refresh');
  const tic = setInterval(async () => {
    try {
      const s = await api('/api/refresh/status');
      if (!s.en_curso) {
        clearInterval(tic); btn.disabled = false;
        toast(`Listo: ${s.nuevos || 0} artículos nuevos` + (s.errores ? ` · ${s.errores} con error` : ''));
        cargarLateral(); irA(state.view);
      }
    } catch { clearInterval(tic); btn.disabled = false; }
  }, 2000);
}

$('#btn-menu').addEventListener('click', () => {
  const abierta = $('#sidebar').classList.toggle('abierta');
  $('#scrim').hidden = !abierta;
  $('#btn-menu').setAttribute('aria-expanded', String(abierta));
});
$('#scrim').addEventListener('click', cerrarLateralMovil);
function cerrarLateralMovil() {
  $('#sidebar').classList.remove('abierta');
  $('#scrim').hidden = true;
  $('#btn-menu').setAttribute('aria-expanded', 'false');
}

$('#btn-feed-edit').addEventListener('click', abrirAjustes);

// ── Paleta de comandos (⌘K) ───────────────────────────────────────────

let cmdSel = 0, cmdOpciones = [], cmdTimer;

function abrirPaleta() {
  $('#modal-cmd').hidden = false;
  $('#cmd-input').value = '';
  pintarPaleta(opcionesBase());
  $('#cmd-input').focus();
}
function cerrarPaleta() { $('#modal-cmd').hidden = true; }

function opcionesBase() {
  const vistas = [
    { tipo: 'vista', nombre: 'Portada', accion: () => irA({ type: 'portada' }) },
    { tipo: 'vista', nombre: 'Hoy', accion: () => irA({ type: 'today' }) },
    { tipo: 'vista', nombre: 'Sin leer', accion: () => irA({ type: 'unread' }) },
    { tipo: 'vista', nombre: 'Todo', accion: () => irA({ type: 'all' }) },
    { tipo: 'vista', nombre: 'Guardados', accion: () => irA({ type: 'saved' }) },
  ];
  const carpetas = state.folders.map(f => ({
    tipo: 'tema', nombre: f.name,
    accion: () => irA({ type: 'folder', id: f.id, title: f.name }),
  }));
  const feeds = state.feeds.map(f => ({
    tipo: 'fuente', nombre: f.title || f.url, icono: f.favicon_url,
    accion: () => irA({ type: 'feed', id: f.id, title: f.title || f.url }),
  }));
  return [...vistas, ...carpetas, ...feeds];
}

function pintarPaleta(opciones) {
  cmdOpciones = opciones; cmdSel = 0;
  const caja = $('#cmd-lista');
  if (!opciones.length) {
    caja.innerHTML = '<div class="cmd-vacio">Sin coincidencias</div>';
    return;
  }
  caja.innerHTML = opciones.map((o, i) => {
    const ico = o.icono
      ? `<img class="favicon" src="${esc(o.icono)}" alt="" onerror="this.remove()">`
      : `<span class="favicon" aria-hidden="true"></span>`;
    return `<button class="cmd-item${i === cmdSel ? ' sel' : ''}" data-i="${i}" role="option">
      ${ico}<span class="cmd-n">${esc(o.nombre)}</span>
      <span class="cmd-tipo">${esc(o.tipo)}</span></button>`;
  }).join('');
  $$('.cmd-item', caja).forEach(b =>
    b.addEventListener('click', () => ejecutarPaleta(Number(b.dataset.i))));
}

function moverPaleta(delta) {
  if (!cmdOpciones.length) return;
  cmdSel = (cmdSel + delta + cmdOpciones.length) % cmdOpciones.length;
  $$('.cmd-item').forEach((b, i) => b.classList.toggle('sel', i === cmdSel));
  $('.cmd-item.sel')?.scrollIntoView({ block: 'nearest' });
}

function ejecutarPaleta(i = cmdSel) {
  const o = cmdOpciones[i];
  if (!o) return;
  cerrarPaleta();
  o.accion();
}

$('#btn-buscar').addEventListener('click', abrirPaleta);

$('#cmd-input').addEventListener('input', e => {
  const q = e.target.value.trim().toLowerCase();
  clearTimeout(cmdTimer);
  if (!q) { pintarPaleta(opcionesBase()); return; }

  const locales = opcionesBase().filter(o => o.nombre.toLowerCase().includes(q));
  pintarPaleta(locales);

  if (q.length >= 2) {
    cmdTimer = setTimeout(async () => {
      try {
        const { articles } = await api(`/api/articles?q=${encodeURIComponent(q)}&limit=8`);
        if ($('#cmd-input').value.trim().toLowerCase() !== q) return;
        const arts = articles.map(a => ({
          tipo: 'artículo', nombre: a.title, icono: a.favicon_url,
          accion: () => { irA({ type: 'search', q }); setTimeout(() => abrirArticulo(a.id), 350); },
        }));
        pintarPaleta([...locales, ...arts,
          { tipo: 'buscar', nombre: `Buscar «${q}» en todo`, accion: () => irA({ type: 'search', q }) }]);
      } catch {}
    }, 260);
  }
});

$('#cmd-input').addEventListener('keydown', e => {
  if (e.key === 'ArrowDown') { e.preventDefault(); moverPaleta(1); }
  if (e.key === 'ArrowUp') { e.preventDefault(); moverPaleta(-1); }
  if (e.key === 'Enter') { e.preventDefault(); ejecutarPaleta(); }
});

// ── Teclado ───────────────────────────────────────────────────────────

document.addEventListener('keydown', e => {
  // ⌘K / Ctrl+K abre la paleta; el resto de combinaciones son del navegador
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault(); abrirPaleta(); return;
  }
  if (e.key === 'Escape') {
    if (!$('#modal-cmd').hidden) { cerrarPaleta(); return; }
    const abierto = $$('.modal-wrap:not([hidden])')[0];
    if (abierto) { abierto.hidden = true; return; }
    if (state.actual) cerrarLector();
    return;
  }
  if (e.metaKey || e.ctrlKey || e.altKey) return;          // no secuestramos atajos del sistema
  if (e.target.matches('input, textarea, select')) return;

  const idx = state.actual ? state.articulos.findIndex(a => a.id === state.actual.id) : -1;
  const abrirRelativo = paso => {
    const sig = state.articulos[idx + paso];
    if (sig) abrirArticulo(sig.id);
  };

  switch (e.key) {
    case 'j': e.preventDefault(); abrirRelativo(1); break;
    case 'k': e.preventDefault(); abrirRelativo(-1); break;
    case 'n': case 'p': {                                   // moverse sin abrir
      e.preventDefault();
      const filas = $$('.arow');
      if (!filas.length) break;
      const act = $('.arow.activa');
      let i = act ? filas.indexOf(act) : -1;
      i = Math.min(Math.max(i + (e.key === 'n' ? 1 : -1), 0), filas.length - 1);
      filas.forEach(f => f.classList.remove('activa'));
      filas[i].classList.add('activa');
      filas[i].scrollIntoView({ block: 'nearest' });
      filas[i].focus();
      break;
    }
    case 'o': case 'Enter': {
      const act = $('.arow.activa');
      if (act && !state.actual) { e.preventDefault(); abrirArticulo(Number(act.dataset.id)); }
      break;
    }
    case 'v':
      if (state.actual?.url) { e.preventDefault(); window.open(state.actual.url, '_blank', 'noopener'); }
      break;
    case 'm':
      if (state.actual) { e.preventDefault(); marcarArticulo(state.actual.id, { read: !state.actual.read }); }
      break;
    case 'x':
      if (state.actual) {                                   // leer y quitar de en medio
        e.preventDefault();
        const id = state.actual.id;
        marcarArticulo(id, { read: true });
        abrirRelativo(1);
      }
      break;
    case 's':
      if (state.actual) { e.preventDefault(); marcarArticulo(state.actual.id, { saved: !state.actual.saved }); }
      break;
    case '+': case '=': case '-': {          // agrandar o encoger la letra
      e.preventDefault();
      const paso = e.key === '-' ? -5 : 5;
      prefs.escala = Math.min(145, Math.max(90, prefs.escala + paso));
      $('#set-escala').value = prefs.escala;
      $('#set-escala-val').textContent = `${prefs.escala} %`;
      aplicarPrefs(); guardarPrefs();
      toast(`Letra al ${prefs.escala} %`);
      break;
    }
    case 'r': e.preventDefault(); $('#btn-refresh').click(); break;
    case 'a': e.preventDefault(); abrirModal('#modal-add'); break;
    case 'A':
      if (e.shiftKey) { e.preventDefault(); $('#btn-mark-read').click(); }
      break;
    case '?': e.preventDefault(); abrirPaleta(); break;
  }
});

// ── Modales ───────────────────────────────────────────────────────────

let focoPrevio = null;
function abrirModal(sel) {
  focoPrevio = document.activeElement;
  $(sel).hidden = false;
  const primero = $('input, button:not(.modal-close)', $(sel));
  primero?.focus();
}
$$('.modal-wrap').forEach(m => {
  m.addEventListener('click', e => { if (e.target === m) { m.hidden = true; focoPrevio?.focus(); } });
  $('.modal-close', m)?.addEventListener('click', () => { m.hidden = true; focoPrevio?.focus(); });
});

// Añadir fuente
$('#btn-add').addEventListener('click', () => {
  abrirModal('#modal-add'); cargarCatalogo(); $('#add-url').focus();
});
$('#btn-discover').addEventListener('click', descubrir);
$('#add-url').addEventListener('keydown', e => { if (e.key === 'Enter') descubrir(); });

async function descubrir() {
  const url = $('#add-url').value.trim();
  if (!url) return;
  const caja = $('#discover-results');
  caja.innerHTML = '<p class="ft-nota">Buscando feeds…</p>';
  try {
    const { results } = await api('/api/discover?url=' + encodeURIComponent(url));
    if (!results.length) {
      caja.innerHTML = '<p style="color:var(--accent)">No encontramos ningún feed en esa dirección.</p>';
      return;
    }
    caja.innerHTML = '';
    for (const r of results) {
      const el = document.createElement('div');
      el.className = 'disc-item';
      el.innerHTML = `<div class="t"><b>${esc(r.title || r.feed_url)}</b><span>${esc(r.feed_url)}</span></div>
        <button class="btn small">Añadir</button>`;
      $('button', el).addEventListener('click', () => anadirFeed(r.feed_url, el));
      caja.appendChild(el);
    }
  } catch (e) { caja.innerHTML = `<p style="color:var(--accent)">Error: ${esc(e.message)}</p>`; }
}

async function anadirFeed(url, el, carpeta) {
  const btn = el ? $('button', el) : null;
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  try {
    const r = await api('/api/feeds', {
      method: 'POST',
      body: { url, folder: carpeta ?? $('#add-folder').value.trim() },
    });
    toast(r.warning ? `Añadida con aviso: ${r.warning}` : `Fuente añadida · ${r.new_articles} artículos`);
    if (btn) btn.textContent = '✓';
    cargarLateral();
  } catch (e) {
    toast('Error: ' + e.message);
    if (btn) { btn.disabled = false; btn.textContent = 'Añadir'; }
  }
}

// Temas listos: cada uno añade su carpeta completa
async function cargarCatalogo() {
  const caja = $('#catalog');
  if (caja.dataset.cargado) return;
  const cat = await api('/api/catalog');
  caja.innerHTML = '';
  for (const c of cat.categories || []) {
    const fila = document.createElement('div');
    fila.className = 'tema-fila';
    fila.innerHTML = `<div class="tema-n"><b>${esc(c.name)}</b>
      <span>${c.feeds.length} fuentes</span></div>
      <button class="btn small">Añadir tema</button>`;
    $('button', fila).addEventListener('click', async () => {
      const btn = $('button', fila);
      btn.disabled = true;
      let ok = 0;
      for (const [i, f] of c.feeds.entries()) {
        btn.textContent = `${i + 1}/${c.feeds.length}`;
        try {
          await api('/api/feeds', { method: 'POST', body: { url: f.feed_url, folder: c.name } });
          ok++;
        } catch {}
      }
      btn.textContent = `✓ ${ok}`;
      toast(`Tema ${c.name}: ${ok} fuentes añadidas`);
      cargarLateral();
    });
    caja.appendChild(fila);
  }
  caja.dataset.cargado = '1';
}

// IA: sugerir fuentes
$('#btn-ai').addEventListener('click', sugerirIA);
$('#ai-prompt').addEventListener('keydown', e => { if (e.key === 'Enter') sugerirIA(); });

async function sugerirIA() {
  const prompt = $('#ai-prompt').value.trim();
  if (!prompt) return;
  const caja = $('#ai-results'), btn = $('#btn-ai');
  btn.disabled = true; btn.textContent = '…';
  caja.innerHTML = '<p class="ft-nota">Buscando fuentes y comprobando que estén vivas…</p>';
  try {
    const r = await api('/api/ai/suggest', { method: 'POST', body: { prompt } });
    caja.innerHTML = r.note ? `<p class="ai-note">${esc(r.note)}</p>` : '';
    if (!r.suggestions.length) caja.innerHTML += '<p>No hubo sugerencias.</p>';
    for (const s of r.suggestions) {
      const el = document.createElement('div');
      el.className = 'disc-item' + (s.verified ? '' : ' unverified');
      el.innerHTML = `<div class="t"><b>${esc(s.title)}</b><span>${esc(s.feed_url)}</span>
        <i>${esc(s.reason || '')}</i></div>
        ${s.verified ? '<button class="btn small">Añadir</button>'
                     : '<span class="mono" style="font-size:10px;color:var(--ink3)">sin verificar</span>'}`;
      if (s.verified) $('button', el).addEventListener('click', () => {
        $('#add-folder').value = $('#add-folder').value || s.folder || '';
        anadirFeed(s.feed_url, el);
      });
      caja.appendChild(el);
    }
  } catch (e) { caja.innerHTML = `<p style="color:var(--accent)">Error: ${esc(e.message)}</p>`; }
  btn.disabled = false; btn.textContent = 'Sugerir';
}

// Guardar artículo suelto
$('#btn-art-add').addEventListener('click', async () => {
  const url = $('#art-url').value.trim();
  if (!url) return;
  const btn = $('#btn-art-add');
  btn.disabled = true; btn.textContent = '…';
  try {
    const r = await api('/api/articles/from_url', { method: 'POST', body: { url } });
    toast(r.warning ? `Guardado con aviso: ${r.warning}` : `Artículo guardado: ${r.title}`);
    $('#art-url').value = '';
    cargarLateral();
  } catch (e) { toast('Error: ' + e.message); }
  btn.disabled = false; btn.textContent = 'Guardar';
});

// ── Filtros de silencio y alertas ─────────────────────────────────────

const ETIQUETA_CAMPO = { cualquiera: 'el artículo', titulo: 'el título', resumen: 'el resumen',
                         autor: 'el autor', dominio: 'el sitio' };
const ETIQUETA_OP = { contiene: 'contiene', palabra: 'tiene la palabra', es: 'es exactamente',
                      empieza: 'empieza por', termina: 'termina en', regex: 'casa con' };

async function pintarReglas() {
  const caja = $('#reglas-lista');
  const { reglas, silenciados } = await api('/api/reglas');

  // El desplegable de ámbito se llena con las carpetas reales
  const amb = $('#rg-ambito');
  if (amb.options.length <= 1) {
    amb.innerHTML = '<option value="">en todo</option>' +
      state.folders.map(f => `<option value="c${f.id}">solo en ${esc(f.name)}</option>`).join('');
  }

  if (!reglas.length) {
    caja.innerHTML = `<p class="ft-nota" style="margin-top:14px">Todavía no tienes filtros. ` +
      `Prueba a silenciar «earnings call» si te aburren las transcripciones de resultados.</p>`;
    return;
  }
  caja.innerHTML =
    `<p class="ft-nota" style="margin:14px 0 8px">${silenciados} artículos escondidos ahora mismo.` +
    (silenciados ? ` <button class="enlace-boton" id="ver-silenciados">Ver cuáles</button>` : '') + `</p>` +
    reglas.map(r => `
      <div class="regla ${r.tipo}" data-id="${r.id}">
        <span class="regla-tipo">${r.tipo === 'silencio' ? 'Silencia' : 'Avisa'}</span>
        <span class="regla-txt">${esc(ETIQUETA_CAMPO[r.campo] || r.campo)}
          ${esc(ETIQUETA_OP[r.operador] || r.operador)}
          <b>${esc(r.patron)}</b>${r.ambito_carpeta ? ' (solo en una carpeta)' : ''}</span>
        <span class="regla-n mono">${r.aciertos}</span>
        <button class="btn ghost small regla-onoff">${r.activa ? 'Activa' : 'Apagada'}</button>
        <button class="btn danger small regla-del">Quitar</button>
      </div>`).join('');

  $('#ver-silenciados')?.addEventListener('click', () => {
    $('#modal-settings').hidden = true;
    irA({ type: 'silenciados', title: 'Artículos escondidos' });
  });
  $$('.regla', caja).forEach(el => {
    const id = Number(el.dataset.id);
    const r = reglas.find(x => x.id === id);
    $('.regla-onoff', el).addEventListener('click', async () => {
      await api(`/api/reglas/${id}`, { method: 'PATCH', body: { activa: !r.activa } });
      await pintarReglas(); cargarLateral(); irA(state.view);
    });
    $('.regla-del', el).addEventListener('click', async () => {
      const { silenciados } = await api(`/api/reglas/${id}`, { method: 'DELETE' });
      toast(`Filtro quitado · ${silenciados} artículos escondidos ahora`);
      await pintarReglas(); cargarLateral(); irA(state.view);
    });
  });
}

$('#btn-regla-crear').addEventListener('click', async () => {
  const patron = $('#rg-patron').value.trim();
  if (!patron) { toast('Escribe qué buscar'); return; }
  const amb = $('#rg-ambito').value;
  const btn = $('#btn-regla-crear');
  btn.disabled = true;
  try {
    const r = await api('/api/reglas', { method: 'POST', body: {
      tipo: $('#rg-tipo').value, campo: $('#rg-campo').value,
      operador: $('#rg-operador').value, patron,
      ambito_carpeta: amb.startsWith('c') ? Number(amb.slice(1)) : null,
    }});
    toast($('#rg-tipo').value === 'silencio'
      ? `Listo: ${r.silenciados} artículos escondidos`
      : `Listo: ${r.alertados} artículos con aviso`);
    $('#rg-patron').value = '';
    await pintarReglas(); cargarLateral(); irA(state.view);
  } catch (e) { toast('No se pudo crear: ' + e.message); }
  btn.disabled = false;
});

// ── Feeds de IA ───────────────────────────────────────────────────────

async function pintarFeedsIA() {
  const { ai_feeds } = await api('/api/ai_feeds');
  const caja = $('#iafeeds-tabla');
  caja.innerHTML = ai_feeds.length ? ai_feeds.map(f => `
    <div class="regla" data-id="${f.id}">
      <span class="regla-txt"><b>${esc(f.nombre)}</b><br>
        <span class="ft-nota">${esc(f.descripcion.slice(0, 120))}</span></span>
      <span class="regla-n mono">${f.articulos}</span>
      <button class="btn ghost small iaf-pasar">Revisar ahora</button>
      <button class="btn danger small iaf-del">Quitar</button>
    </div>`).join('')
    : '<p class="ft-nota" style="margin-top:12px">Aún no tienes feeds de IA.</p>';

  $$('.regla', caja).forEach(el => {
    const id = Number(el.dataset.id);
    $('.iaf-pasar', el).addEventListener('click', async () => {
      const b = $('.iaf-pasar', el); b.disabled = true; b.textContent = 'Revisando…';
      const r = await api(`/api/ai_feeds/${id}/pasar`, { method: 'POST' });
      toast(`${r.mirados} artículos revisados, ${r.dentro} añadidos`);
      b.disabled = false; b.textContent = 'Revisar ahora';
      pintarFeedsIA(); cargarLateral();
    });
    $('.iaf-del', el).addEventListener('click', async () => {
      await api(`/api/ai_feeds/${id}`, { method: 'DELETE' });
      toast('Feed de IA quitado');
      pintarFeedsIA(); cargarLateral();
    });
  });

  // Y en la barra lateral, como si fueran carpetas
  const lateral = $('#aifeeds-lista');
  lateral.innerHTML = ai_feeds.filter(f => f.activo).map(f => `
    <button class="side-item" data-view="aifeed" data-id="${f.id}">
      <span class="si-icon" aria-hidden="true">◈</span>
      <span class="si-label">${esc(f.nombre)}</span>
      <span class="badge">${f.sin_leer || ''}</span>
    </button>`).join('');
  $$('.side-item', lateral).forEach(b => b.addEventListener('click', () => {
    const f = ai_feeds.find(x => x.id === Number(b.dataset.id));
    irA({ type: 'aifeed', id: f.id, title: f.nombre });
  }));
}

$('#btn-iaf-crear').addEventListener('click', async () => {
  const nombre = $('#iaf-nombre').value.trim();
  const descripcion = $('#iaf-desc').value.trim();
  if (!nombre || !descripcion) { toast('Hacen falta nombre y descripción'); return; }
  const btn = $('#btn-iaf-crear'); btn.disabled = true;
  try {
    await api('/api/ai_feeds', { method: 'POST', body: { nombre, descripcion } });
    toast(`«${nombre}» creado. Se está clasificando; los artículos irán apareciendo.`);
    $('#iaf-nombre').value = ''; $('#iaf-desc').value = '';
    pintarFeedsIA();
  } catch (e) { toast('No se pudo crear: ' + e.message); }
  btn.disabled = false;
});

$('#btn-ayuda-busqueda').addEventListener('click', () => {
  const s = $('#cmd-sintaxis'); s.hidden = !s.hidden;
});

// ── Ajustes ───────────────────────────────────────────────────────────

$('#btn-settings').addEventListener('click', abrirAjustes);
function abrirAjustes() {
  abrirModal('#modal-settings');
  pintarTablaFuentes(); pintarClaves(); pintarReglas(); pintarFeedsIA();
}

$$('.tab').forEach(t => t.addEventListener('click', () => {
  $$('.tab').forEach(x => {
    const on = x === t;
    x.classList.toggle('activa', on);
    x.setAttribute('aria-selected', String(on));
  });
  $$('.tabpane').forEach(p => p.hidden = p.id !== 'tab-' + t.dataset.tab);
}));

function pintarTablaFuentes() {
  const caja = $('#tab-fuentes');
  const opciones = fid => ['<option value="">— sin carpeta —</option>',
    ...state.folders.map(f =>
      `<option value="${esc(f.name)}" ${f.id === fid ? 'selected' : ''}>${esc(f.name)}</option>`)].join('');
  caja.innerHTML = `<div class="tabla-scroll"><table class="table">
    <tr><th>Fuente</th><th>Carpeta</th><th>Cada (min)</th><th></th><th></th></tr>` +
    state.feeds.map(f => `
      <tr data-id="${f.id}">
        <td><b>${esc(f.title || f.url)}</b><br>
          <span class="mono" style="font-size:10px;color:var(--ink3)">${esc(f.url)}</span>
          ${f.last_status === 'error'
            ? `<br><span class="mono" style="font-size:10px;color:var(--accent)">⚠ ${esc((f.error_msg || '').slice(0, 90))}</span>` : ''}</td>
        <td><select class="f-carpeta">${opciones(f.folder_id)}</select></td>
        <td><input class="f-min" type="number" min="5" max="1440" value="${f.refresh_minutes}"></td>
        <td><button class="btn ghost small f-pausa">${f.paused ? '▶' : '⏸'}</button></td>
        <td><button class="btn danger small f-del">Borrar</button></td>
      </tr>`).join('') + '</table></div>';

  $$('tr[data-id]', caja).forEach(tr => {
    const id = Number(tr.dataset.id);
    const feed = state.feeds.find(f => f.id === id);
    $('.f-carpeta', tr).addEventListener('change', async e => {
      await api(`/api/feeds/${id}`, { method: 'PATCH', body: { folder: e.target.value } });
      toast('Carpeta actualizada'); cargarLateral();
    });
    $('.f-min', tr).addEventListener('change', async e => {
      await api(`/api/feeds/${id}`, { method: 'PATCH', body: { refresh_minutes: Number(e.target.value) } });
      toast('Intervalo actualizado');
    });
    $('.f-pausa', tr).addEventListener('click', async () => {
      await api(`/api/feeds/${id}`, { method: 'PATCH', body: { paused: !feed.paused } });
      toast(feed.paused ? 'Fuente reanudada' : 'Fuente pausada');
      await cargarLateral(); pintarTablaFuentes();
    });
    $('.f-del', tr).addEventListener('click', async () => {
      await api(`/api/feeds/${id}`, { method: 'DELETE' });
      toast(`Fuente eliminada: ${feed.title || feed.url}`, {
        texto: 'Volver a añadirla',
        fn: () => anadirFeed(feed.url, null, state.folders.find(x => x.id === feed.folder_id)?.name),
      });
      await cargarLateral(); pintarTablaFuentes();
    });
  });
}

// Lectura
$('#set-escala').addEventListener('input', e => {
  prefs.escala = +e.target.value;
  $('#set-escala-val').textContent = `${prefs.escala} %`;
  aplicarPrefs(); guardarPrefs();
});
$('#set-tamano').addEventListener('input', e => {
  prefs.tam = +e.target.value; $('#set-tamano-val').textContent = `${prefs.tam} px`;
  aplicarPrefs(); guardarPrefs();
});
$('#set-ancho').addEventListener('input', e => {
  prefs.ancho = +e.target.value; $('#set-ancho-val').textContent = `${prefs.ancho} car.`;
  aplicarPrefs(); guardarPrefs();
});
$('#set-fuente-texto').addEventListener('change', e => {
  prefs.familia = e.target.value; aplicarPrefs(); guardarPrefs();
});
$('#set-scroll-leido').addEventListener('change', e => {
  prefs.scrollLeido = e.target.checked; guardarPrefs();
});

// OPML
$('#btn-opml-import').addEventListener('click', async () => {
  const file = $('#opml-file').files[0];
  if (!file) { toast('Elige primero un archivo OPML'); return; }
  const text = await file.text();
  try {
    const r = await fetch('/api/opml/import', { method: 'POST', body: text });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || 'error');
    toast(`OPML: ${j.added} fuentes añadidas, ${j.skipped} ya estaban`);
    cargarLateral();
  } catch (e) { toast('Error al importar: ' + e.message); }
});

// Claves de API
async function pintarClaves() {
  const { keys } = await api('/api/keys');
  $('#key-table').innerHTML = keys.length
    ? '<tr><th>Nombre</th><th>Prefijo</th><th>Creada</th><th>Último uso</th><th></th></tr>' +
      keys.map(k => `<tr>
        <td>${esc(k.name)}</td><td class="mono">${esc(k.prefix)}…</td>
        <td class="mono">${fmtHora(k.created_at)}</td>
        <td class="mono">${k.last_used_at ? fmtHora(k.last_used_at) : '—'}</td>
        <td><button class="btn danger small" data-id="${k.id}">Revocar</button></td></tr>`).join('')
    : '';
  $$('#key-table button').forEach(b => b.addEventListener('click', async () => {
    await api(`/api/keys/${b.dataset.id}`, { method: 'DELETE' });
    toast('Clave revocada'); pintarClaves();
  }));
}
$('#btn-key-create').addEventListener('click', async () => {
  const name = $('#key-name').value.trim();
  const r = await api('/api/keys', { method: 'POST', body: { name } });
  const caja = $('#key-new');
  caja.hidden = false;
  caja.textContent = `${r.key}   ← cópiala ahora, no se vuelve a mostrar`;
  $('#key-name').value = '';
  pintarClaves();
});

// ── Arranque ──────────────────────────────────────────────────────────

$('#set-escala').value = prefs.escala;
$('#set-escala-val').textContent = `${prefs.escala} %`;
$('#set-tamano').value = prefs.tam;
$('#set-tamano-val').textContent = `${prefs.tam} px`;
$('#set-ancho').value = prefs.ancho;
$('#set-ancho-val').textContent = `${prefs.ancho} car.`;
$('#set-fuente-texto').value = prefs.familia;
$('#set-scroll-leido').checked = prefs.scrollLeido;
aplicarPrefs();

cargarLateral()
  .then(() => irA({ type: 'unread' }))
  .catch(e => toast('No se pudo cargar: ' + e.message));

setInterval(refrescarContadores, 90 * 1000);

// Al cambiar el ancho de la ventana la portada se vuelve a repartir.
let _reequilibrio;
window.addEventListener('resize', () => {
  clearTimeout(_reequilibrio);
  _reequilibrio = setTimeout(() => {
    const portada = $('#portada');
    if (portada && !portada.hidden) equilibrarPortada(portada);
  }, 180);
});
