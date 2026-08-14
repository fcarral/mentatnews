"""Asistente IA de MentatNews: sugiere fuentes RSS vía Claude (Sonnet)."""
from __future__ import annotations

import anthropic

MODEL = "claude-sonnet-5"

SCHEMA = {
    "type": "object",
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "feed_url": {"type": "string"},
                    "site_url": {"type": "string"},
                    "reason": {"type": "string"},
                    "folder": {"type": "string"},
                },
                "required": ["title", "feed_url", "site_url", "reason", "folder"],
                "additionalProperties": False,
            },
        },
        "note": {"type": "string"},
    },
    "required": ["suggestions", "note"],
    "additionalProperties": False,
}

SYSTEM = """Eres el asistente de MentatNews, un lector RSS personal en español (usuario en México, CST).
El usuario describe qué quiere seguir y tú propones fuentes RSS/Atom REALES que conozcas.

Reglas:
- Propón entre 3 y 8 fuentes, las mejores primero. Solo feeds que existían realmente (URL canónica del feed, no la portada del sitio). Si no estás seguro de la ruta exacta del feed, da la URL del sitio en feed_url igualmente: el servidor intentará autodescubrir el feed.
- Prefiere fuentes vivas y de calidad; mezcla español e inglés según el tema.
- `folder`: nombre corto de carpeta sugerida en español (p. ej. "Fórmula 1", "IA", "México").
- `reason`: una frase de por qué vale la pena.
- `note`: una frase de contexto o advertencia general (p. ej. si el tema tiene pocos feeds buenos). Sin florituras.
- No repitas fuentes que el usuario ya tiene (se te da la lista)."""


def suggest_feeds(peticion: str, existentes: list[str]) -> dict:
    """Pide a Claude sugerencias de feeds para la petición del usuario."""
    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY del entorno
    ya = "\n".join(existentes[:200]) or "(ninguna)"
    response = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM,
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{
            "role": "user",
            "content": f"Fuentes que ya tengo:\n{ya}\n\nPetición: {peticion}",
        }],
    )
    if response.stop_reason == "refusal":
        return {"suggestions": [], "note": "La petición fue rechazada por el modelo."}
    import json
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)
