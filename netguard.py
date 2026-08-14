"""Descargas HTTP con las riendas cortas.

MentatNews trae contenido de URLs que salen de feeds y de lo que escribe el
usuario. Sin control, una URL puede apuntar —o redirigir— a la red interna del
servidor, donde suele haber servicios sin autenticar; y como el lector devuelve
al navegador lo que descarga, esa respuesta interna acabaría filtrándose.

Aquí se resuelve cada salto antes de conectar y se rechaza todo lo que no
apunte a una IP pública. También se corta la descarga si el cuerpo se pasa de
tamaño, para que un feed enorme no se lleve la memoria por delante.

Límite conocido: entre la comprobación y la conexión hay una nueva resolución
DNS, así que un dominio que cambie de IP en ese instante (DNS rebinding) se
escapa. Cubrir eso exige fijar la IP y hablar TLS a mano; para el nivel de
riesgo de un lector personal, validar cada salto es la línea correcta.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx

MAX_REDIRECTS = 5
MAX_BYTES = 12 * 1024 * 1024  # 12 MB: de sobra para un feed o un artículo


class BlockedURL(Exception):
    """La URL apunta a un sitio al que no vamos a conectarnos."""


def check_url(url: str) -> None:
    """Lanza BlockedURL si la URL no es http(s) pública."""
    parts = urlparse(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedURL(f"esquema no permitido: {parts.scheme or '(ninguno)'}")
    host = parts.hostname
    if not host:
        raise BlockedURL("la URL no tiene host")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise BlockedURL(f"no se pudo resolver {host}: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise BlockedURL(f"{host} resuelve a una dirección interna ({ip})")


class SafeClient:
    """Sustituto de httpx.Client que valida cada salto de redirección."""

    def __init__(self, timeout: float = 20.0, headers: dict | None = None,
                 max_bytes: int = MAX_BYTES):
        self._client = httpx.Client(timeout=timeout, headers=headers or {},
                                    follow_redirects=False)
        self._max_bytes = max_bytes

    def __enter__(self) -> "SafeClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get(self, url: str, headers: dict | None = None) -> httpx.Response:
        """GET siguiendo redirecciones a mano, comprobando cada destino."""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            check_url(current)
            request = self._client.build_request("GET", current, headers=headers)
            response = self._client.send(request, stream=True)
            if response.is_redirect and response.headers.get("location"):
                destino = urljoin(current, response.headers["location"])
                response.close()
                current = destino
                continue
            try:
                response._content = self._read_capped(response)
            finally:
                response.close()
            return response
        raise BlockedURL(f"demasiadas redirecciones desde {url}")

    def _read_capped(self, response: httpx.Response) -> bytes:
        trozos, total = [], 0
        for trozo in response.iter_bytes():
            total += len(trozo)
            if total > self._max_bytes:
                raise BlockedURL(
                    f"la respuesta supera el límite de {self._max_bytes // (1024 * 1024)} MB")
            trozos.append(trozo)
        return b"".join(trozos)


def get(url: str, headers: dict | None = None, timeout: float = 20.0) -> httpx.Response:
    """GET puntual y validado."""
    with SafeClient(timeout=timeout) as client:
        return client.get(url, headers=headers)
