"""
Módulo de búsqueda avanzada para MentatNews (FTS5).
"""

import re
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional

# Expresiones regulares compiladas a nivel de módulo
TOKEN_RE = re.compile(
    r'(?P<filtro_quote>[a-zA-Z_]+:"[^"]*")|'
    r'(?P<filtro>[a-zA-Z_]+:[^\s()"]+)|'
    r'(?P<frase>"[^"]*")|'
    r'(?P<paren>[()])|'
    r'(?P<negado>-[^\s()"]+)|'
    r'(?P<termino>[^\s()"]+)'
)

def escape_fts(t: str) -> str:
    """Escapa las comillas dobles en un término para FTS5."""
    return '"' + t.replace('"', '""') + '"'

def parse_date_filter(val: str) -> Optional[str]:
    """Convierte un sufijo de fecha relativo o absoluto en ISO8601 UTC."""
    val = val.lower()
    now = datetime.now(timezone.utc)
    try:
        if val.endswith('d') and val[:-1].isdigit():
            dt = now - timedelta(days=int(val[:-1]))
        elif val.endswith('h') and val[:-1].isdigit():
            dt = now - timedelta(hours=int(val[:-1]))
        elif val.endswith('w') and val[:-1].isdigit():
            dt = now - timedelta(weeks=int(val[:-1]))
        elif val.endswith('m') and val[:-1].isdigit():
            dt = now - timedelta(days=int(val[:-1])*30)
        else:
            dt = datetime.strptime(val, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None

def agregar_filtro(filtros: Dict[str, Any], clave: str, valor: str) -> bool:
    """Añade un filtro al diccionario, parseando fechas o listas si es necesario."""
    clave = clave.lower()
    
    if clave == "tiene" and valor.lower() == "imagen":
        filtros["tiene_imagen"] = True
        return True
        
    if clave in ("desde", "hasta"):
        parsed = parse_date_filter(valor)
        if not parsed:
            return False
        filtros[clave] = parsed
        return True
        
    if clave in filtros:
        if isinstance(filtros[clave], list):
            filtros[clave].append(valor)
        else:
            filtros[clave] = [filtros[clave], valor]
    else:
        filtros[clave] = valor
    return True

def validate_syntax(tokens: list) -> bool:
    """Verifica que la secuencia de tokens sea válida para FTS5."""
    if not tokens:
        return True
    
    depth = 0
    for t in tokens:
        if t['type'] == '(': depth += 1
        elif t['type'] == ')': depth -= 1
        if depth < 0: return False
    if depth != 0: return False

    if tokens[0]['type'] in ('OP', ')'):
        return False
    if tokens[-1]['type'] in ('OP', '('):
        return False

    for i in range(len(tokens) - 1):
        t1 = tokens[i]
        t2 = tokens[i+1]
        
        if t1['type'] == 'OP':
            if t2['type'] not in ('VAL', '('): return False
        elif t1['type'] == '(':
            if t2['type'] not in ('VAL', '('): return False
        elif t1['type'] == 'VAL':
            if t2['type'] not in ('OP', ')'): return False
        elif t1['type'] == ')':
            if t2['type'] not in ('OP', ')'): return False
            
    return True

def parsear(consulta: str) -> Dict[str, Any]:
    """
    Convierte una cadena de consulta en un diccionario con fts, filtros y error.
    """
    if consulta.count('"') % 2 != 0:
        return {"fts": "", "filtros": {}, "error": "Comillas sin cerrar en la consulta."}
        
    filtros: Dict[str, Any] = {}
    tokens = []
    
    for m in TOKEN_RE.finditer(consulta):
        if m.group('filtro_quote'):
            k, v = m.group('filtro_quote').split(':', 1)
            v = v[1:-1]
            if not agregar_filtro(filtros, k, v):
                return {"fts": "", "filtros": filtros, "error": f"Valor inválido en filtro {k}"}
        elif m.group('filtro'):
            k, v = m.group('filtro').split(':', 1)
            if not agregar_filtro(filtros, k, v):
                return {"fts": "", "filtros": filtros, "error": f"Valor inválido en filtro {k}"}
        elif m.group('frase'):
            v = m.group('frase')[1:-1]
            tokens.append({'type': 'VAL', 'val': escape_fts(v)})
        elif m.group('paren'):
            tokens.append({'type': m.group('paren'), 'val': m.group('paren')})
        elif m.group('negado'):
            v = m.group('negado')[1:]
            tokens.append({'type': 'OP', 'val': 'NOT'})
            if v.endswith('*'):
                tokens.append({'type': 'VAL', 'val': escape_fts(v[:-1]) + '*'})
            else:
                tokens.append({'type': 'VAL', 'val': escape_fts(v)})
        elif m.group('termino'):
            v = m.group('termino')
            if v in ("AND", "OR", "NOT"):
                tokens.append({'type': 'OP', 'val': v})
            else:
                if v.endswith('*'):
                    tokens.append({'type': 'VAL', 'val': escape_fts(v[:-1]) + '*'})
                else:
                    tokens.append({'type': 'VAL', 'val': escape_fts(v)})

    # Implicit AND
    enriched = []
    for i, t in enumerate(tokens):
        if i > 0:
            prev = tokens[i-1]
            if prev['type'] in ('VAL', ')') and t['type'] in ('VAL', '('):
                enriched.append({'type': 'OP', 'val': 'AND'})
        enriched.append(t)
        
    if not validate_syntax(enriched):
        return {"fts": "", "filtros": filtros, "error": "Sintaxis de búsqueda inválida."}
        
    fts_str = " ".join(t['val'] for t in enriched)
    fts_str = fts_str.replace("( ", "(").replace(" )", ")")
    return {"fts": fts_str, "filtros": filtros, "error": ""}

def describir(filtros: Dict[str, Any]) -> str:
    """Devuelve una frase corta en español describiendo los filtros."""
    if not filtros:
        return ""
    
    partes = []
    
    if "fuente" in filtros:
        v = filtros["fuente"]
        if isinstance(v, list):
            partes.append(f"en {', '.join(v)}")
        else:
            partes.append(f"en {v}")
            
    if "tema" in filtros:
        v = filtros["tema"]
        if isinstance(v, list):
            partes.append(f"tema {', '.join(v)}")
        else:
            partes.append(f"tema {v}")
            
    if "autor" in filtros:
        v = filtros["autor"]
        if isinstance(v, list):
            partes.append(f"por {', '.join(v)}")
        else:
            partes.append(f"por {v}")
            
    if "estado" in filtros:
        v = filtros["estado"]
        if v == "sinleer":
            partes.append("sin leer")
        elif v == "leido":
            partes.append("leído")
        elif v == "guardado":
            partes.append("guardado")
        else:
            partes.append(f"estado {v}")
            
    if filtros.get("tiene_imagen"):
        partes.append("con imagen")
        
    if "desde" in filtros:
        partes.append(f"desde {filtros['desde'][:10]}")
        
    if "hasta" in filtros:
        partes.append(f"hasta {filtros['hasta'][:10]}")
        
    return " · ".join(partes)

if __name__ == "__main__":
    pruebas = [
        ("incendio huelva", '"incendio" AND "huelva"', False),
        ('"consejo constitucional"', '"consejo constitucional"', False),
        ("incendio AND Huelva", '"incendio" AND "Huelva"', False),
        ("a OR b", '"a" OR "b"', False),
        ("a -b", '"a" NOT "b"', False),
        ("incend*", '"incend"*', False),
        ("(a OR b) AND c", '("a" OR "b") AND "c"', False),
        ("fuente:xataka incendio", '"incendio"', False),
        ("desde:7d", "", False),
        ('fuente:"el pais" tema:España estado:sinleer', "", False),
        ("a AND", "", True),
        ('comillas sin " cerrar', "", True),
        ('a" OR 1=1 --', "", True)
    ]
    
    fallos = 0
    print("=== AUTOPRUEBAS ===")
    for q, esp_fts, esp_err in pruebas:
        r = parsear(q)
        ok = True
        msg = []
        if esp_err and not r['error']:
            ok = False
            msg.append("Se esperaba error y no lo hubo.")
        elif not esp_err and r['error']:
            ok = False
            msg.append(f"No se esperaba error, pero dio: {r['error']}")
        elif not esp_err and r['fts'] != esp_fts:
            ok = False
            msg.append(f"FTS esperado: {esp_fts!r}, obtenido: {r['fts']!r}")
            
        if ok:
            print(f"OK   | {q!r}")
        else:
            print(f"FAIL | {q!r} -> {' '.join(msg)}")
            fallos += 1
            
    r = parsear("desde:7d")
    if "desde" in r['filtros'] and re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", r['filtros']["desde"]):
        print("OK   | Formato de fecha desde:7d verificado.")
    else:
        print("FAIL | Formato de fecha incorrecto en desde:7d")
        fallos += 1
        
    print(f"=== RESUMEN: {len(pruebas)+1 - fallos} OK, {fallos} FAIL ===")
