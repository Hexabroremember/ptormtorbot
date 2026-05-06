"""Telegram bot: Hebrew UI for פטור מתור — generates PDF via `replace_fields`."""

from __future__ import annotations

import logging
import os
from io import BytesIO
from pathlib import Path

import fitz
from dotenv import load_dotenv
from pydantic import ValidationError
from PIL import Image
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.error import Conflict

from app.main import (
    OUTPUT_PDF_FILENAME,
    ROOT_DIR,
    GeneratePdfRequest,
    replace_fields,
)

load_dotenv(ROOT_DIR / ".env")

# Public HTTPS origin of the FastAPI app (no path), e.g. https://your-service.onrender.com
WEB_APP_URL = os.environ.get("WEB_APP_URL", "").strip()


def mini_app_entry_url() -> str:
    """URL Telegram opens for the Mini App — ``static/index.html`` on the same host."""
    base = WEB_APP_URL.rstrip("/")
    if not base:
        return ""
    low = base.lower()
    if low.endswith("/static/index.html"):
        return base
    if low.endswith("/index.html"):
        return base
    if low.endswith("/static"):
        return f"{base}/index.html"
    return f"{base}/static/index.html"

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

HEBREW, ENGLISH, ID_NUM, EXP_DATE = range(4)

# Hebrew UI (Telegram HTML). Brand: פטור מתור.
MSG_START = (
    "<b>📋 פטור מתור</b>\n"
    "כאן יוצרים את טופס ה־PDF עם הפרטים שלכם — מהיר ופשוט.\n\n"
    "<b>שלב 1 מתוך 4</b>\n"
    "שלחו את <b>השם המלא בעברית</b> כפי שיודפס על הטופס."
)
MSG_STEP_EN = (
    "<b>שלב 2 מתוך 4</b>\n"
    "שם מלא <b>באנגלית</b>.\n"
    "השם יישמר באותיות גדולות (CAPS), כמו בטופס הרשמי."
)
MSG_STEP_ID = "<b>שלב 3 מתוך 4</b>\nמספר זהות (ספרות בלבד):"
MSG_STEP_EXP = (
    "<b>שלב 4 מתוך 4</b>\n"
    "תאריך תפוגה — למשל: <code>30/04/2029</code>"
)
MSG_EMPTY_HE = "נא לשלוח שם בעברית שאינו ריק."
MSG_WORKING = "<b>מייצר את הקובץ…</b>\nעוד רגע וזה מוכן."
MSG_CANCEL = "בוטל. שלחו /start כדי להתחיל מחדש."
MSG_HELP = (
    "<b>📋 פטור מתור — עזרה</b>\n\n"
    "<b>/start</b> — פותחים את הטופס (אפליקציית ווב בתוך טלגרם, אם הוגדר קישור)\n"
    "<b>/form</b> — ממלאים את אותם שדות בשיחה, שלב אחר שלב\n"
    "<b>/cancel</b> — עצירה באמצע מילוי בצ'אט\n\n"
    "<b>/code</b> — בעל הבוט בלבד: הנפקת קוד אישור תשלום חד פעמי ללקוח.\n\n"
    "יש למלא ארבעה שדות: שם בעברית ובאנגלית, מספר זהות ותאריך תפוגה.\n"
    "בסוף נשלחות שתי תמונות ללא כיתוב (ראשונה מקורית ללא סימן מים, שנייה דחוסה עם סימן מים), "
    "ולבסוף קובץ PDF ללא סימן מים.\n"
    "באפליקציה: התצוגה וההורדה בדף הטופס.\n"
    "השם באנגלית נשמר באותיות גדולות."
)
MSG_WEB_APP_START = (
    "<b>📋 פטור מתור</b>\n"
    "לטופס המלא עם תצוגה חיה — לחצו על הכפתור למטה (נפתח בתוך טלגרם).\n\n"
    "שם מלא בעברית ובאנגלית, מספר זהות, תאריך תפוגה וסימן מים — הכול באותו מסך, "
    "ואז תצוגה מקדימה והורדת PDF.\n\n"
    "<b>רוצים למלא בשיחה?</b> שלחו <code>/form</code>"
)
MSG_ERR_VALID = "נתונים לא תקינים — בדקו את השדות ונסו שוב."
MSG_ERR_GEN = "לא הצלחנו ליצור את הקובץ. נסו שוב או פנו למנהל המערכת."
MSG_DOC_CAP = (
    f"✅ <b>PDF ללא סימן מים</b> — <code>{OUTPUT_PDF_FILENAME}</code> · שני עמודים"
)
BTN_RESTART = "🔄 התחלה מחדש"

# בעל הבוט — רק הוא יכול להנפיק קודי תשלום (פקודה /code).
BOT_OWNER_TELEGRAM_ID = 5319095718


async def cmd_code(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Owner-only: issue a one-time code for external payments (Mini App redemption)."""
    if update.message is None:
        return
    user = update.effective_user
    if user is None or user.id != BOT_OWNER_TELEGRAM_ID:
        await update.message.reply_text("⛔ אין לך הרשאה לפקודה הזו.")
        return
    try:
        from app.payment_codes_store import issue_new_code

        code = issue_new_code()
    except Exception as exc:  # noqa: BLE001
        logger.exception("issue payment code failed")
        await update.message.reply_text(f"❌ שגיאה ביצירת קוד: {exc}")
        return
    await update.message.reply_text(
        "<b>קוד אישור תשלום חד פעמי</b>\n\n"
        f"<code>{code}</code>\n\n"
        "שלח את הקוד ללקוח לאחר שקיבלת תשלום מחוץ למערכת. "
        "הלקוח יזין אותו בשלב התשלום במיני אפ — שימוש יחיד.",
        parse_mode="HTML",
    )


def web_app_reply_keyboard() -> ReplyKeyboardMarkup | None:
    url = mini_app_entry_url()
    if not url:
        return None
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(
                    "📋 פתיחת טופס",
                    web_app=WebAppInfo(url=url),
                )
            ]
        ],
        resize_keyboard=True,
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Public entry: Mini App when WEB_APP_URL is set; otherwise same as chat form."""
    if update.message is None:
        return ConversationHandler.END
    if mini_app_entry_url():
        context.user_data.clear()
        kb = web_app_reply_keyboard()
        await update.message.reply_text(
            MSG_WEB_APP_START,
            parse_mode="HTML",
            reply_markup=kb,
        )
        return ConversationHandler.END
    return await begin_chat_flow(update, context)


async def begin_chat_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Multi-step chat form — /form, restart button, or /start when no Web App URL."""
    context.user_data.clear()
    chat_rm = ReplyKeyboardRemove()
    if update.callback_query is not None:
        q = update.callback_query
        await q.answer()
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        chat = update.effective_chat
        if chat is not None:
            await context.bot.send_message(
                chat_id=chat.id,
                text=MSG_START,
                parse_mode="HTML",
                reply_markup=chat_rm,
            )
        return HEBREW
    if update.message is not None:
        await update.message.reply_text(
            MSG_START,
            parse_mode="HTML",
            reply_markup=chat_rm,
        )
        return HEBREW
    return ConversationHandler.END


async def hebrew(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None or not update.message.text:
        return HEBREW
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text(MSG_EMPTY_HE)
        return HEBREW
    context.user_data["hebrew_full_name"] = text
    await update.message.reply_text(MSG_STEP_EN, parse_mode="HTML")
    return ENGLISH


async def english(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None or not update.message.text:
        return ENGLISH
    context.user_data["english_full_name"] = update.message.text.strip()
    await update.message.reply_text(MSG_STEP_ID, parse_mode="HTML")
    return ID_NUM


async def id_num(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None or not update.message.text:
        return ID_NUM
    context.user_data["id_number"] = update.message.text.strip()
    await update.message.reply_text(MSG_STEP_EXP, parse_mode="HTML")
    return EXP_DATE


async def exp_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None or not update.message.text:
        return EXP_DATE
    context.user_data["expiration_date"] = update.message.text.strip()
    await update.message.reply_text(
        MSG_WORKING,
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )
    return await deliver_generated_pdf(update, context)


async def deliver_generated_pdf(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    ud = context.user_data
    reply = update.effective_message
    try:
        req = GeneratePdfRequest(
            hebrew_full_name=ud["hebrew_full_name"],
            english_full_name=ud["english_full_name"],
            id_number=ud["id_number"],
            expiration_date=ud["expiration_date"],
            watermark=False,
        )
        # Watermarked export first, then plain — plain bytes are snapshotted before any
        # rasterization (fitz may touch stream buffers) and used only for the PDF file.
        pdf_watermarked = replace_fields(
            hebrew_full_name=req.hebrew_full_name,
            english_full_name=req.english_full_name,
            id_number=req.id_number,
            expiration_date=req.expiration_date,
            watermark=True,
        )
        pdf_plain = replace_fields(
            hebrew_full_name=req.hebrew_full_name,
            english_full_name=req.english_full_name,
            id_number=req.id_number,
            expiration_date=req.expiration_date,
            watermark=False,
        )
        wm_snap = bytes(pdf_watermarked)
        plain_snap = bytes(pdf_plain)
    except ValidationError as exc:
        if reply:
            await reply.reply_text(f"{MSG_ERR_VALID}\n\n{exc}")
        context.user_data.clear()
        return ConversationHandler.END
    except FileNotFoundError as exc:
        if reply:
            await reply.reply_text(f"❌ {exc}")
        context.user_data.clear()
        return ConversationHandler.END
    except Exception as exc:  # noqa: BLE001
        logger.exception("PDF generation failed")
        if reply:
            await reply.reply_text(MSG_ERR_GEN)
        context.user_data.clear()
        return ConversationHandler.END

    chat = update.effective_chat
    if chat is None:
        context.user_data.clear()
        return ConversationHandler.END
    again_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(BTN_RESTART, callback_data="restart")]]
    )
    stem = Path(OUTPUT_PDF_FILENAME).stem
    jpg_original = pdf_bytes_to_telegram_jpeg(
        plain_snap,
        zoom=3.5,
        jpeg_quality=92,
        max_long_edge=4096,
    )
    if jpg_original:
        await context.bot.send_photo(
            chat_id=chat.id,
            photo=InputFile(BytesIO(jpg_original), filename=f"{stem}-1.jpg"),
        )

    jpg_compressed_wm = pdf_bytes_to_telegram_jpeg(
        wm_snap,
        zoom=2.25,
        jpeg_quality=78,
        max_long_edge=2000,
    )
    if jpg_compressed_wm:
        await context.bot.send_photo(
            chat_id=chat.id,
            photo=InputFile(BytesIO(jpg_compressed_wm), filename=f"{stem}-2.jpg"),
        )

    await context.bot.send_document(
        chat_id=chat.id,
        document=InputFile(BytesIO(plain_snap), filename=OUTPUT_PDF_FILENAME),
        caption=MSG_DOC_CAP,
        parse_mode="HTML",
        reply_markup=again_kb,
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await update.message.reply_text(MSG_CANCEL)
    context.user_data.clear()
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(MSG_HELP, parse_mode="HTML")


def pdf_bytes_to_telegram_jpeg(
    pdf_bytes: bytes,
    *,
    zoom: float = 2.25,
    jpeg_quality: int = 78,
    max_long_edge: int = 2000,
) -> bytes | None:
    """Rasterize first PDF page to a JPEG, scaled for Telegram."""
    try:
        data = bytes(pdf_bytes)
        doc = fitz.open(stream=data, filetype="pdf")
        mat = fitz.Matrix(zoom, zoom)
        if len(doc) < 1:
            doc.close()
            return None
        pix = doc[0].get_pixmap(matrix=mat, alpha=False)
        doc.close()
        image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

        lw, lh = image.size
        longest = max(lw, lh)
        if longest > max_long_edge:
            r = max_long_edge / longest
            image = image.resize(
                (max(1, int(lw * r)), max(1, int(lh * r))),
                Image.Resampling.LANCZOS,
            )

        buf = BytesIO()
        image.save(
            buf,
            format="JPEG",
            quality=jpeg_quality,
            optimize=True,
            progressive=True,
        )
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        logger.exception("Failed to rasterize PDF for Telegram preview image")
        return None


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "Missing TELEGRAM_BOT_TOKEN. Set it in the environment or in .env "
            f"at {ROOT_DIR / '.env'}"
        )

    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        import warnings

        from telegram.warnings import PTBUserWarning

        warnings.filterwarnings("ignore", category=PTBUserWarning)
    except ImportError:
        pass

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        err = context.error
        if isinstance(err, Conflict):
            logger.warning(
                "Telegram Conflict: only one client may poll getUpdates per bot token.\n"
                "  • Close every other bot window (IDE, Task Manager python.exe).\n"
                "  • If you deployed this bot on a server/hosting, stop that copy.\n"
                "  • Webhooks are cleared on startup here; rerun after ~5s when another poller exited."
            )
            return
        if err:
            logger.exception("Unhandled error in telegram handler", exc_info=err)

    async def post_init(application: Application) -> None:
        await application.bot.delete_webhook(drop_pending_updates=True)
        await application.bot.set_my_commands(
            [
                BotCommand("start", "פתיחת טופס (אפליקציה / צ'אט)"),
                BotCommand("form", "מילוי הטופס בצ'אט, שלב אחר שלב"),
                BotCommand("code", "קוד תשלום חד פעמי (בעלים)"),
                BotCommand("help", "עזרה והסבר קצר"),
                BotCommand("cancel", "ביטול מילוי בצ'אט"),
            ]
        )

    application = Application.builder().token(token).post_init(post_init).build()
    application.add_handler(CommandHandler("code", cmd_code))
    application.add_handler(CommandHandler("help", help_command))
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            CommandHandler("form", begin_chat_flow),
            CallbackQueryHandler(begin_chat_flow, pattern=r"^restart$"),
        ],
        states={
            HEBREW: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, hebrew),
            ],
            ENGLISH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, english),
            ],
            ID_NUM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, id_num),
            ],
            EXP_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, exp_date),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", cmd_start),
            CallbackQueryHandler(begin_chat_flow, pattern=r"^restart$"),
        ],
    )
    application.add_handler(conv)
    application.add_error_handler(on_error)
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
