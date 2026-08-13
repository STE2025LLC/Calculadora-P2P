#!/usr/bin/env python3
"""
Bot de Telegram independiente para pedir gráficas de BOB/USD (oficial y
paralelo) en cualquier momento, sin esperar al resumen diario.

Este bot NO calcula tipos de cambio ni guarda historial propio: lee el
mismo state/history.csv y state/rates_state.json que ya publica el
workflow de GitHub Actions del repo "Calculadora-P2P" (directo del raw de
GitHub, sin autenticación porque el repo es público). Por eso puede vivir
en un servicio totalmente aparte, sin mezclarse con ningún otro bot.

Comandos:
  /grafica            -> muestra botones (2 sem, 1 mes, 2, 3, 6 meses, 1 año)
                         para elegir qué rango graficar
  /precio             -> último valor oficial/paralelo conocido, en texto
  /start, /help       -> ayuda

Nota: los rangos de 6 meses y 1 año solo van a mostrar datos completos una
vez que el historial (state/history.csv) acumule esa cantidad de tiempo
guardado. Eso depende de HISTORY_KEEP_DAYS en scripts/check_rates.py del
repo principal.

Solo responde al chat_id configurado en TELEGRAM_CHAT_ID (para que nadie
más pueda usar tu bot aunque adivine el username).

Se despliega en Railway (o cualquier host que corra un proceso Python
persistente). Usa long polling, así que no necesita dominio ni webhook.
"""

import io
import os
import re
import sys
import time
import csv
import json
from datetime import datetime, timedelta, timezone

import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# --------------------------------------------------------------------------
# Configuración (todo por variables de entorno, se setean en Railway)
# --------------------------------------------------------------------------

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]  # tu chat_id, para filtrar

# Repo público de GitHub del que se lee el historial (el mismo que usa el
# workflow de alertas). Cambiá estos dos si tu repo tiene otro owner/nombre.
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "STE2025LLC")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "Calculadora-P2P")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")

RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/{GITHUB_BRANCH}"
HISTORY_URL = f"{RAW_BASE}/state/history.csv"
STATE_URL = f"{RAW_BASE}/state/rates_state.json"

BOLIVIA_TZ = timezone(timedelta(hours=-4))

DEFAULT_RANGES = [
    (14, "2 semanas"),
    (30, "1 mes"),
    (60, "2 meses"),
    (90, "3 meses"),
    (180, "6 meses"),
    (365, "1 año"),
]

API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


# --------------------------------------------------------------------------
# Datos (leídos directo del repo público en GitHub, sin guardar nada local)
# --------------------------------------------------------------------------

def fetch_history():
    resp = requests.get(HISTORY_URL, timeout=20)
    resp.raise_for_status()
    rows = []
    reader = csv.DictReader(io.StringIO(resp.text))
    for row in reader:
        try:
            dt = datetime.fromisoformat(row["timestamp"])
            oficial = float(row["oficial"]) if row["oficial"] else None
            paralelo = float(row["paralelo"]) if row["paralelo"] else None
            rows.append({"dt": dt, "oficial": oficial, "paralelo": paralelo})
        except Exception:
            continue
    return rows


def fetch_state():
    resp = requests.get(STATE_URL, timeout=20)
    resp.raise_for_status()
    return resp.json()


def fmt(v):
    if v is None:
        return "N/D"
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# --------------------------------------------------------------------------
# Gráficas (misma lógica que check_rates.py)
# --------------------------------------------------------------------------

def build_chart_png(history, now_bo, days):
    cutoff = now_bo - timedelta(days=days)
    rows = sorted((r for r in history if r["dt"] >= cutoff), key=lambda r: r["dt"])
    if len(rows) < 2:
        return None

    fechas_of = [r["dt"] for r in rows if r["oficial"] is not None]
    valores_of = [r["oficial"] for r in rows if r["oficial"] is not None]
    fechas_pa = [r["dt"] for r in rows if r["paralelo"] is not None]
    valores_pa = [r["paralelo"] for r in rows if r["paralelo"] is not None]

    fig, ax = plt.subplots(figsize=(9, 5), dpi=150)
    if fechas_of:
        ax.plot(fechas_of, valores_of, label="Oficial", color="#2e7d32", linewidth=1.6)
        ax.annotate(
            fmt(valores_of[-1]),
            xy=(fechas_of[-1], valores_of[-1]),
            xytext=(8, 0), textcoords="offset points",
            va="center", fontsize=9, fontweight="bold", color="#2e7d32",
        )
    if fechas_pa:
        ax.plot(fechas_pa, valores_pa, label="Paralelo", color="#1565c0", linewidth=1.6)
        ax.annotate(
            fmt(valores_pa[-1]),
            xy=(fechas_pa[-1], valores_pa[-1]),
            xytext=(8, 0), textcoords="offset points",
            va="center", fontsize=9, fontweight="bold", color="#1565c0",
        )

    ax.set_title(f"Evolución BOB/USD · últimos {days} días")
    ax.set_ylabel("BOB por USD")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left")

    # Formato de fecha "inteligente": muestra día/mes normalmente y agrega
    # el año solo donde el rango cruza de un año a otro.
    locator = mdates.AutoDateLocator()
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    fig.autofmt_xdate()

    # Margen a la derecha para que no se corten las etiquetas de precio
    ax.margins(x=0.08)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def build_chart_album(history, now_bo, ranges=DEFAULT_RANGES):
    photos = []
    for days, label in ranges:
        png = build_chart_png(history, now_bo, days=days)
        if png:
            photos.append((png, f"📈 <b>Evolución BOB/USD · últimos {label}</b>"))
    return photos


# --------------------------------------------------------------------------
# Envío a Telegram
# --------------------------------------------------------------------------

def send_message(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    requests.post(f"{API_URL}/sendMessage", json=payload, timeout=15)


def answer_callback(callback_query_id):
    requests.post(f"{API_URL}/answerCallbackQuery", json={
        "callback_query_id": callback_query_id,
    }, timeout=15)


def grafica_keyboard():
    """Botones inline, 2 por fila, uno por cada rango de DEFAULT_RANGES."""
    buttons = [
        {"text": label, "callback_data": f"grafica:{days}"}
        for days, label in DEFAULT_RANGES
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return {"inline_keyboard": rows}


def send_photo(chat_id, photo_bytes, caption=""):
    requests.post(
        f"{API_URL}/sendPhoto",
        data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
        files={"photo": ("grafica.png", photo_bytes, "image/png")},
        timeout=30,
    )


def send_photo_album(chat_id, photos):
    media = []
    files = {}
    for i, (photo_bytes, caption) in enumerate(photos):
        key = f"photo{i}"
        files[key] = (f"{key}.png", photo_bytes, "image/png")
        item = {"type": "photo", "media": f"attach://{key}"}
        if caption:
            item["caption"] = caption
            item["parse_mode"] = "HTML"
        media.append(item)

    requests.post(
        f"{API_URL}/sendMediaGroup",
        data={"chat_id": chat_id, "media": json.dumps(media)},
        files=files,
        timeout=60,
    )


# --------------------------------------------------------------------------
# Manejo de comandos
# --------------------------------------------------------------------------

def handle_grafica(chat_id):
    """/grafica -> muestra los botones para elegir el rango."""
    send_message(chat_id, "📊 ¿Qué rango quieres ver?", reply_markup=grafica_keyboard())


def handle_grafica_choice(chat_id, days):
    try:
        history = fetch_history()
    except Exception as e:
        send_message(chat_id, f"⚠️ No pude leer el historial ahora mismo ({e}).")
        return

    if not history:
        send_message(chat_id, "Todavía no hay suficiente historial guardado.")
        return

    now_bo = datetime.now(BOLIVIA_TZ)
    label = next((l for d, l in DEFAULT_RANGES if d == days), f"{days} días")
    png = build_chart_png(history, now_bo, days=days)
    if png:
        send_photo(chat_id, png, caption=f"📈 <b>Evolución BOB/USD · últimos {label}</b>")
    else:
        send_message(chat_id, f"No hay suficientes datos en los últimos {label} todavía.")


def handle_precio(chat_id):
    try:
        state = fetch_state()
    except Exception as e:
        send_message(chat_id, f"⚠️ No pude leer la última cotización ahora mismo ({e}).")
        return

    updated_at = state.get("updated_at", "")
    msg = (
        "💱 <b>Último valor conocido</b>\n"
        f"Oficial: <b>{fmt(state.get('oficial'))}</b> BOB/USD\n"
        f"Paralelo: <b>{fmt(state.get('paralelo'))}</b> BOB/USD\n"
        f"EUR/USDT: <b>{fmt(state.get('eur_usdt'))}</b>\n"
        f"Actualizado: {updated_at}"
    )
    send_message(chat_id, msg)


def handle_help(chat_id):
    msg = (
        "🤖 <b>Bot de gráficas BOB/USD</b>\n\n"
        "/grafica – elige el rango con botones (2 sem, 1, 2, 3, 6 meses, 1 año)\n"
        "/precio – último valor oficial y paralelo\n"
    )
    send_message(chat_id, msg)


# --------------------------------------------------------------------------
# Loop principal (long polling)
# --------------------------------------------------------------------------

def run():
    print("Bot arrancado, esperando comandos...", flush=True)
    offset = None
    while True:
        try:
            params = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset
            resp = requests.get(f"{API_URL}/getUpdates", params=params, timeout=40)
            resp.raise_for_status()
            updates = resp.json().get("result", [])

            for update in updates:
                offset = update["update_id"] + 1

                # Click en un botón (elegir rango de la gráfica)
                callback = update.get("callback_query")
                if callback:
                    chat_id = str(callback["message"]["chat"]["id"])
                    answer_callback(callback["id"])
                    if chat_id != str(TELEGRAM_CHAT_ID):
                        continue
                    data = callback.get("data", "")
                    if data.startswith("grafica:"):
                        try:
                            days = int(data.split(":", 1)[1])
                        except ValueError:
                            continue
                        handle_grafica_choice(chat_id, days)
                    continue

                msg = update.get("message") or update.get("channel_post")
                if not msg:
                    continue

                chat_id = str(msg["chat"]["id"])
                if chat_id != str(TELEGRAM_CHAT_ID):
                    continue  # ignora a cualquiera que no sea vos

                text = (msg.get("text") or "").strip().lower()
                if not text:
                    continue

                if text.startswith("/grafica"):
                    handle_grafica(chat_id)
                elif text.startswith("/precio"):
                    handle_precio(chat_id)
                elif text.startswith("/start") or text.startswith("/help"):
                    handle_help(chat_id)

        except requests.exceptions.RequestException as e:
            print(f"Error de red, reintentando en 5s: {e}", file=sys.stderr, flush=True)
            time.sleep(5)
        except Exception as e:
            print(f"Error inesperado: {e}", file=sys.stderr, flush=True)
            time.sleep(5)


if __name__ == "__main__":
    run()
