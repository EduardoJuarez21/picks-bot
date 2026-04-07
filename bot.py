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

TOKEN           = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID      = os.getenv("TELEGRAM_CHAT_ID")          # canal fútbol
MLB_CHANNEL_ID  = os.getenv("TELEGRAM_MLB_CHAT_ID")      # canal MLB
ADMIN_CHAT      = os.getenv("TELEGRAM_RESULTS_CHAT_ID")  # notificaciones del cron
INBOX_CHAT      = os.getenv("TELEGRAM_INBOX_CHAT_ID")    # chat donde llegan mensajes de usuarios
CMD_CHAT        = os.getenv("TELEGRAM_ADMIN_CHAT_ID")    # chat desde donde se envían comandos /msg /invite
SITE_URL          = os.getenv("SITE_URL", "https://picksterx.win")
DB_URL            = os.getenv("DATABASE_URL")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PRICE_ID   = os.getenv("STRIPE_PRICE_ID", "price_1TFRnp3CGjhkjYbVsnHAAlOl")
STRIPE_COUPON_25  = os.getenv("STRIPE_COUPON_25_ID")   # cupón 25 % primera compra sin referido
def _fetch_bot_username() -> str:
    try:
        me = requests.get(f"https://api.telegram.org/bot{TOKEN}/getMe", timeout=10).json()
        return me["result"]["username"]
    except Exception as e:
        log.error("No se pudo obtener el username del bot: %s", e)
        return ""

BOT_USERNAME = _fetch_bot_username()

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
                ALTER TABLE trial_users
                ADD COLUMN IF NOT EXISTS sport TEXT NOT NULL DEFAULT 'futbol'
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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS referrals (
                    referrer_id   BIGINT NOT NULL,
                    referred_id   BIGINT NOT NULL PRIMARY KEY,
                    coupon_id     TEXT,
                    coupon_used   BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
        conn.commit()


def _get_expired_users() -> list:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id, first_name, username, plan, sport
                FROM trial_users
                WHERE expires_at < NOW() AND removed_at IS NULL
            """)
            return cur.fetchall()


def _get_manually_removed_users() -> list:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id, first_name, username, plan, sport
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


def _has_paid(user_id: int) -> bool:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM trial_users WHERE user_id = %s AND plan = 'vip'", (user_id,))
            return cur.fetchone() is not None


def _save_referral(referrer_id: int, referred_id: int):
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO referrals (referrer_id, referred_id)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
            """, (referrer_id, referred_id))
        conn.commit()


def _get_referrer(referred_id: int) -> int | None:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT referrer_id FROM referrals
                WHERE referred_id = %s AND coupon_id IS NULL
            """, (referred_id,))
            row = cur.fetchone()
            return row[0] if row else None


def _save_coupon_for_referrer(referrer_id: int, coupon_id: str):
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE referrals SET coupon_id = %s
                WHERE referrer_id = %s AND coupon_id IS NULL AND coupon_used = FALSE
            """, (coupon_id, referrer_id))
        conn.commit()


def _get_pending_coupon(referrer_id: int) -> str | None:
    """Devuelve el coupon_id pendiente de usar del referidor."""
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT coupon_id FROM referrals
                WHERE referrer_id = %s AND coupon_id IS NOT NULL AND coupon_used = FALSE
                LIMIT 1
            """, (referrer_id,))
            row = cur.fetchone()
            return row[0] if row else None


def _mark_coupon_used(referrer_id: int):
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE referrals SET coupon_used = TRUE
                WHERE referrer_id = %s AND coupon_used = FALSE
            """, (referrer_id,))
        conn.commit()


def _get_all_users() -> list:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id, first_name FROM trial_users")
            return cur.fetchall()


def _has_used_trial(user_id: int) -> bool:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM trial_users WHERE user_id = %s", (user_id,))
            return cur.fetchone() is not None



def _save_trial(user_id: int, username: str, first_name: str, expires_at: datetime, plan: str = "vip", sport: str = "futbol"):
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO trial_users (user_id, username, first_name, expires_at, plan, sport)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE
                    SET expires_at = EXCLUDED.expires_at,
                        plan       = EXCLUDED.plan,
                        sport      = EXCLUDED.sport,
                        removed_at = NULL
            """, (user_id, username, first_name, expires_at, plan, sport))
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


def _channels_for_sport(sport: str) -> list[str]:
    """Devuelve la lista de channel IDs según el deporte elegido."""
    channels = []
    if sport in ("futbol", "ambos") and CHANNEL_ID:
        channels.append(CHANNEL_ID)
    if sport in ("mlb", "ambos") and MLB_CHANNEL_ID:
        channels.append(MLB_CHANNEL_ID)
    return channels or ([CHANNEL_ID] if CHANNEL_ID else [])


def kick_user(user_id: int, sport: str = "futbol") -> bool:
    """Remueve al usuario de los canales que le corresponden según su deporte."""
    ok = False
    for ch in _channels_for_sport(sport):
        r = requests.post(f"{API}/banChatMember", json={"chat_id": ch, "user_id": user_id}, timeout=10)
        if not r.json().get("ok"):
            log.error("banChatMember failed user_id=%s channel=%s: %s", user_id, ch, r.json())
            continue
        time.sleep(1)
        requests.post(f"{API}/unbanChatMember", json={"chat_id": ch, "user_id": user_id}, timeout=10)
        ok = True
    return ok


def create_invite_links(expire_date: int, user_id: int, sport: str) -> list[str]:
    """Crea invite links para los canales que corresponden al deporte elegido."""
    links = []
    for ch in _channels_for_sport(sport):
        requests.post(f"{API}/unbanChatMember", json={"chat_id": ch, "user_id": user_id}, timeout=10)
        resp = requests.post(f"{API}/createChatInviteLink", json={
            "chat_id": ch,
            "expire_date": expire_date,
            "member_limit": 1,
        }, timeout=10)
        data = resp.json()
        log.info("createChatInviteLink channel=%s response: %s", ch, data)
        if data.get("ok"):
            links.append(data["result"]["invite_link"])
        else:
            log.error("createChatInviteLink failed channel=%s: %s", ch, data)
    return links


def create_invite_link(expire_date: int, user_id: int = None) -> str | None:
    """Compatibilidad: invite al canal de fútbol."""
    links = create_invite_links(expire_date, user_id or 0, "futbol")
    return links[0] if links else None


# ── Stripe ───────────────────────────────────────────────────────────────────

def create_stripe_checkout(telegram_id: int, name: str) -> str | None:
    if not STRIPE_SECRET_KEY:
        return None
    try:
        kwargs = dict(
            mode="payment",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            metadata={"telegram_id": str(telegram_id), "telegram_name": name},
            success_url=f"{SITE_URL}/success.html",
            cancel_url=SITE_URL,
        )
        # Prioridad: cupón 40% de referido > 25% primera compra > precio normal
        pending_coupon = _get_pending_coupon(telegram_id)
        if pending_coupon:
            kwargs["discounts"] = [{"coupon": pending_coupon}]
            _mark_coupon_used(telegram_id)
        elif not _has_paid(telegram_id) and STRIPE_COUPON_25:
            key = "promotion_code" if STRIPE_COUPON_25.startswith("promo_") else "coupon"
            kwargs["discounts"] = [{key: STRIPE_COUPON_25}]
        else:
            kwargs["allow_promotion_codes"] = True
        session = stripe_lib.checkout.Session.create(**kwargs)
        return session.url
    except Exception as e:
        log.error("Stripe checkout error: %s", e)
        return None


def create_stripe_coupon_40() -> str | None:
    """Crea un cupón interno de 40% de un solo uso. Se aplica automáticamente al checkout."""
    if not STRIPE_SECRET_KEY:
        return None
    try:
        coupon = stripe_lib.Coupon.create(
            percent_off=40,
            duration="once",
            max_redemptions=1,
        )
        return coupon.id
    except Exception as e:
        log.error("Stripe coupon 40% error: %s", e)
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

    # Deep link de referido — registrar quién lo invitó
    if param.startswith("ref_"):
        try:
            referrer_id = int(param[4:])
            if referrer_id != user_id:
                _save_referral(referrer_id, user_id)
                log.info("Referral guardado: referrer=%s referred=%s", referrer_id, user_id)
        except ValueError:
            pass

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
        ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
        send_message(user_id, (
            f"Hola {name} 👋\n\n"
            f"Ya utilizaste tu prueba gratuita.\n\n"
            f"Para continuar con acceso VIP, suscríbete.\n\n"
            f"🔗 <b>Tu link de referido:</b>\n"
            f"{ref_link}\n\n"
            f"Comparte y gana <b>40% de descuento</b> en tu próxima compra."
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
    user_id = user["id"]
    if _has_used_trial(user_id):
        answer_callback(callback_id, "Ya utilizaste tu prueba gratuita.")
        return
    handle_trial_sport(user, "ambos", callback_id)


def handle_trial_sport(user: dict, sport: str, callback_id: str):
    user_id  = user["id"]
    name     = user.get("first_name", "")
    username = user.get("username", "")

    if _has_used_trial(user_id):
        answer_callback(callback_id, "Ya utilizaste tu prueba gratuita.")
        return

    expires_at  = datetime.now(timezone.utc) + timedelta(days=7)
    expire_unix = int((datetime.now(timezone.utc) + timedelta(hours=24)).timestamp())
    links = create_invite_links(expire_unix, user_id, sport)

    if not links:
        answer_callback(callback_id, "Error generando el acceso. Intenta de nuevo.")
        return

    _save_trial(user_id, username, name, expires_at, "trial", sport)

    # Si vino referido, guardar cupón del 40%
    referrer_id = _get_referrer(user_id)
    if referrer_id:
        coupon_id = create_stripe_coupon_40()
        if coupon_id:
            _save_coupon_for_referrer(referrer_id, coupon_id)
            send_message(referrer_id, (
                f"🎁 <b>¡Alguien se unió con tu link de referido!</b>\n\n"
                f"Tienes un <b>40% de descuento</b> guardado para tu próxima compra.\n"
                f"Se aplicará automáticamente cuando vayas a pagar. 🎯"
            ))
            log.info("Cupón 40%% guardado para referidor=%s por referido=%s", referrer_id, user_id)

    sport_label = {"futbol": "⚽ Fútbol", "mlb": "⚾ MLB", "ambos": "⚽ Fútbol + ⚾ MLB"}.get(sport, sport)
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
    answer_callback(callback_id)

    channel_labels = []
    if sport in ("futbol", "ambos"):
        channel_labels.append("⚽ Fútbol")
    if sport in ("mlb", "ambos"):
        channel_labels.append("⚾ MLB")
    buttons = [[{"text": f"📢 Unirse — {channel_labels[i] if i < len(channel_labels) else sport_label}", "url": lnk}] for i, lnk in enumerate(links)]
    send_message(user_id, (
        f"✅ <b>Acceso de prueba activado — 7 días gratis</b>\n\n"
        f"Canal(es): <b>{sport_label}</b>\n\n"
        f"Toca el botón para unirte.\n"
        f"⏳ El link expira en 24 horas — úsalo ya.\n"
        f"📅 Tu acceso es válido por 7 días.\n\n"
        f"🔗 <b>¿Conoces a alguien que le interese?</b>\n"
        f"Comparte tu link y gana <b>40% de descuento</b> en tu próxima compra:\n"
        f"{ref_link}"
    ), reply_markup={"inline_keyboard": buttons})
    notify_inbox(
        f"🆓 Trial activado — {sport_label}\n"
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
    link = create_invite_link(expire_unix, user_id)

    if not link:
        answer_callback(callback_id, "Error generando el link.")
        return

    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
    send_message(user_id, (
        f"✅ <b>Acceso aprobado — 7 días gratis</b>\n\n"
        f"Úsalo para unirte al canal privado:\n{link}\n\n"
        f"⏳ El link expira en 24 horas — úsalo ya.\n"
        f"📅 Tu acceso es válido por 7 días.\n\n"
        f"🔗 <b>¿Conoces a alguien que le interese?</b>\n"
        f"Comparte tu link y gana <b>40% de descuento</b> en tu próxima compra:\n"
        f"{ref_link}"
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
    elif data.startswith("trial_sport:"):
        sport = data.split(":")[1]
        user_from = callback.get("from", {})
        handle_trial_sport(user_from, sport, callback_id)
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

        if text.startswith("/broadcast_ref"):
            parts = text.split()[1:]
            if parts:
                # IDs específicos
                id_list = []
                for p in parts:
                    try:
                        id_list.append((int(p), None))
                    except ValueError:
                        pass
                users = id_list
            else:
                users = _get_all_users()
            sent = 0
            failed = 0
            for uid, first_name in users:
                # Si vino de IDs específicos, obtener first_name de la DB
                if first_name is None:
                    with _db() as conn:
                        with conn.cursor() as cur:
                            cur.execute("SELECT first_name FROM trial_users WHERE user_id = %s", (uid,))
                            row = cur.fetchone()
                            first_name = row[0] if row else ""
                ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"
                try:
                    send_message(uid, (
                        f"Hola {first_name or ''} 👋\n\n"
                        f"🔗 <b>Tu link de referido:</b>\n"
                        f"{ref_link}\n\n"
                        f"Compártelo con alguien que le interesen los picks y gana <b>40% de descuento</b> en tu próxima compra. 🎯"
                    ))
                    sent += 1
                except Exception:
                    failed += 1
                time.sleep(0.05)  # ~20 msg/s, bajo límite de Telegram
            send_message(int(_cmd_chat), f"✅ Broadcast ref enviado: {sent} ok, {failed} fallidos.")
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
                    link = create_invite_link(expire_unix, target_id)
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
    elif text == "/ref":
        ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
        send_message(user_id, (
            f"🔗 <b>Tu link de referido</b>\n\n"
            f"{ref_link}\n\n"
            f"Cuando alguien se una con tu link, recibirás un <b>40% de descuento</b> en tu próxima compra."
        ))
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
    log.info("Bot iniciado como @%s. Escuchando...", BOT_USERNAME)
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
            for user_id, first_name, username, plan, sport in expired:
                log.info("Expirado plan=%s sport=%s removiendo user_id=%s", plan, sport, user_id)
                if kick_user(user_id, sport):
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

            for user_id, first_name, username, plan, sport in _get_manually_removed_users():
                log.info("Remoción manual plan=%s sport=%s user_id=%s", plan, sport, user_id)
                if kick_user(user_id, sport):
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
