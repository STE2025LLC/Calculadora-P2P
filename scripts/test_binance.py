import os
import requests

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# --- FILTROS: ajustá estos valores a tu gusto ---
BANCO_REQUERIDO = "Banco Ganadero"   # None para no filtrar por banco
MIN_USDT_DISPONIBLE = 1000           # volumen disponible en el anuncio
# (sin filtro de cantidad de órdenes/mes)

url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
payload = {
    "asset": "USDT",
    "fiat": "BOB",
    "tradeType": "SELL",
    "page": 1,
    "rows": 20,
    "payTypes": [],
    "publisherType": None,
}
headers = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


def fmt(v):
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def send_telegram(text):
    tg_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(tg_url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }, timeout=15)
    resp.raise_for_status()


def pasa_filtros(item):
    adv = item["adv"]

    disponible = float(adv.get("surplusAmount", 0))
    if disponible < MIN_USDT_DISPONIBLE:
        return False

    if BANCO_REQUERIDO:
        encontrado = False
        for m in adv.get("tradeMethods", []):
            # Revisamos ambos campos posibles: el nombre visible del método
            # de pago (tradeMethodName, ej. "Bank Transfer") y el banco
            # específico si el anunciante lo detalló (payBank). Cuál de
            # los dos trae el dato varía según el anuncio, así que
            # aceptamos que aparezca en cualquiera de los dos.
            nombre_metodo = (m.get("tradeMethodName") or "")
            banco_especifico = (m.get("payBank") or "")
            texto = (nombre_metodo + " " + banco_especifico).lower()
            if BANCO_REQUERIDO.lower() in texto:
                encontrado = True
                break
        if not encontrado:
            return False

    return True


def main():
    r = requests.post(url, json=payload, headers=headers, timeout=15)
    data = r.json()

    if not data.get("success"):
        send_telegram(f"🧪 Error consultando Binance P2P:\n{data}")
        return

    # DEBUG: esto queda solo en los logs de Actions, no se manda a Telegram.
    print("Ejemplo del primer anuncio crudo:")
    print(data["data"][0] if data["data"] else "sin anuncios")

    coinciden = [item for item in data["data"] if pasa_filtros(item)]

    if not coinciden:
        send_telegram(
            "🧪 Ningún anuncio cumple los filtros ahora mismo "
            f"(banco={BANCO_REQUERIDO}, min USDT disp.={MIN_USDT_DISPONIBLE})."
        )
        return

    lines = [f"🧪 <b>Binance P2P filtrado</b> ({len(coinciden)} anuncios)"]
    for item in coinciden:
        adv = item["adv"]
        advertiser = item["advertiser"]
        price = fmt(float(adv["price"]))
        disponible = fmt(float(adv["surplusAmount"]))
        ordenes = advertiser.get("monthOrderCount", "?")
        nombre = advertiser["nickName"]
        lines.append(f"Bs. {price} — {nombre} ({disponible} USDT, {ordenes} órdenes/mes)")

    send_telegram("\n".join(lines))


if __name__ == "__main__":
    main()
