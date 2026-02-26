import logging
import requests
import psycopg2
import psycopg2.extras
import json
import re
import random
import string
from openai import OpenAI
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from telegram.ext import ApplicationBuilder, MessageHandler, CallbackQueryHandler, ContextTypes, filters, CommandHandler

# ─── НАСТРОЙКИ ───────────────────────────────
TELEGRAM_TOKEN = "8569833784:AAEHo5l5vUVmMEzHjKBYxiWViwz-V29qO8A"
OPENAI_API_KEY = "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"

# URL где будет лежать shop.html (GitHub Pages, Vercel, etc.)
SHOP_URL   = "https://kosmostack.github.io/kosmoshop/"
# Твой личный Telegram chat_id — получи у @userinfobot
ADMIN_CHAT_ID = 123456789  # ← замени на свой

DB = {
    "host": "aws-1-eu-west-1.pooler.supabase.com",
    "port": 6543,
    "database": "postgres",
    "user": "postgres.bzrffecjkseqkymefaeb",
    "password": "8s%y6t&TfDfq%5#",
}

# ─── МЕНЮ НАПИТКОВ (1 балл = 1 MDL) ─────────
DRINKS = [
    {"name": "Эспрессо",           "price": 50},
    {"name": "Американо",          "price": 50},
    {"name": "Капучино (малый)",   "price": 50},
    {"name": "Капучино (большой)", "price": 50},
    {"name": "Флэт",               "price": 60},
    {"name": "Латте",              "price": 70},
]
# ─────────────────────────────────────────────

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

ai = OpenAI(api_key=OPENAI_API_KEY)
memory = {}


# ══════════════════════════════
# КЛАВИАТУРЫ
# ══════════════════════════════

def main_menu_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["💎 Мой баланс",      "📸 Отправить чек"],
            ["⭐ Оценить бариста", "🏠 Оценить место"],
            ["☕ Потратить баллы", "🎁 Дополнительные баллы"],
            [KeyboardButton("🛍 Купить кофе домой", web_app=WebAppInfo(url=SHOP_URL))],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def drinks_keyboard():
    """Инлайн-меню напитков."""
    rows = []
    for i, d in enumerate(DRINKS):
        rows.append([InlineKeyboardButton(
            f"{d['name']} — {d['price']} баллов",
            callback_data=f"drink|{i}"
        )])
    return InlineKeyboardMarkup(rows)


def confirm_keyboard(drink_index):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Подтвердить",  callback_data=f"confirm|{drink_index}"),
        InlineKeyboardButton("❌ Отмена",        callback_data="confirm|cancel"),
    ]])


def rating_keyboard(category: str):
    buttons = [
        InlineKeyboardButton(f"{'⭐' * i}", callback_data=f"rate|{category}|{i}")
        for i in range(1, 6)
    ]
    return InlineKeyboardMarkup([buttons])


def bonus_keyboard(db_user):
    has_phone    = bool(db_user and db_user.get("contact_number"))
    has_email    = bool(db_user and db_user.get("email"))
    has_birthday = bool(db_user and db_user.get("birthday"))

    def btn(label, field, done):
        text = f"✅ {label}" if done else f"➕ {label} (+10 баллов)"
        return InlineKeyboardButton(text, callback_data=f"bonus|{field}" if not done else "bonus|already")

    return InlineKeyboardMarkup([
        [btn("Номер телефона", "phone",    has_phone)],
        [btn("Gmail",          "email",    has_email)],
        [btn("Дата рождения",  "birthday", has_birthday)],
    ])


# ══════════════════════════════
# БАЗА ДАННЫХ
# ══════════════════════════════

def db():
    return psycopg2.connect(**DB)


def get_user(telegram_id):
    try:
        with db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT telegram_id, username, first_name, contact_number,
                           email, birthday, chat_id,
                           COALESCE(total_points, 0) AS total_points
                    FROM telegram_users WHERE telegram_id = %s
                """, (telegram_id,))
                row = cur.fetchone()
                return dict(row) if row else None
    except Exception as e:
        log.error(f"get_user error: {e}")
        return None


def ensure_user(telegram_id, username, first_name, chat_id):
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO telegram_users (telegram_id, username, first_name, chat_id, total_points)
                    VALUES (%s, %s, %s, %s, 0)
                    ON CONFLICT (telegram_id) DO UPDATE SET
                        username = EXCLUDED.username,
                        first_name = EXCLUDED.first_name,
                        chat_id = EXCLUDED.chat_id
                """, (telegram_id, username, first_name, chat_id))
                conn.commit()
    except Exception as e:
        log.error(f"ensure_user error: {e}")


def add_points(telegram_id, points):
    try:
        with db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    UPDATE telegram_users
                    SET total_points = COALESCE(total_points, 0) + %s
                    WHERE telegram_id = %s RETURNING total_points
                """, (points, telegram_id))
                conn.commit()
                row = cur.fetchone()
                return row["total_points"] if row else 0
    except Exception as e:
        log.error(f"add_points error: {e}")
        return 0


def deduct_points(telegram_id, points):
    """Списывает баллы. Возвращает остаток или None если недостаточно."""
    try:
        with db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    UPDATE telegram_users
                    SET total_points = total_points - %s
                    WHERE telegram_id = %s AND total_points >= %s
                    RETURNING total_points
                """, (points, telegram_id, points))
                conn.commit()
                row = cur.fetchone()
                return row["total_points"] if row else None
    except Exception as e:
        log.error(f"deduct_points error: {e}")
        return None


def generate_code():
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def save_redemption(telegram_id, drink_name, points_spent, code):
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO redemptions (telegram_id, drink_name, points_spent, code, used, created_at)
                    VALUES (%s, %s, %s, %s, false, NOW())
                """, (telegram_id, drink_name, points_spent, code))
                conn.commit()
        return True
    except Exception as e:
        log.error(f"save_redemption error: {e}")
        return False


def save_receipt(telegram_id, price, receipt_id, date, points):
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO receipts (telegram_id, price, receipt_id, receipt_date, points, created_at)
                    VALUES (%s, %s, %s, %s, %s, NOW())
                """, (telegram_id, price, receipt_id, date, points))
                conn.commit()
        return True
    except Exception as e:
        log.error(f"save_receipt error: {e}")
        return False


def can_rate_today(telegram_id, category):
    column = "barista_rating" if category == "barista" else "place_rating"
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT 1 FROM ratings
                    WHERE telegram_id = %s AND {column} IS NOT NULL
                      AND created_at::date = CURRENT_DATE LIMIT 1
                """, (telegram_id,))
                return cur.fetchone() is None
    except Exception as e:
        log.error(f"can_rate_today error: {e}")
        return True


def save_rating(telegram_id, rating, category, points):
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO ratings (telegram_id, barista_rating, place_rating, points_earned, created_at)
                    VALUES (%s, %s, %s, %s, NOW())
                """, (
                    telegram_id,
                    rating if category == "barista" else None,
                    rating if category == "place" else None,
                    points
                ))
                conn.commit()
        return True
    except Exception as e:
        log.error(f"save_rating error: {e}")
        return False


# ══════════════════════════════
# AI HELPERS
# ══════════════════════════════

def ask_ai(system, user_msg, chat_id=None):
    msgs = [{"role": "system", "content": system}]
    if chat_id:
        if chat_id not in memory:
            memory[chat_id] = []
        memory[chat_id].append({"role": "user", "content": user_msg})
        msgs += memory[chat_id][-20:]
    else:
        msgs.append({"role": "user", "content": user_msg})
    resp = ai.chat.completions.create(model="gpt-3.5-turbo", messages=msgs, max_tokens=600)
    reply = resp.choices[0].message.content
    if chat_id:
        memory[chat_id].append({"role": "assistant", "content": reply})
    return reply


def classify_intent(text):
    prompt = f"""Classify the intent of this message. Reply with ONLY one word:
- BALANCE (asking about points/balance)
- RATE (wants to rate barista or place)
- CHAT (anything else)

Message: "{text}"
Intent:"""
    resp = ai.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=10
    )
    return resp.choices[0].message.content.strip().upper()


# ══════════════════════════════
# CALLBACKS
# ══════════════════════════════

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    data = query.data

    # ── ВЫБОР НАПИТКА ─────────────────────────
    if data.startswith("drink|"):
        drink_index = int(data.split("|")[1])
        drink = DRINKS[drink_index]
        db_user = get_user(user.id)
        balance = db_user["total_points"] if db_user else 0

        if balance < drink["price"]:
            await query.edit_message_text(
                f"❌ *Недостаточно баллов*\n\n"
                f"☕ {drink['name']} — {drink['price']} баллов\n"
                f"💎 Твой баланс: {balance} баллов\n"
                f"Не хватает: {drink['price'] - balance} баллов\n\n"
                f"Отправляй чеки чтобы накопить больше! 📸",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(
                f"☕ *{drink['name']}* — {drink['price']} баллов\n\n"
                f"💎 Твой баланс: {balance} баллов\n"
                f"После списания останется: {balance - drink['price']} баллов\n\n"
                f"Подтвердить покупку?",
                parse_mode="Markdown",
                reply_markup=confirm_keyboard(drink_index)
            )

    # ── ПОДТВЕРЖДЕНИЕ ПОКУПКИ ─────────────────
    elif data.startswith("confirm|"):
        action = data.split("|")[1]

        if action == "cancel":
            await query.edit_message_text("Отменено. Возвращайся когда захочешь! ☕")
            return

        drink_index = int(action)
        drink = DRINKS[drink_index]

        remaining = deduct_points(user.id, drink["price"])

        if remaining is None:
            await query.edit_message_text(
                "❌ Недостаточно баллов. Попробуй накопить ещё! 📸"
            )
            return

        code = generate_code()
        save_redemption(user.id, drink["name"], drink["price"], code)

        await query.edit_message_text(
            f"✅ *Готово! Покажи этот код бариста:*\n\n"
            f"┌─────────────┐\n"
            f"│   `{code}`   │\n"
            f"└─────────────┘\n\n"
            f"☕ Напиток: {drink['name']}\n"
            f"💎 Списано: {drink['price']} баллов\n"
            f"💰 Остаток: {remaining} баллов\n\n"
            f"⚠️ Код одноразовый",
            parse_mode="Markdown"
        )

    # ── ОЦЕНКА ────────────────────────────────
    elif data.startswith("rate|"):
        _, category, rating_str = data.split("|")
        rating = int(rating_str)

        if not can_rate_today(user.id, category):
            target = "бариста" if category == "barista" else "место"
            await query.edit_message_text(f"⏳ Ты уже оценил {target} сегодня. Возвращайся завтра!")
            return

        points = 2
        save_rating(user.id, rating, category, points)
        total = add_points(user.id, points)
        target = "бариста" if category == "barista" else "место"
        await query.edit_message_text(
            f"{'⭐' * rating} Спасибо за оценку {target}: {rating}/5\n"
            f"✅ Начислено: {points} балла\n"
            f"💎 Всего баллов: {total}"
        )

    # ── БОНУС already ─────────────────────────
    elif data == "bonus|already":
        await query.answer("Ты уже добавил это ранее ✅", show_alert=False)

    # ── БОНУС: запрос поля ────────────────────
    elif data.startswith("bonus|"):
        field = data.split("|")[1]
        prompts = {
            "phone":    "📱 Напиши свой номер телефона (например: +37369123456)",
            "email":    "📧 Напиши свой email (например: name@gmail.com)",
            "birthday": "🎂 Напиши дату рождения (например: 1995.06.15)",
        }
        context.user_data["awaiting_bonus_field"] = field
        await query.edit_message_text(prompts[field])


# ══════════════════════════════
# ОБРАБОТКА ЗАКАЗОВ ИЗ МАГАЗИНА
# ══════════════════════════════

async def handle_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получаем заказ из мини-приложения и пересылаем админу."""
    try:
        data = json.loads(update.message.web_app_data.data)
        order_text = data.get("orderText", "Заказ без деталей")

        # Подтверждение клиенту
        await update.message.reply_text(
            "✅ *Заказ принят!*\n\n"
            "Мы свяжемся с тобой в ближайшее время ☕",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )

        # Уведомление админу
        user = update.effective_user
        admin_msg = (
            f"🛍 *НОВЫЙ ЗАКАЗ ИЗ МАГАЗИНА*\n\n"
            f"👤 Клиент: {user.first_name} (@{user.username})\n"
            f"🆔 TG ID: `{user.id}`\n\n"
            f"{order_text}"
        )
        await context.bot.send_message(
            chat_id=956408409,
            text=admin_msg,
            parse_mode="Markdown",
        )

    except Exception as e:
        log.error(f"web_app_data error: {e}")
        await update.message.reply_text("❌ Ошибка при оформлении заказа. Попробуй ещё раз.")


# ══════════════════════════════
# ОБРАБОТКА ФОТО
# ══════════════════════════════

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username, user.first_name, update.effective_chat.id)

    await update.message.reply_text("📸 Читаю QR код...")

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    file_bytes = await file.download_as_bytearray()

    try:
        resp = requests.post(
            "https://api.qrserver.com/v1/read-qr-code/",
            files={"file": ("qr.jpg", bytes(file_bytes), "image/jpeg")},
            timeout=15,
        )
        qr_data = resp.json()[0]["symbol"][0]["data"]
    except Exception as e:
        await update.message.reply_text("❌ Не удалось прочитать QR код. Попробуй ещё раз.")
        log.error(f"QR error: {e}")
        return

    if not qr_data or "receipt-verifier" not in qr_data:
        await update.message.reply_text("❌ Это не похоже на чек из кассы.")
        return

    try:
        parts = qr_data.split("/")
        price = float(parts[5])
        points = int(price // 10)
        receipt_id = parts[6]
        date = parts[7]
    except Exception:
        await update.message.reply_text("❌ Неверный формат чека.")
        return

    save_receipt(user.id, price, receipt_id, date, points)
    total = add_points(user.id, points)

    await update.message.reply_text(
        f"✅ *Чек обработан!*\n\n"
        f"💰 Сумма: {price} MDL\n"
        f"⭐ Начислено: {points} баллов\n"
        f"💎 Всего баллов: {total}",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


# ══════════════════════════════
# ОБРАБОТКА ТЕКСТА
# ══════════════════════════════

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    text = update.message.text or ""

    ensure_user(user.id, user.username, user.first_name, chat_id)
    db_user = get_user(user.id)

    # ── /start ───────────────────────────────
    if text.strip() == "/start":
        await update.message.reply_text(
            f"☕ Привет, {user.first_name}!\n"
            f"💎 Твой баланс: {db_user['total_points']} баллов\n\n"
            f"Выбери действие 👇",
            reply_markup=main_menu_keyboard(),
        )
        return

    # ── ОЖИДАЕМ БОНУСНОЕ ПОЛЕ ────────────────
    awaiting = context.user_data.get("awaiting_bonus_field")
    if awaiting:
        field_map = {"phone": "contact_number", "email": "email", "birthday": "birthday"}
        db_field = field_map.get(awaiting)

        # Проверяем что поле ещё не заполнено
        if db_user.get(db_field):
            context.user_data.pop("awaiting_bonus_field", None)
            await update.message.reply_text(
                "✅ Это поле уже заполнено!",
                reply_markup=main_menu_keyboard()
            )
            return

        try:
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE telegram_users SET {db_field} = %s WHERE telegram_id = %s",
                        (text.strip(), user.id)
                    )
                    conn.commit()
            total = add_points(user.id, 10)
            context.user_data.pop("awaiting_bonus_field", None)
            labels = {"phone": "Номер телефона", "email": "Email", "birthday": "Дата рождения"}
            await update.message.reply_text(
                f"✅ {labels[awaiting]} сохранён!\n"
                f"🎁 Начислено: 10 баллов\n"
                f"💎 Всего баллов: {total}",
                reply_markup=main_menu_keyboard()
            )
        except Exception as e:
            log.error(f"bonus field save error: {e}")
            await update.message.reply_text("❌ Ошибка сохранения. Попробуй ещё раз.")
        return

    # ── МОЙ БАЛАНС ───────────────────────────
    if text == "💎 Мой баланс":
        await update.message.reply_text(
            f"💎 *Твой баланс: {db_user['total_points']} баллов*\n\n"
            f"1 балл = 1 MDL\n"
            f"Отправь фото чека чтобы заработать ещё!",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        return

    # ── ОТПРАВИТЬ ЧЕК ────────────────────────
    if text == "📸 Отправить чек":
        await update.message.reply_text(
            "📸 Сфотографируй QR-код на чеке и отправь сюда!",
            reply_markup=main_menu_keyboard(),
        )
        return

    # ── ПОТРАТИТЬ БАЛЛЫ ──────────────────────
    if text == "☕ Потратить баллы":
        balance = db_user["total_points"]
        await update.message.reply_text(
            f"☕ *Меню напитков*\n\n"
            f"💎 Твой баланс: {balance} баллов\n"
            f"1 балл = 1 MDL\n\n"
            f"Выбери напиток 👇",
            parse_mode="Markdown",
            reply_markup=drinks_keyboard(),
        )
        return

    # ── ОЦЕНИТЬ БАРИСТА ──────────────────────
    if text == "⭐ Оценить бариста":
        if not can_rate_today(user.id, "barista"):
            await update.message.reply_text("⏳ Ты уже оценил бариста сегодня. Возвращайся завтра!", reply_markup=main_menu_keyboard())
            return
        await update.message.reply_text("Оцени бариста 👇", reply_markup=rating_keyboard("barista"))
        return

    # ── ОЦЕНИТЬ МЕСТО ────────────────────────
    if text == "🏠 Оценить место":
        if not can_rate_today(user.id, "place"):
            await update.message.reply_text("⏳ Ты уже оценил место сегодня. Возвращайся завтра!", reply_markup=main_menu_keyboard())
            return
        await update.message.reply_text("Оцени наше место 👇", reply_markup=rating_keyboard("place"))
        return

    # ── ДОПОЛНИТЕЛЬНЫЕ БАЛЛЫ ─────────────────
    if text == "🎁 Дополнительные баллы":
        filled = sum([
            bool(db_user.get("contact_number")),
            bool(db_user.get("email")),
            bool(db_user.get("birthday")),
        ])
        await update.message.reply_text(
            f"🎁 *Дополнительные баллы*\n\n"
            f"Заполни профиль и получи по +10 баллов за каждое поле!\n"
            f"Заполнено: {filled}/3",
            parse_mode="Markdown",
            reply_markup=bonus_keyboard(db_user),
        )
        return

    # ── ОБЩИЙ ЧАТ ────────────────────────────
    intent = classify_intent(text)
    if intent == "BALANCE":
        await update.message.reply_text(
            f"💎 *Твой баланс: {db_user['total_points']} баллов*",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
    else:
        reply = ask_ai(
            "You are a friendly barista at a specialty coffee shop. "
            "Be warm, concise (2-3 sentences), use coffee vocabulary naturally. "
            "Always speak in Russian.",
            text, chat_id=chat_id
        )
        await update.message.reply_text(reply, reply_markup=main_menu_keyboard())


# ══════════════════════════════
# ЗАПУСК
# ══════════════════════════════

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_web_app_data))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT, handle_text))
    log.info("☕ Client bot started!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
