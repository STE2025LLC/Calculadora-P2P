#!/usr/bin/env python3
"""Compra y venta del dólar que PUBLICA cada banco -> state/bancos_state.json

Orden de fuentes por banco:
  1) la web / API del propio banco (lectura directa)
  2) factura.bo (respaldo, solo si la lectura directa falla)
  3) el último dato guardado, marcado como "desactualizado"
Además agrega, por banco, a cuánto operó realmente ayer según el BCB.
"""
import json
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

OUT = Path(__file__).resolve().parent.parent / "state" / "bancos_state.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-BO,es;q=0.9",
    "Cache-Control": "no-cache",
}
TIMEOUT = (10, 25)
NUM = r"(\d{1,2}[.,]\d{1,3})"


# ----------------------------------------------------------------- red
class AdaptadorBCP(HTTPAdapter):
    """El servidor del BCP exige un cifrado que Python excluye por defecto."""
    def init_poolmanager(self, *a, **k):
        ctx = create_urllib3_context()
        cifrados = [c["name"] for c in ctx.get_ciphers() if c["protocol"] != "TLSv1.3"]
        ctx.set_ciphers(":".join(cifrados + ["AES256-GCM-SHA384"]))
        k["ssl_context"] = ctx
        return super().init_poolmanager(*a, **k)


def pedir(url, metodo="GET", extra=None, reintentos=2):
    for i in range(reintentos + 1):
        try:
            with requests.Session() as s:
                if "bcp.com.bo" in url:
                    s.mount("https://www.bcp.com.bo/", AdaptadorBCP())
                r = s.request(metodo, url, headers={**HEADERS, **(extra or {})}, timeout=TIMEOUT)
            r.raise_for_status()
            return r
        except requests.RequestException:
            if i == reintentos:
                raise
            time.sleep(2 ** i)


def sopa(url):
    return BeautifulSoup(pedir(url).text, "html.parser")


def json_de(url, **kw):
    return pedir(url, **kw).json()


# ------------------------------------------------------------- parseo
def txt(el):
    return " ".join(el.get_text(" ", strip=True).split()) if el else ""


def plano(s):
    """minúsculas y sin acentos"""
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn").lower()


def a_float(s):
    return float(s.replace(",", "."))


def tras_etiqueta(texto, etiqueta):
    m = re.search(etiqueta + r"\s*[:\-|]?\s*(?:BOB|Bs\.?)?\s*" + NUM, texto, re.I)
    if not m:
        raise ValueError(f"no encontré «{etiqueta}»")
    return a_float(m.group(1))


def par(texto, compra=r"compra", venta=r"venta"):
    return {"compra": tras_etiqueta(texto, compra), "venta": tras_etiqueta(texto, venta)}


# -------------------------------------------------- un lector por banco
def bisa():
    return par(txt(sopa("https://www.bisa.com/").select_one("section.marquee-container p.marquee-text")),
               r"d[oó]lar\s+compra", r"d[oó]lar\s+venta")


def bcp():
    spans = sopa("https://www.bcp.com.bo/").select(".marquee-content span")
    return par(" | ".join(txt(s) for s in spans))


def nacion_argentina():
    base = "https://www.bna.com.bo/"
    s = sopa(base)
    c, v = s.select_one('span[x-text="cotizacion.compra"]'), s.select_one('span[x-text="cotizacion.venta"]')
    if re.search(r"\d", txt(c)) and re.search(r"\d", txt(v)):
        return {"compra": a_float(re.search(NUM, txt(c)).group(1)), "venta": a_float(re.search(NUM, txt(v)).group(1))}
    datos = json_de(base + "Home/CargarCotizaciones")
    d = next(x for x in datos if str(x.get("moneda", "")).upper() == "USD")
    return {"compra": float(d["compra"]), "venta": float(d["venta"])}


def bnb():
    spans = sopa("https://www.bnb.com.bo/PortalBNB/Principal/BancaPersonas").select("span")
    val = {}
    for i, sp in enumerate(spans[:-1]):
        et = plano(txt(sp))
        if et in ("dolar compra", "dolar venta"):
            val[et] = a_float(re.search(NUM, txt(spans[i + 1])).group(1))
    return {"compra": val["dolar compra"], "venta": val["dolar venta"]}


def economico():
    base = "https://www.baneco.com.bo/"
    contenido = txt(sopa(base).select_one("#cotizacion"))
    if not contenido:
        contenido = json_de(base + "gbGLOBALTiposDeCambio")["gbGLOBALTiposDeCambioResult"]
    return par(contenido)


def fortaleza():
    base = "https://www.bancofortaleza.com.bo/"
    s = sopa(base)
    c, v = s.select_one('span[data-exchange="buyExchange"]'), s.select_one('span[data-exchange="saleExchange"]')
    if re.search(r"\d", txt(c)) and re.search(r"\d", txt(v)):
        return {"compra": a_float(re.search(NUM, txt(c)).group(1)), "venta": a_float(re.search(NUM, txt(v)).group(1))}
    d = json_de(base + "proxy-exchange.php")["response"]
    return {"compra": float(d["buyExchange"]), "venta": float(d["saleExchange"])}


def ganadero():
    """Su portada no tiene «compra» como tal: publica «T. Cambio Oficial» y
    «Valor Ref. Venta USD». Se usan esos dos y se marca la nota.
    Tres intentos, del más preciso al más tolerante."""
    s = sopa("https://www.bg.com.bo/")
    por_etiqueta = {}

    def numero_en(t):
        m = re.search(r"(?<!\d)(\d{1,2}[.,]\d{1,5})(?!\d)", t.replace("\xa0", " "))
        return a_float(m.group(1)) if m else None

    # valores dentro de #indicadores; la etiqueta está en el bloque más cercano
    # que tenga letras y un solo número
    for valor in s.select("#indicadores div"):
        n = numero_en(txt(valor))
        if n is None or len(re.findall(NUM, txt(valor))) != 1:
            continue
        for anc in valor.parents:
            t = txt(anc)
            if re.search(r"[a-zA-Z]{3}", t) and len(re.findall(NUM, t)) == 1:
                por_etiqueta[plano(t)] = n
                break
    try:
        compra = next(v for k, v in por_etiqueta.items() if "cambio oficial" in k)
        venta = next(v for k, v in por_etiqueta.items() if "ref. venta" in k or "ref venta" in k)
    except StopIteration:
        # 3) texto plano de toda la portada
        t = plano(txt(s))
        m1 = re.search(r"cambio oficial\D{0,15}?" + NUM, t)
        m2 = re.search(r"ref\.? venta[^0-9]{0,25}?" + NUM, t)
        if not (m1 and m2):
            vistos = list(por_etiqueta)[:4]
            i = t.find("cambio oficial")
            raise ValueError(f"no encontré «T. Cambio Oficial» / «Valor Ref. Venta». "
                             f"Etiquetas vistas: {vistos}. Texto cerca: {t[max(0, i-60):i+120] if i >= 0 else 'sin «cambio oficial» en el HTML (¿se carga con JavaScript?)'}")
        compra, venta = a_float(m1.group(1)), a_float(m2.group(1))
    return {"compra": compra, "venta": venta,
            "nota": "Ganadero publica «T. Cambio Oficial» y «Valor Ref. Venta»; no es una compra/venta propia confirmada."}


def mercantil():
    d = json_de("https://backportal.bmsc.com.bo:1443/api/bmscservices/tipotre")
    return {"compra": float(d["compra"]), "venta": float(d["venta"])}


def bancosol():
    return par(txt(sopa("https://www.bancosol.com.bo/").select_one(".indicador-grupo .valores-grupo .highlight-field")))


def union():
    s = sopa("https://www.bancounion.com.bo/")
    for p in s.select(".opacity.mb-3-tasas p.card-text"):
        if "dolar" in plano(txt(p)):
            return par(txt(p), r"compra\s+BOB", r"venta")
    return par(txt(s), r"compra\s+BOB", r"venta")      # respaldo: texto de toda la portada


def fie():
    doc = json_de("https://www.bancofie.com.bo/api/tcl", metodo="POST",
                  extra={"Content-Type": "application/json", "Referer": "https://www.bancofie.com.bo/"})["resultado"]["documento"]
    return par(doc, r"d[oó]lar\s+compra", r"d[oó]lar\s+venta")


def pyme_comunidad():
    filas = {plano(txt(f.select_one("th"))): txt(f.select_one("td"))
             for f in sopa("https://www.bco.com.bo/").select(".csc-tc__tabla tr") if f.select_one("th") and f.select_one("td")}
    return {"compra": a_float(re.search(NUM, filas["compra"]).group(1)),
            "venta": a_float(re.search(NUM, filas["venta"]).group(1))}


BANCOS = {  # nombre en pantalla -> (lector, sitio)
    "Unión": (union, "bancounion.com.bo"), "BCP": (bcp, "bcp.com.bo"), "BISA": (bisa, "bisa.com"),
    "BNB": (bnb, "bnb.com.bo"), "BancoSol": (bancosol, "bancosol.com.bo"),
    "Mercantil SC": (mercantil, "bmsc.com.bo"), "Ganadero": (ganadero, "bg.com.bo"),
    "Económico": (economico, "baneco.com.bo"), "FIE": (fie, "bancofie.com.bo"),
    "Fortaleza": (fortaleza, "bancofortaleza.com.bo"), "Pyme de la Comunidad": (pyme_comunidad, "bco.com.bo"),
    "Nación Argentina": (nacion_argentina, "bna.com.bo"),
}   # Prodem queda fuera: su web no responde desde GitHub Actions

BCB_NOMBRES = {
    "banco nacional de bolivia": "BNB", "banco bisa": "BISA", "banco mercantil santa cruz": "Mercantil SC",
    "banco economico": "Económico", "banco fie": "FIE", "banco fortaleza": "Fortaleza",
    "banco solidario": "BancoSol", "banco ganadero": "Ganadero", "banco union": "Unión",
    "banco de credito": "BCP", "banco pyme de la comunidad": "Pyme de la Comunidad",
    "banco de la nacion argentina": "Nación Argentina",
}


def validar(d):
    c, v = d["compra"], d["venta"]
    if not (5 <= c <= 30 and 5 <= v <= 30):
        raise ValueError(f"valores fuera de rango: {c} / {v}")
    if c > v:
        raise ValueError(f"compra mayor que venta: {c} / {v}")
    return d


# ------------------------------------------------------------ respaldos
def factura_bo():
    """Respaldo y referencia Binance P2P. Si falla, no pasa nada."""
    try:
        datos = json_de("https://api.factura.bo/BankExchangeRate")["datos"]["bancos"]
    except Exception:
        return {}, None
    bancos, p2p = {}, None
    for b in datos:
        n = b["banco"]
        if "Ã" in n:
            try:
                n = n.encode("latin-1").decode("utf-8")
            except Exception:
                pass
        if n == "Binance P2P":
            p2p = {"compra": b.get("compra") or None, "venta": b.get("venta") or None}
        else:
            bancos[n.strip()] = {"compra": b.get("compra") or None, "venta": b.get("venta") or None}
    return bancos, p2p


def bcb_operado():
    try:
        html = pedir("https://www.bcb.gob.bo/bcb_tco_publico_ultima_cotizacion.php").text
    except Exception:
        return {}, None
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "|", html))
    filas = {}
    for m in re.finditer(r"\|\s*(BANCO[^|]{2,60}?)\s*\|+\s*(\d{1,2},\d{2})\s*\|+\s*([\d.]+)\s*\|", t):
        corto = BCB_NOMBRES.get(plano(m.group(1)).strip())
        if corto:
            filas[corto] = {"bcb_compra": a_float(m.group(2)), "bcb_monto_usd": int(m.group(3).replace(".", ""))}
    corte = re.search(r"FECHA DE CORTE:\s*([^|]+?)\s*VIGENCIA", t)
    return filas, (corte.group(1).strip().title() if corte else None)


# ----------------------------------------------------------------- main
def leer(nombre):
    funcion, _ = BANCOS[nombre]
    return nombre, validar(funcion())


def main():
    ahora = datetime.now(timezone.utc).isoformat()
    previo = {}
    if OUT.exists():
        try:
            previo = {b["banco"]: b for b in json.loads(OUT.read_text(encoding="utf-8")).get("bancos", [])}
        except Exception:
            pass

    directos, errores = {}, []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futuros = {n: ex.submit(leer, n) for n in BANCOS}
        for n, f in futuros.items():
            try:
                directos[n] = f.result()[1]
                print(f"OK  {n}: {directos[n]}")
            except Exception as e:
                errores.append(f"{n}: {type(e).__name__}: {e}")
                print(f"ERR {n}: {e}")

    respaldo, p2p = factura_bo()
    bcb, corte = bcb_operado()

    lista = []
    for nombre, (_, sitio) in BANCOS.items():
        if nombre in directos:
            d = directos[nombre]
            reg = {"banco": nombre, "compra": d["compra"], "venta": d["venta"],
                   "fuente": sitio, "actualizado": ahora}
            if d.get("nota"):
                reg["nota"] = d["nota"]
        elif nombre in respaldo and (respaldo[nombre]["compra"] or respaldo[nombre]["venta"]):
            reg = {"banco": nombre, **respaldo[nombre], "fuente": "factura.bo (respaldo)", "actualizado": ahora}
        elif nombre in previo:
            reg = {**previo[nombre], "desactualizado": True}
        else:
            continue
        reg.update(bcb.get(nombre, {}))
        lista.append(reg)

    if not any(not b.get("desactualizado") for b in lista):
        sys.exit("Ningún banco respondió: " + " | ".join(errores))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"updated_at": ahora, "bcb_fecha_corte": corte, "p2p": p2p,
                               "bancos": lista, "errores": errores}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Guardados {len(lista)} bancos; lectura directa: {len(directos)}; fallos: {len(errores)}")


if __name__ == "__main__":
    main()
