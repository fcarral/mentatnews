"""MentatNews — lector RSS estilo Feedly. FastAPI + SQLite, puerto 9160."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

import ai as ai_mod
import busqueda
import db
import dedup
import endpoints_portada
import endpoints_aifeeds
import endpoints_reglas
import equis
import extractor
import fetcher
import limpieza
import netguard
import opml as opml_mod

_limpieza = limpieza   # nombre que usa el rellenado de image_url

log = logging.getLogger("mentatnews")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

STATIC = Path(__file__).parent / "static"
FETCH_CONCURRENCY = 4
SCHEDULER_TICK_S = 60
RETENTION_DAYS = 90
MAX_PER_FEED = 2000

_fetch_sem: asyncio.Semaphore | None = None


# ── Núcleo de fetch/persistencia ──────────────────────────────────────

def _modulo_limpieza():
    """Reintenta el import: el módulo puede aparecer sin reiniciar el servicio."""
    global _limpieza
    if _limpieza is None:
        try:
            import limpieza as mod
            _limpieza = mod
        except Exception:
            return None
    return _limpieza


IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif")


def _imagen_entrada(e: dict) -> str:
    """Imagen destacada de una entrada. El rascado del HTML es de limpieza; aquí
    solo queda el respaldo de leer los enclosures, que ya vienen resueltos."""
    encl = e.get("enclosures") or []
    mod = _modulo_limpieza()
    if mod is not None:
        try:
            img = mod.imagen_destacada(
                e.get("content_html") or e.get("summary_html") or "",
                base_url=e.get("url") or "", enclosures=encl)
            return (img or "")[:2000]
        except Exception:
            log.warning("limpieza.imagen_destacada falló; se usa el enclosure", exc_info=True)
    for enc in encl:
        url = str(enc.get("url") or "")
        tipo = str(enc.get("type") or "")
        if tipo.startswith("image/") or url.lower().split("?")[0].endswith(IMG_EXT):
            return url[:2000]
    return ""


def upsert_articles(feed_id: int, entries: list[dict]) -> int:
    """Inserta entradas nuevas (dedupe por feed_id+guid). Devuelve nº insertadas."""
    conn = db.get_db()
    now = db.utcnow()
    new = 0
    for e in entries:
        content = e.get("content_html") or e.get("summary_html") or ""
        summary = e.get("summary_html") or ""
        cur = conn.execute(
            """INSERT OR IGNORE INTO articles
               (feed_id, guid, url, title, author, summary, content, published_at,
                fetched_at, sort_at, image_url)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (feed_id, e.get("guid") or e.get("url") or "", e.get("url") or "",
             e.get("title") or "(sin título)", e.get("author") or "",
             summary, content, e.get("published"), now, e.get("published") or now,
             _imagen_entrada(e)),
        )
        if cur.rowcount:
            new += 1
            etiquetas = {t.strip()[:100] for t in (e.get("tags") or []) if t and t.strip()}
            if etiquetas:
                conn.executemany(
                    "INSERT OR IGNORE INTO article_tags(article_id, tag) VALUES (?,?)",
                    [(cur.lastrowid, t) for t in etiquetas],
                )
    conn.commit()
    return new


def fetch_one_feed(feed_row: dict) -> dict:
    """Fetch síncrono de un feed + persistencia. Corre en threadpool."""
    conn = db.get_db()
    fid = feed_row["id"]
    res = fetcher.fetch_and_parse(
        feed_row["url"], etag=feed_row["etag"], last_modified=feed_row["last_modified"]
    )
    now = db.utcnow()
    if res["status"] == "error":
        conn.execute(
            "UPDATE feeds SET last_fetch_at=?, last_status='error', error_count=error_count+1, error_msg=? WHERE id=?",
            (now, res["error"][:500], fid),
        )
        conn.commit()
        return {"feed_id": fid, "status": "error", "new": 0, "error": res["error"]}

    new = 0
    nuevos_ids: list[int] = []
    if res["status"] == "ok":
        antes = conn.execute("SELECT COALESCE(MAX(id),0) m FROM articles").fetchone()["m"]
        new = upsert_articles(fid, res["entries"])
        if new:
            nuevos_ids = [r["id"] for r in conn.execute(
                "SELECT id FROM articles WHERE id > ? AND feed_id = ?", (antes, fid))]
        # título/site/descripción solo se rellenan si el feed aún no los tiene.
        # res["favicon_url"] se descarta a propósito: apunta a Google (ver
        # _bajar_favicon).
        conn.execute(
            """UPDATE feeds SET
                 title = CASE WHEN title='' THEN ? ELSE title END,
                 site_url = CASE WHEN site_url='' THEN ? ELSE site_url END,
                 description = CASE WHEN description='' THEN ? ELSE description END
               WHERE id=?""",
            (res["title"] or feed_row["url"], res["site_url"], res["description"], fid),
        )
    conn.execute(
        "UPDATE feeds SET etag=?, last_modified=?, last_fetch_at=?, last_status=?, error_count=0, error_msg='' WHERE id=?",
        (res["etag"], res["last_modified"], now, res["status"], fid),
    )
    conn.commit()
    # Los filtros de silencio y las alertas se aplican a lo que acaba de entrar,
    # no al listar: así una lista no paga el coste de evaluar reglas cada vez.
    if nuevos_ids:
        try:
            endpoints_reglas.marcar_articulos_nuevos(nuevos_ids)
        except Exception:
            log.exception("reglas: no se pudieron aplicar a los artículos nuevos")
    return {"feed_id": fid, "status": res["status"], "new": new, "error": "",
            "nuevos_ids": nuevos_ids}


# Un servidor a la vez: pedirle a Reddit sus dos feeds en paralelo devuelve 429,
# y 20minutos responde 403 bajo varias peticiones simultáneas. El límite global
# sigue mandando; esto solo evita pisar al mismo dominio.
_sem_dominio: dict[str, asyncio.Semaphore] = {}


async def fetch_feed_async(feed_row: dict) -> dict:
    dominio = (urlparse(feed_row["url"]).hostname or "").lower()
    sem_dom = _sem_dominio.setdefault(dominio, asyncio.Semaphore(1))
    async with _fetch_sem, sem_dom:
        return await asyncio.to_thread(fetch_one_feed, feed_row)


# ── Favicons propios ──────────────────────────────────────────────────
# Pedirlos a google.com/s2/favicons le entrega a Google la lista completa de
# suscripciones del usuario. Se bajan una vez del propio sitio (por netguard,
# que es quien impide que una URL de feed apunte a la red interna) y se sirven
# desde aquí. Si no se puede, el campo queda vacío y el front pone el logo.

FAVICON_DIR = Path(__file__).parent / "data" / "favicons"
FAVICON_MAX = 512 * 1024
FAVICON_TIMEOUT = 6.0
FAVICON_REINTENTO_DIAS = 7
FAVICON_POR_TICK = 3  # se rellenan poco a poco: no encarecen el refresco
UA = "MentatNews/1.0 (+https://github.com/fcarral/mentatnews)"

FAVICON_MIME = {
    ".ico": "image/x-icon", ".png": "image/png", ".jpg": "image/jpeg",
    ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp",
}


def _extension_imagen(datos: bytes, content_type: str) -> str | None:
    """Extensión según los bytes reales; el content-type solo desempata. Muchos
    servidores devuelven un HTML de error con 200 y hay que descartarlo."""
    if datos.startswith(b"\x89PNG"):
        return ".png"
    if datos.startswith(b"\x00\x00\x01\x00"):
        return ".ico"
    if datos.startswith(b"GIF8"):
        return ".gif"
    if datos.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if datos[:4] == b"RIFF" and datos[8:12] == b"WEBP":
        return ".webp"
    cabeza = datos[:512].lstrip().lower()
    if cabeza.startswith(b"<svg") or (cabeza.startswith(b"<?xml") and b"<svg" in cabeza):
        return ".svg"
    if content_type.split(";")[0].strip().lower() == "image/svg+xml":
        return ".svg"
    return None


def _bajar_favicon(fid: int, site_url: str, feed_url: str) -> str:
    """Guarda el icono del sitio en data/favicons/. Devuelve la URL pública o ''."""
    p = urlparse(site_url or feed_url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return ""
    raiz = f"{p.scheme}://{p.netloc}"
    for ruta in ("/favicon.ico", "/apple-touch-icon.png"):
        try:
            with netguard.SafeClient(timeout=FAVICON_TIMEOUT, max_bytes=FAVICON_MAX) as cli:
                r = cli.get(raiz + ruta, headers={"User-Agent": UA})
            if r.status_code != 200 or not r.content:
                continue
            ext = _extension_imagen(r.content, r.headers.get("content-type", ""))
            if ext is None:
                continue
            FAVICON_DIR.mkdir(parents=True, exist_ok=True)
            destino = FAVICON_DIR / f"{fid}{ext}"
            for viejo in FAVICON_DIR.glob(f"{fid}.*"):
                if viejo != destino:
                    viejo.unlink(missing_ok=True)
            destino.write_bytes(r.content)
            return f"/api/favicon/{fid}"
        except Exception as e:
            log.info("favicon %s%s: %s", raiz, ruta, e)
    return ""


def _favicons_pendientes() -> None:
    """Un puñado por tick: feeds sin icono cuyo último intento sea viejo."""
    conn = db.get_db()
    filas = conn.execute(
        """SELECT id, site_url, url FROM feeds
           WHERE favicon_url='' AND url NOT LIKE 'manual:%'
             AND (favicon_at IS NULL OR favicon_at < strftime('%Y-%m-%dT%H:%M:%SZ','now',?))
           ORDER BY favicon_at IS NOT NULL, id LIMIT ?""",
        (f"-{FAVICON_REINTENTO_DIAS} days", FAVICON_POR_TICK),
    ).fetchall()
    for f in filas:
        url = _bajar_favicon(f["id"], f["site_url"] or "", f["url"])
        conn.execute("UPDATE feeds SET favicon_url=?, favicon_at=? WHERE id=?",
                     (url, db.utcnow(), f["id"]))
        conn.commit()


def _due_feeds() -> list[dict]:
    conn = db.get_db()
    rows = conn.execute(
        """SELECT * FROM feeds WHERE paused=0 AND (
             last_fetch_at IS NULL OR
             strftime('%s','now') - strftime('%s', last_fetch_at)
               >= refresh_minutes * 60 * MIN(1 << MIN(error_count, 4), 16)
           )"""
    ).fetchall()
    return [dict(r) for r in rows]


def _prune() -> None:
    conn = db.get_db()
    conn.execute(
        """DELETE FROM articles WHERE saved=0 AND read=1
           AND fetched_at < strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)""",
        (f"-{RETENTION_DAYS} days",),
    )
    conn.execute(
        """DELETE FROM articles WHERE saved=0 AND id IN (
             SELECT id FROM (
               SELECT id, ROW_NUMBER() OVER (
                 PARTITION BY feed_id ORDER BY sort_at DESC
               ) rn FROM articles WHERE saved=0
             ) WHERE rn > ?)""",
        (MAX_PER_FEED,),
    )
    # Los tuits citados por artículos que ya no existen no le sirven a nadie.
    conn.execute(
        """DELETE FROM tuits
           WHERE guardado_en < strftime('%Y-%m-%dT%H:%M:%SZ','now','-60 days')""")
    conn.commit()
    db.reconciliar_contadores()  # seguro barato tras un borrado masivo


# Sello del último tick. Vivía en la tabla settings y se escribía cada 60 s: una
# transacción por minuto en el WAL para un dato que solo mira /api/stats. Ahora
# vive en memoria y baja a disco cada 10 minutos, que es toda la precisión que
# necesita sobrevivir a un reinicio.
_scheduler_last_run: str | None = None
_scheduler_persistido = 0.0
SCHEDULER_PERSIST_S = 600


def _marcar_tick(forzar: bool = False) -> None:
    global _scheduler_last_run, _scheduler_persistido
    _scheduler_last_run = db.utcnow()
    ahora = time.monotonic()
    if forzar or ahora - _scheduler_persistido >= SCHEDULER_PERSIST_S:
        _scheduler_persistido = ahora
        conn = db.get_db()
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('scheduler_last_run',?)",
                     (_scheduler_last_run,))
        conn.commit()


async def scheduler_loop() -> None:
    """Bucle: cada minuto refresca los feeds que tocan según su intervalo."""
    tick = 0
    while True:
        try:
            due = await asyncio.to_thread(_due_feeds)
            if due:
                results = await asyncio.gather(*(fetch_feed_async(f) for f in due))
                new = sum(r["new"] for r in results)
                errs = sum(1 for r in results if r["status"] == "error")
                log.info("scheduler: %d feeds, %d artículos nuevos, %d errores", len(due), new, errs)
            await asyncio.to_thread(_marcar_tick)
            await asyncio.to_thread(_favicons_pendientes)
            tick += 1
            # Las portadas se rehacen antes de caducar. Sin este paso solo se
            # renovaban tras un refresco manual, y el primer clic en un tema
            # después de un par de horas esperaba media hora de reloj al modelo.
            if tick % 10 == 0:
                resultado = await endpoints_portada.preparar_portadas()
                if resultado["hechas"]:
                    log.info("portadas: %d renovadas, %d aún frescas",
                             resultado["hechas"], resultado["frescas"])
            # Los feeds de IA van comiendo lo pendiente poco a poco. Cada pasada
            # es un puñado de llamadas a un modelo barato; repartirlas en el
            # tiempo evita tanto el pico de gasto como el de latencia.
            if tick % 3 == 0:
                resultado = await endpoints_aifeeds.pasar_todos()
                if resultado["mirados"]:
                    log.info("feeds de IA: %d artículos mirados, %d dentro",
                             resultado["mirados"], resultado["dentro"])
            if tick % 30 == 0:
                await asyncio.to_thread(db.checkpoint_wal)
            if tick % 1440 == 0:
                await asyncio.to_thread(_prune)
        except Exception:
            log.exception("scheduler: error en tick")
        await asyncio.sleep(SCHEDULER_TICK_S)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _fetch_sem, _scheduler_last_run
    db.init_db()
    fila = db.get_db().execute(
        "SELECT value FROM settings WHERE key='scheduler_last_run'").fetchone()
    _scheduler_last_run = fila["value"] if fila else None
    _fetch_sem = asyncio.Semaphore(FETCH_CONCURRENCY)
    task = asyncio.create_task(scheduler_loop())
    # Tras un reinicio puede haber portadas caducadas: se rehacen desde ya, sin
    # bloquear el arranque, para que la primera visita no espere.
    calentar = asyncio.create_task(endpoints_portada.preparar_portadas())
    yield
    calentar.cancel()
    task.cancel()
    if _refresco_task and not _refresco_task.done():
        _refresco_task.cancel()
    if _scheduler_last_run:
        _marcar_tick(forzar=True)
    db.cerrar_conexiones()


app = FastAPI(title="MentatNews", lifespan=lifespan)
@app.get("/api/x")
async def tuits_incrustados(ids: str):
    """Contenido de las publicaciones de X que cita un artículo.

    En lote: un artículo puede citar varios tuits y no tiene sentido una ida y
    vuelta por cada uno.
    """
    pedidos = [i.strip() for i in ids.split(",") if i.strip()][:12]
    if not pedidos:
        return {"tuits": {}}
    return {"tuits": await equis.traer_varios(pedidos)}


app.include_router(endpoints_portada.router)
app.include_router(endpoints_reglas.router)
app.include_router(endpoints_aifeeds.router)


@app.middleware("http")
async def api_key_auth(request: Request, call_next):
    """Si viene X-API-Key se valida contra la DB (acceso programático).
    Sin header se confía en la autenticación del proxy inverso (ver README)."""
    key = request.headers.get("x-api-key")
    if key is not None and request.url.path.startswith("/api/"):
        ok = await asyncio.to_thread(db.validate_api_key, key)
        if not ok:
            return JSONResponse({"error": "API key inválida"}, status_code=401)
    return await call_next(request)


# ── Feeds y carpetas ──────────────────────────────────────────────────

def _feed_public(r: dict) -> dict:
    r = dict(r)
    r.pop("etag", None)
    r.pop("last_modified", None)
    return r


@app.get("/api/feeds")
def list_feeds():
    conn = db.get_db()
    feeds = [_feed_public(r) for r in conn.execute(
        """SELECT f.*, COALESCE(u.n,0) AS unread FROM feeds f
           LEFT JOIN (SELECT feed_id, COUNT(*) n FROM articles WHERE read=0 GROUP BY feed_id) u
             ON u.feed_id = f.id
           ORDER BY f.title COLLATE NOCASE"""
    ).fetchall()]
    folders = [dict(r) for r in conn.execute(
        "SELECT * FROM folders ORDER BY position, name COLLATE NOCASE").fetchall()]
    return {"feeds": feeds, "folders": folders}


@app.post("/api/feeds")
async def add_feed(payload: dict = Body(...)):
    url = (payload.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url requerida")
    folder_id = db.ensure_folder(payload.get("folder") or "") if "folder" in payload else payload.get("folder_id")
    conn = db.get_db()
    if conn.execute("SELECT id FROM feeds WHERE url=?", (url,)).fetchone():
        raise HTTPException(409, "Ese feed ya existe")
    cur = conn.execute(
        "INSERT INTO feeds(url, folder_id, created_at, refresh_minutes) VALUES (?,?,?,?)",
        (url, folder_id, db.utcnow(), int(payload.get("refresh_minutes") or 30)),
    )
    conn.commit()
    fid = cur.lastrowid
    row = dict(conn.execute("SELECT * FROM feeds WHERE id=?", (fid,)).fetchone())
    res = await fetch_feed_async(row)
    if res["status"] == "error":
        # el feed queda registrado pero avisamos; quizá la URL era del sitio, no del feed
        return JSONResponse({"feed_id": fid, "warning": res["error"]}, status_code=202)
    row = dict(conn.execute("SELECT * FROM feeds WHERE id=?", (fid,)).fetchone())
    return {"feed": _feed_public(row), "new_articles": res["new"]}


@app.patch("/api/feeds/{fid}")
def update_feed(fid: int, payload: dict = Body(...)):
    conn = db.get_db()
    if not conn.execute("SELECT id FROM feeds WHERE id=?", (fid,)).fetchone():
        raise HTTPException(404, "feed no existe")
    sets, vals = [], []
    if "title" in payload:
        sets.append("title=?"); vals.append(str(payload["title"]).strip())
    if "refresh_minutes" in payload:
        m = max(5, min(1440, int(payload["refresh_minutes"])))
        sets.append("refresh_minutes=?"); vals.append(m)
    if "paused" in payload:
        sets.append("paused=?"); vals.append(1 if payload["paused"] else 0)
    if "folder" in payload:
        sets.append("folder_id=?"); vals.append(db.ensure_folder(payload["folder"]))
    if "url" in payload:
        sets.append("url=?"); vals.append(str(payload["url"]).strip())
    if not sets:
        raise HTTPException(400, "nada que actualizar")
    vals.append(fid)
    conn.execute(f"UPDATE feeds SET {', '.join(sets)} WHERE id=?", vals)
    conn.commit()
    return {"ok": True}


@app.delete("/api/feeds/{fid}")
def delete_feed(fid: int):
    conn = db.get_db()
    conn.execute("DELETE FROM feeds WHERE id=?", (fid,))
    conn.execute("DELETE FROM folders WHERE id NOT IN (SELECT DISTINCT folder_id FROM feeds WHERE folder_id IS NOT NULL)")
    conn.commit()
    return {"ok": True}


@app.post("/api/feeds/{fid}/refresh")
async def refresh_feed(fid: int):
    conn = db.get_db()
    row = conn.execute("SELECT * FROM feeds WHERE id=?", (fid,)).fetchone()
    if not row:
        raise HTTPException(404, "feed no existe")
    return await fetch_feed_async(dict(row))


# Estado del refresco global. Con 200 fuentes y 4 en paralelo, hacerlo dentro de
# la petición son minutos de espera: se lanza en segundo plano y el cliente
# sondea. Basta una variable de módulo porque la app tiene un solo proceso.
_refresco = {"job_id": None, "en_curso": False, "total": 0, "hechos": 0,
             "nuevos": 0, "errores": 0, "iniciado_en": None, "terminado_en": None}
_refresco_task: asyncio.Task | None = None


async def _refrescar_todo() -> None:
    try:
        rows = await asyncio.to_thread(
            lambda: [dict(r) for r in db.get_db().execute(
                "SELECT * FROM feeds WHERE paused=0").fetchall()])
        _refresco["total"] = len(rows)
        for terminado in asyncio.as_completed([fetch_feed_async(r) for r in rows]):
            res = await terminado
            _refresco["hechos"] += 1
            _refresco["nuevos"] += res["new"]
            if res["status"] == "error":
                _refresco["errores"] += 1
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("refresco global: fallo")
    finally:
        _refresco["en_curso"] = False
        _refresco["terminado_en"] = db.utcnow()

    # Con lo nuevo ya dentro, se dejan las portadas listas en caché: así abrir
    # la Portada es instantáneo en vez de esperar al modelo con la app abierta.
    try:
        resultado = await endpoints_portada.preparar_portadas()
        log.info("portadas: %d nuevas, %d seguían vigentes",
                 resultado["hechas"], resultado["vigentes"])
    except Exception:
        log.exception("portadas: no se pudieron preparar")


@app.post("/api/refresh")
async def refresh_all():
    """Arranca el refresco global y devuelve al instante. Uno cada vez."""
    global _refresco_task
    if _refresco["en_curso"]:
        # 409 y no un segundo trabajo: dos refrescos a la vez duplican el tráfico
        # a cada fuente y se pisan al escribir etag/last_modified.
        return JSONResponse({**_refresco, "error": "ya hay un refresco en curso"},
                            status_code=409)
    # Sin await entre la comprobación y la marca: nadie puede colarse en medio.
    _refresco.update(job_id=uuid.uuid4().hex[:12], en_curso=True, total=0, hechos=0,
                     nuevos=0, errores=0, iniciado_en=db.utcnow(), terminado_en=None)
    _refresco_task = asyncio.create_task(_refrescar_todo())
    # new/errors siguen ahí por compatibilidad con el front anterior, que los leía
    # de la respuesta; el progreso real está en /api/refresh/status.
    return JSONResponse({**_refresco, "new": 0, "errors": []}, status_code=202)


@app.get("/api/refresh/status")
def refresh_status():
    return dict(_refresco)


@app.get("/api/favicon/{fid}")
def favicon(fid: int):
    for f in FAVICON_DIR.glob(f"{fid}.*"):
        return FileResponse(f, media_type=FAVICON_MIME.get(f.suffix, "image/x-icon"),
                            headers={"Cache-Control": "public, max-age=604800"})
    raise HTTPException(404, "sin favicon")


@app.patch("/api/folders/{folder_id}")
def rename_folder(folder_id: int, payload: dict = Body(...)):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name requerido")
    conn = db.get_db()
    conn.execute("UPDATE folders SET name=? WHERE id=?", (name, folder_id))
    conn.commit()
    return {"ok": True}


# ── Artículos ─────────────────────────────────────────────────────────

# Orden de todas las listas. Es una columna real (ver db.SCHEMA) precisamente
# para que los índices puedan servirlo sin B-TREE temporal.
SORT = "a.sort_at"

# "Hoy" es el día natural en CST (UTC-6 fijo, la zona en la que se lee la app),
# no las últimas 24 h. sort_at se guarda en UTC: se corre 'now' a CST, se corta
# el día allí y se devuelven ambos extremos a UTC.
HOY_INI = "strftime('%Y-%m-%dT%H:%M:%SZ','now','-6 hours','start of day','+6 hours')"
HOY_FIN = "strftime('%Y-%m-%dT%H:%M:%SZ','now','-6 hours','start of day','+1 day','+6 hours')"


@app.get("/api/articles")
def list_articles(feed_id: int | None = None, folder_id: int | None = None,
                  unread: int = 0, saved: int = 0, today: int = 0,
                  q: str | None = None, limit: int = 50, before_id: int | None = None,
                  full: int = 0, incluir_silenciados: int = 0,
                  solo_silenciados: int = 0, alertas: int = 0,
                  ai_feed: int | None = None):
    conn = db.get_db()
    limit = max(1, min(200, limit))
    where, vals = ["1=1"], []
    if solo_silenciados:
        where.append("a.silenciado=1")
    elif not incluir_silenciados:
        where.append("a.silenciado=0")
    if feed_id:
        where.append("a.feed_id=?"); vals.append(feed_id)
    if folder_id:
        # Filtrar por la tabla ya unida (y no con un IN de subconsulta) deja que
        # SQLite recorra el índice de orden y descarte por carpeta al vuelo.
        where.append("f.folder_id=?"); vals.append(folder_id)
    if unread:
        where.append("a.read=0")
    if saved:
        where.append("a.saved=1")
    if today:
        where.append(f"{SORT} >= {HOY_INI} AND {SORT} < {HOY_FIN}")
    if alertas:
        where.append("a.id IN (SELECT articulo_id FROM alertas_articulo)")
    if ai_feed:
        where.append(
            "a.id IN (SELECT articulo_id FROM ai_feed_articulo WHERE ai_feed_id=?)")
        vals.append(ai_feed)
    aviso_busqueda = ""
    if q:
        # Búsqueda avanzada: operadores, frases, exclusiones y filtros por campo
        # (fuente:, tema:, desde:, estado:…). Antes se troceaba la consulta y se
        # entrecomillaba cada palabra, así que "incendio AND Huelva" buscaba la
        # palabra literal "AND" y no devolvía nada.
        analizada = busqueda.parsear(q)
        if analizada["error"]:
            aviso_busqueda = analizada["error"]
        else:
            f = analizada["filtros"]
            if analizada["fts"]:
                where.append(
                    "a.id IN (SELECT rowid FROM articles_fts WHERE articles_fts MATCH ?)")
                vals.append(analizada["fts"])
            for nombre in f.get("fuente", []) or []:
                where.append("f.title LIKE ?"); vals.append(f"%{nombre}%")
            for tema in f.get("tema", []) or []:
                where.append(
                    "f.folder_id IN (SELECT id FROM folders WHERE name LIKE ?)")
                vals.append(f"%{tema}%")
            for autor in f.get("autor", []) or []:
                where.append("a.author LIKE ?"); vals.append(f"%{autor}%")
            if f.get("desde"):
                where.append(f"{SORT} >= ?"); vals.append(f["desde"])
            if f.get("hasta"):
                where.append(f"{SORT} <= ?"); vals.append(f["hasta"])
            if f.get("estado") == "sinleer":
                where.append("a.read=0")
            elif f.get("estado") == "leido":
                where.append("a.read=1")
            elif f.get("estado") == "guardado":
                where.append("a.saved=1")
            if f.get("tiene_imagen"):
                where.append("a.image_url <> ''")
    if before_id:
        ref = conn.execute(
            f"SELECT {SORT} s, id FROM articles a WHERE id=?", (before_id,)).fetchone()
        if ref:
            # Comparación por tupla: SQLite la resuelve como búsqueda dentro del
            # índice. El OR anidado equivalente lo recorre desde el principio.
            where.append(f"({SORT}, a.id) < (?, ?)")
            vals += [ref["s"], before_id]
    body = "a.content" if full else "''"
    # Se pide un colchón por encima del límite: al colapsar la misma noticia
    # repetida en varias fuentes se pierden filas, y sin ese margen la página
    # llegaría corta y el scroll infinito se daría por terminado antes de tiempo.
    pedidas = min(limit + 25, 200)
    rows = conn.execute(
        f"""SELECT a.id, a.feed_id, a.url, a.title, a.author, a.published_at, a.fetched_at,
                   a.read, a.saved, a.image_url, substr(a.summary,1,600) AS summary,
                   {body} AS content, f.title AS feed_title, f.favicon_url
            FROM articles a JOIN feeds f ON f.id=a.feed_id
            WHERE {' AND '.join(where)}
            ORDER BY {SORT} DESC, a.id DESC LIMIT ?""",
        vals + [pedidas],
    ).fetchall()
    arts = [dict(r) for r in rows]
    hay_mas = len(arts) == pedidas

    # Resumen legible: fuera los metadatos que algunos feeds meten en el cuerpo
    # ("Article URL: … Points: 7"), que si no ocupan el 39% de las filas.
    for a in arts:
        a["summary_limpio"] = limpieza.limpiar_resumen(a.get("summary") or "",
                                                       titulo=a["title"])

    # La misma noticia contada por varias fuentes se muestra una vez, con un
    # "+N" que dice cuántas más la traen. Representa al grupo la que aparecía
    # primero, que en este orden es la más reciente.
    repeticiones: dict[int, int] = {}
    ocultos: set[int] = set()
    for grupo in dedup.agrupar(arts):
        if len(grupo) < 2:
            continue
        del_grupo = {a["id"] for a in grupo}
        rep = next(a["id"] for a in arts if a["id"] in del_grupo)
        repeticiones[rep] = len(grupo) - 1
        ocultos |= del_grupo - {rep}
    if ocultos:
        arts = [a for a in arts if a["id"] not in ocultos]
    for a in arts:
        a["duplicados"] = repeticiones.get(a["id"], 0)

    if len(arts) > limit:
        arts = arts[:limit]
        hay_mas = True
    # Las etiquetas de la página entera en una sola consulta, no una por artículo.
    if arts:
        ids = [a["id"] for a in arts]
        por_id: dict[int, list[str]] = {}
        for r in conn.execute(
            f"SELECT article_id, tag FROM article_tags WHERE article_id IN ({','.join('?' * len(ids))})",
            ids,
        ):
            por_id.setdefault(r["article_id"], []).append(r["tag"])
        for a in arts:
            a["tags"] = por_id.get(a["id"], [])
    return {"articles": arts, "has_more": hay_mas}


@app.get("/api/articles/{aid}")
def get_article(aid: int):
    conn = db.get_db()
    row = conn.execute(
        """SELECT a.*, f.title AS feed_title, f.favicon_url, f.site_url
           FROM articles a JOIN feeds f ON f.id=a.feed_id WHERE a.id=?""", (aid,)).fetchone()
    if not row:
        raise HTTPException(404, "no existe")
    art = dict(row)
    art["tags"] = [r["tag"] for r in conn.execute(
        "SELECT tag FROM article_tags WHERE article_id=? ORDER BY tag", (aid,))]
    return art


@app.patch("/api/articles/{aid}")
def update_article(aid: int, payload: dict = Body(...)):
    conn = db.get_db()
    sets, vals = [], []
    for field in ("read", "saved"):
        if field in payload:
            sets.append(f"{field}=?"); vals.append(1 if payload[field] else 0)
    if not sets:
        raise HTTPException(400, "nada que actualizar")
    vals.append(aid)
    conn.execute(f"UPDATE articles SET {', '.join(sets)} WHERE id=?", vals)
    conn.commit()
    return {"ok": True}


# Orden sin alias de tabla, para usarlo en UPDATE (el de arriba lleva "a.")
SORT_PLANO = "sort_at"

# Último lote marcado como leído, para poder deshacerlo. La app tiene un solo
# usuario: guardarlo en memoria basta y evita una tabla de historial.
_ultimo_marcado: list[int] = []


@app.post("/api/articles/mark_read")
def mark_read(payload: dict = Body(default={})):
    """Marca como leído SOLO lo que cae dentro de la vista actual."""
    global _ultimo_marcado
    conn = db.get_db()
    where, vals = ["read=0"], []
    if payload.get("feed_id"):
        where.append("feed_id=?"); vals.append(payload["feed_id"])
    if payload.get("folder_id"):
        where.append("feed_id IN (SELECT id FROM feeds WHERE folder_id=?)")
        vals.append(payload["folder_id"])
    if payload.get("saved"):
        where.append("saved=1")
    if payload.get("today"):
        where.append(f"{SORT_PLANO} >= {HOY_INI} AND {SORT_PLANO} < {HOY_FIN}")
    if payload.get("q"):
        fts = " ".join(f'"{t}"' for t in str(payload["q"]).replace('"', "").split())
        where.append("id IN (SELECT rowid FROM articles_fts WHERE articles_fts MATCH ?)")
        vals.append(fts)
    if payload.get("before_id"):
        where.append("id <= ?"); vals.append(payload["before_id"])

    clausula = " AND ".join(where)
    ids = [r["id"] for r in conn.execute(
        f"SELECT id FROM articles WHERE {clausula}", vals).fetchall()]
    if not ids:
        return {"marked": 0}
    conn.execute(f"UPDATE articles SET read=1 WHERE {clausula}", vals)
    conn.commit()
    _ultimo_marcado = ids
    return {"marked": len(ids)}


@app.post("/api/articles/mark_read/undo")
def mark_read_undo():
    """Devuelve a no leído el último lote marcado."""
    global _ultimo_marcado
    if not _ultimo_marcado:
        raise HTTPException(404, "no hay nada que deshacer")
    conn = db.get_db()
    for i in range(0, len(_ultimo_marcado), 500):
        lote = _ultimo_marcado[i:i + 500]
        conn.execute(
            f"UPDATE articles SET read=0 WHERE id IN ({','.join('?' * len(lote))})", lote)
    conn.commit()
    n = len(_ultimo_marcado)
    _ultimo_marcado = []
    return {"restored": n}


# ── Texto completo, artículos por URL, asistente IA ───────────────────

MANUAL_FEED_URL = "manual://guardados"


@app.get("/api/articles/{aid}/fulltext")
async def article_fulltext(aid: int, refresh: int = 0):
    """Extrae (y cachea) el artículo completo desde la URL original."""
    conn = db.get_db()
    row = conn.execute("SELECT id, url, fulltext FROM articles WHERE id=?", (aid,)).fetchone()
    if not row:
        raise HTTPException(404, "no existe")
    if row["fulltext"] and not refresh:
        return {"html": row["fulltext"], "cached": True}
    if not row["url"]:
        raise HTTPException(400, "el artículo no tiene URL original")
    res = await asyncio.to_thread(extractor.extract_fulltext, row["url"])
    if res["status"] != "ok":
        raise HTTPException(422, res["error"] or "no se pudo extraer contenido")
    html = fetcher.sanitize_html(res["html"])
    conn.execute("UPDATE articles SET fulltext=? WHERE id=?", (html, aid))
    conn.commit()
    return {"html": html, "cached": False, "title": res["title"]}


def _manual_feed_id() -> int:
    conn = db.get_db()
    row = conn.execute("SELECT id FROM feeds WHERE url=?", (MANUAL_FEED_URL,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO feeds(url, title, paused, created_at) VALUES (?,?,1,?)",
        (MANUAL_FEED_URL, "Guardados por URL", db.utcnow()),
    )
    conn.commit()
    return cur.lastrowid


@app.post("/api/articles/from_url")
async def article_from_url(payload: dict = Body(...)):
    """Guarda una URL suelta como artículo (extracción de texto completo)."""
    url = (payload.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "URL inválida")
    res = await asyncio.to_thread(extractor.extract_fulltext, url)
    fid = _manual_feed_id()
    conn = db.get_db()
    html = fetcher.sanitize_html(res["html"]) if res["status"] == "ok" else ""
    title = res["title"] or url
    ahora = db.utcnow()
    cur = conn.execute(
        """INSERT OR IGNORE INTO articles
           (feed_id, guid, url, title, author, summary, content, fulltext,
            published_at, fetched_at, sort_at, saved)
           VALUES (?,?,?,?,?,?,?,?,NULL,?,?,1)""",
        (fid, url, url, title, res.get("author") or "", html[:800], html, html,
         ahora, ahora),
    )
    conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(409, "Esa URL ya estaba guardada")
    if res["status"] != "ok":
        return JSONResponse({"id": cur.lastrowid, "warning": f"guardado sin contenido: {res['error']}"},
                            status_code=202)
    return {"id": cur.lastrowid, "title": title}


@app.post("/api/ai/suggest")
async def ai_suggest(payload: dict = Body(...)):
    """Claude sugiere fuentes según la petición; el servidor las verifica."""
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt requerido")
    conn = db.get_db()
    existentes = [r["url"] for r in conn.execute("SELECT url FROM feeds").fetchall()]
    try:
        data = await asyncio.to_thread(ai_mod.suggest_feeds, prompt, existentes)
    except Exception as e:
        log.exception("ai_suggest: error llamando a Claude")
        raise HTTPException(502, f"Error consultando a la IA: {e}")

    async def verificar(s: dict) -> dict:
        r = await asyncio.to_thread(fetcher.fetch_and_parse, s["feed_url"])
        if r["status"] == "ok" and r["entries"]:
            return {**s, "verified": True, "title": r["title"] or s["title"]}
        found = await asyncio.to_thread(
            opml_mod.discover_feeds, s.get("site_url") or s["feed_url"])
        if found:
            return {**s, "verified": True, "feed_url": found[0]["feed_url"],
                    "title": found[0]["title"] or s["title"]}
        return {**s, "verified": False}

    sugerencias = await asyncio.gather(*(verificar(s) for s in data.get("suggestions", [])))
    # dedupe: el autodiscovery puede resolver dos sugerencias al mismo feed
    vistos, unicas = set(existentes), []
    for s in sugerencias:
        if s["feed_url"] in vistos:
            continue
        vistos.add(s["feed_url"])
        unicas.append(s)
    vivos = [s for s in unicas if s["verified"]]
    muertos = [s for s in unicas if not s["verified"]]
    return {"suggestions": vivos + muertos, "note": data.get("note", "")}


# ── Descubrimiento, OPML, catálogo ────────────────────────────────────

@app.get("/api/discover")
async def discover(url: str):
    results = await asyncio.to_thread(opml_mod.discover_feeds, url)
    return {"results": results}


@app.get("/api/opml/export")
def opml_export():
    conn = db.get_db()
    rows = conn.execute(
        """SELECT f.title, f.url AS xml_url, f.site_url AS html_url,
                  COALESCE(fo.name,'') AS folder
           FROM feeds f LEFT JOIN folders fo ON fo.id=f.folder_id
           ORDER BY fo.name, f.title"""
    ).fetchall()
    xml = opml_mod.build_opml([dict(r) for r in rows])
    return Response(content=xml, media_type="text/x-opml",
                    headers={"Content-Disposition": "attachment; filename=mentatnews.opml"})


@app.post("/api/opml/import")
async def opml_import(request: Request):
    text = (await request.body()).decode("utf-8", errors="replace")
    try:
        items = opml_mod.parse_opml(text)
    except ValueError as e:
        raise HTTPException(400, f"OPML inválido: {e}")
    conn = db.get_db()
    added = skipped = 0
    for it in items:
        if conn.execute("SELECT 1 FROM feeds WHERE url=?", (it["xml_url"],)).fetchone():
            skipped += 1
            continue
        conn.execute(
            "INSERT INTO feeds(url, title, site_url, folder_id, created_at) VALUES (?,?,?,?,?)",
            (it["xml_url"], it.get("title") or "", it.get("html_url") or "",
             db.ensure_folder(it.get("folder") or ""), db.utcnow()),
        )
        added += 1
    conn.commit()
    return {"added": added, "skipped": skipped}


@app.get("/api/catalog")
def catalog():
    path = STATIC / "catalog.json"
    if not path.exists():
        return {"version": 0, "categories": []}
    return json.loads(path.read_text())


# ── API keys, stats, salud ────────────────────────────────────────────

@app.get("/api/keys")
def list_keys():
    conn = db.get_db()
    rows = conn.execute(
        "SELECT id, name, prefix, created_at, last_used_at FROM api_keys WHERE revoked=0 ORDER BY id"
    ).fetchall()
    return {"keys": [dict(r) for r in rows]}


@app.post("/api/keys")
def create_key(payload: dict = Body(...)):
    name = (payload.get("name") or "").strip() or "sin nombre"
    kid, key = db.create_api_key(name)
    return {"id": kid, "key": key, "aviso": "Guárdala ahora: no se volverá a mostrar."}


@app.delete("/api/keys/{kid}")
def revoke_key(kid: int):
    conn = db.get_db()
    conn.execute("UPDATE api_keys SET revoked=1 WHERE id=?", (kid,))
    conn.commit()
    return {"ok": True}


@app.get("/api/stats")
def stats():
    """Ningún contador recorre articles: los totales salen de la tabla de
    contadores y 'hoy' es un rango sobre el índice de sort_at."""
    conn = db.get_db()
    cont = db.contadores()
    row = conn.execute(
        f"""SELECT
             (SELECT COUNT(*) FROM feeds) feeds,
             (SELECT COUNT(*) FROM articles WHERE sort_at >= {HOY_INI} AND sort_at < {HOY_FIN}) today,
             (SELECT COUNT(*) FROM feeds WHERE last_status='error') feeds_error,
             (SELECT COUNT(*) FROM alertas_articulo x JOIN articles a ON a.id=x.articulo_id
              WHERE a.read=0 AND a.silenciado=0) alertas,
             (SELECT COUNT(*) FROM articles WHERE silenciado=1) silenciados"""
    ).fetchone()
    return {
        "feeds": row["feeds"],
        "articles": cont.get("articles", 0),
        "unread": cont.get("unread", 0),
        "saved": cont.get("saved", 0),
        "today": row["today"],
        "feeds_error": row["feeds_error"],
        "alertas": row["alertas"],
        "silenciados": row["silenciados"],
        "scheduler_last_run": _scheduler_last_run,
    }


@app.get("/api/health")
def health():
    try:
        s = stats()
        return {"ok": True, **s}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ── UI estática ───────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/robots.txt")
def robots():
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=9160)
