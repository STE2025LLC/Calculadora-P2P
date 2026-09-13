"""
Función serverless (Vercel) que reemplaza al bot de Railway.

En vez de un proceso corriendo 24/7 preguntándole a Telegram "¿hay algo
nuevo?" (long polling, lo que hacía bot-grafica/bot.py en Railway),
Telegram llama directamente a esta URL cada vez que le escribís algo al
bot (eso es un "webhook"). Vercel solo "despierta" esta función en ese
instante y se apaga después -- por eso entra en el plan gratis sin
límite de tiempo ni tarjeta: no se cobra por tenerla "prendida" esperando,
porque nunca está prendida sin hacer nada.

Configuración necesaria en el proyecto de Vercel (Settings → Environment
Variables), igual que tenías en Railway:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GITHUB_OWNER, GITHUB_REPO, GITHUB_BRANCH

Después de desplegar, hay que avisarle UNA VEZ a Telegram cuál es la URL
de este webhook (ver instrucciones en el mensaje, sección "Registrar el
webhook").
"""

import io
import os
import csv
import json
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler

import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

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
    (730, "2 años"),
    (1825, "5 años"),
    (None, "Todo"),
]

API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


# --------------------------------------------------------------------------
# Datos (igual que antes: leídos directo del repo público en GitHub)
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
# Gráficas (idéntico a bot-grafica/bot.py)
# --------------------------------------------------------------------------

def build_chart_png(history, now_bo, days):
    if days is None:
        rows = sorted(history, key=lambda r: r["dt"])
        titulo_rango = "histórico completo"
    else:
        cutoff = now_bo - timedelta(days=days)
        rows = sorted((r for r in history if r["dt"] >= cutoff), key=lambda r: r["dt"])
        titulo_rango = f"últimos {days} días"

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

    ax.set_title(f"Evolución BOB/USD · {titulo_rango}")
    ax.set_ylabel("BOB por USD")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left")

    locator = mdates.AutoDateLocator()
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    fig.autofmt_xdate()

    ax.margins(x=0.08)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


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
    buttons = [
        {"text": label, "callback_data": f"grafica:{days if days is not None else 'all'}"}
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


# --------------------------------------------------------------------------
# Comandos
# --------------------------------------------------------------------------

def handle_grafica(chat_id):
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
        send_photo(chat_id, png, caption=f"📈 <b>Evolución BOB/USD · {label}</b>")
    else:
        send_message(chat_id, f"No hay suficientes datos para \"{label}\" todavía.")


def handle_precio(chat_id):
    try:
        state = fetch_state()
    except Exception as e:
        send_message(chat_id, f"⚠️ No pude leer la última cotización ({e}).")
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
        "/grafica – elige el rango con botones (2 sem, 1, 2, 3, 6 meses, 1, 2, 5 años o Todo)\n"
        "/precio – último valor oficial y paralelo\n"
    )
    send_message(chat_id, msg)


def process_update(update):
    callback = update.get("callback_query")
    if callback:
        chat_id = str(callback["message"]["chat"]["id"])
        answer_callback(callback["id"])
        if chat_id != str(TELEGRAM_CHAT_ID):
            return
        data = callback.get("data", "")
        if data.startswith("grafica:"):
            raw = data.split(":", 1)[1]
            days = None if raw == "all" else int(raw)
            handle_grafica_choice(chat_id, days)
        return

    msg = update.get("message") or update.get("channel_post")
    if not msg:
        return

    chat_id = str(msg["chat"]["id"])
    if chat_id != str(TELEGRAM_CHAT_ID):
        return  # ignora a cualquiera que no seas vos

    text = (msg.get("text") or "").strip().lower()
    if not text:
        return

    if text.startswith("/grafica"):
        handle_grafica(chat_id)
    elif text.startswith("/precio"):
        handle_precio(chat_id)
    elif text.startswith("/start") or text.startswith("/help"):
        handle_help(chat_id)


# --------------------------------------------------------------------------
# Entrada de Vercel: una función se ejecuta, procesa 1 update y termina
# --------------------------------------------------------------------------

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            update = json.loads(body or b"{}")
            process_update(update)
        except Exception as e:
            print(f"Error procesando update: {e}")
        finally:
            # Siempre respondemos 200 para que Telegram no reintente de más.
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

    def do_GET(self):
        # Solo para poder chequear en el navegador que la función responde.
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Webhook del bot BOB/USD activo.".encode("utf-8"))
