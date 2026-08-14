"""Feeds de IA: un tema descrito con palabras que se comporta como una carpeta.

Describes lo que te interesa —"despidos en empresas de tecnología"— y a partir
de ahí cada artículo que entra pasa por un modelo barato que decide si encaja.
Es lo que Feedly reserva a su plan más caro y limita a dos; aquí no hay tope.

La clasificación va en segundo plano y por lotes: nunca en la ruta de una
petición, porque hacer esperar a quien abre la app por una llamada a un modelo
es exactamente lo que arruina la sensación de rapidez.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Body, HTTPException

import aifeeds as motor
import db

log = logging.getLogger("mentatnews.aifeeds")
router = APIRouter()

# Cuántos artículos sin clasificar se miran por pasada. El modelo es barato,
# pero no hace falta procesar cinco mil de golpe.
POR_PASADA = 180

_clasificando: set[int] = set()
_tareas: set = set()


def _pendientes(ai_feed_id: int, limite: int = POR_PASADA) -> list[dict]:
    """Artículos recientes que este feed de IA todavía no ha mirado."""
    conn = db.get_db()
    filas = conn.execute(
        """SELECT a.id, a.title, a.summary, f.title AS feed_title
           FROM articles a JOIN feeds f ON f.id = a.feed_id
           WHERE a.silenciado = 0
             AND a.id NOT IN (SELECT articulo_id FROM ai_feed_articulo
                              WHERE ai_feed_id = ?)
             AND a.id NOT IN (SELECT articulo_id FROM ai_feed_descartado
                              WHERE ai_feed_id = ?)
           ORDER BY a.sort_at DESC LIMIT ?""",
        (ai_feed_id, ai_feed_id, limite),
    ).fetchall()
    return [dict(r) for r in filas]


def _guardar(ai_feed_id: int, dentro: list[int], mirados: list[int]) -> None:
    """Guarda los que entran y también los descartados.

    Sin registrar los descartes, cada pasada volvería a preguntar por los mismos
    artículos y la factura no dejaría de crecer.
    """
    conn = db.get_db()
    if dentro:
        conn.executemany(
            "INSERT OR IGNORE INTO ai_feed_articulo(ai_feed_id, articulo_id) VALUES (?,?)",
            [(ai_feed_id, a) for a in dentro])
    fuera = [a for a in mirados if a not in set(dentro)]
    if fuera:
        conn.executemany(
            "INSERT OR IGNORE INTO ai_feed_descartado(ai_feed_id, articulo_id) VALUES (?,?)",
            [(ai_feed_id, a) for a in fuera])
    conn.execute("UPDATE ai_feeds SET ultima_pasada=? WHERE id=?", (db.utcnow(), ai_feed_id))
    conn.commit()


async def pasar_feed(ai_feed_id: int, limite: int = POR_PASADA) -> dict:
    """Clasifica lo pendiente de un feed de IA. No se solapa consigo mismo."""
    if ai_feed_id in _clasificando:
        return {"estado": "ya en marcha", "dentro": 0, "mirados": 0}
    _clasificando.add(ai_feed_id)
    try:
        fila = await asyncio.to_thread(
            lambda: db.get_db().execute(
                "SELECT * FROM ai_feeds WHERE id=? AND activo=1", (ai_feed_id,)).fetchone())
        if not fila:
            return {"estado": "no existe o está apagado", "dentro": 0, "mirados": 0}
        articulos = await asyncio.to_thread(_pendientes, ai_feed_id, limite)
        if not articulos:
            return {"estado": "al día", "dentro": 0, "mirados": 0}
        res = await asyncio.to_thread(
            motor.clasificar_por_lotes, articulos, fila["descripcion"])
        mirados = [a["id"] for a in articulos]
        await asyncio.to_thread(_guardar, ai_feed_id, res["pertenecen"], mirados)
        return {"estado": res.get("error") or "ok",
                "dentro": len(res["pertenecen"]), "mirados": len(mirados)}
    except Exception as e:
        log.exception("feed de IA %s: falló la pasada", ai_feed_id)
        return {"estado": str(e), "dentro": 0, "mirados": 0}
    finally:
        _clasificando.discard(ai_feed_id)


async def pasar_todos(limite: int = POR_PASADA) -> dict:
    conn = db.get_db()
    ids = [r["id"] for r in conn.execute("SELECT id FROM ai_feeds WHERE activo=1")]
    total = {"feeds": 0, "dentro": 0, "mirados": 0}
    for fid in ids:
        r = await pasar_feed(fid, limite)
        total["feeds"] += 1
        total["dentro"] += r["dentro"]
        total["mirados"] += r["mirados"]
    return total


# ── Endpoints ─────────────────────────────────────────────────────────

@router.get("/api/ai_feeds")
def listar():
    conn = db.get_db()
    filas = conn.execute(
        """SELECT af.*,
                  (SELECT COUNT(*) FROM ai_feed_articulo x WHERE x.ai_feed_id = af.id) AS articulos,
                  (SELECT COUNT(*) FROM ai_feed_articulo x JOIN articles a ON a.id = x.articulo_id
                   WHERE x.ai_feed_id = af.id AND a.read = 0 AND a.silenciado = 0) AS sin_leer
           FROM ai_feeds af ORDER BY af.nombre COLLATE NOCASE"""
    ).fetchall()
    return {"ai_feeds": [dict(r) for r in filas]}


@router.post("/api/ai_feeds")
async def crear(payload: dict = Body(...)):
    nombre = (payload.get("nombre") or "").strip()
    descripcion = (payload.get("descripcion") or "").strip()
    if not nombre or not descripcion:
        raise HTTPException(400, "hacen falta un nombre y una descripción")
    conn = db.get_db()
    if conn.execute("SELECT 1 FROM ai_feeds WHERE nombre=?", (nombre,)).fetchone():
        raise HTTPException(409, "ya tienes un feed de IA con ese nombre")
    cur = conn.execute(
        "INSERT INTO ai_feeds(nombre, descripcion, activo, creado_at) VALUES (?,?,1,?)",
        (nombre, descripcion, db.utcnow()))
    conn.commit()
    nuevo = cur.lastrowid
    # La primera pasada va por detrás: crear el feed responde al momento.
    tarea = asyncio.create_task(pasar_feed(nuevo))
    _tareas.add(tarea)
    tarea.add_done_callback(_tareas.discard)
    return {"id": nuevo, "nombre": nombre, "clasificando": True}


@router.patch("/api/ai_feeds/{fid}")
def editar(fid: int, payload: dict = Body(...)):
    conn = db.get_db()
    if not conn.execute("SELECT 1 FROM ai_feeds WHERE id=?", (fid,)).fetchone():
        raise HTTPException(404, "ese feed de IA no existe")
    campos, vals = [], []
    if "nombre" in payload:
        campos.append("nombre=?"); vals.append(str(payload["nombre"]).strip())
    if "activo" in payload:
        campos.append("activo=?"); vals.append(int(bool(payload["activo"])))
    if "descripcion" in payload:
        campos.append("descripcion=?"); vals.append(str(payload["descripcion"]).strip())
        # Cambiar la descripción cambia el criterio: lo ya clasificado no vale.
        conn.execute("DELETE FROM ai_feed_articulo WHERE ai_feed_id=?", (fid,))
        conn.execute("DELETE FROM ai_feed_descartado WHERE ai_feed_id=?", (fid,))
    if not campos:
        raise HTTPException(400, "nada que cambiar")
    vals.append(fid)
    conn.execute(f"UPDATE ai_feeds SET {', '.join(campos)} WHERE id=?", vals)
    conn.commit()
    return {"ok": True}


@router.delete("/api/ai_feeds/{fid}")
def borrar(fid: int):
    conn = db.get_db()
    conn.execute("DELETE FROM ai_feeds WHERE id=?", (fid,))
    conn.commit()
    return {"ok": True}


@router.post("/api/ai_feeds/{fid}/pasar")
async def pasada_manual(fid: int):
    return await pasar_feed(fid)
