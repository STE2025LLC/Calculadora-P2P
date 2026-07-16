#!/usr/bin/env python3
"""
Chequea el tipo de cambio oficial (BCB, vía bo.dolarapi.com) y el paralelo
(dolarparalelobolivia.net) y avisa por Telegram si:
  - alguno cambió respecto a la última vez que se revisó, o
  - es la hora del resumen diario (una vez al día).

Guarda el último estado conocido en state/rates_state.json, que este mismo
script actualiza y que el workflow de GitHub Actions vuelve a commitear al
repo para recordar entre ejecuciones.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

import requests

STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "state", "rates_state.json")

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Hora local de Bolivia (UTC-4) a la que se manda el resumen diario, aunque no haya cambios.
DAILY_SUMMARY_HOUR_BOLIVIA = 8

BOLIVIA_TZ = timezone(timedelta(hours=-4))


def fetch_oficial():
    """Tipo de cambio oficial, tomado del BCB vía bo.dolarapi.com (con fallback a bcb.gob.bo)."""
    try:
        r = requests.get("https://bo.dolarapi.com/v1/dolares/oficial", timeout=15)
        r.raise_for_status()
        data = r.json()
        val = float(data.get("venta") or data.get("compra"))
        if 3 < val < 30:
            return val, "bo.dolarapi.com (dato oficial BCB)"
    except Exception:
        pass

    # Fallback: intenta leer la portada del BCB directamente
    try:
        r = requests.get("https://www.bcb.gob.bo/", timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        html = r.text
        block_match = re.search(r"Tipo de cambio oficial[\s\S]{0,600}", html, re.IGNORECASE)
        scope = block_match.group(0) if block_match else html
        m = re.search(r"(\d{1,2}[.,]\d{2})", scope)
        if m:
            val = float(m.group(1).replace(",", "."))
            if 3 < val < 30:
                return val, "bcb.gob.bo"
    except Exception:
        pass

    return None, None


def fetch_paralelo():
    """Tipo de cambio paralelo, leído de dolarparalelobolivia.net (sin API pública oficial)."""
    sources = [
        ("https://dolarparalelobolivia.net/", [
            r"cotiza\s*(?:hoy)?\s*a\s*Bs\.?\s*([\d]+[.,]\d{1,2})",
            r"paralelo[^0-9]{0,40}Bs\.?\s*([\d]+[.,]\d{1,2})",
        ]),
        ("https://www.dolarbluebolivia.click/", [
            r"venta[^0-9]{0,20}Bs\.?\s*([\d]+[.,]\d{1,2})",
            r"paralelo[^0-9]{0,40}Bs\.?\s*([\d]+[.,]\d{1,2})",
        ]),
    ]
    for url, patterns in sources:
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            html = r.text
            for pattern in patterns:
                m = re.search(pattern, html, re.IGNORECASE)
                if m:
                    val = float(m.group(1).replace(",", "."))
                    if 5 < val < 30:
                        return val, url
        except Exception:
            continue
    return None, None


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"oficial": None, "paralelo": None, "last_daily_summary_date": None}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }, timeout=15)
    resp.raise_for_status()


def fmt(v):
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def main():
    state = load_state()

    oficial, oficial_src = fetch_oficial()
    paralelo, paralelo_src = fetch_paralelo()

    now_bo = datetime.now(BOLIVIA_TZ)
    today_str = now_bo.strftime("%Y-%m-%d")

    messages = []

    # --- Modo de prueba: manda un mensaje siempre, sin esperar un cambio real ---
    if os.environ.get("TEST_MODE") == "true":
        if oficial is not None and paralelo is not None:
            diff = paralelo - oficial
            better = "Paralelo" if diff > 0 else ("Oficial" if diff < 0 else "Igual")
            messages.append(
                f"🧪 <b>Mensaje de prueba</b>\n"
                f"Oficial: <b>{fmt(oficial)}</b> BOB/USD ({oficial_src})\n"
                f"Paralelo: <b>{fmt(paralelo)}</b> BOB/USD ({paralelo_src})\n"
                f"Te conviene: <b>{better}</b>\n\n"
                f"Si ves esto, el bot está funcionando correctamente. ✅"
            )
        else:
            messages.append("🧪 Prueba: no se pudo leer alguna de las dos cotizaciones ahora mismo.")

    # --- Detección de cambios ---
    if oficial is not None and state.get("oficial") is not None and oficial != state["oficial"]:
        direction = "subió" if oficial > state["oficial"] else "bajó"
        messages.append(
            f"🟢 <b>Oficial {direction}</b>\n"
            f"{fmt(state['oficial'])} → <b>{fmt(oficial)}</b> BOB/USD\n"
            f"Fuente: {oficial_src}"
        )

    if paralelo is not None and state.get("paralelo") is not None and paralelo != state["paralelo"]:
        direction = "subió" if paralelo > state["paralelo"] else "bajó"
        messages.append(
            f"🔵 <b>Paralelo {direction}</b>\n"
            f"{fmt(state['paralelo'])} → <b>{fmt(paralelo)}</b> BOB/USD\n"
            f"Fuente: {paralelo_src}"
        )

    # --- Resumen diario ---
    is_summary_time = now_bo.hour == DAILY_SUMMARY_HOUR_BOLIVIA
    already_sent_today = state.get("last_daily_summary_date") == today_str

    if is_summary_time and not already_sent_today and oficial is not None and paralelo is not None:
        diff = paralelo - oficial
        better = "Paralelo" if diff > 0 else ("Oficial" if diff < 0 else "Igual")
        messages.append(
            f"📅 <b>Resumen diario · {now_bo.strftime('%d/%m/%Y')}</b>\n"
            f"Oficial: <b>{fmt(oficial)}</b> BOB/USD\n"
            f"Paralelo: <b>{fmt(paralelo)}</b> BOB/USD\n"
            f"Te conviene: <b>{better}</b> ({fmt(abs(diff))} BOB de diferencia por dólar)"
        )
        state["last_daily_summary_date"] = today_str

    for msg in messages:
        send_telegram(msg)
        print("Enviado:", msg.splitlines()[0])

    if not messages:
        print("Sin cambios ni resumen pendiente. Oficial:", oficial, "Paralelo:", paralelo)

    if oficial is not None:
        state["oficial"] = oficial
    if paralelo is not None:
        state["paralelo"] = paralelo

    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
