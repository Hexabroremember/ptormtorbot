"""Telegram bot: Hebrew UI for פטור מתור — generates PDF via `replace_fields`."""

from __future__ import annotations

import html
import logging
import os
import re
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

import fitz
from dotenv import load_dotenv
from pydantic import ValidationError
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    KeyboardButton,
    MenuButtonWebApp,
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
from app.activity_store import log_event
from app.admin_auth import admin_ids, effective_admin_secret, mint_admin_tg_sess, mint_user_tg_sess
from app.payment_code_meta import TIER_LABELS as CODE_TIER_LABELS
from app.payment_code_meta import heading_for_issue_key, meta_for_issue_key
from app.pdf_raster import pdf_bytes_to_telegram_jpeg
from app.public_url import effective_public_base_url

load_dotenv(ROOT_DIR / ".env", override=False)


def normalize_https_origin(raw: str) -> str:
    """
    Telegram Web App URLs must be absolute HTTPS. Bare hosts (common in Railway env UI)
    need ``https://`` or the API rejects them (\"only https links are allowed\").
    """
    s = raw.strip()
    if not s:
        return ""
    if re.match(r"^[a-z][a-z0-9+.-]*://", s, re.IGNORECASE):
        if s.lower().startswith("http://"):
            s = "https://" + s[7:]
        return s.rstrip("/")
    return ("https://" + s.lstrip("/")).rstrip("/")


def mini_app_entry_url(telegram_user_id: int | None = None) -> str:
    """URL Telegram opens for the Mini App — ``static/index.html`` on the same host.

    When a user-specific keyboard button is sent, include a signed user session so
    payment callbacks can still DM the user if Telegram initData is unavailable.

    When ``telegram_user_id`` is omitted (e.g. global chat menu button), no ``tg_user_sess``
    is appended — Telegram does not provide a user id at menu registration time.
    """
    base = normalize_https_origin(effective_public_base_url())
    if not base:
        return ""
    low = base.lower()
    if low.endswith("/static/index.html") or low.endswith("/index.html"):
        url = base
    elif low.endswith("/static"):
        url = f"{base}/index.html"
    else:
        url = f"{base}/static/index.html"
    if telegram_user_id is None:
        return url
    sess = mint_user_tg_sess(telegram_user_id)
    if not sess:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}tg_user_sess={quote(sess, safe='')}"


# Deprecated for URL building — use ``effective_public_base_url()`` / ``mini_app_entry_url()`` instead.
WEB_APP_URL = os.environ.get("WEB_APP_URL", "").strip()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# True after first Conflict log — PTB may retry polling and emit many identical errors.
_conflict_warning_emitted = False

HEBREW, ENGLISH, ID_NUM, EXP_DATE = range(4)

# Hebrew UI (Telegram HTML). Brand: פטור מתור — טון רשמי ואחיד, עם אימוג'י לפי סוג.
MSG_START = (
    "<b>📋 הנפקת פטור מתור</b>\n\n"
    "📄 תצוגה מקדימה חיה לפני תשלום\n"
    "⚡ תהליך מהיר ופשוט\n"
    "⬇️ הורדה מיידית של קובץ PDF\n"
    "📱 זמין ישירות מהטלפון\n"
    "🔄 אפשר להוריד שוב בכל זמן\n"
    "🌍 מתאים גם לשימוש בחו״ל\n"
    "🔒 תהליך פרטי ונוח\n"
    "🕒 מוכן תוך דקות\n\n"
    "<b>שלב 1 מתוך 4</b>\n"
    "שלחו את <b>השם המלא בעברית</b> כפי שיופיע בהדפסה."
)
MSG_STEP_EN = (
    "<b>שלב 2 מתוך 4</b>\n"
    "שלחו את <b>השם המלא באנגלית</b>.\n"
    "השם יישמר באותיות גדולות (CAPS), בהתאם לפורמט הרשמי."
)
MSG_STEP_ID = "<b>שלב 3 מתוך 4</b>\nשלחו את <b>מספר הזהות</b> (ספרות בלבד)."
MSG_STEP_EXP = (
    "<b>שלב 4 מתוך 4</b>\n"
    "שלחו את <b>תאריך התפוגה</b> להדפסה — לדוגמה: <code>30/04/2029</code>"
)
MSG_EMPTY_HE = "⚠️ יש להזין שם בעברית שאינו ריק."
MSG_WORKING = (
    "<b>⏳ מעבדים את הבקשה</b>\n"
    "נוצר קובץ PDF — נא להמתין רגע."
)
MSG_CANCEL = (
    "◀️ <b>הפעולה בוטלה</b>\n\n"
    "לפתיחה מחדש של השירות שלחו את הפקודה /start."
)
MSG_HELP = (
    "<b>📖 פטור מתור — מדריך קצר</b>\n\n"
    "<b>📌 כפתורי המקלדת</b>\n"
    "• <b>📋 הנפקת פטור מתור</b> — פותח את המיני־אפליקציה בתוך טלגרם.\n"
    "• <b>❓ עזרה</b> — קישור למסך העזרה.\n"
    "בתפריט הצ'אט ייתכן גם כפתור <b>📋 טופס PDF</b> (כאשר הוגדר).\n\n"
    "<b>פקודות</b>\n"
    "• <code>/start</code> — תפריט ראשי והסבר.\n"
    "• <code>/cancel</code> — יציאה ממילוי בצ'אט (כאשר לא משתמשים במיני־אפ).\n"
    "• <code>/code</code> — הנפקת קוד תשלום (מנהלים בלבד).\n\n"
    "<b>תהליך בצ'אט</b>\n"
    "לאחר מילוי ארבעת השדות נשלחות תמונות תצוגה ולאחריהן קובץ PDF ללא סימן מים.\n"
    "במיני־אפליקציה קיימות תצוגה מקדימה והורדה לפי שלבי התשלום.\n"
    "השם באנגלית נשמר באותיות גדולות."
)
MSG_WEB_APP_START = (
    "<b>📋 הנפקת פטור מתור</b>\n\n"
    "📄 תצוגה מקדימה חיה לפני תשלום\n"
    "⚡ תהליך מהיר ופשוט\n"
    "⬇️ הורדה מיידית של קובץ PDF\n"
    "📱 זמין ישירות מהטלפון\n"
    "🔄 אפשר להוריד שוב בכל זמן\n"
    "🌍 מתאים גם לשימוש בחו״ל\n"
    "🔒 תהליך פרטי ונוח\n"
    "🕒 מוכן תוך דקות"
)
MSG_REPLY_KEYBOARD_HINT = (
    "⌨️ <b>ניתן לפתוח את הטופס גם מהכפתורים בתחתית המסך.</b>"
)
MSG_ERR_VALID = "⚠️ הנתונים אינם תקינים. נא לבדוק את השדות ולנסות שוב."
MSG_ERR_GEN = (
    "❌ לא ניתן ליצור את הקובץ כעת.\n"
    "נא לנסות שוב; אם הבעיה נמשכת, יש לפנות למנהל המערכת."
)
MSG_DOC_CAP = (
    f"✅ <b>קובץ PDF ללא סימן מים</b>\n"
    f"<code>{OUTPUT_PDF_FILENAME}</code> · שני עמודים"
)
BTN_RESTART = "🔄 התחלה מחדש"

# Reply keyboard labels + external help (Telegram magic link).
BTN_ISSUE_FORM = "📋 הנפקת פטור מתור"
BTN_HELP = "❓ עזרה"


def code_issue_inline_keyboard() -> InlineKeyboardMarkup:
    """בחירת סוג קוד: גלובלי או לפי חבילת תוקף."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🌐 קוד גלובלי · כל התקופות",
                    callback_data="ci:g",
                )
            ],
            [
                InlineKeyboardButton(
                    CODE_TIER_LABELS["300"],
                    callback_data="ci:300",
                ),
                InlineKeyboardButton(
                    CODE_TIER_LABELS["500"],
                    callback_data="ci:500",
                ),
            ],
            [
                InlineKeyboardButton(
                    CODE_TIER_LABELS["900"],
                    callback_data="ci:900",
                ),
                InlineKeyboardButton(
                    CODE_TIER_LABELS["1200"],
                    callback_data="ci:1200",
                ),
            ],
            [
                InlineKeyboardButton(
                    CODE_TIER_LABELS["1500"],
                    callback_data="ci:1500",
                )
            ],
        ]
    )
HELP_TELEGRAM_WEB_URL = "https://t.me/m/5jdTPOGGZWEx"

# בעל הבוט — רק מנהלים יכולים להנפיק קודי תשלום (פקודה /code).
BOT_OWNER_TELEGRAM_ID = int(os.environ.get("BOT_OWNER_TELEGRAM_ID", "5319095718"))


def admin_mini_app_url_for_user(telegram_user_id: int | None) -> str:
    """Admin Web App URL. For admins, embeds tg_sess so the API trusts Telegram without initData."""
    base = normalize_https_origin(effective_public_base_url())
    if not base:
        return ""
    url = f"{base}/admin"
    if telegram_user_id is None or telegram_user_id not in admin_ids():
        return url
    sess = mint_admin_tg_sess(telegram_user_id)
    if not sess:
        return url
    return f"{url}?tg_sess={quote(sess, safe='')}"


async def cmd_code(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """מנהלים: תפריט הנפקת קוד תשלום (גלובלי או לפי תקופת תוקף)."""
    if update.message is None:
        return
    user = update.effective_user
    if user is None or user.id not in admin_ids():
        await update.message.reply_text(
            "🔒 <b>אין הרשאה</b>\n\nפקודה זו מיועדת למנהלי המערכת בלבד.",
            parse_mode="HTML",
        )
        return
    await update.message.reply_text(
        "🔑 <b>הנפקת קוד אישור תשלום חד־פעמי</b>\n\n"
        "נא לבחור את סוג הקוד:\n"
        "• <b>קוד גלובלי</b> — מתאים לכל בחירת תקופת תוקף במיני־אפליקציה.\n"
        "• <b>קוד לפי תקופה</b> — הלקוח נדרש לבחור במיני־אפליקציה את אותה חבילת תוקף "
        "שהונפקה עבור הקוד.\n\n"
        "לאחר הבחירה יוצג הקוד להעתקה ולשליחה ללקוח לאחר קבלת התשלום.",
        parse_mode="HTML",
        reply_markup=code_issue_inline_keyboard(),
    )


async def callback_issue_payment_code(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """מנהלים: יצירת קוד לאחר לחיצה על כפתור סוג ההנפקה."""
    q = update.callback_query
    if q is None or not q.data:
        return
    user = update.effective_user
    if user is None or user.id not in admin_ids():
        await q.answer(text="אין הרשאה לפעולה זו.", show_alert=True)
        return
    raw = q.data.strip()
    if not raw.startswith("ci:"):
        return
    suffix = raw[3:]
    from app.payment_codes_store import issue_new_code

    meta: dict[str, object]
    heading: str
    try:
        if suffix == "g":
            issue_key = "global"
        elif suffix in CODE_TIER_LABELS:
            issue_key = suffix
        else:
            await q.answer(text="בחירה לא תקינה.", show_alert=True)
            return

        meta = meta_for_issue_key(issue_key)
        heading = heading_for_issue_key(issue_key)
        code = issue_new_code(meta=meta)
        log_event(
            "payment_code_issued",
            source="telegram_bot",
            telegram_user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            meta={
                "issue_scope": meta.get("issue_scope"),
                "expiry_option": meta.get("expiry_option"),
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("issue payment code failed")
        await q.answer(text="שגיאה ביצירת הקוד.", show_alert=True)
        if q.message:
            await q.message.reply_text(
                f"❌ <b>שגיאה ביצירת הקוד</b>\n\n<pre>{html.escape(str(exc))}</pre>",
                parse_mode="HTML",
            )
        return

    await q.answer(text="הקוד הונפק בהצלחה.", show_alert=False)
    if q.message:
        await q.message.reply_text(
            "✅ <b>קוד אישור תשלום הונפק</b>\n\n"
            f"<b>סוג:</b> {html.escape(heading)}\n\n"
            f"<pre>{html.escape(code)}</pre>\n\n"
            "יש להעביר את הקוד ללקוח לאחר קבלת התשלום בפועל.\n"
            "הקוד מיועד לשימוש חד־פעמי בלבד.",
            parse_mode="HTML",
        )


def web_app_reply_keyboard() -> ReplyKeyboardMarkup | None:
    """Bottom reply keyboard: Mini App + help (text opens link via handler)."""
    return web_app_reply_keyboard_for_user(None)


def web_app_reply_keyboard_for_user(user_id: int | None) -> ReplyKeyboardMarkup | None:
    """Bottom reply keyboard: public Mini App; admin Mini App only for admins."""
    mini = mini_app_entry_url(user_id)
    if not mini:
        return None
    rows = [
        [
            KeyboardButton(
                BTN_ISSUE_FORM,
                web_app=WebAppInfo(url=mini),
            ),
        ],
    ]
    if user_id in admin_ids():
        admin_url = admin_mini_app_url_for_user(user_id)
        rows.append(
            [
                KeyboardButton(
                    "🛡 פאנל ניהול",
                    web_app=WebAppInfo(url=admin_url),
                ),
            ]
        )
    rows.append([KeyboardButton(BTN_HELP)])
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="ניתן לבחור מהכפתורים למטה או להקליד כאן…",
    )


def mini_app_issue_inline_markup(user_id: int | None) -> InlineKeyboardMarkup | None:
    """כפתור מוטבע לפתיחת המיני־אפ — מתחת להודעה; ``None`` כשאין כתובת ציבורית."""
    mini = mini_app_entry_url(user_id)
    if not mini:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(BTN_ISSUE_FORM, web_app=WebAppInfo(url=mini))]]
    )


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """מנהלים: קישור לפאנל הניהול וקוד גיבוי לדפדפן."""
    if update.message is None:
        return
    user = update.effective_user
    if user is None or user.id not in admin_ids():
        await update.message.reply_text(
            "🔒 <b>אין הרשאה</b>\n\nפקודה זו מיועדת למנהלי המערכת בלבד.",
            parse_mode="HTML",
        )
        return
    admin_url = admin_mini_app_url_for_user(user.id)
    if not admin_url:
        await update.message.reply_text(
            "⚠️ לא נמצאה כתובת ציבורית לשרת "
            "(יש להגדיר WEB_APP_URL או דומיין בסביבת ההפעלה).\n"
            "ללא כתובת זו לא ניתן לפתוח את פאנל הניהול.",
            parse_mode="HTML",
        )
        return
    secret = effective_admin_secret()
    secret_line = (
        f"\n\n🔑 <b>קוד גיבוי (פתיחה מדפדפן חיצוני בלבד):</b>\n<code>{secret}</code>"
        if secret
        else "\n\n⚠️ לא מוגדר TELEGRAM_BOT_TOKEN — קוד גיבוי אינו זמין."
    )
    await update.message.reply_text(
        f"🛡 <b>פאנל ניהול — גישה למנהלים</b>{secret_line}\n\n"
        "לפתיחה מאובטחת מתוך טלגרם לחצו על הכפתור למטה.\n"
        "אם נפתח חלון מחוץ לטלגרם, השתמשו בקוד הגיבוי לעיל.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🛡 פתיחת פאנל ניהול", web_app=WebAppInfo(url=admin_url))]]
        ),
    )


async def help_keyboard_open_link(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Reply keyboard \"עזרה\": reply keyboards cannot embed URLs — follow with an inline link."""
    if update.message is None:
        return
    await update.message.reply_text(
        "📖 <b>עזרה</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("📖 פתיחת מסך העזרה", url=HELP_TELEGRAM_WEB_URL)]]
        ),
    )


def _help_then_state(return_state: int):
    async def _handler(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        await help_keyboard_open_link(update, context)
        return return_state

    return _handler


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Public entry: Mini App when WEB_APP_URL is set; otherwise same as chat form."""
    if update.message is None:
        return ConversationHandler.END
    user = update.effective_user
    log_event(
        "bot_start",
        source="telegram_bot",
        telegram_user_id=user.id if user else None,
        username=user.username if user else None,
        first_name=user.first_name if user else None,
    )
    if mini_app_entry_url():
        context.user_data.clear()
        uid = user.id if user else None
        kb = web_app_reply_keyboard_for_user(uid)
        inline = mini_app_issue_inline_markup(uid)
        await update.message.reply_text(
            MSG_WEB_APP_START,
            parse_mode="HTML",
            reply_markup=inline,
        )
        if kb:
            await update.message.reply_text(
                MSG_REPLY_KEYBOARD_HINT,
                parse_mode="HTML",
                reply_markup=kb,
            )
        return ConversationHandler.END
    return await begin_chat_flow(update, context)


async def begin_chat_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Multi-step chat form — restart button, or /start when no Web App URL."""
    context.user_data.clear()
    user = update.effective_user
    log_event(
        "bot_form_started",
        source="telegram_bot",
        telegram_user_id=user.id if user else None,
        username=user.username if user else None,
        first_name=user.first_name if user else None,
    )
    uid = user.id if user else None
    bottom_kb = web_app_reply_keyboard_for_user(uid)
    chat_rm = bottom_kb or ReplyKeyboardRemove()
    inline = mini_app_issue_inline_markup(uid)
    if update.callback_query is not None:
        q = update.callback_query
        await q.answer()
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        chat = update.effective_chat
        if chat is None:
            return ConversationHandler.END
        if inline:
            await context.bot.send_message(
                chat_id=chat.id,
                text=MSG_START,
                parse_mode="HTML",
                reply_markup=inline,
            )
            if bottom_kb:
                await context.bot.send_message(
                    chat_id=chat.id,
                    text=MSG_REPLY_KEYBOARD_HINT,
                    parse_mode="HTML",
                    reply_markup=bottom_kb,
                )
        else:
            await context.bot.send_message(
                chat_id=chat.id,
                text=MSG_START,
                parse_mode="HTML",
                reply_markup=chat_rm,
            )
        return HEBREW
    if update.message is not None:
        if inline:
            await update.message.reply_text(
                MSG_START,
                parse_mode="HTML",
                reply_markup=inline,
            )
            if bottom_kb:
                await update.message.reply_text(
                    MSG_REPLY_KEYBOARD_HINT,
                    parse_mode="HTML",
                    reply_markup=bottom_kb,
                )
        else:
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
        reply_markup=web_app_reply_keyboard_for_user(update.effective_user.id if update.effective_user else None)
        or ReplyKeyboardRemove(),
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
    except FileNotFoundError:
        if reply:
            await reply.reply_text(
                "❌ <b>חסר קובץ נדרש בשרת</b>\n\nנא לפנות למנהל המערכת.",
                parse_mode="HTML",
            )
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
    user = update.effective_user
    log_event(
        "bot_pdf_delivered",
        source="telegram_bot",
        telegram_user_id=user.id if user else None,
        username=user.username if user else None,
        first_name=user.first_name if user else None,
    )
    reopen_kb = web_app_reply_keyboard_for_user(user.id if user else None)
    if reopen_kb:
        await context.bot.send_message(
            chat_id=chat.id,
            text=(
                "⌨️ <b>להמשך שימוש בטופס</b>\n\n"
                "ניתן לפתוח שוב את המיני־אפליקציה באמצעות הכפתורים בתחתית המסך "
                "(📋 הנפקת פטור מתור / ❓ עזרה) או באמצעות כפתור התפריט "
                "<b>📋 טופס PDF</b>."
            ),
            parse_mode="HTML",
            reply_markup=reopen_kb,
        )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        user = update.effective_user
        kb = web_app_reply_keyboard_for_user(user.id if user else None)
        await update.message.reply_text(
            MSG_CANCEL,
            reply_markup=kb if kb else ReplyKeyboardRemove(),
        )
    context.user_data.clear()
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        user = update.effective_user
        kb = web_app_reply_keyboard_for_user(user.id if user else None)
        await update.message.reply_text(
            MSG_HELP,
            parse_mode="HTML",
            reply_markup=kb,
        )


def _run_polling_with_token(token: str) -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        import warnings

        from telegram.warnings import PTBUserWarning

        warnings.filterwarnings("ignore", category=PTBUserWarning)
    except ImportError:
        pass

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        global _conflict_warning_emitted  # noqa: PLW0603
        err = context.error
        if isinstance(err, Conflict):
            if not _conflict_warning_emitted:
                _conflict_warning_emitted = True
                logger.warning(
                    "Telegram Conflict: only one client may poll getUpdates per bot token.\n"
                    "  • Stop your local bot / second Railway service using the same TELEGRAM_BOT_TOKEN.\n"
                    "  • Or set START_TELEGRAM_BOT_SUBPROCESS=0 on one deployment so only one poller runs.\n"
                    "  • Wait ~10s after stopping the other poller, then redeploy or restart this service."
                )
            return
        if err:
            logger.exception("Unhandled error in telegram handler", exc_info=err)

    async def post_init(application: Application) -> None:
        await application.bot.delete_webhook(drop_pending_updates=True)
        logger.info(
            "[telegram:bot] webhook cleared drop_pending_updates=True mode=polling "
            "(START_TELEGRAM_BOT_SUBPROCESS / standalone bot process)"
        )
        await application.bot.set_my_commands(
            [
                BotCommand("start", "פותח את השירות (מיני־אפ או צ'אט)"),
                BotCommand("code", "הנפקת קוד תשלום (מנהלים בלבד)"),
                BotCommand("admin", "פאנל ניהול (מנהלים בלבד)"),
                BotCommand("help", "מדריך והסבר קצר"),
                BotCommand("cancel", "ביטול מילוי בצ'אט"),
            ]
        )
        menu_url = mini_app_entry_url()
        if menu_url:
            try:
                await application.bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp(
                        text="📋 טופס PDF",
                        web_app=WebAppInfo(url=menu_url),
                    ),
                )
                logger.info("[telegram:bot] menu button set web_app_url_configured=True")
            except Exception as exc:  # noqa: BLE001
                logger.warning("[telegram:bot] set_chat_menu_button failed: %s", exc)
        else:
            logger.warning(
                "[telegram:bot] menu button skipped web_app_url_configured=False "
                "(set WEB_APP_URL / public domain for Mini App entry)"
            )

    application = Application.builder().token(token).post_init(post_init).build()
    application.add_handler(CallbackQueryHandler(callback_issue_payment_code, pattern=r"^ci:"))
    application.add_handler(CommandHandler("code", cmd_code))
    application.add_handler(CommandHandler("admin", cmd_admin))
    application.add_handler(CommandHandler("help", help_command))
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            CallbackQueryHandler(begin_chat_flow, pattern=r"^restart$"),
        ],
        states={
            HEBREW: [
                MessageHandler(filters.Regex("^❓ עזרה$"), _help_then_state(HEBREW)),
                MessageHandler(filters.TEXT & ~filters.COMMAND, hebrew),
            ],
            ENGLISH: [
                MessageHandler(filters.Regex("^❓ עזרה$"), _help_then_state(ENGLISH)),
                MessageHandler(filters.TEXT & ~filters.COMMAND, english),
            ],
            ID_NUM: [
                MessageHandler(filters.Regex("^❓ עזרה$"), _help_then_state(ID_NUM)),
                MessageHandler(filters.TEXT & ~filters.COMMAND, id_num),
            ],
            EXP_DATE: [
                MessageHandler(filters.Regex("^❓ עזרה$"), _help_then_state(EXP_DATE)),
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
    application.add_handler(MessageHandler(filters.Regex("^❓ עזרה$"), help_keyboard_open_link))
    application.add_error_handler(on_error)
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


def run_bot_process_entry(token: str) -> None:
    """Multiprocessing entry: polling runs on the child process main thread (asyncio-safe)."""
    try:
        _run_polling_with_token(token)
    except Exception:  # noqa: BLE001
        logger.exception("Telegram bot subprocess exited with an error")


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "Missing TELEGRAM_BOT_TOKEN. Set it in the environment or in .env "
            f"at {ROOT_DIR / '.env'}"
        )
    _run_polling_with_token(token)


if __name__ == "__main__":
    main()
