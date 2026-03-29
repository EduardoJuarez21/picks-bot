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
import stripe as stripe_lib
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("TELEGRAM_CHAT_ID")          # -1003809470070 (Pickster)
ADMIN_CHAT = os.getenv("TELEGRAM_RESULTS_CHAT_ID")  # notificaciones del cron
INBOX_CHAT = os.getenv("TELEGRAM_INBOX_CHAT_ID")    # chat donde llegan mensajes de usuarios
CMD_CHAT   = os.getenv("TELEGRAM_ADMIN_CHAT_ID")    # chat desde donde se envían comandos /msg /invite
SITE_URL          = os.getenv("SITE_URL", "https://picksterx.win")
DB_URL            = os.getenv("DATABASE_URL")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PRICE_ID   = os.getenv("STRIPE_PRICE_ID", "price_1TFRnp3CGjhkjYbVsnHAAlOl")

if STRIPE_SECRET_KEY:
    stripe_lib.api_key = STRIPE_SECRET_KEY

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
                    expires_at  TIMESTAMPTZ NOT NULL,
                    removed_at  TIMESTAMPTZ
                )
            """)
            # migraciones por si la tabla ya existe sin las columnas
            cur.execute("""
                ALTER TABLE trial_users
                ADD COLUMN IF NOT EXISTS removed_at TIMESTAMPTZ
            """)
            cur.execute("""
                ALTER TABLE trial_users
                ADD COLUMN IF NOT EXISTS plan TEXT NOT NULL DEFAULT 'vip'
            """)
            cur.execute("""
                ALTER TABLE trial_users
                ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            """)
            cur.execute("""
                ALTER TABLE trial_users
                ADD COLUMN IF NOT EXISTS email TEXT
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id                  SERIAL PRIMARY KEY,
                    user_id             BIGINT,
                    stripe_session_id   TEXT UNIQUE,
                    stripe_customer_id  TEXT,
                    email               TEXT,
                    first_name          TEXT,
                    amount              INTEGER,
                    currency            TEXT,
                    plan                TEXT,
                    status              TEXT NOT NULL DEFAULT 'paid',
                    expires_at          TIMESTAMPTZ,
                    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
        conn.commit()


def _get_expired_users() -> list:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id, first_name, username, plan
                FROM trial_users
                WHERE expires_at < NOW() AND removed_at IS NULL
            """)
            return cur.fetchall()


def _get_manually_removed_users() -> list:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id, first_name, username, plan
                FROM trial_users
                WHERE removed_at IS NOT NULL AND expires_at > NOW()
            """)
            return cur.fetchall()


def _mark_removed(user_id: int):
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE trial_users SET removed_at = NOW() WHERE user_id = %s",
                (user_id,)
            )
        conn.commit()


def _has_used_trial(user_id: int) -> bool:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM trial_users WHERE user_id = %s", (user_id,))
            return cur.fetchone() is not None


def _save_trial(user_id: int, username: str, first_name: str, expires_at: datetime, plan: str = "vip"):
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO trial_users (user_id, username, first_name, expires_at, plan)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE
                    SET expires_at = EXCLUDED.expires_at,
                        plan       = EXCLUDED.plan,
                        removed_at = NULL
            """, (user_id, username, first_name, expires_at, plan))
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


def kick_user(user_id: int) -> bool:
    """Remueve al usuario del canal y levanta el ban para que pueda volver a ser invitado."""
    r = requests.post(f"{API}/banChatMember", json={
        "chat_id": CHANNEL_ID,
        "user_id": user_id,
    }, timeout=10)
    if not r.json().get("ok"):
        log.error("banChatMember failed user_id=%s: %s", user_id, r.json())
        return False
    requests.post(f"{API}/unbanChatMember", json={
        "chat_id": CHANNEL_ID,
        "user_id": user_id,
        "only_if_banned": True,
    }, timeout=10)
    return True


def create_invite_link(expire_date: int) -> str | None:
    resp = requests.post(f"{API}/createChatInviteLink", json={
        "chat_id": CHANNEL_ID,
        "expire_date": expire_date,
    }, timeout=10)
    data = resp.json()
    log.info("createChatInviteLink response: %s", data)
    if data.get("ok"):
        return data["result"]["invite_link"]
    log.error("createChatInviteLink failed: %s", data)
    return None


# ── Stripe ───────────────────────────────────────────────────────────────────

def create_stripe_checkout(telegram_id: int, name: str) -> str | None:
    if not STRIPE_SECRET_KEY:
        return None
    try:
        session = stripe_lib.checkout.Session.create(
            mode="payment",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            metadata={"telegram_id": str(telegram_id), "telegram_name": name},
            success_url=f"{SITE_URL}/success.html",
            cancel_url=SITE_URL,
        )
        return session.url
    except Exception as e:
        log.error("Stripe checkout error: %s", e)
        return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def notify_admin(text: str):
    chat = CMD_CHAT or ADMIN_CHAT
    if chat:
        send_message(int(chat), text)


def notify_inbox(text: str, reply_markup: dict = None):
    chat = INBOX_CHAT or ADMIN_CHAT
    if chat:
        send_message(int(chat), text, reply_markup=reply_markup)


# ── Handlers ─────────────────────────────────────────────────────────────────

def handle_start(user: dict, param: str = ""):
    user_id  = user["id"]
    name     = user.get("first_name", "")
    username = user.get("username", "")
    log.info("/start user_id=%s username=%s name=%s param=%s", user_id, username, name, param)

    # Deep link desde el sitio web — ir directo al pago
    if param == "pay":
        checkout_url = create_stripe_checkout(user_id, name)
        if checkout_url:
            send_message(user_id, (
                f"💳 <b>Suscripción VIP — MXN 250/mes</b>\n\n"
                f"Toca el botón para completar tu pago.\n\n"
                f"Una vez pagado recibirás el acceso al canal automáticamente."
            ), reply_markup={"inline_keyboard": [[
                {"text": "💳 Pagar ahora", "url": checkout_url}
            ]]})
        else:
            send_message(user_id, "Error generando el link de pago. Intenta de nuevo.")
        return

    if _has_used_trial(user_id):
        send_message(user_id, (
            f"Hola {name} 👋\n\n"
            f"Ya utilizaste tu prueba gratuita.\n\n"
            f"Para continuar con acceso VIP, suscríbete:"
        ), reply_markup={"inline_keyboard": [[
            {"text": "💳 Suscribirme — MXN 250/mes", "callback_data": "subscribe"}
        ]]})
    else:
        send_message(user_id, (
            f"Hola {name} 👋\n\n"
            f"Bienvenido a <b>Pickster</b>.\n\n"
            f"📊 Estadísticas públicas: {SITE_URL}\n\n"
            f"Elige tu opción:"
        ), reply_markup={"inline_keyboard": [
            [{"text": "🆓 Prueba gratuita (7 días)", "callback_data": "trial"}],
            [{"text": "💳 Suscribirme — MXN 250/mes", "callback_data": "subscribe"}],
        ]})
        notify_inbox(
            f"🔔 Nuevo usuario\n"
            f"👤 {name} (@{username}) [{user_id}]"
        )


def handle_trial_request(user: dict, callback_id: str):
    user_id  = user["id"]
    name     = user.get("first_name", "")
    username = user.get("username", "")
    if _has_used_trial(user_id):
        answer_callback(callback_id, "Ya utilizaste tu prueba gratuita.")
        return

    expires_at  = datetime.now(timezone.utc) + timedelta(days=7)
    expire_unix = int((datetime.now(timezone.utc) + timedelta(hours=24)).timestamp())
    link = create_invite_link(expire_unix)

    if not link:
        answer_callback(callback_id, "Error generando el acceso. Intenta de nuevo.")
        return

    _save_trial(user_id, username, name, expires_at, "trial")
    answer_callback(callback_id)
    send_message(user_id, (
        f"✅ <b>Acceso de prueba activado — 7 días gratis</b>\n\n"
        f"Toca el botón para unirte al canal privado.\n\n"
        f"⏳ El link expira en 24 horas — úsalo ya.\n"
        f"📅 Tu acceso es válido por 7 días."
    ), reply_markup={"inline_keyboard": [[
        {"text": "📢 Unirse al canal", "url": link}
    ]]})
    notify_inbox(
        f"🆓 Trial activado automáticamente\n"
        f"👤 {name} (@{username}) [{user_id}]"
    )


def handle_subscribe_request(user: dict, callback_id: str | None):
    user_id = user["id"]
    name    = user.get("first_name", "")
    checkout_url = create_stripe_checkout(user_id, name)
    if checkout_url:
        if callback_id:
            answer_callback(callback_id)
        send_message(user_id, (
            f"💳 <b>Suscripción VIP — MXN 250/mes</b>\n\n"
            f"Toca el botón para completar tu pago.\n\n"
            f"Una vez pagado recibirás el acceso al canal automáticamente."
        ), reply_markup={"inline_keyboard": [[
            {"text": "💳 Pagar ahora", "url": checkout_url}
        ]]})
    else:
        if callback_id:
            answer_callback(callback_id, "Error generando el link de pago.")
        send_message(user_id, "Error generando el link de pago. Intenta de nuevo.")


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
    elif data == "trial":
        user_from = callback.get("from", {})
        handle_trial_request(user_from, callback_id)
    elif data == "subscribe":
        user_from = callback.get("from", {})
        handle_subscribe_request(user_from, callback_id)
    else:
        answer_callback(callback_id)


# ── Polling ───────────────────────────────────────────────────────────────────

def process_update(update: dict):
    if "callback_query" in update:
        handle_callback_query(update["callback_query"])
        return

    if "chat_member" in update:
        cm     = update["chat_member"]
        new    = cm.get("new_chat_member", {})
        status = new.get("status")
        if status == "member":
            user_id    = new["user"]["id"]
            first_name = new["user"].get("first_name", "")
            username   = new["user"].get("username", "")
            if not _has_used_trial(user_id):
                log.warning("Intruso detectado user_id=%s — no registrado, kickeando", user_id)
                kick_user(user_id)
                send_message(user_id, (
                    "⚠️ <b>Problema con tu acceso</b>\n\n"
                    "No encontramos una suscripción activa asociada a tu cuenta.\n\n"
                    "Si crees que es un error, contáctanos para resolverlo."
                ))
                notify_admin(f"🚨 Intruso kickeado — {first_name} (@{username}) [{user_id}] entró sin registro")
        return

    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    text    = (msg.get("text") or "").strip()
    user    = msg.get("from", {})
    chat_id = msg.get("chat", {}).get("id")

    # Comandos admin
    _cmd_chat = CMD_CHAT or ADMIN_CHAT
    if str(chat_id) == str(_cmd_chat):
        if text.startswith("/msg"):
            parts = text.split(" ", 2)
            if len(parts) >= 3:
                try:
                    target_id = int(parts[1])
                    send_message(target_id, parts[2])
                    send_message(int(_cmd_chat), f"✅ Mensaje enviado a [{target_id}]")
                except ValueError:
                    send_message(int(_cmd_chat), "❌ Formato: /msg <user_id> <mensaje>")
            else:
                send_message(int(_cmd_chat), "❌ Formato: /msg <user_id> <mensaje>")
            return

        if text.startswith("/unban"):
            parts = text.split(" ", 1)
            if len(parts) == 2:
                try:
                    target_id = int(parts[1])
                    r = requests.post(f"{API}/unbanChatMember", json={
                        "chat_id": CHANNEL_ID,
                        "user_id": target_id,
                    }, timeout=10)
                    if r.json().get("ok"):
                        send_message(int(_cmd_chat), f"✅ Unban aplicado a [{target_id}]")
                    else:
                        send_message(int(_cmd_chat), f"❌ Error: {r.json()}")
                except ValueError:
                    send_message(int(_cmd_chat), "❌ Formato: /unban <user_id>")
            else:
                send_message(int(_cmd_chat), "❌ Formato: /unban <user_id>")
            return

        if text.startswith("/invite"):
            parts = text.split(" ", 2)
            if len(parts) >= 2:
                try:
                    target_id  = int(parts[1])
                    is_trial   = len(parts) == 3 and parts[2].strip().lower() == "trial"
                    plan       = "trial" if is_trial else "vip"
                    days       = 7 if is_trial else 30
                    expires_at = datetime.now(timezone.utc) + timedelta(days=days)
                    expire_unix = int((datetime.now(timezone.utc) + timedelta(hours=24)).timestamp())
                    # obtener first_name y username del usuario
                    chat_resp = requests.post(f"{API}/getChat", json={"chat_id": target_id}, timeout=10).json()
                    chat_info = chat_resp.get("result", {})
                    first_name = chat_info.get("first_name", "")
                    username   = chat_info.get("username", "")
                    link = create_invite_link(expire_unix)
                    if link:
                        _save_trial(target_id, username, first_name, expires_at, plan)
                        send_message(target_id, (
                            f"✅ <b>Acceso al canal activado</b>\n\n"
                            f"Úsalo para unirte al canal privado:\n{link}\n\n"
                            f"⏳ El link expira en 24 horas — úsalo ya.\n"
                            f"📅 Tu acceso es válido por {days} días."
                        ))
                        send_message(int(_cmd_chat), f"✅ Invite enviado a [{target_id}] — plan {plan} ({days} días) registrado.")
                    else:
                        send_message(int(_cmd_chat), f"❌ Error generando link para [{target_id}]")
                except ValueError:
                    send_message(int(_cmd_chat), "❌ Formato: /invite <user_id> [trial]")
            else:
                send_message(int(_cmd_chat), "❌ Formato: /invite <user_id> [trial]")
            return

    user_id = user.get("id")

    if text.startswith("/start"):
        param = text.split(" ", 1)[1].strip() if " " in text else ""
        handle_start(user, param)
    elif any(p in text.lower() for p in ["quiero trial", "trial", "prueba"]):
        if not _has_used_trial(user_id):
            name     = user.get("first_name", "")
            username = user.get("username", "")
            send_message(user_id,
                "✅ Solicitud de prueba recibida. En breve te envío el acceso al canal."
            )
            notify_inbox(
                f"🆓 Solicitud de trial\n"
                f"👤 {name} (@{username}) [{user_id}]\n\n"
                f"Usa /invite {user_id} trial para dar acceso."
            )
        else:
            handle_subscribe_request(user, None)
    elif text:
        log.info("Mensaje de usuario user_id=%s text=%r", user_id, text[:50])
        name     = user.get("first_name", "")
        username = user.get("username", "")
        notify_inbox(f"💬 {name} (@{username}) [{user_id}]: {text}")


def run():
    _ensure_table()
    log.info("Bot iniciado. Escuchando...")
    offset = None
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message", "callback_query", "chat_member"]}
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


def _run_expiry_check():
    """Corre cada 24 horas y remueve usuarios con trial expirado."""
    while True:
        try:
            expired = _get_expired_users()
            for user_id, first_name, username, plan in expired:
                log.info("Expirado plan=%s removiendo user_id=%s", plan, user_id)
                if kick_user(user_id):
                    _mark_removed(user_id)
                    checkout_url = create_stripe_checkout(user_id, first_name or "")
                    if plan == "trial":
                        text = (
                            "⏰ <b>Tu prueba gratuita de 7 días ha expirado.</b>\n\n"
                            "¿Te gustó el servicio? Suscríbete para seguir recibiendo picks VIP:"
                        )
                    else:
                        text = (
                            "⏰ <b>Tu suscripción VIP ha expirado.</b>\n\n"
                            "Renueva para seguir teniendo acceso al canal de picks:"
                        )
                    markup = {"inline_keyboard": [[
                        {"text": "💳 Renovar — MXN 250", "url": checkout_url}
                    ]]} if checkout_url else None
                    send_message(user_id, text, reply_markup=markup)
                    notify_admin(
                        f"🔴 {plan.upper()} expirado — {first_name} (@{username}) [{user_id}] removido del canal"
                    )
                    log.info("Removido user_id=%s", user_id)

            for user_id, first_name, username, plan in _get_manually_removed_users():
                log.info("Remoción manual plan=%s user_id=%s", plan, user_id)
                if kick_user(user_id):
                    notify_admin(
                        f"🔴 Removido manualmente — {first_name} (@{username}) [{user_id}]"
                    )
                    log.info("Removido manualmente user_id=%s", user_id)
        except Exception as e:
            log.error("Error en expiry check: %s", e)
        time.sleep(24 * 60 * 60)


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
    threading.Thread(target=_run_expiry_check, daemon=True).start()
    run()
