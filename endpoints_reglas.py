"""Filtros de silencio, alertas por palabra clave y feeds de IA.

Las tres cosas que Feedly cobra en su plan caro, aquí sin tope de cuántas.

Un filtro de silencio no borra: marca el artículo. Así, quitar la regla lo
devuelve a la lista, y siempre se puede ver qué se está escondiendo — que es lo
que uno quiere revisar cuando sospecha que un filtro se pasó de listo.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Body, HTTPException

import db
import reglas as motor_reglas

log = logging.getLogger("mentatnews.reglas")
router = APIRouter()

CAMPOS = {"titulo", "resumen", "autor", "dominio", "cualquiera"}
OPERADORES = {"contiene", "es", "empieza", "termina", "regex", "palabra"}


def _fila_regla(r) -> dict:
    d = dict(r)
    d["sensible"] = bool(d["sensible"])
    d["activa"] = bool(d["activa"])
    return d


def cargar_reglas(tipo: str | None = None) -> list[dict]:
    """Las reglas activas, listas para pasar al motor."""
    conn = db.get_db()
    sql = "SELECT * FROM reglas WHERE activa=1"
    vals: list = []
    if tipo:
        sql += " AND tipo=?"
        vals.append(tipo)
    return [_fila_regla(r) for r in conn.execute(sql, vals).fetchall()]


# ── Reglas ────────────────────────────────────────────────────────────

@router.get("/api/reglas")
def listar_reglas():
    conn = db.get_db()
    filas = [_fila_regla(r) for r in conn.execute(
        "SELECT * FROM reglas ORDER BY tipo, id DESC").fetchall()]
    silenciados = conn.execute(
        "SELECT COUNT(*) n FROM articles WHERE silenciado=1").fetchone()["n"]
    return {"reglas": filas, "silenciados": silenciados}


@router.post("/api/reglas")
async def crear_regla(payload: dict = Body(...)):
    tipo = payload.get("tipo")
    if tipo not in ("silencio", "alerta"):
        raise HTTPException(400, "tipo debe ser 'silencio' o 'alerta'")
    patron = (payload.get("patron") or "").strip()
    if not patron:
        raise HTTPException(400, "hace falta un patrón")
    campo = payload.get("campo") or "cualquiera"
    operador = payload.get("operador") or "contiene"
    if campo not in CAMPOS:
        raise HTTPException(400, f"campo no válido: {campo}")
    if operador not in OPERADORES:
        raise HTTPException(400, f"operador no válido: {operador}")

    regla = {
        "id": 0, "tipo": tipo, "campo": campo, "operador": operador,
        "patron": patron, "sensible": bool(payload.get("sensible")),
        "ambito_carpeta": payload.get("ambito_carpeta"),
        "ambito_feed": payload.get("ambito_feed"), "activa": True,
    }
    try:                                   # que no entre una regex rota
        motor_reglas.compilar(regla)
    except ValueError as e:
        raise HTTPException(400, str(e))

    conn = db.get_db()
    cur = conn.execute(
        """INSERT INTO reglas(tipo, nombre, campo, operador, patron, sensible,
                              ambito_carpeta, ambito_feed, activa, creada_at)
           VALUES (?,?,?,?,?,?,?,?,1,?)""",
        (tipo, (payload.get("nombre") or "").strip(), campo, operador, patron,
         int(bool(payload.get("sensible"))), payload.get("ambito_carpeta"),
         payload.get("ambito_feed"), db.utcnow()),
    )
    conn.commit()
    rid = cur.lastrowid
    afectados = await asyncio.to_thread(aplicar_reglas_a_historico)
    return {"id": rid, **afectados}


@router.patch("/api/reglas/{rid}")
async def editar_regla(rid: int, payload: dict = Body(...)):
    conn = db.get_db()
    if not conn.execute("SELECT 1 FROM reglas WHERE id=?", (rid,)).fetchone():
        raise HTTPException(404, "esa regla no existe")
    campos, vals = [], []
    for k in ("nombre", "campo", "operador", "patron", "ambito_carpeta", "ambito_feed"):
        if k in payload:
            campos.append(f"{k}=?"); vals.append(payload[k])
    for k in ("sensible", "activa"):
        if k in payload:
            campos.append(f"{k}=?"); vals.append(int(bool(payload[k])))
    if not campos:
        raise HTTPException(400, "nada que cambiar")
    vals.append(rid)
    conn.execute(f"UPDATE reglas SET {', '.join(campos)} WHERE id=?", vals)
    conn.commit()
    afectados = await asyncio.to_thread(aplicar_reglas_a_historico)
    return {"ok": True, **afectados}


@router.delete("/api/reglas/{rid}")
async def borrar_regla(rid: int):
    conn = db.get_db()
    conn.execute("DELETE FROM reglas WHERE id=?", (rid,))
    conn.commit()
    afectados = await asyncio.to_thread(aplicar_reglas_a_historico)
    return {"ok": True, **afectados}


def aplicar_reglas_a_historico() -> dict:
    """Reevalúa TODO lo guardado con las reglas de ahora.

    Se recalcula entero en vez de ir sumando: al tocar una regla, los artículos
    que escondía deben reaparecer, y llevar la cuenta incremental de eso es
    justo donde estas cosas acaban descuadrando.
    """
    conn = db.get_db()
    activas = cargar_reglas()
    conn.execute("UPDATE articles SET silenciado=0, motivo_silencio=NULL WHERE silenciado=1")
    conn.execute("DELETE FROM alertas_articulo")
    if not activas:
        conn.commit()
        return {"silenciados": 0, "alertados": 0}

    filas = conn.execute(
        """SELECT a.id, a.title, a.summary, a.author, a.url, a.feed_id, f.folder_id
           FROM articles a JOIN feeds f ON f.id = a.feed_id"""
    ).fetchall()
    resultados = motor_reglas.evaluar_lote([dict(r) for r in filas], activas)

    silenciados = [(v["motivo_silencio"], aid) for aid, v in resultados.items() if v["silenciar"]]
    alertas = [(aid, rid) for aid, v in resultados.items() for rid in v["alertas"]]
    if silenciados:
        conn.executemany(
            "UPDATE articles SET silenciado=1, motivo_silencio=? WHERE id=?", silenciados)
    if alertas:
        conn.executemany(
            "INSERT OR IGNORE INTO alertas_articulo(articulo_id, regla_id) VALUES (?,?)", alertas)
    conn.execute("UPDATE reglas SET aciertos=0")
    conn.executemany(
        "UPDATE reglas SET aciertos=aciertos+1 WHERE id=?",
        [(rid,) for _, rid in alertas] + [(m,) for m, _ in silenciados if m])
    conn.commit()
    return {"silenciados": len(silenciados), "alertados": len(set(a for a, _ in alertas))}


@router.post("/api/reglas/aplicar")
async def endpoint_aplicar():
    """Reevalúa el histórico a mano (normalmente no hace falta)."""
    return await asyncio.to_thread(aplicar_reglas_a_historico)


def marcar_articulos_nuevos(ids: list[int]) -> None:
    """Pasa las reglas por los artículos recién insertados."""
    if not ids:
        return
    activas = cargar_reglas()
    if not activas:
        return
    conn = db.get_db()
    marcadores = ",".join("?" * len(ids))
    filas = conn.execute(
        f"""SELECT a.id, a.title, a.summary, a.author, a.url, a.feed_id, f.folder_id
            FROM articles a JOIN feeds f ON f.id = a.feed_id
            WHERE a.id IN ({marcadores})""", ids).fetchall()
    resultados = motor_reglas.evaluar_lote([dict(r) for r in filas], activas)
    for aid, v in resultados.items():
        if v["silenciar"]:
            conn.execute("UPDATE articles SET silenciado=1, motivo_silencio=? WHERE id=?",
                         (v["motivo_silencio"], aid))
            if v["motivo_silencio"]:
                conn.execute("UPDATE reglas SET aciertos=aciertos+1 WHERE id=?",
                             (v["motivo_silencio"],))
        for rid in v["alertas"]:
            conn.execute(
                "INSERT OR IGNORE INTO alertas_articulo(articulo_id, regla_id) VALUES (?,?)",
                (aid, rid))
            conn.execute("UPDATE reglas SET aciertos=aciertos+1 WHERE id=?", (rid,))
    conn.commit()
