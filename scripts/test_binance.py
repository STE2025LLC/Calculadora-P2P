import requests
import json

url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
payload = {
    "asset": "USDT",
    "fiat": "BOB",
    "tradeType": "SELL",
    "page": 1,
    "rows": 20,
    "payTypes": [],
    "publisherType": None
}
headers = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

r = requests.post(url, json=payload, headers=headers, timeout=15)
data = r.json()

if not data.get("success"):
    print("Error:", data)
else:
    for item in data["data"]:
        adv = item["adv"]
        advertiser = item["advertiser"]
        print(
            f"{advertiser['nickName']:20s} | precio: {adv['price']:>8} | "
            f"disponible: {adv['surplusAmount']:>12} USDT | "
            f"min-max: {adv['minSingleTransAmount']}-{adv['maxSingleTransAmount']} BOB | "
            f"promocionado: {adv.get('tradeMethods') and item.get('advertiseRole') == 'MERCHANT'}"
        )
