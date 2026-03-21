"""
bot.py
------
Bot de Telegram para Picks EJT.
Maneja /start y /trial (7 días gratis al canal Pickster).

Uso: python bot.py
"""
import os
import time
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timedelta, timezone

import requests
import psycopg2
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("TELEGRAM_CHAT_ID")          # -1003809470070 (Pickster)
ADMIN_CHAT = os.getenv("TELEGRAM_RESULTS_CHAT_ID")  # chat privado de notificaciones
SITE_URL   = os.getenv("SITE_URL", "https://guileless-sorbet-6f7a7a.netlify.app")
DB_URL     = os.getenv("DATABASE_URL")

API = f"https://api.telegram.org/bot{TOKEN}"


# ── DB ──────────────────────────────────────────────────────────────────────

def _db():
    return psycopg2.connect(DB_URL)


def _ensure_table():
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trial_users (
                    user_id     BIGINT PRIMARY KEY,
                    username    TEXT,
                    first_name  TEXT,
                    started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    expires_at  TIMESTAMPTZ NOT NULL
                )
            """)
        conn.commit()


def _has_used_trial(user_id: int) -> bool:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM trial_users WHERE user_id = %s", (user_id,))
            return cur.fetchone() is not None


def _save_trial(user_id: int, username: str, first_name: str, expires_at: datetime):
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO trial_users (user_id, username, first_name, expires_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id) DO NOTHING
            """, (user_id, username, first_name, expires_at))
        conn.commit()


# ── Telegram API ─────────────────────────────────────────────────────────────

def send_message(chat_id: int, text: str):
    requests.post(f"{API}/sendMessage", json={
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }, timeout=10)


def create_invite_link(expire_date: int) -> str | None:
    resp = requests.post(f"{API}/createChatInviteLink", json={
        "chat_id": CHANNEL_ID,
        "expire_date": expire_date,
        "member_limit": 1,
    }, timeout=10)
    data = resp.json()
    if data.get("ok"):
        return data["result"]["invite_link"]
    log.error("createChatInviteLink failed: %s", data)
    return None


# ── Handlers ─────────────────────────────────────────────────────────────────

def notify_admin(text: str):
    if ADMIN_CHAT:
        send_message(int(ADMIN_CHAT), text)


def handle_start(user: dict):
    name     = user.get("first_name", "")
    username = user.get("username", "")
    log.info("/start user_id=%s username=%s name=%s", user["id"], username, name)
    send_message(user["id"], (
        f"Hola {name} 👋\n\n"
        f"Soy el bot de <b>Picks EJT</b>.\n\n"
        f"📊 Estadísticas públicas: {SITE_URL}\n\n"
        f"Escribe /trial para acceder <b>7 días gratis</b> al canal privado con picks diarios."
    ))
    notify_admin(f"🔔 /start — {name} (@{username}) [{user['id']}]")


def handle_trial(user: dict):
    user_id    = user["id"]
    username   = user.get("username", "")
    first_name = user.get("first_name", "")

    log.info("/trial user_id=%s username=%s name=%s", user_id, username, first_name)
    if _has_used_trial(user_id):
        log.info("Trial ya usado user_id=%s", user_id)
        send_message(user_id,
            "Ya usaste tu prueba gratuita de 7 días.\n\n"
            "Para seguir recibiendo picks escríbeme para ver los planes de suscripción."
        )
        return

    expires_at  = datetime.now(timezone.utc) + timedelta(days=7)
    expire_unix = int(expires_at.timestamp())

    link = create_invite_link(expire_unix)
    if not link:
        send_message(user_id, "Ocurrió un error generando tu acceso. Inténtalo en unos minutos.")
        return

    _save_trial(user_id, username, first_name, expires_at)

    send_message(user_id, (
        f"✅ <b>7 días gratis activados</b>\n\n"
        f"Úsalo para unirte al canal privado:\n{link}\n\n"
        f"⏳ El link expira en 24 horas — úsalo ya.\n"
        f"📅 Tu acceso es válido por 7 días."
    ))
    log.info("Trial activado user_id=%s username=%s", user_id, username)
    notify_admin(f"✅ /trial — {first_name} (@{username}) [{user_id}] — acceso 7 días activado")


# ── Polling ───────────────────────────────────────────────────────────────────

def process_update(update: dict):
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    text = (msg.get("text") or "").strip()
    user = msg.get("from", {})
    chat_id = msg.get("chat", {}).get("id")

    # Comando admin: /msg <user_id> <mensaje>
    if text.startswith("/msg") and str(chat_id) == str(ADMIN_CHAT):
        parts = text.split(" ", 2)
        if len(parts) >= 3:
            try:
                target_id = int(parts[1])
                message = parts[2]
                send_message(target_id, message)
                notify_admin(f"✅ Mensaje enviado a [{target_id}]")
            except ValueError:
                notify_admin("❌ Formato: /msg <user_id> <mensaje>")
        else:
            notify_admin("❌ Formato: /msg <user_id> <mensaje>")
        return

    if text.startswith("/start"):
        handle_start(user)
    elif text.startswith("/trial"):
        handle_trial(user)
    elif text:
        log.info("Mensaje no reconocido user_id=%s text=%r", user.get("id"), text[:50])


def run():
    _ensure_table()
    log.info("Bot iniciado. Escuchando...")
    offset = None
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if offset:
                params["offset"] = offset
            resp = requests.get(f"{API}/getUpdates", params=params, timeout=40)
            data = resp.json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                process_update(update)
        except Exception as e:
            log.error("Error en polling: %s", e)
            time.sleep(5)


def _start_health_server():
    port = int(os.getenv("PORT", 8080))
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        def log_message(self, *args):
            pass
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    threading.Thread(target=_start_health_server, daemon=True).start()
    run()
