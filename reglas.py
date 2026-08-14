import re
import unicodedata
import urllib.parse
from typing import Any, Callable

def normalizar_texto(texto: str) -> str:
    if texto is None:
        return ""
    texto_norm = unicodedata.normalize('NFD', str(texto))
    return ''.join(c for c in texto_norm if unicodedata.category(c) != 'Mn').lower()

def compilar(regla: dict) -> dict:
    regla_compilada = regla.copy()
    if 'sensible' not in regla_compilada:
        regla_compilada['sensible'] = False
    if 'ambito_carpeta' not in regla_compilada:
        regla_compilada['ambito_carpeta'] = None
    if 'ambito_feed' not in regla_compilada:
        regla_compilada['ambito_feed'] = None
    if 'activa' not in regla_compilada:
        regla_compilada['activa'] = True

    tipo = regla_compilada.get('tipo')
    if tipo not in ["silencio", "alerta"]:
        raise ValueError(f"Tipo desconocido: {tipo}")
        
    campo = regla_compilada.get('campo')
    if campo not in ["titulo", "resumen", "autor", "dominio", "cualquiera"]:
        raise ValueError(f"Campo desconocido: {campo}")

    operador = regla_compilada.get('operador')
    valid_ops = ["contiene", "es", "empieza", "termina", "regex", "palabra"]
    if operador not in valid_ops:
        raise ValueError(f"Operador desconocido: {operador}")

    patron = regla_compilada.get('patron')
    if not patron:
        raise ValueError("El patrón no puede estar vacío")

    sensible = regla_compilada['sensible']
    
    # normalizamos patron si no es sensible
    patron_str = str(patron)
    if not sensible and operador != "regex":
        patron_procesado = normalizar_texto(patron_str)
    elif not sensible and operador == "regex":
        # Para regex solo normalizamos acentos y usamos IGNORECASE para no romper escapes como \W
        texto_norm = unicodedata.normalize('NFD', patron_str)
        patron_procesado = ''.join(c for c in texto_norm if unicodedata.category(c) != 'Mn')
    else:
        patron_procesado = patron_str

    func: Callable[[str], bool]
    
    if operador == "regex":
        import time
        flags = 0 if sensible else re.IGNORECASE
        t0 = time.perf_counter()
        if len(patron_procesado) > 500:
            raise ValueError("Expresión regular demasiado larga")
        try:
            regex_compilada = re.compile(patron_procesado, flags)
        except re.error as e:
            raise ValueError(f"Expresión regular mal formada: {e}")
        t1 = time.perf_counter()
        if t1 - t0 > 0.1:
            raise ValueError("La expresión regular tarda demasiado en compilar")
            
        def func_regex(texto: str) -> bool:
            return bool(regex_compilada.search(texto))
        func = func_regex
        
    elif operador == "palabra":
        patron_esc = re.escape(patron_procesado)
        # re.search will find the word boundaries
        regex_palabra = re.compile(rf"\b{patron_esc}\b", 0 if sensible else re.IGNORECASE)
        def func_palabra(texto: str) -> bool:
            return bool(regex_palabra.search(texto))
        func = func_palabra
        
    elif operador == "contiene":
        def func_contiene(texto: str) -> bool:
            return patron_procesado in texto
        func = func_contiene
        
    elif operador == "es":
        def func_es(texto: str) -> bool:
            return patron_procesado == texto
        func = func_es
        
    elif operador == "empieza":
        def func_empieza(texto: str) -> bool:
            return texto.startswith(patron_procesado)
        func = func_empieza
        
    elif operador == "termina":
        def func_termina(texto: str) -> bool:
            return texto.endswith(patron_procesado)
        func = func_termina

    regla_compilada['_probar'] = func
    return regla_compilada

def evaluar(articulo: dict, reglas_compiladas: list[dict]) -> dict:
    resultado: dict[str, Any] = {
        "silenciar": False,
        "motivo_silencio": None,
        "alertas": []
    }
    
    valores_brutos: dict[str, str] = {}
    valores_normalizados: dict[str, str] = {}
    
    for regla in reglas_compiladas:
        if not regla.get('activa', True):
            continue
            
        folder_id = articulo.get('folder_id')
        if regla.get('ambito_carpeta') is not None and regla['ambito_carpeta'] != folder_id:
            continue
            
        feed_id = articulo.get('feed_id')
        if regla.get('ambito_feed') is not None and regla['ambito_feed'] != feed_id:
            continue
            
        campo = regla['campo']
        sensible = regla['sensible']
        
        if campo not in valores_brutos:
            if campo == "titulo":
                val = articulo.get('title') or ""
            elif campo == "resumen":
                val = articulo.get('summary') or ""
            elif campo == "autor":
                val = articulo.get('author') or ""
            elif campo == "dominio":
                url = articulo.get('url') or ""
                val = urllib.parse.urlparse(str(url)).netloc
                if val.startswith("www."):
                    val = val[4:]
            elif campo == "cualquiera":
                t = articulo.get('title') or ""
                s = articulo.get('summary') or ""
                a = articulo.get('author') or ""
                val = f"{t} {s} {a}"
            else:
                val = ""
            valores_brutos[campo] = str(val)
            
        val = valores_brutos[campo]
        
        if not sensible:
            if campo not in valores_normalizados:
                valores_normalizados[campo] = normalizar_texto(val)
            texto_a_probar = valores_normalizados[campo]
        else:
            texto_a_probar = val
                
        if regla['_probar'](texto_a_probar):
            if regla['tipo'] == "silencio":
                if not resultado['silenciar']:
                    resultado['silenciar'] = True
                    resultado['motivo_silencio'] = regla['id']
            elif regla['tipo'] == "alerta":
                resultado['alertas'].append(regla['id'])
                
    return resultado

def evaluar_lote(articulos: list[dict], reglas: list[dict]) -> dict[int, dict]:
    reglas_compiladas = [compilar(r) for r in reglas]
    return {art.get("id", 0): evaluar(art, reglas_compiladas) for art in articulos}

if __name__ == "__main__":
    import sys
    import time
    
    total = 0
    ok = 0
    
    def test(name, cond):
        global total, ok
        total += 1
        if cond:
            print(f"OK: {name}")
            ok += 1
        else:
            print(f"FAIL: {name}")
            
    # a) "contiene" sin acentos ni mayusculas
    r1 = compilar({"id": 1, "tipo": "alerta", "campo": "titulo", "operador": "contiene", "patron": "peliculas"})
    res1 = evaluar({"title": "Las Mejores Películas"}, [r1])
    test("Contiene sin acentos ni mayusculas", 1 in res1['alertas'])
    
    # b) "palabra"
    r2 = compilar({"id": 2, "tipo": "alerta", "campo": "cualquiera", "operador": "palabra", "patron": "IA"})
    res2_a = evaluar({"title": "GUIA"}, [r2])
    res2_b = evaluar({"title": "la IA generativa"}, [r2])
    test("Palabra IA", 2 not in res2_a['alertas'] and 2 in res2_b['alertas'])
    
    # c) "dominio"
    r3 = compilar({"id": 3, "tipo": "alerta", "campo": "dominio", "operador": "es", "patron": "xataka.com"})
    res3 = evaluar({"url": "https://www.xataka.com/x"}, [r3])
    test("Dominio", 3 in res3['alertas'])
    
    # d) "ambito_carpeta"
    r4 = compilar({"id": 4, "tipo": "alerta", "campo": "titulo", "operador": "contiene", "patron": "test", "ambito_carpeta": 10})
    res4_a = evaluar({"title": "test", "folder_id": 10}, [r4])
    res4_b = evaluar({"title": "test", "folder_id": 20}, [r4])
    test("Ambito carpeta", 4 in res4_a['alertas'] and 4 not in res4_b['alertas'])
    
    # e) Alerta y silencio a la vez
    r5 = compilar({"id": 5, "tipo": "silencio", "campo": "titulo", "operador": "contiene", "patron": "test"})
    r6 = compilar({"id": 6, "tipo": "alerta", "campo": "titulo", "operador": "contiene", "patron": "test"})
    res_es = evaluar({"title": "test"}, [r5, r6])
    test("Alerta y silencio a la vez", res_es['silenciar'] is True and res_es['motivo_silencio'] == 5 and 6 in res_es['alertas'])
    
    # f) regex invalida
    try:
        compilar({"id": 7, "tipo": "alerta", "campo": "titulo", "operador": "regex", "patron": "[a-"})
        test("Regex invalida lanza ValueError", False)
    except ValueError as e:
        test("Regex invalida lanza ValueError", "mal formada" in str(e).lower() or "desconocido" not in str(e).lower())
        
    # g) Regla con activa=False
    r7 = compilar({"id": 7, "tipo": "alerta", "campo": "titulo", "operador": "contiene", "patron": "test", "activa": False})
    res7 = evaluar({"title": "test"}, [r7])
    test("Regla inactiva", 7 not in res7['alertas'])
    
    # h) Rendimiento
    arts_synth = [{"id": i, "title": f"Articulo de prueba {i} con IA y más cosas", "summary": "Resumen largo "*10, "author": "Juan Perez", "url": "https://www.ejemplo.com/path", "folder_id": 1, "feed_id": 1} for i in range(5000)]
    reglas_synth = [{"id": i, "tipo": "alerta" if i%2==0 else "silencio", "campo": "cualquiera", "operador": "contiene", "patron": f"prueba {i}", "activa": True} for i in range(20)]
    t0 = time.perf_counter()
    res_rend = evaluar_lote(arts_synth, reglas_synth)
    t1 = time.perf_counter()
    test(f"Rendimiento ({t1-t0:.2f}s)", (t1-t0) < 2.0)
    
    print(f"{ok}/{total} OK")
    if ok < total:
        sys.exit(1)
