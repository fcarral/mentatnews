"""Endpoints de La Portada.

Va aparte de main.py para que enchufarlo sean dos líneas y para poder tocarlo
sin remover el resto de la API. La portada se guarda en la tabla `settings`,
que ya existe, con la clave `portada:<carpeta>`: así no hace falta migrar nada
y una portada recién hecha se reutiliza un par de horas en lugar de pagar una
llamada al modelo en cada visita.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

import db
import limpieza
import portada as motor

log = logging.getLogger("mentatnews.portada")
router = APIRouter()

MAX_CANDIDATOS = 40
MARGEN_RENOVACION = 40      # minutos de antelación con que se rehace una portada

_renovando: set[int] = set()   # temas que se están rehaciendo ahora mismo
_tareas: set = set()           # referencias vivas de las renovaciones de fondo


def _articulos_del_tema(folder_id: int) -> list[dict]:
    """Lo que no has leído en esa carpeta, con el resumen ya limpio."""
    conn = db.get_db()
    filas = conn.execute(
        """SELECT a.id, a.title, a.summary, a.url, a.image_url,
                  a.published_at, a.fetched_at, f.title AS feed_title
           FROM articles a JOIN feeds f ON f.id = a.feed_id
           WHERE a.read = 0 AND f.folder_id = ?
           ORDER BY a.sort_at DESC
           LIMIT ?""",
        (folder_id, MAX_CANDIDATOS),
    ).fetchall()
    articulos = []
    for r in filas:
        a = dict(r)
        a["summary"] = limpieza.limpiar_resumen(a.get("summary") or "", titulo=a["title"])
        articulos.append(a)
    return articulos


def _nombre_carpeta(folder_id: int) -> str | None:
    fila = db.get_db().execute(
        "SELECT name FROM folders WHERE id=?", (folder_id,)).fetchone()
    return fila["name"] if fila else None


def _leer_cache_cruda(clave: str) -> dict | None:
    """La portada guardada, esté al día o no."""
    fila = db.get_db().execute(
        "SELECT value FROM settings WHERE key=?", (clave,)).fetchone()
    if not fila:
        return None
    try:
        return json.loads(fila["value"])
    except json.JSONDecodeError:
        return None


def _leer_cache(clave: str) -> dict | None:
    guardada = _leer_cache_cruda(clave)
    if not guardada:
        return None
    return guardada if motor.esta_vigente(guardada.get("generada")) else None


def _antiguedad_minutos(generada: str | None) -> float:
    if not generada:
        return 1e9
    try:
        marca = datetime.fromisoformat(generada.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 1e9
    return (datetime.now(timezone.utc) - marca).total_seconds() / 60


def _guardar_cache(clave: str, datos: dict) -> None:
    conn = db.get_db()
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)",
                 (clave, json.dumps(datos, ensure_ascii=False)))
    conn.commit()


def _respuesta(portada: dict, articulos: list[dict], tema: str, generada: str) -> dict:
    """Solo viajan los artículos que la portada usa, con lo justo para pintarlos."""
    usados = {portada["principal"]["id"]} if portada.get("principal") else set()
    usados |= {s["id"] for s in portada.get("secundarias", [])}
    usados |= set(portada.get("breves", []))
    campos = ("id", "title", "url", "image_url", "published_at", "fetched_at",
              "feed_title", "summary")
    return {
        "tema": tema,
        "generada": generada,
        "portada": portada,
        "articulos": {a["id"]: {k: a.get(k) for k in campos}
                      for a in articulos if a["id"] in usados},
    }


def _carpetas_con_pendientes() -> list[tuple[int, str]]:
    """Temas que tienen algo sin leer, de más a menos."""
    filas = db.get_db().execute(
        """SELECT fo.id, fo.name, COUNT(a.id) n
           FROM folders fo
           JOIN feeds f ON f.folder_id = fo.id
           JOIN articles a ON a.feed_id = f.id AND a.read = 0
           GROUP BY fo.id HAVING n > 0 ORDER BY n DESC"""
    ).fetchall()
    return [(r["id"], r["name"]) for r in filas]


async def _renovar(folder_id: int, tema: str) -> bool:
    """Rehace la portada de un tema. Nunca dos a la vez para el mismo."""
    if folder_id in _renovando:
        return False
    _renovando.add(folder_id)
    try:
        articulos = await asyncio.to_thread(_articulos_del_tema, folder_id)
        if not articulos:
            return False
        portada = await asyncio.to_thread(motor.construir, articulos, tema)
        if portada.get("modo") != "editada":
            log.warning("portada de %s salió en modo %s", tema, portada.get("modo"))
            return False
        await asyncio.to_thread(
            _guardar_cache, f"portada:{folder_id}",
            _respuesta(portada, articulos, tema, db.utcnow()))
        return True
    except Exception:
        log.exception("portada de %s: no se pudo renovar", tema)
        return False
    finally:
        _renovando.discard(folder_id)


async def preparar_portadas(limite: int = 12, margen: int = MARGEN_RENOVACION) -> dict:
    """Mantiene las portadas calientes renovándolas ANTES de que caduquen.

    Con `margen` minutos de antelación: si se esperara a que expiren, el primer
    clic del día en ese tema pagaría los ~30 s que tarda el modelo.
    """
    hechas = frescas = 0
    for folder_id, tema in (await asyncio.to_thread(_carpetas_con_pendientes))[:limite]:
        guardada = await asyncio.to_thread(_leer_cache_cruda, f"portada:{folder_id}")
        edad = _antiguedad_minutos(guardada.get("generada") if guardada else None)
        if guardada and edad < (motor.VIGENCIA_MINUTOS - margen):
            frescas += 1
            continue
        if await _renovar(folder_id, tema):
            hechas += 1
    return {"hechas": hechas, "frescas": frescas}


@router.post("/api/portada/preparar")
async def endpoint_preparar():
    """Fuerza la preparación de las portadas pendientes."""
    return await preparar_portadas()


@router.get("/api/portada")
async def obtener_portada(folder_id: int, refrescar: int = 0):
    """La primera plana de un tema. Se reaprovecha durante dos horas."""
    tema = await asyncio.to_thread(_nombre_carpeta, folder_id)
    if tema is None:
        raise HTTPException(404, "esa carpeta no existe")

    clave = f"portada:{folder_id}"
    guardada = await asyncio.to_thread(_leer_cache_cruda, clave)

    if guardada and not refrescar:
        # Aunque haya caducado se entrega al instante y se rehace por detrás:
        # una portada de hace dos horas es mejor que medio minuto mirando un
        # cartel de "armando la portada".
        if not motor.esta_vigente(guardada.get("generada")):
            tarea = asyncio.create_task(_renovar(folder_id, tema))
            _tareas.add(tarea)
            tarea.add_done_callback(_tareas.discard)
            guardada = {**guardada, "renovando": True}
        return guardada

    articulos = await asyncio.to_thread(_articulos_del_tema, folder_id)
    if not articulos:
        return {"tema": tema, "generada": db.utcnow(),
                "portada": {"principal": None, "secundarias": [], "breves": [],
                            "resumen": "", "modo": "vacia"},
                "articulos": {}}

    portada = await asyncio.to_thread(motor.construir, articulos, tema)
    datos = _respuesta(portada, articulos, tema, db.utcnow())
    if portada.get("modo") == "editada":      # una portada de respaldo no se guarda
        await asyncio.to_thread(_guardar_cache, clave, datos)
    return datos
