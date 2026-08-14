"""Motor de descarga y parseo de feeds RSS/Atom para MentatNews."""

import calendar
import datetime
import hashlib
import html
from html.parser import HTMLParser
import urllib.parse
from typing import Any

import feedparser
import httpx

import netguard

# Identifícate ante los servidores de los feeds. Cámbialo por tu propia URL.
USER_AGENT = "MentatNews/1.0 (+https://github.com/fcarral/mentatnews)"

ALLOWED_TAGS = {
    "p", "a", "img", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li",
    "blockquote", "pre", "code", "em", "strong", "b", "i", "br", "hr",
    "figure", "figcaption", "table", "thead", "tbody", "tr", "th", "td",
    "span", "div", "audio", "video", "source"
}

DROP_CONTENT_TAGS = {
    "script", "style", "iframe", "object", "embed", "form", "input"
}

VOID_TAGS = {"br", "hr", "img", "source"}

ALLOWED_ATTRS = {
    "href", "src", "alt", "title", "width", "height", "type", "controls"
}


class _HTMLSanitizer(HTMLParser):
    """Sanitizador HTML basado en lista blanca para limpiar contenido de feeds."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.drop_depth: int = 0
        self.result: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        if tag_lower in DROP_CONTENT_TAGS:
            self.drop_depth += 1
            return

        if self.drop_depth > 0:
            return

        if tag_lower in ALLOWED_TAGS:
            cleaned_attrs: list[str] = []
            for attr_name, attr_val in attrs:
                attr_lower = attr_name.lower()
                if attr_lower.startswith("on"):
                    continue
                if attr_lower not in ALLOWED_ATTRS:
                    continue
                val_str = attr_val or ""
                cleaned_val = "".join(val_str.lower().split())
                if "javascript:" in cleaned_val:
                    continue
                if attr_lower in ("href", "src"):
                    parsed = urllib.parse.urlparse(val_str.strip().lower())
                    if parsed.scheme and parsed.scheme not in ("http", "https"):
                        continue
                escaped_val = html.escape(val_str, quote=True)
                cleaned_attrs.append(f'{attr_lower}="{escaped_val}"')

            if tag_lower == "a":
                cleaned_attrs = [
                    a for a in cleaned_attrs
                    if not (a.startswith('target="') or a.startswith('rel="'))
                ]
                cleaned_attrs.append('target="_blank"')
                cleaned_attrs.append('rel="noopener noreferrer"')

            attr_str = (" " + " ".join(cleaned_attrs)) if cleaned_attrs else ""
            self.result.append(f"<{tag_lower}{attr_str}>")

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if tag_lower in DROP_CONTENT_TAGS:
            if self.drop_depth > 0:
                self.drop_depth -= 1
            return

        if self.drop_depth > 0:
            return

        if tag_lower in ALLOWED_TAGS:
            if tag_lower not in VOID_TAGS:
                self.result.append(f"</{tag_lower}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        tag_lower = tag.lower()
        if tag_lower not in VOID_TAGS and self.drop_depth == 0 and tag_lower in ALLOWED_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self.drop_depth > 0:
            return
        self.result.append(html.escape(data))

    def get_result(self) -> str:
        return "".join(self.result)


def sanitize_html(html_content: str) -> str:
    """Sanitiza una cadena HTML eliminando elementos y atributos no permitidos."""
    if not html_content or not isinstance(html_content, str):
        return ""
    try:
        parser = _HTMLSanitizer()
        parser.feed(html_content)
        parser.close()
        return parser.get_result()
    except Exception:
        return html.escape(html_content)


def fetch_and_parse(
    url: str,
    etag: str | None = None,
    last_modified: str | None = None,
    timeout: float = 20.0
) -> dict[str, Any]:
    """Descarga y parsea un feed RSS/Atom desde una URL dada."""
    try:
        headers = {
            "User-Agent": USER_AGENT
        }
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        with netguard.SafeClient(timeout=timeout) as client:
            response = client.get(url, headers=headers)

        http_status: int | None = response.status_code
        res_etag: str | None = response.headers.get("etag")
        res_last_modified: str | None = response.headers.get("last-modified")

        if http_status == 304:
            return {
                "status": "not_modified",
                "http_status": 304,
                "etag": res_etag or etag,
                "last_modified": res_last_modified or last_modified,
                "title": "",
                "site_url": "",
                "description": "",
                "favicon_url": "",
                "entries": [],
                "error": ""
            }

        response.raise_for_status()

        parsed = feedparser.parse(response.content)

        if parsed.get("bozo") and not parsed.entries:
            bozo_exc = parsed.get("bozo_exception")
            err_msg = str(bozo_exc) if bozo_exc else "Error al parsear el feed"
            return {
                "status": "error",
                "http_status": http_status,
                "etag": res_etag,
                "last_modified": res_last_modified,
                "title": "",
                "site_url": "",
                "description": "",
                "favicon_url": "",
                "entries": [],
                "error": err_msg
            }

        feed_meta = parsed.get("feed", {})
        title = str(feed_meta.get("title", ""))
        site_url = str(feed_meta.get("link", ""))
        description = str(
            feed_meta.get("description") or feed_meta.get("subtitle") or ""
        )

        domain_target = site_url if site_url else url
        parsed_domain = urllib.parse.urlparse(domain_target).hostname or ""
        favicon_url = (
            f"https://www.google.com/s2/favicons?domain={parsed_domain}&sz=64"
            if parsed_domain
            else ""
        )

        entries: list[dict[str, Any]] = []
        for entry in parsed.entries:
            entry_title = str(entry.get("title", ""))
            entry_link = str(entry.get("link", ""))
            entry_author = str(entry.get("author", ""))

            st = entry.get("published_parsed") or entry.get("updated_parsed")
            published_str: str | None = None
            if st:
                try:
                    ts = calendar.timegm(st)
                    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
                    published_str = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                except Exception:
                    published_str = None

            entry_id = entry.get("id")
            if entry_id:
                guid = str(entry_id)
            elif entry_link:
                guid = entry_link
            else:
                raw_guid = (entry_title + (published_str or "")).encode("utf-8")
                guid = hashlib.sha256(raw_guid).hexdigest()[:32]

            raw_content = ""
            if "content" in entry and entry.content:
                first_content = entry.content[0]
                if isinstance(first_content, dict):
                    raw_content = first_content.get("value", "")
                else:
                    raw_content = getattr(first_content, "value", "")

            content_html = sanitize_html(str(raw_content))
            summary_html = sanitize_html(str(entry.get("summary", "")))

            tags: list[str] = []
            if "tags" in entry and entry.tags:
                for t in entry.tags:
                    term = t.get("term") if isinstance(t, dict) else getattr(t, "term", None)
                    if term and isinstance(term, str):
                        tags.append(term)

            enclosures: list[dict[str, str]] = []
            if "enclosures" in entry and entry.enclosures:
                for enc in entry.enclosures:
                    enc_url = (
                        enc.get("href") or enc.get("url")
                        if isinstance(enc, dict)
                        else (getattr(enc, "href", None) or getattr(enc, "url", None))
                    )
                    enc_type = (
                        enc.get("type")
                        if isinstance(enc, dict)
                        else getattr(enc, "type", None)
                    )
                    if enc_url:
                        enclosures.append({
                            "url": str(enc_url),
                            "type": str(enc_type or "")
                        })

            entries.append({
                "guid": guid,
                "url": entry_link if entry_link else "",
                "title": entry_title,
                "author": entry_author,
                "summary_html": summary_html,
                "content_html": content_html,
                "published": published_str,
                "tags": tags,
                "enclosures": enclosures,
            })

        return {
            "status": "ok",
            "http_status": http_status,
            "etag": res_etag,
            "last_modified": res_last_modified,
            "title": title,
            "site_url": site_url,
            "description": description,
            "favicon_url": favicon_url,
            "entries": entries,
            "error": ""
        }

    except Exception as e:
        status_code = None
        if hasattr(e, "response") and getattr(e, "response") is not None:
            status_code = getattr(e.response, "status_code", None)

        return {
            "status": "error",
            "http_status": status_code,
            "etag": None,
            "last_modified": None,
            "title": "",
            "site_url": "",
            "description": "",
            "favicon_url": "",
            "entries": [],
            "error": str(e)
        }


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Uso: python fetcher.py <URL_DEL_FEED>")
        sys.exit(1)

    feed_url = sys.argv[1]
    result = fetch_and_parse(feed_url)

    print(f"Status: {result['status']}")
    print(f"HTTP Status: {result['http_status']}")
    print(f"Título: {result['title']}")
    print(f"Número de entradas: {len(result['entries'])}")
    if result["error"]:
        print(f"Error: {result['error']}")

    print("\nPrimeras 3 entradas:")
    for i, entry in enumerate(result["entries"][:3], start=1):
        print(f"  {i}. {entry['title']} ({entry['published']})")
