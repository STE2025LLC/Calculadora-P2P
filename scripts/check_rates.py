#!/usr/bin/env python3
"""
Chequea el tipo de cambio oficial (BCB, DIRECTO de bcb.gob.bo, con respaldo
en bo.dolarapi.com) y el paralelo (Binance P2P DIRECTO, filtrado por método
de pago Banco Ganadero, mínimo de USDT disponibles, y límite de transacción
realista, sin pasar por criptoya) y avisa por Telegram si:
  - alguno cambió respecto a la última vez que se revisó, o
  - es la hora del resumen diario (una vez al día), que incluye mínimos y
    máximos de ayer, de esta semana, del mes actual y de los 3 meses
    anteriores (como referencia).

Guarda:
  - state/rates_state.json  -> último valor visto + fecha del último resumen
  - state/history.csv       -> historial de lecturas (fecha/hora, oficial,
                                paralelo), usado para calcular los mínimos y
                                máximos del resumen diario.

Ambos los vuelve a commitear el workflow de GitHub Actions para recordar
entre ejecuciones.
"""

import csv
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

import requests

BASE_DIR = os.path.join(os.path.dirname(__file__), "..")
STATE_PATH = os.path.join(BASE_DIR, "state", "rates_state.json")
HISTORY_PATH = os.path.join(BASE_DIR, "state", "history.csv")

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

DAILY_SUMMARY_HOUR_BOLIVIA = 9
DAILY_SUMMARY_MINUTE_BOLIVIA = 30

HISTORY_KEEP_DAYS = 100

COMPARE_DECIMALS = 2

# --- Filtros para el paralelo (Binance P2P directo) ---
BANCO_REQUERIDO = "Banco Ganadero"
MIN_USDT_DISPONIBLE = 1000
MIN_MAX_TRANS_BOB = 1000
# (sin filtro de cantidad de órdenes/mes, a propósito)

BOLIVIA_TZ = timezone(timedelta(hours=-4))


# --------------------------------------------------------------------------
# Lectura de cotizaciones
# --------------------------------------------------------------------------

def fetch_oficial():
    """Tipo de cambio oficial, tomado DIRECTO de bcb.gob.bo (fuente original,
    la más actualizada), con fallback a bo.dolarapi.com si el scraping falla.

    Nota: bo.dolarapi.com refleja el mismo dato del BCB pero con retraso de
    hasta varios días en algunas ocasiones, por eso ya no es la fuente
    principal -- se dejó como respaldo por si el scraping directo del BCB
    falla (por ejemplo si cambian el HTML de su portada).
    """
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

    try:
        r = requests.get("https://bo.dolarapi.com/v1/dolares/oficial", timeout=15)
        r.raise_for_status()
        data = r.json()
        val = float(data.get("venta") or data.get("compra"))
        if 3 < val < 30:
            return val, "bo.dolarapi.com (respaldo, puede tener retraso)"
    except Exception:
        pass

    return None, None


def _anuncio_cumple_filtros(adv):
    """Aplica los filtros de banco, USDT disponible, y límite de transacción
    realista a un anuncio de Binance P2P."""
    disponible = float(adv.get("surplusAmount", 0) or 0)
    if disponible < MIN_USDT_DISPONIBLE:
        return False

    max_trans = float(adv.get("maxSingleTransAmount", 0) or 0)
    if max_trans < MIN_MAX_TRANS_BOB:
        return False

    if BANCO_REQUERIDO:
        encontrado = False
        for m in adv.get("tradeMethods", []):
            nombre_metodo = (m.get("tradeMethodName") or "")
            banco_especifico = (m.get("payBank") or "")
            texto = (nombre_metodo + " " + banco_especifico).lower()
            if BANCO_REQUERIDO.lower() in texto:
                encontrado = True
                break
        if not encontrado:
            return False

    return True


def fetch_paralelo_binance_directo():
    """Tipo de cambio paralelo, leído DIRECTO del endpoint (no oficial, no
    documentado por Binance) que usa la propia web p2p.binance.com.

    Se piden anuncios de venta de USDT/BOB, se filtran por los criterios de
    arriba (banco + mínimo disponible + límite de transacción realista), y
    se toma el MEJOR precio (el más alto) entre los que cumplen -- porque
    estás vendiendo USDT y recibiendo BOB, así que precio más alto = más
    bolivianos por tus dólares.

    Devuelve (None, None) si la API falla o si ningún anuncio cumple los
    filtros (en ese caso, quien llama a esta función debe recurrir al
    respaldo).
    """
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

    r = requests.post(url, json=payload, headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()

    if not data.get("success"):
        return None, None

    candidatos = []
    for item in data.get("data", []):
        adv = item.get("adv", {})
        advertiser = item.get("advertiser", {})

        if not _anuncio_cumple_filtros(adv):
            continue

        try:
            candidatos.append((float(adv["price"]), adv, advertiser))
        except (TypeError, ValueError, KeyError):
            continue

    if not candidatos:
        return None, None

    mejor_precio, mejor_adv, mejor_advertiser = max(candidatos, key=lambda c: c[0])
    if 5 < mejor_precio < 30:
        return mejor_precio, "Binance P2P directo"

    return None, None


def fetch_paralelo():
    """Tipo de cambio paralelo.

    Fuente principal: Binance P2P DIRECTO (sin pasar por criptoya), aplicando
    tus filtros de banco, volumen mínimo y límite de transacción realista --
    ver fetch_paralelo_binance_directo().

    Respaldo 1: si esa consulta falla o ningún anuncio cumple los filtros, se
    usa el mismo dato de Binance P2P pero vía criptoya.com (sin tus filtros
    específicos, solo el "bid" general con 500 USDT de referencia).

    Respaldo 2: si también falla, se intenta leer (mejor esfuerzo) el HTML de
    dolarparalelobolivia.net y dolarbluebolivia.click.
    """
    try:
        val, src = fetch_paralelo_binance_directo()
        if val is not None:
            return val, src
    except Exception:
        pass

    try:
        r = requests.get("https://criptoya.com/api/binancep2p/usdt/bob/500", timeout=15)
        r.raise_for_status()
        data = r.json()
        val = float(data["bid"])
        if 5 < val < 30:
            return val, "criptoya.com/bo (Binance P2P, respaldo sin filtros)"
    except Exception:
        pass

    sources = [
        ("https://dolarparalelobolivia.net/", [
            r"cotiza\s*a\s*Bs\.?\s*([\d]+[.,]\d{1,2})\s*hoy",
            r"Bs\s*([\d]+[.,]\d{1,2})[\s\S]{0,30}Dolar paralelo",
            r"paralelo[\s\S]{0,100}?Bs\.?\s*([\d]+[.,]\d{1,2})",
        ]),
        ("https://www.dolarbluebolivia.click/", [
            r"venta[^0-9]{0,20}Bs\.?\s*([\d]+[.,]\d{1,2})",
            r"paralelo[\s\S]{0,100}?Bs\.?\s*([\d]+[.,]\d{1,2})",
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


# --------------------------------------------------------------------------
# Estado (último valor visto)
# --------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"oficial": None, "paralelo": None, "last_daily_summary_date": None}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------
# Historial (para mínimos/máximos)
# --------------------------------------------------------------------------

def load_history():
    rows = []
    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    dt = datetime.fromisoformat(row["timestamp"])
                    oficial = float(row["oficial"]) if row["oficial"] else None
                    paralelo = float(row["paralelo"]) if row["paralelo"] else None
                    rows.append({"dt": dt, "oficial": oficial, "paralelo": paralelo})
                except Exception:
                    continue
    return rows


def save_history(rows):
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    with open(HISTORY_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "oficial", "paralelo"])
        for row in rows:
            writer.writerow([
                row["dt"].isoformat(),
                row["oficial"] if row["oficial"] is not None else "",
                row["paralelo"] if row["paralelo"] is not None else "",
            ])


def prune_history(rows, now, keep_days=HISTORY_KEEP_DAYS):
    cutoff = now - timedelta(days=keep_days)
    return [r for r in rows if r["dt"] >= cutoff]


def minmax_in_range(rows, start, end, field):
    values = [r[field] for r in rows if r[field] is not None and start <= r["dt"] < end]
    if not values:
        return None
    return min(values), max(values)


def subtract_months(dt, months):
    """Resta `months` meses a una fecha, manejando el cambio de año."""
    month = dt.month - 1 - months
    year = dt.year + month // 12
    month = month % 12 + 1
    return dt.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

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


def fmt_range(minmax):
    if minmax is None:
        return "sin datos"
    lo, hi = minmax
    if lo == hi:
        return f"{fmt(lo)}"
    return f"{fmt(lo)} – {fmt(hi)}"


# --------------------------------------------------------------------------
# Resumen diario con mínimos/máximos
# --------------------------------------------------------------------------

def build_daily_summary(oficial, paralelo, oficial_src, paralelo_src, now_bo, history):
    today_start = now_bo.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)

    week_start = today_start - timedelta(days=today_start.weekday())  # lunes de esta semana

    month_start = today_start.replace(day=1)
    three_months_ago_start = subtract_months(month_start, 3)

    ranges = {
        "Ayer": (yesterday_start, today_start),
        "Esta semana": (week_start, now_bo),
        "Este mes": (month_start, now_bo),
        "3 meses anteriores": (three_months_ago_start, month_start),
    }

    diff = paralelo - oficial
    better = "Paralelo" if diff > 0 else ("Oficial" if diff < 0 else "Igual")

    lines = [
        f"📅 <b>Resumen diario · {now_bo.strftime('%d/%m/%Y')}</b>",
        f"Oficial ahora: <b>{fmt(oficial)}</b> BOB/USD",
        f"Paralelo ahora: <b>{fmt(paralelo)}</b> BOB/USD",
        f"Te conviene: <b>{better}</b> ({fmt(abs(diff))} BOB de diferencia)",
        "",
        "<b>Mín–máx históricos (Oficial / Paralelo):</b>",
    ]

    for label, (start, end) in ranges.items():
        of_range = fmt_range(minmax_in_range(history, start, end, "oficial"))
        pa_range = fmt_range(minmax_in_range(history, start, end, "paralelo"))
        lines.append(f"• {label}: {of_range}  /  {pa_range}")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    state = load_state()
    history = load_history()

    oficial, oficial_src = fetch_oficial()
    paralelo, paralelo_src = fetch_paralelo()

    now_bo = datetime.now(BOLIVIA_TZ)
    today_str = now_bo.strftime("%Y-%m-%d")

    if oficial is not None or paralelo is not None:
        history.append({"dt": now_bo, "oficial": oficial, "paralelo": paralelo})
    history = prune_history(history, now_bo)

    messages = []

    if os.environ.get("TEST_MODE") == "true":
        if oficial is not None and paralelo is not None:
            messages.append(
                "🧪 <b>Mensaje de prueba</b>\n" +
                build_daily_summary(oficial, paralelo, oficial_src, paralelo_src, now_bo, history) +
                "\n\nSi ves esto, el bot está funcionando correctamente. ✅"
            )
        else:
            messages.append("🧪 Prueba: no se pudo leer alguna de las dos cotizaciones ahora mismo.")

    if (
        oficial is not None
        and state.get("oficial") is not None
        and round(oficial, COMPARE_DECIMALS) != round(state["oficial"], COMPARE_DECIMALS)
    ):
        direction = "subió" if oficial > state["oficial"] else "bajó"
        messages.append(
            f"🟢 <b>Oficial {direction}</b>\n"
            f"{fmt(state['oficial'])} → <b>{fmt(oficial)}</b> BOB/USD\n"
            f"Fuente: {oficial_src}"
        )

    if (
        paralelo is not None
        and state.get("paralelo") is not None
        and round(paralelo, COMPARE_DECIMALS) != round(state["paralelo"], COMPARE_DECIMALS)
    ):
        direction = "subió" if paralelo > state["paralelo"] else "bajó"
        messages.append(
            f"🔵 <b>Paralelo {direction}</b>\n"
            f"{fmt(state['paralelo'])} → <b>{fmt(paralelo)}</b> BOB/USD\n"
            f"Fuente: {paralelo_src}"
        )

    target_minutes = DAILY_SUMMARY_HOUR_BOLIVIA * 60 + DAILY_SUMMARY_MINUTE_BOLIVIA
    now_minutes = now_bo.hour * 60 + now_bo.minute
    is_summary_time = now_minutes >= target_minutes
    already_sent_today = state.get("last_daily_summary_date") == today_str

    if is_summary_time and not already_sent_today and oficial is not None and paralelo is not None:
        messages.append(build_daily_summary(oficial, paralelo, oficial_src, paralelo_src, now_bo, history))
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
    save_history(history)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
