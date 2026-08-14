"""
Módulo para el manejo de archivos OPML y autodescubrimiento de feeds RSS/Atom/JSON.
"""

import sys
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from typing import List, Dict, Optional
import urllib.parse

import httpx

import netguard
import feedparser


def parse_opml(xml_text: str) -> List[Dict[str, str]]:
    """
    Parsea un texto en formato OPML (Feedly, Inoreader, etc.).
    Devuelve una lista de diccionarios con la estructura:
    {title: str, xml_url: str, html_url: str, folder: str}
    """
    if not xml_text or not xml_text.strip():
        raise ValueError("El contenido XML de OPML está vacío.")

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise ValueError(f"XML inválido: {e}") from e

    body = None
    for child in root:
        child_tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if child_tag.lower() == "body":
            body = child
            break

    if body is None:
        raise ValueError("El archivo OPML no contiene la etiqueta <body>.")

    result: List[Dict[str, str]] = []

    def extract_outlines(element: ET.Element, current_folder: str = ""):
        for child in element:
            child_tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if child_tag.lower() != "outline":
                continue

            # Extraer atributos tolerando mayúsculas/minúsculas
            attribs = {k.lower(): v for k, v in child.attrib.items()}

            xml_url = attribs.get("xmlurl", "")
            title = attribs.get("title") or attribs.get("text") or ""
            html_url = attribs.get("htmlurl", "")

            if xml_url:
                # Es un feed
                result.append({
                    "title": title,
                    "xml_url": xml_url,
                    "html_url": html_url,
                    "folder": current_folder
                })
                # Si tiene hijos (anidados), procesar recursivamente usando el título actual o el ancestro
                next_folder = title if title else current_folder
                extract_outlines(child, next_folder)
            else:
                # Es un outline carpeta (o agrupador)
                next_folder = title if title else current_folder
                extract_outlines(child, next_folder)

    extract_outlines(body, "")
    return result


def build_opml(feeds: List[Dict[str, Optional[str]]]) -> str:
    """
    Genera una cadena XML en formato OPML 2.0 válido a partir de una lista de feeds.
    Feeds agrupados por carpeta (folder).
    """
    opml = ET.Element("opml", version="2.0")
    
    # Head
    head = ET.SubElement(opml, "head")
    title_elem = ET.SubElement(head, "title")
    title_elem.text = "MentatNews export"
    date_created = ET.SubElement(head, "dateCreated")
    # Formato RFC 822 / RFC 1123 en UTC
    date_created.text = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")

    # Body
    body = ET.SubElement(opml, "body")

    # Agrupar feeds por folder
    folders: Dict[str, List[Dict[str, Optional[str]]]] = {}
    root_feeds: List[Dict[str, Optional[str]]] = []

    for feed in feeds:
        folder = feed.get("folder")
        if folder:
            folder_str = str(folder).strip()
            if folder_str:
                folders.setdefault(folder_str, []).append(feed)
                continue
        root_feeds.append(feed)

    # Añadir feeds raíz
    for feed in root_feeds:
        ET.SubElement(
            body,
            "outline",
            text=feed.get("title") or "",
            title=feed.get("title") or "",
            type="rss",
            xmlUrl=feed.get("xml_url") or "",
            htmlUrl=feed.get("html_url") or ""
        )

    # Añadir carpetas y sus feeds
    for folder_name, folder_feeds in folders.items():
        folder_outline = ET.SubElement(
            body,
            "outline",
            text=folder_name,
            title=folder_name
        )
        for feed in folder_feeds:
            ET.SubElement(
                folder_outline,
                "outline",
                text=feed.get("title") or "",
                title=feed.get("title") or "",
                type="rss",
                xmlUrl=feed.get("xml_url") or "",
                htmlUrl=feed.get("html_url") or ""
            )

    xml_bytes = ET.tostring(opml, encoding="utf-8", xml_declaration=True)
    return xml_bytes.decode("utf-8")


class _HTMLFeedLinkParser(HTMLParser):
    """
    Parser HTML interno para extraer etiquetas <link rel="alternate">.
    """
    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.found_links: List[Dict[str, str]] = []
        self._valid_types = {
            "application/rss+xml",
            "application/atom+xml",
            "application/feed+json"
        }

    def handle_starttag(self, tag: str, attrs: list):
        if tag.lower() != "link":
            return
        
        attr_dict = {k.lower(): v for k, v in attrs if k and v is not None}
        rel = attr_dict.get("rel", "").lower().split()
        
        if "alternate" in rel:
            link_type = attr_dict.get("type", "").lower()
            if link_type in self._valid_types:
                href = attr_dict.get("href", "")
                if href:
                    abs_url = urllib.parse.urljoin(self.base_url, href)
                    title = attr_dict.get("title", "")
                    self.found_links.append({
                        "title": title,
                        "feed_url": abs_url
                    })


def discover_feeds(url: str, timeout: float = 15.0) -> List[Dict[str, str]]:
    """
    Realiza el autodescubrimiento de feeds dada una URL de sitio o de feed.
    Devuelve lista de {title: str, feed_url: str} (máximo 10, sin duplicados).
    """
    # a. Normaliza URL
    url_trimmed = url.strip()
    if not url_trimmed:
        return []

    parsed = urllib.parse.urlparse(url_trimmed)
    if not parsed.scheme:
        url_trimmed = "https://" + url_trimmed

    results: List[Dict[str, str]] = []
    seen_urls = set()

    def add_result(title: str, feed_url: str):
        if feed_url not in seen_urls and len(results) < 10:
            seen_urls.add(feed_url)
            results.append({"title": title, "feed_url": feed_url})

    # b. Descargar URL inicial con httpx
    client_headers = {"User-Agent": "MentatNews/1.0"}
    try:
        with netguard.SafeClient(timeout=timeout, headers=client_headers) as client:
            resp = client.get(url_trimmed)
            if resp.status_code >= 400:
                return []

            final_url = str(resp.url)
            content_type = resp.headers.get("content-type", "").lower()
            text_content = resp.text
            bytes_content = resp.content

            # Verificar si el contenido en sí es un feed
            is_feed_direct = False
            feed_title = ""

            if any(ft in content_type for ft in ["xml", "rss", "atom", "json"]):
                is_feed_direct = True
            
            if not is_feed_direct:
                lower_text = text_content[:2000].lower()
                if any(tag in lower_text for tag in ["<rss", "<feed", "<rdf"]):
                    is_feed_direct = True

            parsed_feed = feedparser.parse(bytes_content or text_content)
            if len(parsed_feed.entries) > 0 or (parsed_feed.version and parsed_feed.version != ""):
                is_feed_direct = True
                feed_title = parsed_feed.feed.get("title", "") if hasattr(parsed_feed, "feed") else ""

            if is_feed_direct:
                title = feed_title or (parsed_feed.feed.get("title", "") if hasattr(parsed_feed, "feed") else "")
                add_result(title, final_url)
                return results

            # c. Si es HTML, buscar etiquetas <link>
            parser = _HTMLFeedLinkParser(base_url=final_url)
            try:
                parser.feed(text_content)
            except Exception:
                pass

            for item in parser.found_links:
                item_title = item["title"]
                add_result(item_title, item["feed_url"])

            if results:
                return results

            # d. Probar rutas comunes si no se encontró ninguno
            common_paths = ["/feed", "/feed/", "/rss", "/rss.xml", "/atom.xml", "/index.xml", "/feed.xml"]
            for path in common_paths:
                if len(results) >= 10:
                    break
                candidate_url = urllib.parse.urljoin(final_url, path)
                if candidate_url in seen_urls:
                    continue

                try:
                    c_resp = client.get(candidate_url)
                    if c_resp.status_code == 200:
                        c_parsed = feedparser.parse(c_resp.content or c_resp.text)
                        if len(c_parsed.entries) > 0 or (c_parsed.version and c_parsed.version != ""):
                            c_title = c_parsed.feed.get("title", "") if hasattr(c_parsed, "feed") else ""
                            add_result(c_title, str(c_resp.url))
                except Exception:
                    continue

    except Exception:
        # e. Nunca lanza excepción por errores de red
        pass

    return results


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target_url = sys.argv[1]
        discovered = discover_feeds(target_url)
        print(f"Feeds descubiertos para '{target_url}':")
        for f in discovered:
            print(f"  - Título: {f['title']!r} | URL: {f['feed_url']}")
    else:
        print("Uso: python opml.py <URL>")
