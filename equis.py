"""Resuelve publicaciones de X para poder enseñarlas dentro del artículo.

El widget oficial de X es un script de terceros: la política de contenido del
sitio no lo carga (y aunque lo hiciera, dejaría que X viera a quién le sirve
cada lectura). Sin ese script, el medio deja en el artículo un `blockquote` a
medias —a veces solo un enlace vacío—, que es justo lo que se veía roto.

X publica el contenido de un tuit en el mismo sitio del que tira su widget,
`cdn.syndication.twimg.com`, sin credenciales. Aquí se pide una vez, se
normaliza a lo que la interfaz necesita y se guarda: un tuit ya publicado no
cambia, así que se cachea a lo grande y los fallos poco, por si son pasajeros.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from datetime import datetime, timedelta, timezone

import db
import netguard

log = logging.getLogger("mentatnews.equis")

# X corta si se le piden muchos tuits a la vez; de todos modos casi siempre
# están en caché y la espera no se nota.
_sem = asyncio.Semaphore(4)

FUENTE = "https://cdn.syndication.twimg.com/tweet-result?id={id}&lang=es&token={token}"
# Sin user-agent de navegador responde 403.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

VIGENCIA_HORAS = 24 * 30        # un tuit publicado ya no cambia
VIGENCIA_FALLO_HORAS = 6        # borrado, privado o caída pasajera: reintentar
TIEMPO_LIMITE = 12.0

DIGITOS36 = "0123456789abcdefghijklmnopqrstuvwxyz"

RE_ID = re.compile(r"(?:twitter|x)\.com/[^/]+/status/(\d{5,25})", re.I)
SOLO_DIGITOS = re.compile(r"^\d{5,25}$")


def _token(tuit_id: str) -> str:
    """El pase que calcula el widget de X a partir del id del tuit.

    Sin él la respuesta llega vacía. No es un secreto ni una credencial: es una
    cuenta determinista sobre el propio identificador, la misma que hace el
    script público de X.
    """
    n = (int(tuit_id) / 1e15) * math.pi
    entero, fraccion = int(n), n - int(n)
    cabeza = ""
    while entero:
        cabeza = DIGITOS36[entero % 36] + cabeza
        entero //= 36
    salida = (cabeza or "0") + "."
    for _ in range(20):
        fraccion *= 36
        d = int(fraccion)
        salida += DIGITOS36[d]
        fraccion -= d
    return re.sub(r"(0+|\.)", "", salida)


def id_de_url(url: str) -> str | None:
    m = RE_ID.search(url or "")
    return m.group(1) if m else None


def _sin_cola(texto: str, rango: list | None) -> str:
    """Quita el t.co que X pega al final para enlazar la foto o el vídeo."""
    if rango and len(rango) == 2:
        try:
            # El rango viene en puntos de código, que es como Python indexa.
            texto = texto[rango[0]:rango[1]]
        except (TypeError, ValueError):
            pass
    return re.sub(r"\s*https://t\.co/\w+\s*$", "", texto).strip()


def _medios(crudo: dict) -> list[dict]:
    medios = []
    for m in crudo.get("mediaDetails") or []:
        url = m.get("media_url_https")
        if not url:
            continue
        tam = (m.get("original_info") or {})
        medios.append({
            "tipo": "video" if m.get("type") in ("video", "animated_gif") else "foto",
            "url": url,
            "ancho": tam.get("width") or 0,
            "alto": tam.get("height") or 0,
            "alt": (m.get("ext_alt_text") or "").strip(),
        })
    return medios


def _video(crudo: dict) -> dict | None:
    """El mp4 del tuit, para poder verlo aquí en vez de acabar en X.

    X ofrece varias calidades y una lista HLS que el navegador no reproduce
    solo; se coge el mp4 más grande que no se pase de 1280 de ancho.
    """
    v = crudo.get("video") or {}
    mp4 = [x.get("src") for x in v.get("variants") or []
           if x.get("type") == "video/mp4" and x.get("src")]
    if not mp4:
        return None

    def ancho(url: str) -> int:
        m = re.search(r"/(\d{2,4})x\d{2,4}/", url)
        return int(m.group(1)) if m else 0

    elegido = max((u for u in mp4 if ancho(u) <= 1280), key=ancho, default=mp4[0])
    aspecto = v.get("aspectRatio") or []
    return {
        "mp4": elegido,
        "poster": v.get("poster") or "",
        "duracion_ms": v.get("durationMs") or 0,
        "aspecto": f"{aspecto[0]}/{aspecto[1]}" if len(aspecto) == 2 else "16/9",
    }


def _normalizar(crudo: dict, tuit_id: str) -> dict:
    usuario = crudo.get("user") or {}
    cuenta = usuario.get("screen_name") or ""
    # Los tuits largos traen el texto completo aparte.
    nota = (crudo.get("note_tweet") or {}).get("text")
    texto = nota or _sin_cola(crudo.get("text") or "",
                              crudo.get("display_text_range"))
    return {
        "id": tuit_id,
        "url": f"https://x.com/{cuenta or 'i'}/status/{tuit_id}",
        "autor": usuario.get("name") or "",
        "cuenta": f"@{cuenta}" if cuenta else "",
        "avatar": usuario.get("profile_image_url_https") or "",
        "verificado": bool(usuario.get("verified") or usuario.get("is_blue_verified")),
        "texto": texto,
        "fecha": crudo.get("created_at") or "",
        "medios": _medios(crudo),
        "video": _video(crudo),
    }


def _leer_cache(tuit_id: str) -> dict | None:
    fila = db.get_db().execute(
        "SELECT datos, guardado_en FROM tuits WHERE id=?", (tuit_id,)).fetchone()
    if not fila:
        return None
    try:
        guardado = datetime.fromisoformat(fila["guardado_en"].replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    datos = json.loads(fila["datos"]) if fila["datos"] else None
    horas = VIGENCIA_HORAS if datos else VIGENCIA_FALLO_HORAS
    if datetime.now(timezone.utc) - guardado > timedelta(hours=horas):
        return None
    return datos or {"id": tuit_id, "error": "no disponible"}


def _guardar(tuit_id: str, datos: dict | None) -> None:
    db.get_db().execute(
        "INSERT OR REPLACE INTO tuits(id, datos, guardado_en) VALUES (?,?,?)",
        (tuit_id, json.dumps(datos, ensure_ascii=False) if datos else None, db.utcnow()))
    db.get_db().commit()


def traer(tuit_id: str) -> dict:
    """El tuit, de la caché o de X. Devuelve `{"error": ...}` si no se pudo."""
    if not SOLO_DIGITOS.match(tuit_id or ""):
        return {"id": tuit_id, "error": "identificador inválido"}

    guardado = _leer_cache(tuit_id)
    if guardado is not None:
        return guardado

    try:
        with netguard.SafeClient(timeout=TIEMPO_LIMITE,
                                 headers={"User-Agent": UA}) as cliente:
            r = cliente.get(FUENTE.format(id=tuit_id, token=_token(tuit_id)))
        if r.status_code != 200:
            raise ValueError(f"X respondió {r.status_code}")
        datos = _normalizar(r.json(), tuit_id)
        if not (datos["texto"] or datos["medios"] or datos["autor"]):
            raise ValueError("X devolvió un tuit vacío (borrado o privado)")
    except Exception as e:
        log.info("tuit %s: no se pudo traer (%s)", tuit_id, e)
        _guardar(tuit_id, None)
        return {"id": tuit_id, "error": "no disponible"}

    _guardar(tuit_id, datos)
    return datos


async def traer_varios(ids: list[str]) -> dict[str, dict]:
    """Varios tuits a la vez, sin agobiar a X."""
    async def uno(tuit_id: str) -> dict:
        async with _sem:
            return await asyncio.to_thread(traer, tuit_id)

    resueltos = await asyncio.gather(*(uno(i) for i in ids))
    return {t["id"]: t for t in resueltos}
