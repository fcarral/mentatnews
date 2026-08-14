"""La Portada: una primera plana por tema, armada con Claude.

La idea es la de un jefe de redacción. Se le da todo lo que entró sin leer en
una carpeta y decide qué va arriba: una nota principal, dos o tres secundarias
y el resto en breves. No escribe noticias — solo elige y jerarquiza artículos
que ya existen, y cada elección se valida contra los ids que se le pasaron.
Así la portada no puede inventarse nada.

Si la llamada al modelo falla, se devuelve una portada cronológica para que la
pantalla nunca aparezca vacía.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import anthropic

log = logging.getLogger("mentatnews.portada")

MODELO = "claude-sonnet-5"
MAX_CANDIDATOS = 60          # cuántos artículos ve el modelo como mucho
VIGENCIA_MINUTOS = 120       # cada cuánto se rehace una portada
CST = timezone(timedelta(hours=-6))

ESQUEMA = {
    "type": "object",
    "properties": {
        "resumen": {"type": "string"},
        "principal": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "motivo": {"type": "string"},
            },
            "required": ["id", "motivo"],
            "additionalProperties": False,
        },
        "secundarias": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "motivo": {"type": "string"},
                },
                "required": ["id", "motivo"],
                "additionalProperties": False,
            },
        },
        "breves": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["resumen", "principal", "secundarias", "breves"],
    "additionalProperties": False,
}

# Quién lee la portada. Ayuda al modelo a calibrar el tono y qué destacar.
PERFIL = os.environ.get(
    "MENTATNEWS_PERFIL",
    "una persona que sigue este tema de cerca y quiere enterarse rápido",
)

SISTEMA = f"""Eres el jefe de redacción de la portada de un tema en MentatNews, un lector de noticias personal. Quien la lee es {PERFIL}.

Se te entrega la lista de artículos sin leer de UNA carpeta temática. Tu trabajo es montar la primera plana de ese tema.

Reglas:
- Elige UNA nota principal: la de mayor importancia real, no la más reciente ni la más llamativa. Si dos fuentes cuentan lo mismo, quédate con la mejor y manda la otra a breves.
- Elige 2 o 3 secundarias: lo siguiente en importancia, que aporte algo distinto a la principal.
- Manda a breves entre 4 y 8 más, las que valga la pena saber que existen.
- Deja fuera lo repetido, lo promocional y lo trivial. No tienes que colocar todos los artículos: si un tema trae poco de valor, una portada corta es la respuesta correcta.
- Es una portada de HOY: prioriza lo publicado en las últimas 48 horas. Solo sube algo más antiguo cuando siga siendo claramente lo más importante del tema y no haya nada reciente que lo desplace.
- `motivo` explica en UNA frase por qué esa nota merece ese lugar. Escribe para alguien que aún no la ha leído: di qué pasó y por qué importa, no "es relevante para el sector".
- `resumen`: dos o tres frases sobre cómo viene el día en este tema. Concreto, sin relleno, sin adjetivos de más. Si el día está flojo, dilo.
- Español de México, tono sobrio, sin emojis y sin signos de exclamación.

Solo puedes usar los identificadores numéricos que aparecen en la lista. No inventes ninguno y no repitas un id en dos secciones."""


def _candidatos(articulos: list[dict]) -> list[dict]:
    """Los más recientes primero, recortados a lo que cabe en una llamada."""
    def clave(a: dict) -> str:
        return a.get("published_at") or a.get("fetched_at") or ""
    return sorted(articulos, key=clave, reverse=True)[:MAX_CANDIDATOS]


def _portada_cronologica(articulos: list[dict], motivo: str) -> dict:
    """Respaldo sin IA: lo más nuevo arriba. Nunca deja la pantalla vacía."""
    orden = _candidatos(articulos)
    if not orden:
        return {"resumen": "", "principal": None, "secundarias": [], "breves": [],
                "modo": "vacia", "nota": motivo}
    return {
        "resumen": "",
        "principal": {"id": orden[0]["id"], "motivo": ""},
        "secundarias": [{"id": a["id"], "motivo": ""} for a in orden[1:4]],
        "breves": [a["id"] for a in orden[4:12]],
        "modo": "cronologica",
        "nota": motivo,
    }


def construir(articulos: list[dict], tema: str) -> dict:
    """Arma la portada de un tema. Devuelve siempre algo utilizable."""
    if not articulos:
        return _portada_cronologica([], "sin artículos")

    candidatos = _candidatos(articulos)
    lineas = []
    for a in candidatos:
        fecha = (a.get("published_at") or a.get("fetched_at") or "")[:16].replace("T", " ")
        resumen = (a.get("summary") or "")[:220].replace("\n", " ")
        lineas.append(f'[{a["id"]}] ({a.get("feed_title", "")} · {fecha}) {a["title"]}\n    {resumen}')
    listado = "\n".join(lineas)

    try:
        cliente = anthropic.Anthropic()
        respuesta = cliente.messages.create(
            model=MODELO,
            max_tokens=4000,
            system=SISTEMA,
            output_config={"format": {"type": "json_schema", "schema": ESQUEMA}},
            messages=[{
                "role": "user",
                "content": f"Carpeta: {tema}\nHoy es {datetime.now(CST):%A %d de %B de %Y} (CST).\n\n"
                           f"Artículos sin leer:\n{listado}",
            }],
        )
        if respuesta.stop_reason == "refusal":
            return _portada_cronologica(articulos, "el modelo declinó la petición")
        texto = next(b.text for b in respuesta.content if b.type == "text")
        datos = json.loads(texto)
    except Exception as e:
        log.warning("portada de %s: %s", tema, e)
        return _portada_cronologica(articulos, str(e))

    return _validar(datos, {a["id"] for a in candidatos}, articulos)


def _validar(datos: dict, ids_validos: set[int], articulos: list[dict]) -> dict:
    """Descarta cualquier id que el modelo no haya sacado de la lista."""
    usados: set[int] = set()

    def admitir(id_articulo) -> bool:
        if not isinstance(id_articulo, int) or id_articulo not in ids_validos:
            return False
        if id_articulo in usados:
            return False
        usados.add(id_articulo)
        return True

    principal = datos.get("principal") or {}
    if not admitir(principal.get("id")):
        return _portada_cronologica(articulos, "el modelo devolvió una principal inválida")

    secundarias = [s for s in datos.get("secundarias", [])
                   if isinstance(s, dict) and admitir(s.get("id"))][:3]
    breves = [i for i in datos.get("breves", []) if admitir(i)][:8]

    return {
        "resumen": (datos.get("resumen") or "").strip(),
        "principal": {"id": principal["id"], "motivo": (principal.get("motivo") or "").strip()},
        "secundarias": [{"id": s["id"], "motivo": (s.get("motivo") or "").strip()}
                        for s in secundarias],
        "breves": breves,
        "modo": "editada",
        "nota": "",
    }


def esta_vigente(generada_en: str | None) -> bool:
    """Una portada sirve un par de horas antes de volver a montarla."""
    if not generada_en:
        return False
    try:
        marca = datetime.fromisoformat(generada_en.replace("Z", "+00:00"))
    except ValueError:
        return False
    return datetime.now(timezone.utc) - marca < timedelta(minutes=VIGENCIA_MINUTOS)
