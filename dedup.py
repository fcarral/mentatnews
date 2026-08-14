"""Modulo de deduplicacion y agrupacion de articulos de noticias para MentatNews.

Proporciona funciones para normalizar titulares, generar firmas hash SHA-256
y agrupar noticias duplicadas o similares usando un indice invertido eficiente,
difflib.SequenceMatcher y Union-Find (DSU).
"""

from collections import Counter, defaultdict
from datetime import datetime, timezone
import difflib
import hashlib
import re
import time
import unicodedata
from typing import Any

SEPARATORS = (" - ", " | ", " — ", " · ")
STOP_WORDS = {
    "el",
    "la",
    "los",
    "las",
    "un",
    "una",
    "de",
    "del",
    "en",
    "the",
    "a",
    "of",
    "in",
    "on",
    "for",
}


def normalizar(titulo: str) -> str:
    """Convierte el titulo a forma canonica comparable.

    Pasos:
    1. Recorta el sufijo del medio (separador ' - ', ' | ', ' — ', ' · ' al final
       si el sufijo tiene <= 40 caracteres). Debe hacerse ANTES de quitar puntuacion.
    2. Convierte a minusculas.
    3. Elimina acentos/diacriticos (unicodedata NFD + descarte de marcas Mn).
    4. Elimina signos de puntuacion.
    5. Colapsa espacios repetidos.
    6. Quita articulos y preposiciones muy comunes en espanol e ingles como palabras completas.
    """
    if not titulo or not isinstance(titulo, str):
        return ""

    s = titulo.strip()

    # 1. Recorte de sufijo del medio (ANTES de eliminar puntuacion)
    best_idx = -1
    best_sep_len = 0
    for sep in SEPARATORS:
        idx = s.rfind(sep)
        if idx > best_idx:
            best_idx = idx
            best_sep_len = len(sep)

    if best_idx != -1:
        sufijo = s[best_idx + best_sep_len :].strip()
        prefijo = s[:best_idx].strip()
        if 0 < len(sufijo) <= 40 and len(prefijo) > 0:
            s = prefijo

    # 2. Minusculas
    s = s.lower()

    # 3. Eliminar acentos y diacriticos
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")

    # 4. Eliminar signos de puntuacion
    s = re.sub(r"[^\w\s]|_", " ", s)

    # 5. Colapsar espacios y eliminar stop words
    words = [w for w in s.split() if w not in STOP_WORDS]
    return " ".join(words)


def firma(titulo: str) -> str:
    """Genera un hash SHA-256 de 16 caracteres hexadecimales.

    Basado en la version normalizada del titulo para deteccion O(1) de duplicados exactos.
    """
    norm = normalizar(titulo)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


class _DSU:
    """Estructura Disjoint Set Union (Union-Find) con compresion de caminos."""

    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        root = i
        while root != self.parent[root]:
            root = self.parent[root]
        curr = i
        while curr != root:
            nxt = self.parent[curr]
            self.parent[curr] = root
            curr = nxt
        return root

    def union(self, i: int, j: int) -> None:
        ri = self.find(i)
        rj = self.find(j)
        if ri != rj:
            self.parent[ri] = rj


def _parse_timestamp(dt_val: Any) -> float | None:
    """Parsea una cadena ISO8601 UTC a timestamp float en segundos epoch."""
    if not dt_val or not isinstance(dt_val, str):
        return None
    try:
        s = dt_val.strip()
        if s.endswith("Z") or s.endswith("z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def agrupar(
    articulos: list[dict], *, umbral: float = 0.82, ventana_horas: int = 48
) -> list[list[dict]]:
    """Agrupa articulos que corresponden a la misma noticia.

    DECISION DE DISENO PARA RENDIMIENTO (INDICE INVERTIDO Y COTA DE FRECUENCIA):
    Para evitar comparar todos contra todos (complejidad O(n^2)), se construye un
    indice invertido agrupando los articulos por palabras significativas (longitud > 3
    caracteres) de su titulo normalizado. Se evaluan únicamente los pares de articulos
    candidatos que:
      a) Tengan la misma firma SHA-256 (coincidencia exacta), o
      b) Compartan AL MENOS DOS palabras significativas (coincidencia aproximada).
    Ademas, para acotar el costo de palabras altamente frecuentes (como etiquetas globales
    o vocabulario repetitivo), las listas de posteo del indice invertido se acotan a los
    ultimos 100 elementos procesados. Esto garantiza un tiempo de ejecucion muy inferior
    a 2 segundos para lotes de miles de articulos.

    Reglas aplicadas:
    a. Ventana temporal: solo se agrupan articulos cuya diferencia de fecha sea menor que
       `ventana_horas`. Si a alguno le falta la fecha, la restriccion temporal no aplica.
    b. Mismo feed_id: dos articulos del mismo feed_id NUNCA se agrupan directamente entre si.
    c. Duplicado exacto: si firma() coincide, se agrupan directamente (si cumplen a y b).
    d. Duplicado aproximado: difflib.SequenceMatcher ratio >= umbral (si cumplen a y b).
    f. Transitividad: la agrupacion es transitiva mediante Union-Find (DSU).
    g. Ordenacion determinista: grupos ordenados segun la primera aparicion de su miembro
       mas temprano en la lista original de entrada. Dentro de cada grupo, ordenados por
       fecha ascendente (los que no tienen fecha van al final del grupo).
    """
    N = len(articulos)
    if N == 0:
        return []

    # Precalculo de campos normalizados
    norms = [normalizar(a.get("title", "")) for a in articulos]
    firmas_list = [
        hashlib.sha256(n.encode("utf-8")).hexdigest()[:16] for n in norms
    ]
    timestamps = [_parse_timestamp(a.get("published_at")) for a in articulos]
    feed_ids = [a.get("feed_id") for a in articulos]
    sig_words_list = [
        set(w for w in n.split() if len(w) > 3) for n in norms
    ]

    dsu = _DSU(N)
    firma_to_indices = defaultdict(list)
    word_to_indices = defaultdict(list)
    ventana_sec = ventana_horas * 3600.0
    # Recorte de las listas de posteo para que una palabra muy frecuente no
    # dispare las comparaciones. Con 100 se perdían duplicados separados por
    # muchos artículos que compartían vocabulario; 800 mantiene el recall con
    # 90 fuentes y sigue muy por debajo del presupuesto de tiempo.
    MAX_POSTING = 800

    for j in range(N):
        f_j = firmas_list[j]
        sig_w_j = sig_words_list[j]
        feed_j = feed_ids[j]
        ts_j = timestamps[j]

        # Candidatos por firma exacta
        exact_cands = set(firma_to_indices[f_j])

        # Candidatos por palabras significativas compartidas (>= 2)
        match_counts = Counter()
        for w in sig_w_j:
            postings = word_to_indices[w]
            if len(postings) > MAX_POSTING:
                postings = postings[-MAX_POSTING:]
            for i in postings:
                match_counts[i] += 1

        approx_cands = {i for i, count in match_counts.items() if count >= 2}
        candidates = exact_cands | approx_cands

        for i in candidates:
            # Si ya forman parte del mismo grupo transitivo, saltar
            if dsu.find(i) == dsu.find(j):
                continue
            # Regla b: enlace directo del mismo feed prohibido
            if feed_ids[i] is not None and feed_ids[i] == feed_j:
                continue
            # Regla a: diferencia temporal < ventana_horas
            if timestamps[i] is not None and ts_j is not None:
                if abs(timestamps[i] - ts_j) >= ventana_sec:
                    continue
            # Regla c: duplicado exacto por firma
            if firmas_list[i] == f_j:
                dsu.union(i, j)
            else:
                # Regla d: ratio de similitud textil
                # SequenceMatcher no es simétrico: comparar A con B puede dar
                # 0,737 y B con A, 0,821. Con un solo sentido, dos titulares de
                # formato idéntico pero asunto distinto ("Cable One (CABO) Q2
                # 2026 Earnings Call Transcript" y el de otra empresa) se
                # agrupaban según el orden de la lista, ocultando un artículo
                # legítimo. Se toma el sentido más bajo: ante la duda, no fundir.
                ratio = min(
                    difflib.SequenceMatcher(None, norms[i], norms[j]).ratio(),
                    difflib.SequenceMatcher(None, norms[j], norms[i]).ratio(),
                )
                if ratio >= umbral:
                    dsu.union(i, j)

        # Actualizar indice invertido con el articulo j
        firma_to_indices[f_j].append(j)
        for w in sig_w_j:
            word_to_indices[w].append(j)

    # Agrupar indices por raiz de DSU
    groups_map = defaultdict(list)
    for idx in range(N):
        r = dsu.find(idx)
        groups_map[r].append(idx)

    # Criterio de ordenacion interna por fecha ascendente (sin fecha al final)
    def member_sort_key(idx: int):
        ts = timestamps[idx]
        if ts is not None:
            return (0, ts, idx)
        return (1, float("inf"), idx)

    raw_groups = list(groups_map.values())
    for grp in raw_groups:
        grp.sort(key=member_sort_key)

    # Ordenar grupos por posicion inicial de aparicion de su primer elemento
    raw_groups.sort(key=lambda grp: min(grp))

    return [[articulos[idx] for idx in grp] for grp in raw_groups]


if __name__ == "__main__":
    import random

    ok_count = 0
    fail_count = 0

    def probar(nombre: str, condicion: bool):
        global ok_count, fail_count
        if condicion:
            print(f"[OK] {nombre}")
            ok_count += 1
        else:
            print(f"[FAIL] {nombre}")
            fail_count += 1

    # Prueba 1: 5 titulares de la misma noticia en 5 medios distintos
    arts_p1 = [
        {
            "id": 1,
            "title": "Terremoto de magnitud 7.5 sacude Mexico - Reuters",
            "published_at": "2026-08-14T10:00:00Z",
            "feed_id": 1,
        },
        {
            "id": 2,
            "title": "Un terremoto de magnitud 7.5 sacude Mexico | BBC News",
            "published_at": "2026-08-14T10:15:00Z",
            "feed_id": 2,
        },
        {
            "id": 3,
            "title": "Terremoto de magnitud 7.5 sacude el sur de Mexico — El Pais",
            "published_at": "2026-08-14T10:30:00Z",
            "feed_id": 3,
        },
        {
            "id": 4,
            "title": "Terremoto de magnitud 7.5 sacude Mexico · Milenio",
            "published_at": "2026-08-14T09:50:00Z",
            "feed_id": 4,
        },
        {
            "id": 5,
            "title": "Terremoto magnitud 7.5 sacude Mexico City - CNN",
            "published_at": "2026-08-14T11:00:00Z",
            "feed_id": 5,
        },
    ]
    res_p1 = agrupar(arts_p1)
    probar(
        "Cinco titulares de la misma noticia en cinco medios distintos se agrupan en 1 solo grupo",
        len(res_p1) == 1 and len(res_p1[0]) == 5,
    )

    # Prueba 2: Dos noticias claramente distintas
    arts_p2 = [
        {
            "id": 1,
            "title": "El Banco Central sube los tipos de interes al 4% - Reuters",
            "published_at": "2026-08-14T10:00:00Z",
            "feed_id": 1,
        },
        {
            "id": 2,
            "title": "Descubren una nueva especie de dinosaurio en la Patagonia — El Pais",
            "published_at": "2026-08-14T10:00:00Z",
            "feed_id": 2,
        },
    ]
    res_p2 = agrupar(arts_p2)
    probar(
        "Dos noticias claramente distintas resultan en dos grupos de 1",
        len(res_p2) == 2 and all(len(g) == 1 for g in res_p2),
    )

    # Prueba 3: Dos titulares casi identicos del mismo feed_id (NO se agrupan)
    arts_p3 = [
        {
            "id": 1,
            "title": "Apple presenta el nuevo iPhone 18 con chip A20 - TechCrunch",
            "published_at": "2026-08-14T10:00:00Z",
            "feed_id": 1,
        },
        {
            "id": 2,
            "title": "Apple presenta el nuevo iPhone 18 Pro con chip A20 - TechCrunch",
            "published_at": "2026-08-14T10:05:00Z",
            "feed_id": 1,
        },
    ]
    res_p3 = agrupar(arts_p3)
    probar(
        "Dos titulares casi identicos del mismo feed_id NO se agrupan directamente",
        len(res_p3) == 2,
    )

    # Prueba 4: Dos titulares identicos separados por 5 dias (fuera de ventana)
    arts_p4 = [
        {
            "id": 1,
            "title": "La Bolsa de Madrid cierra con una subida del 2% - EFE",
            "published_at": "2026-08-01T12:00:00Z",
            "feed_id": 1,
        },
        {
            "id": 2,
            "title": "La Bolsa de Madrid cierra con una subida del 2% - Reuters",
            "published_at": "2026-08-06T12:00:00Z",
            "feed_id": 2,
        },
    ]
    res_p4 = agrupar(arts_p4)
    probar(
        "Dos titulares identicos separados por 5 dias NO se agrupan por estar fuera de ventana",
        len(res_p4) == 2,
    )

    # Prueba 5: Prueba de rendimiento con 3.000 articulos sinteticos (< 2 segundos)
    random.seed(42)
    words_pool = [f"palabra{i}" for i in range(1000)]
    outlets = [
        " - Reuters",
        " | BBC News",
        " — El Pais",
        " · Milenio",
        " - EFE",
        "",
    ]
    synth_arts = []
    for i in range(3000):
        w_choice = random.sample(words_pool, 6)
        t = " ".join(w_choice) + random.choice(outlets)
        f_id = random.randint(1, 50)
        ts = f"2026-08-14T{random.randint(0,23):02d}:{random.randint(0,59):02d}:00Z"
        synth_arts.append(
            {"id": i + 1, "title": t, "published_at": ts, "feed_id": f_id}
        )

    t_inicio = time.perf_counter()
    res_p5 = agrupar(synth_arts)
    t_fin = time.perf_counter()
    duracion = t_fin - t_inicio
    print(f"Tiempo medido en prueba de 3.000 articulos: {duracion:.4f} segundos")

    probar(
        "Prueba de rendimiento con 3.000 articulos sinteticos (tarda menos de 2 segundos)",
        duracion < 2.0,
    )

    print(f"TOTAL: {ok_count} OK, {fail_count} FAIL")
