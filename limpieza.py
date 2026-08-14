import re
import difflib
import unicodedata
from urllib.parse import urljoin, urlparse
from lxml import html as lxml_html

# Regex compiladas para limpieza de metadatos
RE_HN_ARTICLE_URL = re.compile(r"Article URL:\s*https?://\S+", re.IGNORECASE)
RE_HN_COMMENTS_URL = re.compile(r"Comments URL:\s*https?://\S+", re.IGNORECASE)
RE_HN_POINTS = re.compile(r"Points:\s*\d+", re.IGNORECASE)
RE_HN_COMMENTS = re.compile(r"#?\s*Comments:\s*\d+", re.IGNORECASE)
RE_REDDIT = re.compile(r"submitted by /u/\S+ \[link\] \[comments\]", re.IGNORECASE)
RE_WP_THE_POST = re.compile(r"The post .*? appeared first on .*?\.", re.IGNORECASE)
RE_WP_I18N_IT = re.compile(r"L'articolo .*? proviene da .*?\.", re.IGNORECASE)
RE_WP_I18N_ES = re.compile(r"El art[ií]culo .*? aparece primero en .*?\.", re.IGNORECASE)
RE_READ_MORE = re.compile(r"(Read more\.\.\.|Continue reading\.\.\.|Leer mas\.\.\.|Leer m[aá]s\.\.\.|Leer mas|Leer m[aá]s)$", re.IGNORECASE)
RE_IMAGE_CREDIT = re.compile(r"^(?:(?:Image|Photo|Foto|Imagen)\s*:|Photo by\s+|Foto de\s+|Fotograf[íi]a por\s+)[^.]*(?:\.|$)\s*", re.IGNORECASE)
RE_ONLY_URLS = re.compile(r"^(\s*https?://\S+\s*)+$", re.IGNORECASE)
RE_LOOSE_MARKUP = re.compile(r"^\s*(?:</?[a-zA-Z0-9]+[^>]*>|</?[a-zA-Z0-9]+|[<>]+)\s*|\s*(?:</?[a-zA-Z0-9]+[^>]*>|</?[a-zA-Z0-9]+|[<>]+)\s*$")

def limpiar_resumen(html: str, *, titulo: str = "") -> str:
    """Limpia el HTML de un resumen y elimina metadatos."""
    if not html or not html.strip():
        return ""
    try:
        doc = lxml_html.fromstring(html)
        texto = doc.text_content()
    except Exception:
        texto = html
    
    # Remover patrones
    texto = RE_HN_ARTICLE_URL.sub("", texto)
    texto = RE_HN_COMMENTS_URL.sub("", texto)
    texto = RE_HN_POINTS.sub("", texto)
    texto = RE_HN_COMMENTS.sub("", texto)
    texto = RE_REDDIT.sub("", texto)
    texto = RE_WP_THE_POST.sub("", texto)
    texto = RE_WP_I18N_IT.sub("", texto)
    texto = RE_WP_I18N_ES.sub("", texto)
    texto = RE_IMAGE_CREDIT.sub("", texto)
    texto = texto.strip()
    texto = RE_READ_MORE.sub("", texto)
    
    # Limpiar restos de markup colgantes
    while True:
        prev = texto
        texto = RE_LOOSE_MARKUP.sub("", texto)
        if texto == prev: break
    texto = re.sub(r"[,;:\-]\s*$", "", texto).strip()
    
    # Colapsar espacios
    texto = re.sub(r'\s+', ' ', texto).strip()
    
    if RE_ONLY_URLS.match(texto):
        return ""
    
    # Check similitud con titulo
    def normalize(s: str) -> str:
        s = "".join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')
        return s.lower()
        
    if titulo and texto:
        norm_titulo = normalize(titulo)
        norm_texto = normalize(texto)
        sim = difflib.SequenceMatcher(None, norm_titulo, norm_texto).ratio()
        if sim > 0.9:
            return ""
            
    # Recortar a 400 caracteres
    if len(texto) > 400:
        texto = texto[:400]
        # Cortar en frontera de palabra
        idx = texto.rfind(" ")
        if idx > 0:
            texto = texto[:idx]
        # Quitar puntuacion colgante
        while texto and texto[-1] in ".,;:!?":
            texto = texto[:-1]
            
    return texto

def imagen_destacada(html: str, *, base_url: str = "", enclosures: list[dict] | None = None) -> str:
    """Extrae la URL de la mejor imagen del articulo."""
    try:
        if enclosures:
            for enc in enclosures:
                url = enc.get("url") or enc.get("href")
                t = enc.get("type", "")
                if t.startswith("image/") and url:
                    if _es_imagen_valida(url, -1):
                        return _absolutizar_url(url, base_url)
                        
        if not html or not html.strip():
            return ""
            
        doc = lxml_html.fromstring(html)
        imgs = doc.xpath('//img')
        
        mejor_url = ""
        max_area = -1
        primera_url = ""
        
        for img in imgs:
            src = img.get("src")
            if not src:
                continue
            
            w = img.get("width")
            h = img.get("height")
            
            try:
                area = int(w) * int(h) if w and h else -1
            except ValueError:
                area = -1
                
            if _es_imagen_valida(src, area):
                abs_url = _absolutizar_url(src, base_url)
                if abs_url:
                    if not primera_url:
                        primera_url = abs_url
                    if area > max_area:
                        max_area = area
                        mejor_url = abs_url
                        
        if max_area > 0 and mejor_url:
            return mejor_url
        return primera_url
    except Exception:
        return ""
        
def _es_imagen_valida(url: str, area: int) -> bool:
    if area != -1 and area <= 4:
        return False
        
    url_lower = url.lower()
    if url_lower.startswith("data:"):
        return False
        
    bad_keywords = ["pixel", "tracker", "beacon", "doubleclick", "feedburner", "gravatar", "blank.", "spacer.", "emoji"]
    for kw in bad_keywords:
        if kw in url_lower:
            return False
            
    if url_lower.endswith(".gif") and area <= 1:
        return False
        
    return True

def _absolutizar_url(url: str, base: str) -> str:
    abs_url = urljoin(base, url)
    parsed = urlparse(abs_url)
    if parsed.scheme in ("http", "https"):
        return abs_url
    return ""

if __name__ == "__main__":
    pruebas = [
        ("HN test",
         limpiar_resumen("Article URL: https://e360.yale.edu/x Comments URL: https://news.ycombinator.com/item?id=49298910 Points: 7 # Comments: 0", titulo="algo"),
         lambda x: "http" not in x and "Points" not in x and "Comments" not in x),
        ("The Verge test",
         limpiar_resumen("Un resumen legitimo de The Verge con dos frases. Debe conservarse integro.", titulo="titulo"),
         lambda x: "Un resumen legitimo" in x),
        ("Repite titulo",
         limpiar_resumen("Este es el super titulo", titulo="Este es el SUPER titulo!"),
         lambda x: x == ""),
        ("WP test",
         limpiar_resumen("Texto real del resumen. The post Como hacer X appeared first on Xataka.", titulo="a"),
         lambda x: "Texto real del resumen." in x and "The post" not in x),
        ("Image tracking",
         imagen_destacada('<img src="https://t.com/pixel.gif" width="1" height="1"><img src="https://f.com/a.jpg" width="800" height="600">'),
         lambda x: x == "https://f.com/a.jpg"),
        ("Base URL",
         imagen_destacada('<img src="/fotos/a.jpg">', base_url="https://sitio.com/nota"),
         lambda x: x == "https://sitio.com/fotos/a.jpg"),
        ("Solo URL HN",
         limpiar_resumen("<p><a href=\"...\">https://ai.google.dev/gemini-api/docs/models/gemini-3.7-flas...</a></p>"),
         lambda x: x == ""),
        ("Markup suelto",
         limpiar_resumen("Workers AI: serverless GPU-powered inference on Cloudflare's global network This post is also available in Deutsch, Espanol, Francais, ... and ... . <"),
         lambda x: not x.endswith("<") and "Workers AI" in x),
        ("URL en frase",
         limpiar_resumen("API Error: 529 Overloaded. This is a server-side issue, usually temporary - try again in a moment. If it persists, check https://status.claude.com."),
         lambda x: "https://status.claude.com." in x and len(x) > 20),
        ("Credit 1",
         limpiar_resumen("Photo by Foo. El presidente hablo ayer."),
         lambda x: x == "El presidente hablo ayer."),
        ("Credit Regresion",
         limpiar_resumen("Image sensors are getting cheaper. Y siguen bajando."),
         lambda x: x == "Image sensors are getting cheaper. Y siguen bajando.")
    ]
    
    ok = 0
    fail = 0
    for nombre, res, assert_fn in pruebas:
        if assert_fn(res):
            print(f"OK: {nombre}")
            ok += 1
        else:
            print(f"FAIL: {nombre} (Got: '{res}')")
            fail += 1
            
    print(f"TOTAL: OK={ok}, FAIL={fail}")
