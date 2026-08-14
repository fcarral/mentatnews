"""Capa SQLite de MentatNews: esquema, conexión y helpers."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "mentatnews.db"

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    position INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    site_url TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    favicon_url TEXT NOT NULL DEFAULT '',
    folder_id INTEGER REFERENCES folders(id) ON DELETE SET NULL,
    refresh_minutes INTEGER NOT NULL DEFAULT 30,
    etag TEXT,
    last_modified TEXT,
    last_fetch_at TEXT,
    last_status TEXT NOT NULL DEFAULT '',
    error_count INTEGER NOT NULL DEFAULT 0,
    error_msg TEXT NOT NULL DEFAULT '',
    paused INTEGER NOT NULL DEFAULT 0,
    favicon_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY,
    feed_id INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    guid TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    sort_at TEXT,
    image_url TEXT NOT NULL DEFAULT '',
    read INTEGER NOT NULL DEFAULT 0,
    saved INTEGER NOT NULL DEFAULT 0,
    UNIQUE(feed_id, guid)
);

-- Tabla y no columna JSON: "dame lo etiquetado como X" es la razón de guardar
-- las etiquetas, y con JSON esa consulta es un json_each sobre toda la tabla.
-- Aquí es una búsqueda por índice, y cada etiqueta se guarda una sola vez por
-- artículo. COLLATE NOCASE colapsa "AI"/"ai", que los feeds mezclan.
CREATE TABLE IF NOT EXISTS article_tags (
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    tag TEXT NOT NULL COLLATE NOCASE,
    PRIMARY KEY (article_id, tag)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_article_tags_tag ON article_tags(tag, article_id);

-- sort_at materializa COALESCE(published_at, fetched_at): una expresión no es
-- indexable como tal y obligaba a ordenar la tabla entera en cada listado.
-- Los índices llevan el ORDER BY completo (sort_at DESC, id DESC) para que el
-- desempate por id tampoco necesite un B-TREE temporal.
CREATE INDEX IF NOT EXISTS idx_art_sort ON articles(sort_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_art_sort_unread ON articles(sort_at DESC, id DESC) WHERE read=0;
CREATE INDEX IF NOT EXISTS idx_art_sort_saved ON articles(sort_at DESC, id DESC) WHERE saved=1;
CREATE INDEX IF NOT EXISTS idx_art_feed_sort ON articles(feed_id, sort_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_art_feed_sort_unread
    ON articles(feed_id, sort_at DESC, id DESC) WHERE read=0;

-- Red de seguridad: cualquier inserción futura que olvide sort_at queda ordenada
-- igual. El WHEN la deja en nada cuando la ruta normal ya lo rellenó.
CREATE TRIGGER IF NOT EXISTS articles_sort_ins AFTER INSERT ON articles
WHEN new.sort_at IS NULL BEGIN
    UPDATE articles SET sort_at = COALESCE(new.published_at, new.fetched_at) WHERE id = new.id;
END;

CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    title, content, content='articles', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS articles_ai AFTER INSERT ON articles BEGIN
    INSERT INTO articles_fts(rowid, title, content) VALUES (new.id, new.title, new.content);
END;
CREATE TRIGGER IF NOT EXISTS articles_ad AFTER DELETE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, content)
    VALUES ('delete', old.id, old.title, old.content);
END;

-- Publicaciones de X ya resueltas. `datos` NULL = no se pudo traer, se
-- guarda igual para no volver a pedirlo en cada lectura del artículo.
CREATE TABLE IF NOT EXISTS tuits (
    id           TEXT PRIMARY KEY,
    datos        TEXT,
    guardado_en  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    prefix TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked INTEGER NOT NULL DEFAULT 0
);

-- Reglas de texto: las de silencio esconden un artículo, las de alerta lo
-- destacan. Comparten motor y tabla porque solo cambian en qué se traduce el
-- acierto; separarlas duplicaría el CRUD y la interfaz sin ganar nada.
CREATE TABLE IF NOT EXISTS reglas (
    id INTEGER PRIMARY KEY,
    tipo TEXT NOT NULL CHECK (tipo IN ('silencio', 'alerta')),
    nombre TEXT NOT NULL DEFAULT '',
    campo TEXT NOT NULL DEFAULT 'cualquiera',
    operador TEXT NOT NULL DEFAULT 'contiene',
    patron TEXT NOT NULL,
    sensible INTEGER NOT NULL DEFAULT 0,
    ambito_carpeta INTEGER REFERENCES folders(id) ON DELETE CASCADE,
    ambito_feed INTEGER REFERENCES feeds(id) ON DELETE CASCADE,
    activa INTEGER NOT NULL DEFAULT 1,
    aciertos INTEGER NOT NULL DEFAULT 0,
    creada_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reglas_tipo ON reglas(tipo) WHERE activa=1;

-- Qué alerta disparó cada artículo. Un artículo puede disparar varias.
CREATE TABLE IF NOT EXISTS alertas_articulo (
    articulo_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    regla_id INTEGER NOT NULL REFERENCES reglas(id) ON DELETE CASCADE,
    visto INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (articulo_id, regla_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_alertas_regla ON alertas_articulo(regla_id, articulo_id);
CREATE INDEX IF NOT EXISTS idx_alertas_sin_ver ON alertas_articulo(articulo_id) WHERE visto=0;

-- Feeds de IA: un tema descrito en lenguaje natural que se comporta como una
-- carpeta. La pertenencia la decide un modelo barato al entrar cada artículo.
CREATE TABLE IF NOT EXISTS ai_feeds (
    id INTEGER PRIMARY KEY,
    nombre TEXT NOT NULL UNIQUE,
    descripcion TEXT NOT NULL,
    activo INTEGER NOT NULL DEFAULT 1,
    ultima_pasada TEXT,
    creado_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_feed_articulo (
    ai_feed_id INTEGER NOT NULL REFERENCES ai_feeds(id) ON DELETE CASCADE,
    articulo_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    PRIMARY KEY (ai_feed_id, articulo_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_aifeed_art ON ai_feed_articulo(articulo_id);

-- Los descartados se anotan igual que los aceptados: si no, cada pasada
-- volvería a preguntarle al modelo por los mismos artículos.
CREATE TABLE IF NOT EXISTS ai_feed_descartado (
    ai_feed_id INTEGER NOT NULL REFERENCES ai_feeds(id) ON DELETE CASCADE,
    articulo_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    PRIMARY KEY (ai_feed_id, articulo_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Contadores materializados. El watchdog sondea /api/health cada 30 s: contar
-- filas ahí ata el coste al tamaño del corpus (50k filas por sondeo). Un índice
-- parcial abarata el COUNT pero sigue recorriendo una entrada por artículo; la
-- tabla de contadores lo deja en O(1) y los triggers la mantienen aunque alguien
-- escriba por fuera de la app (sqlite3 a mano, borrado en cascada de un feed).
-- init_db la reconcilia en cada arranque, así una deriva no puede perpetuarse.
CREATE TABLE IF NOT EXISTS contadores (
    clave TEXT PRIMARY KEY,
    valor INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO contadores(clave, valor) VALUES ('articles',0),('unread',0),('saved',0);

CREATE TRIGGER IF NOT EXISTS articles_cnt_ins AFTER INSERT ON articles BEGIN
    UPDATE contadores SET valor = valor + 1 WHERE clave='articles';
    UPDATE contadores SET valor = valor + 1 WHERE clave='unread' AND new.read=0;
    UPDATE contadores SET valor = valor + 1 WHERE clave='saved'  AND new.saved=1;
END;
CREATE TRIGGER IF NOT EXISTS articles_cnt_del AFTER DELETE ON articles BEGIN
    UPDATE contadores SET valor = valor - 1 WHERE clave='articles';
    UPDATE contadores SET valor = valor - 1 WHERE clave='unread' AND old.read=0;
    UPDATE contadores SET valor = valor - 1 WHERE clave='saved'  AND old.saved=1;
END;
CREATE TRIGGER IF NOT EXISTS articles_cnt_read AFTER UPDATE OF read ON articles
WHEN old.read <> new.read BEGIN
    UPDATE contadores SET valor = valor + (CASE WHEN new.read=0 THEN 1 ELSE -1 END)
     WHERE clave='unread';
END;
CREATE TRIGGER IF NOT EXISTS articles_cnt_saved AFTER UPDATE OF saved ON articles
WHEN old.saved <> new.saved BEGIN
    UPDATE contadores SET valor = valor + (CASE WHEN new.saved=1 THEN 1 ELSE -1 END)
     WHERE clave='saved';
END;
"""

_local = threading.local()
_conexiones: list[sqlite3.Connection] = []
_conexiones_lock = threading.Lock()


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_db() -> sqlite3.Connection:
    """Conexión por hilo (uvicorn + threadpool del scheduler)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False no comparte nada: la conexión sigue siendo de un
        # solo hilo. Es para poder cerrarlas al apagar desde el hilo principal,
        # que es el único momento en que otro hilo las toca.
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        # Con WAL, NORMAL no puede corromper la base: como mucho pierde las
        # últimas transacciones si se va la luz. FULL obligaba a un fsync por
        # commit, y aquí se commitea muy a menudo.
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
        with _conexiones_lock:
            _conexiones.append(conn)
    return conn


def checkpoint_wal(modo: str = "PASSIVE") -> None:
    """Vuelca el WAL a la base. Si hay lectores activos no hace nada y ya."""
    try:
        get_db().execute(f"PRAGMA wal_checkpoint({modo})")
    except sqlite3.Error:
        pass


def cerrar_conexiones() -> None:
    """Cierra las conexiones de todos los hilos y compacta el WAL. Al apagar."""
    with _conexiones_lock:
        abiertas, _conexiones[:] = list(_conexiones), []
    for conn in abiertas:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    _local.conn = None
    try:
        final = sqlite3.connect(DB_PATH, timeout=5)
        final.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        final.close()
    except sqlite3.Error:
        pass


def _añadir_columna(conn: sqlite3.Connection, tabla: str, columna: str, decl: str) -> bool:
    """ALTER TABLE ADD COLUMN idempotente. True si la acaba de añadir."""
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({tabla})")}
    if not cols or columna in cols:  # tabla aún inexistente: la crea el esquema
        return False
    conn.execute(f"ALTER TABLE {tabla} ADD COLUMN {columna} {decl}")
    return True


# Índices de la versión anterior: los sustituyen los compuestos sobre sort_at y
# solo encarecían cada INSERT.
INDICES_OBSOLETOS = ("idx_articles_feed", "idx_articles_read", "idx_articles_saved")


def init_db() -> None:
    conn = get_db()
    # Las columnas primero: el esquema crea índices que ya las mencionan.
    _añadir_columna(conn, "articles", "fulltext", "TEXT")
    _añadir_columna(conn, "articles", "sort_at", "TEXT")
    _añadir_columna(conn, "articles", "image_url", "TEXT NOT NULL DEFAULT ''")
    # Silenciado por una regla: el artículo se guarda igual —para poder revisarlo
    # y para que quitar la regla lo devuelva— pero no aparece en las listas.
    _añadir_columna(conn, "articles", "silenciado", "INTEGER NOT NULL DEFAULT 0")
    _añadir_columna(conn, "articles", "motivo_silencio", "INTEGER")
    _añadir_columna(conn, "feeds", "favicon_at", "TEXT")
    conn.executescript(SCHEMA)

    # Relleno del orden materializado en las filas antiguas (solo las que faltan,
    # así arrancar dos veces no vuelve a tocar nada).
    conn.execute(
        "UPDATE articles SET sort_at = COALESCE(published_at, fetched_at) WHERE sort_at IS NULL"
    )
    for idx in INDICES_OBSOLETOS:
        conn.execute(f"DROP INDEX IF EXISTS {idx}")
    # Los iconos de Google le entregaban a Google la lista de suscripciones del
    # usuario en cada carga. Se vacían y se vuelven a bajar desde cada sitio.
    conn.execute(
        "UPDATE feeds SET favicon_url='', favicon_at=NULL "
        "WHERE favicon_url LIKE '%google.com/s2/favicons%'"
    )
    conn.commit()
    reconciliar_contadores()


def reconciliar_contadores() -> dict[str, int]:
    """Recalcula los contadores desde la tabla. Tres recorridos de índice, solo
    en arranque y tras la poda; el resto del tiempo los llevan los triggers."""
    conn = get_db()
    reales = {
        "articles": conn.execute("SELECT COUNT(*) c FROM articles").fetchone()["c"],
        "unread": conn.execute("SELECT COUNT(*) c FROM articles WHERE read=0").fetchone()["c"],
        "saved": conn.execute("SELECT COUNT(*) c FROM articles WHERE saved=1").fetchone()["c"],
    }
    for clave, valor in reales.items():
        conn.execute(
            "INSERT INTO contadores(clave,valor) VALUES (?,?) "
            "ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor",
            (clave, valor),
        )
    conn.commit()
    return reales


def contadores() -> dict[str, int]:
    conn = get_db()
    return {r["clave"]: r["valor"] for r in conn.execute("SELECT clave, valor FROM contadores")}


def ensure_folder(name: str) -> int | None:
    """Devuelve id de la carpeta, creándola si hace falta. '' → None (raíz)."""
    name = (name or "").strip()
    if not name:
        return None
    db = get_db()
    row = db.execute("SELECT id FROM folders WHERE name=?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = db.execute(
        "INSERT INTO folders(name, position) VALUES (?, COALESCE((SELECT MAX(position)+1 FROM folders), 0))",
        (name,),
    )
    db.commit()
    return cur.lastrowid


# ── API keys ──────────────────────────────────────────────────────────

def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_api_key(name: str) -> tuple[int, str]:
    """Crea una key; devuelve (id, key en claro — solo se muestra una vez)."""
    key = "mnews_" + secrets.token_urlsafe(32)
    db = get_db()
    cur = db.execute(
        "INSERT INTO api_keys(name, key_hash, prefix, created_at) VALUES (?,?,?,?)",
        (name, hash_key(key), key[:12], utcnow()),
    )
    db.commit()
    return cur.lastrowid, key


def validate_api_key(key: str) -> bool:
    db = get_db()
    row = db.execute(
        "SELECT id FROM api_keys WHERE key_hash=? AND revoked=0", (hash_key(key),)
    ).fetchone()
    if not row:
        return False
    db.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (utcnow(), row["id"]))
    db.commit()
    return True
