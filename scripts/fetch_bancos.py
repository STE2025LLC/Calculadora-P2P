#!/usr/bin/env python3
"""Precio de compra y venta del dólar por banco -> state/bancos_state.json

Fuentes (se combinan; si una falla, las otras siguen):
  1. api.factura.bo/BankExchangeRate  -> compra/venta publicada por banco
  2. bancounion.com.bo (portada)      -> compra/venta de Banco Unión
  3. bcb.gob.bo (tabla del TCO)       -> compra realmente operada ayer, por banco
"""
import json, re, sys, urllib.request
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "state" / "bancos_state.json"
UA = {"User-Agent": "Mozilla/5.0 (Calculadora-P2P bot)"}

# nombre BCB -> nombre corto
BCB_NAMES = {
    "BANCO NACIONAL DE BOLIVIA": "BNB", "BANCO BISA": "BISA",
    "BANCO MERCANTIL SANTA CRUZ": "Mercantil SC", "BANCO ECONOMICO": "Económico",
    "BANCO FIE": "FIE", "BANCO FORTALEZA": "Fortaleza", "BANCO SOLIDARIO": "BancoSol",
    "BANCO GANADERO": "Ganadero", "BANCO UNION": "Unión", "BANCO DE CREDITO": "BCP",
}
ORDER = ["Unión", "BCP", "BISA", "BNB", "BancoSol", "Mercantil SC", "Ganadero",
         "Económico", "FIE", "Fortaleza"]


def get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")


def fix_name(n):
    # corrige "EconÃ³mico" -> "Económico"
    if "Ã" in n:
        try:
            n = n.encode("latin-1").decode("utf-8")
        except Exception:
            pass
    return n.strip()


def num(txt):
    txt = txt.strip()
    if "," in txt:
        return float(txt.replace(".", "").replace(",", "."))
    return float(txt.replace(".", "")) if txt.count(".") > 1 else float(txt)


def from_factura():
    data = json.loads(get("https://api.factura.bo/BankExchangeRate"))
    out, p2p = {}, None
    for b in data["datos"]["bancos"]:
        name = fix_name(b["banco"])
        compra = b.get("compra") or None   # 0 = sin dato
        venta = b.get("venta") or None
        if name == "Binance P2P":
            p2p = {"compra": compra, "venta": venta, "actualizado": b.get("fecha_actualizacion")}
            continue
        out[name] = {"compra": compra, "venta": venta,
                     "fuente": "factura.bo", "actualizado": b.get("fecha_actualizacion")}
    return out, p2p


def from_union():
    html = get("https://bancounion.com.bo/")
    m = re.search(r"Compra\s*BOB:\s*([\d.,]+)\s*/\s*Venta\s*([\d.,]+)", html)
    if not m:
        raise ValueError("no se encontró Compra/Venta en la portada de Unión")
    return {"compra": num(m.group(1)), "venta": num(m.group(2)),
            "fuente": "bancounion.com.bo",
            "actualizado": datetime.now(timezone.utc).isoformat()}


def from_bcb():
    html = get("https://www.bcb.gob.bo/bcb_tco_publico_ultima_cotizacion.php")
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "|", html))
    rows = {}
    for m in re.finditer(
        r"\|\s*(BANCO[^|]{2,60}?)\s*\|+\s*(\d{1,2},\d{2})\s*\|+\s*([\d.]+)\s*\|+\s*([\d.]+)\s*\|", text):
        short = BCB_NAMES.get(m.group(1).strip())
        if short:
            rows[short] = {"bcb_compra": num(m.group(2)), "bcb_monto_usd": num(m.group(3))}
    corte = re.search(r"FECHA DE CORTE:\s*([^|]+?)\s*VIGENCIA", text)
    return rows, (corte.group(1).strip().title() if corte else None)


def main():
    bancos, p2p, errores, corte = {}, None, [], None

    try:
        bancos, p2p = from_factura()
    except Exception as e:
        errores.append(f"factura.bo: {e}")
    try:
        u = from_union()
        bancos["Unión"] = {**bancos.get("Unión", {}), **u}   # la web del banco manda
    except Exception as e:
        errores.append(f"Unión: {e}")
    try:
        bcb, corte = from_bcb()
        for k, v in bcb.items():
            bancos.setdefault(k, {"compra": None, "venta": None, "fuente": None, "actualizado": None})
            bancos[k].update(v)
    except Exception as e:
        errores.append(f"BCB: {e}")

    if not bancos:
        sys.exit("Ninguna fuente respondió: " + " | ".join(errores))

    lista = []
    for name in sorted(bancos, key=lambda n: ORDER.index(n) if n in ORDER else 99):
        lista.append({"banco": name, **bancos[name]})

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "bcb_fecha_corte": corte,
        "p2p": p2p,
        "bancos": lista,
        "errores": errores,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK: {len(lista)} bancos. Avisos: {errores or 'ninguno'}")


if __name__ == "__main__":
    main()
