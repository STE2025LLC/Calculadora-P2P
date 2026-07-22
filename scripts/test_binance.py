import os
import requests

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
payload = {
    "asset": "USDT",
    "fiat": "BOB",
    "tradeType": "SELL",
    "page": 1,
    "rows": 10,
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
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }, timeout=15)
    resp.raise_for_status()


def main():
    r = requests.post(url, json=payload, headers=headers, timeout=15)
    data = r.json()

    if not data.get("success"):
        send_telegram(f"🧪 Error consultando Binance P2P directo:\n{data}")
        return

    lines = ["🧪 <b>Binance P2P directo (USDT/BOB, venta)</b>"]
    for item in data["data"][:10]:
        adv = item["adv"]
        advertiser = item["advertiser"]
        price = fmt(float(adv["price"]))
        disponible = fmt(float(adv["surplusAmount"]))
        nombre = advertiser["nickName"]
        lines.append(f"Bs. {price} — {nombre} ({disponible} USDT disp.)")

    send_telegram("\n".join(lines))


if __name__ == "__main__":
    main()
