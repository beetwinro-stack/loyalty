import logging
import psycopg2
import psycopg2.extras
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters, CommandHandler

# ─── НАСТРОЙКИ ───────────────────────────────
BARISTA_TOKEN = "7859104360:AAHxkUf033YYdMfM6Ph54O-sfAByN9RZbek"  # создай нового бота через @BotFather

DB = {
    "host": "aws-1-eu-west-1.pooler.supabase.com",
    "port": 6543,
    "database": "postgres",
    "user": "postgres.bzrffecjkseqkymefaeb",
    "password": "8s%y6t&TfDfq%5#",
}
# ─────────────────────────────────────────────

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)


def db():
    return psycopg2.connect(**DB)


def verify_and_use_code(code: str):
    """
    Проверяет код. Возвращает:
      - dict с инфо о заказе если код валидный
      - "used"    если код уже использован
      - "expired" если код старше 30 минут
      - None      если код не найден
    """
    try:
        with db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT r.*, u.first_name, u.username
                    FROM redemptions r
                    LEFT JOIN telegram_users u ON u.telegram_id = r.telegram_id
                    WHERE UPPER(r.code) = UPPER(%s)
                """, (code.strip(),))
                row = cur.fetchone()

                if not row:
                    return None

                if row["used"]:
                    return "used"

                # Помечаем как использованный
                cur.execute("""
                    UPDATE redemptions SET used = true, used_at = NOW()
                    WHERE UPPER(code) = UPPER(%s)
                """, (code.strip(),))
                conn.commit()

                return dict(row)
    except Exception as e:
        log.error(f"verify_code error: {e}")
        return None


def barista_keyboard():
    return ReplyKeyboardMarkup(
        [["🔍 Проверить код"]],
        resize_keyboard=True
    )


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "☕ Привет! Это бот для бариста.\n\n"
        "Введи код который показывает клиент 👇",
        reply_markup=barista_keyboard()
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == "🔍 Проверить код":
        await update.message.reply_text("Введи 6-значный код клиента:")
        return

    # Если похоже на код (6 символов)
    if len(text) == 6 and text.replace(" ", "").isalnum():
        result = verify_and_use_code(text)

        if result is None:
            await update.message.reply_text(
                "❌ Код не найден. Проверь правильность ввода."
            )
        elif result == "used":
            await update.message.reply_text(
                "⚠️ Этот код уже был использован!"
            )
        else:
            client_name = result.get("first_name") or result.get("username") or "Клиент"
            await update.message.reply_text(
                f"✅ *КОД ДЕЙСТВИТЕЛЕН!*\n\n"
                f"👤 Клиент: {client_name}\n"
                f"☕ Напиток: {result['drink_name']}\n"
                f"💎 Списано баллов: {result['points_spent']}\n\n"
                f"Приготовь напиток! 🎉",
                parse_mode="Markdown"
            )
    else:
        await update.message.reply_text(
            "Введи 6-значный код клиента (буквы и цифры).\n"
            "Например: A8K3J2"
        )


def main():
    app = ApplicationBuilder().token(BARISTA_TOKEN).build()
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(MessageHandler(filters.TEXT, handle_text))
    log.info("☕ Barista bot started!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
