"""Módulo de extracción de texto completo de artículos para un lector RSS.

Utiliza httpx para la descarga HTTP, trafilatura para la extracción principal de texto/HTML
y metadatos, y lxml.html para fallbacks heurísticos y manipulación/absolutización de enlaces HTML.
"""

import sys
from typing import TypedDict

import httpx

import netguard
import lxml.html
import trafilatura


class ExtractionResult(TypedDict):
    status: str
    title: str
    author: str
    html: str
    text_len: int
    error: str


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _get_plain_text(html_str: str) -> str:
    """Extrae el texto plano de una cadena HTML usando lxml.html."""
    if not html_str or not html_str.strip():
        return ""
    try:
        doc = lxml.html.fromstring(html_str)
        return doc.text_content().strip()
    except Exception:
        return ""


def _make_urls_absolute(html_str: str, base_url: str) -> str:
    """Absolutiza URLs relativas en etiquetas <img src> y <a href> usando base_url."""
    if not html_str or not html_str.strip():
        return html_str
    try:
        doc = lxml.html.fromstring(html_str)
        doc.make_links_absolute(base_url, resolve_base_href=True)
        return lxml.html.tostring(doc, encoding="utf-8").decode("utf-8")
    except Exception:
        return html_str


def _extract_lxml_fallback(html_content: str) -> str:
    """Fallback heurístico con lxml.html para cuando trafilatura no devuelve suficiente texto.

    Busca el nodo <article> o bien el container <div>/<main>/<section> con mayor densidad
    de texto en sus etiquetas <p>, habiendo eliminado previamente tags no deseadas.
    """
    try:
        doc = lxml.html.fromstring(html_content)

        # Eliminar del árbol script, style, nav, header, footer, aside, form
        for element in doc.xpath("//script | //style | //nav | //header | //footer | //aside | //form"):
            element.getparent().remove(element)

        # 1. Intentar buscar <article>
        articles = doc.xpath("//article")
        if articles:
            best_article = max(articles, key=lambda el: len(el.text_content().strip()))
            if len(best_article.text_content().strip()) > 0:
                return lxml.html.tostring(best_article, encoding="utf-8").decode("utf-8")

        # 2. Si no hay <article>, elegir <div>, <main>, <section> con mayor densidad de texto en sus <p>
        candidates = doc.xpath("//div | //main | //section")
        best_candidate = None
        max_p_text_len = 0

        for cand in candidates:
            p_text = "".join(p.text_content() for p in cand.xpath(".//p"))
            p_len = len(p_text.strip())
            if p_len > max_p_text_len:
                max_p_text_len = p_len
                best_candidate = cand

        if best_candidate is not None and max_p_text_len > 0:
            return lxml.html.tostring(best_candidate, encoding="utf-8").decode("utf-8")

        return ""
    except Exception:
        return ""


def extract_fulltext(url: str, timeout: float = 20.0) -> dict:
    """Extrae el contenido de texto completo y metadatos de una URL dada.

    Args:
        url: URL del artículo a extraer.
        timeout: Tiempo límite en segundos para la descarga HTTP.

    Returns:
        dict con claves exactas: status, title, author, html, text_len, error.
    """
    result: dict = {
        "status": "error",
        "title": "",
        "author": "",
        "html": "",
        "text_len": 0,
        "error": "",
    }

    try:
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept-Language": "es,en;q=0.8",
        }
        with netguard.SafeClient(timeout=timeout, headers=headers) as client:
            response = client.get(url)
            response.raise_for_status()

        # Validar Content-Type
        content_type = response.headers.get("content-type", "").lower()
        if not ("text/html" in content_type or "application/xhtml+xml" in content_type):
            result["error"] = f"content-type no soportado: {content_type}"
            return result

        final_url = str(response.url)
        html_content = response.text

        # Metadatos con trafilatura
        title = ""
        author = ""
        try:
            metadata = trafilatura.extract_metadata(html_content, default_url=final_url)
            if metadata:
                title = metadata.title or ""
                author = metadata.author or ""
        except Exception:
            pass

        # Si trafilatura.extract_metadata falla o no da título, buscar en el <title> del HTML vía lxml
        if not title:
            try:
                doc = lxml.html.fromstring(html_content)
                title_elements = doc.xpath("//title")
                if title_elements and title_elements[0].text:
                    title = title_elements[0].text.strip()
            except Exception:
                pass

        result["title"] = title
        result["author"] = author

        # Extracción principal con trafilatura
        extracted_html = trafilatura.extract(
            html_content,
            output_format="html",
            include_images=True,
            include_links=True,
            include_tables=True,
            include_formatting=True,
            url=final_url,
        )

        plain_text = _get_plain_text(extracted_html) if extracted_html else ""

        # Fallback si trafilatura devuelve None o text_len < 200
        if not extracted_html or len(plain_text) < 200:
            fallback_html = _extract_lxml_fallback(html_content)
            fallback_text = _get_plain_text(fallback_html)
            if len(fallback_text) > len(plain_text):
                extracted_html = fallback_html
                plain_text = fallback_text

        # Validar resultado final
        if not extracted_html or len(plain_text) < 200:
            result["error"] = "no se pudo extraer contenido"
            return result

        # Absolutizar URLs en el HTML resultante
        final_html = _make_urls_absolute(extracted_html, final_url)
        final_text_len = len(_get_plain_text(final_html))

        result["status"] = "ok"
        result["html"] = final_html
        result["text_len"] = final_text_len
        result["error"] = ""
        return result

    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python3 extractor.py <URL>")
        sys.exit(1)

    input_url = sys.argv[1]
    res = extract_fulltext(input_url)

    print(f"Status:   {res['status']}")
    print(f"Title:    {res['title']}")
    print(f"Author:   {res['author']}")
    print(f"Text len: {res['text_len']}")
    print(f"Error:    {res['error']}")
    print("--- HTML Preview (primeros 500 chars) ---")
    print(res["html"][:500])
