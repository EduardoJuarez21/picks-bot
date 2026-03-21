"""
bot.py
------
Bot de Telegram para Pickster.
Maneja /start con aprobación manual del admin.

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
INBOX_CHAT = os.getenv("TELEGRAM_INBOX_CHAT_ID")    # chat donde llegan mensajes de usuarios
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

def send_message(chat_id: int, text: str, reply_markup: dict = None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    requests.post(f"{API}/sendMessage", json=payload, timeout=10)


def edit_message_reply_markup(chat_id: int, message_id: int, reply_markup: dict = None):
    payload = {"chat_id": chat_id, "message_id": message_id}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    else:
        payload["reply_markup"] = {"inline_keyboard": []}
    requests.post(f"{API}/editMessageReplyMarkup", json=payload, timeout=10)


def answer_callback(callback_id: str, text: str = ""):
    requests.post(f"{API}/answerCallbackQuery", json={
        "callback_query_id": callback_id,
        "text": text,
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


# ── Helpers ───────────────────────────────────────────────────────────────────

def notify_admin(text: str):
    if ADMIN_CHAT:
        send_message(int(ADMIN_CHAT), text)


def notify_inbox(text: str, reply_markup: dict = None):
    chat = INBOX_CHAT or ADMIN_CHAT
    if chat:
        send_message(int(chat), text, reply_markup=reply_markup)


# ── Handlers ─────────────────────────────────────────────────────────────────

def handle_start(user: dict):
    user_id    = user["id"]
    name       = user.get("first_name", "")
    username   = user.get("username", "")
    log.info("/start user_id=%s username=%s name=%s", user_id, username, name)

    if _has_used_trial(user_id):
        send_message(user_id,
            "Ya usaste tu prueba gratuita de 7 días.\n\n"
            "Para seguir recibiendo picks escríbeme para ver los planes de suscripción."
        )
        return

    send_message(user_id, (
        f"Hola {name} 👋\n\n"
        f"Bienvenido a <b>Pickster</b>.\n\n"
        f"📊 Estadísticas públicas: {SITE_URL}\n\n"
        f"Tu solicitud de acceso al canal privado ha sido enviada. "
        f"En breve recibirás tu link de acceso."
    ))

    buttons = {
        "inline_keyboard": [[
            {"text": "✅ Aprobar", "callback_data": f"approve:{user_id}"},
            {"text": "❌ Rechazar", "callback_data": f"reject:{user_id}"},
        ]]
    }
    notify_inbox(
        f"🔔 Solicitud de acceso\n"
        f"👤 {name} (@{username}) [{user_id}]",
        reply_markup=buttons
    )


def handle_approve(user_id: int, callback_id: str, message_id: int, chat_id: int):
    log.info("Aprobando user_id=%s", user_id)

    if _has_used_trial(user_id):
        answer_callback(callback_id, "Ya tiene acceso activo.")
        edit_message_reply_markup(chat_id, message_id)
        return

    expires_at  = datetime.now(timezone.utc) + timedelta(days=7)
    expire_unix = int(expires_at.timestamp())
    link = create_invite_link(expire_unix)

    if not link:
        answer_callback(callback_id, "Error generando el link.")
        return

    _save_trial(user_id, "", "", expires_at)

    send_message(user_id, (
        f"✅ <b>Acceso aprobado — 7 días gratis</b>\n\n"
        f"Úsalo para unirte al canal privado:\n{link}\n\n"
        f"⏳ El link expira en 24 horas — úsalo ya.\n"
        f"📅 Tu acceso es válido por 7 días."
    ))

    answer_callback(callback_id, "✅ Aprobado")
    edit_message_reply_markup(chat_id, message_id)
    log.info("Trial aprobado user_id=%s", user_id)


def handle_reject(user_id: int, callback_id: str, message_id: int, chat_id: int):
    log.info("Rechazando user_id=%s", user_id)
    send_message(user_id,
        "Tu solicitud de acceso no fue aprobada en este momento.\n\n"
        "Si tienes dudas, escríbenos directamente."
    )
    answer_callback(callback_id, "❌ Rechazado")
    edit_message_reply_markup(chat_id, message_id)


def handle_callback_query(callback: dict):
    callback_id = callback["id"]
    data        = callback.get("data", "")
    msg         = callback.get("message", {})
    message_id  = msg.get("message_id")
    chat_id     = msg.get("chat", {}).get("id")

    if data.startswith("approve:"):
        user_id = int(data.split(":")[1])
        handle_approve(user_id, callback_id, message_id, chat_id)
    elif data.startswith("reject:"):
        user_id = int(data.split(":")[1])
        handle_reject(user_id, callback_id, message_id, chat_id)
    else:
        answer_callback(callback_id)


# ── Polling ───────────────────────────────────────────────────────────────────

def process_update(update: dict):
    if "callback_query" in update:
        handle_callback_query(update["callback_query"])
        return

    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    text    = (msg.get("text") or "").strip()
    user    = msg.get("from", {})
    chat_id = msg.get("chat", {}).get("id")

    # Comando admin: /msg <user_id> <mensaje>
    if text.startswith("/msg") and str(chat_id) == str(ADMIN_CHAT):
        parts = text.split(" ", 2)
        if len(parts) >= 3:
            try:
                target_id = int(parts[1])
                send_message(target_id, parts[2])
                notify_admin(f"✅ Mensaje enviado a [{target_id}]")
            except ValueError:
                notify_admin("❌ Formato: /msg <user_id> <mensaje>")
        else:
            notify_admin("❌ Formato: /msg <user_id> <mensaje>")
        return

    if text.startswith("/start"):
        handle_start(user)
    elif text:
        log.info("Mensaje de usuario user_id=%s text=%r", user.get("id"), text[:50])
        name     = user.get("first_name", "")
        username = user.get("username", "")
        notify_inbox(f"💬 {name} (@{username}) [{user.get('id')}]: {text}")


def run():
    _ensure_table()
    log.info("Bot iniciado. Escuchando...")
    offset = None
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
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
