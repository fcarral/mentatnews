"""Motor de AI Feeds para MentatNews: clasifica artículos en temas descritos en lenguaje natural."""
from __future__ import annotations

import html
import json
import re
import sys
import anthropic

MODELO = "claude-haiku-4-5"

ESQUEMA = {
    "type": "object",
    "properties": {"pertenecen": {"type": "array", "items": {"type": "integer"}}},
    "required": ["pertenecen"],
    "additionalProperties": False,
}

SYSTEM = """Eres el clasificador de AI Feeds de MentatNews, un lector RSS en español (usuario en México).
Tu trabajo es determinar cuáles artículos pertenecen estrictamente al tema o filtro descrito por el usuario.

Reglas de clasificación:
- Eres un CLASIFICADOR, no un crítico ni un curador de calidad. Solo decides PERTENENCIA al tema descrito: no opinas si el artículo es bueno, importante o interesante.
- ANTE LA DUDA, EXCLUIR. Más vale un feed limpio que uno inflado.
- Debe manejar tanto descripciones amplias ("inteligencia artificial") como estrechas ("solo despidos, no contrataciones"): si la descripción pone una restricción explícita, hay que respetarla al pie de la letra.
- Devuelve solo los ids que pertenecen; si ninguno pertenece, lista vacía.
- Español de México, sin florituras."""

MAX_TOKENS = 2000
MAX_SUMMARY_LEN = 200


def _limpiar_texto(texto: str) -> str:
    """Limpia etiquetas HTML, desescapa entidades y colapsa espacios en blanco."""
    if not texto:
        return ""
    texto = re.sub(r"<[^>]+>", " ", texto)
    texto = html.unescape(texto)
    return re.sub(r"\s+", " ", texto).strip()


def clasificar(articulos: list[dict], descripcion: str, *, nombre: str = "") -> dict:
    """Clasifica hasta 100 artículos según la descripción de un feed de IA."""
    modelo_usado = MODELO
    if not articulos or not descripcion.strip():
        return {
            "pertenecen": [],
            "error": "",
            "modelo": modelo_usado,
        }

    try:
        articulos_procesados = []
        ids_validos = set()
        for art in articulos:
            art_id = art["id"]
            ids_validos.add(art_id)
            title = art.get("title", "")
            summary = art.get("summary", "")
            feed_title = art.get("feed_title", "")

            summary_limpio = _limpiar_texto(summary)[:MAX_SUMMARY_LEN]
            articulos_procesados.append(
                f"ID: {art_id}\nFuente: {feed_title}\nTítulo: {title}\nResumen: {summary_limpio}"
            )

        prompt_articulos = "\n\n---\n\n".join(articulos_procesados)
        contexto_nombre = f"Nombre del feed: {nombre}\n" if nombre.strip() else ""
        content_user = (
            f"{contexto_nombre}Descripción del tema:\n{descripcion.strip()}\n\n"
            f"Artículos a clasificar:\n\n{prompt_articulos}"
        )

        client = anthropic.Anthropic()
        response = client.messages.create(
            model=MODELO,
            max_tokens=MAX_TOKENS,
            system=SYSTEM,
            output_config={"format": {"type": "json_schema", "schema": ESQUEMA}},
            messages=[{"role": "user", "content": content_user}],
        )

        if response.stop_reason == "refusal":
            return {
                "pertenecen": [],
                "error": "El modelo rechazó la petición.",
                "modelo": modelo_usado,
            }

        text = next(b.text for b in response.content if b.type == "text")
        data = json.loads(text)
        pertenecen_raw = data.get("pertenecen", [])

        pertenecen_filtrados = sorted(
            list({art_id for art_id in pertenecen_raw if art_id in ids_validos})
        )

        return {
            "pertenecen": pertenecen_filtrados,
            "error": "",
            "modelo": modelo_usado,
        }
    except Exception as e:
        return {
            "pertenecen": [],
            "error": str(e),
            "modelo": modelo_usado,
        }


def clasificar_por_lotes(articulos: list[dict], descripcion: str, *, tam_lote: int = 60) -> dict:
    """Clasifica artículos partiéndolos en lotes de tamaño especificado."""
    modelo_usado = MODELO
    if not articulos or not descripcion.strip():
        return {
            "pertenecen": [],
            "error": "",
            "modelo": modelo_usado,
        }

    pertenecen_totales: set[int] = set()
    errores: list[str] = []

    for i in range(0, len(articulos), tam_lote):
        lote = articulos[i : i + tam_lote]
        res = clasificar(lote, descripcion)
        pertenecen_totales.update(res.get("pertenecen", []))
        err = res.get("error", "")
        if err:
            errores.append(err)

    return {
        "pertenecen": sorted(list(pertenecen_totales)),
        "error": "; ".join(errores),
        "modelo": modelo_usado,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python3 aifeeds.py <descripcion_del_feed>")
        sys.exit(1)

    descripcion_input = sys.argv[1]

    import sqlite3
    from pathlib import Path

    db_path = Path(__file__).parent / "data" / "mentatnews.db"
    if not db_path.exists():
        print(f"Error: No se encontró la base de datos en {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    query = """
        SELECT a.id, a.title, a.summary, f.title AS feed_title
        FROM articles a LEFT JOIN feeds f ON f.id = a.feed_id
        ORDER BY a.id DESC LIMIT 30
    """
    rows = cursor.execute(query).fetchall()
    articulos_db = [dict(r) for r in rows]
    conn.close()

    resultado = clasificar(articulos_db, descripcion_input)

    if resultado["error"]:
        print(f"Error durante la clasificación: {resultado['error']}")

    pertenecen_ids = set(resultado["pertenecen"])
    entraron = [a for a in articulos_db if a["id"] in pertenecen_ids]
    excluidos = [a for a in articulos_db if a["id"] not in pertenecen_ids]

    print(f"PERTENECEN ({len(entraron)})")
    for a in entraron:
        titulo_cortado = (a["title"] or "")[:60]
        print(f"  [{a['id']}] {titulo_cortado}")

    print(f"\nEXCLUIDOS ({len(excluidos)})")
    for a in excluidos:
        titulo_cortado = (a["title"] or "")[:60]
        print(f"  [{a['id']}] {titulo_cortado}")
