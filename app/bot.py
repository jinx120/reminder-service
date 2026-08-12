import logging
from functools import partial

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

from app.db import Database
from app.models import Reminder
from app.service import ack_reminder, find_reply_ack_target, latest_notification
from app.timeutil import as_utc_iso, utcnow

logger = logging.getLogger("reminder.bot")

CALLBACK_PREFIX = "ack:"


def _compose(reminder: Reminder) -> str:
    """Plain-text message body. No parse_mode, so titles never need escaping."""
    lines = [f"⏰ {reminder.title}"]
    if reminder.note:
        lines.append(reminder.note)
    lines.append(
        f"Due {as_utc_iso(reminder.due_at)} · "
        f"attempt {reminder.retry_count + 1}/{reminder.max_retries}"
    )
    return "\n\n".join(lines)


async def send_reminder_message(bot, chat_id: int, reminder: Reminder) -> int:
    """Send one nag with a Done button. Returns the Telegram message id."""
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Done", callback_data=f"{CALLBACK_PREFIX}{reminder.id}")]]
    )
    message = await bot.send_message(
        chat_id=chat_id,
        text=_compose(reminder),
        reply_markup=keyboard,
    )
    return message.message_id


async def handle_callback(update, context, *, db: Database, chat_id: int) -> None:
    """Inline '✅ Done' button tap."""
    query = update.callback_query
    if query.message.chat_id != chat_id:
        logger.warning("ignoring callback from unauthorised chat %s", query.message.chat_id)
        await query.answer("Not authorised.")
        return

    data = query.data or ""
    if not data.startswith(CALLBACK_PREFIX):
        await query.answer()
        return
    try:
        reminder_id = int(data[len(CALLBACK_PREFIX):])
    except ValueError:
        await query.answer()
        return

    await query.answer()
    now = utcnow()
    with db.session() as session:
        acked = ack_reminder(session, reminder_id, now=now)

    original = query.message.text or ""
    suffix = f"✅ Done at {now:%H:%M} UTC" if acked else "(already resolved)"
    # Passing no reply_markup drops the button, so the message cannot be re-tapped.
    await query.edit_message_text(text=f"{original}\n\n{suffix}")


async def handle_text(update, context, *, db: Database, chat_id: int) -> None:
    """Any plain-text reply counts as an ack (spec §5) — no intent parsing."""
    if update.effective_chat.id != chat_id:
        logger.warning("ignoring message from unauthorised chat %s", update.effective_chat.id)
        return

    now = utcnow()
    with db.session() as session:
        target = find_reply_ack_target(session)
        if target is None:
            await update.message.reply_text("Nothing pending.")
            return
        title = target.title
        notification = latest_notification(session, target.id)
        message_id = notification.telegram_message_id if notification else None
        ack_reminder(session, target.id, now=now)

    await update.message.reply_text(f"✅ Marked “{title}” done.")

    if message_id is not None:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"⏰ {title}\n\n✅ Done at {now:%H:%M} UTC",
            )
        except Exception:
            # The original may be too old to edit; the ack itself already stuck.
            logger.info("could not edit message %s for reminder ack", message_id)


def build_application(token: str, chat_id: int, db: Database) -> Application:
    """Wire the long-polling Telegram application.

    The chat filter is a second guard in front of the per-handler check, so an
    unauthorised chat is dropped before any handler body runs.
    """
    application = Application.builder().token(token).build()
    application.add_handler(
        CallbackQueryHandler(partial(handle_callback, db=db, chat_id=chat_id))
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Chat(chat_id=chat_id),
            partial(handle_text, db=db, chat_id=chat_id),
        )
    )
    return application
