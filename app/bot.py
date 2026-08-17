import logging
from functools import partial

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

from app.config import Settings
from app.db import Database
from app.errors import ServiceError
from app.models import Reminder
from app.service import ack_reminder, find_reply_ack_target, latest_notification, snooze_reminder
from app.timeutil import as_local_iso, to_local_naive, utcnow

logger = logging.getLogger("reminder.bot")

CALLBACK_PREFIX = "ack:"
SNOOZE_PREFIX = "snooze:"


def _compose(reminder: Reminder, tz: str) -> str:
    """Plain-text message body. No parse_mode, so titles never need escaping."""
    lines = [f"⏰ {reminder.title}"]
    if reminder.note:
        lines.append(reminder.note)
    if reminder.recurrence:
        lines.append(f"Repeats: {reminder.recurrence}")
    lines.append(
        f"Due {as_local_iso(reminder.due_at, tz)} · "
        f"attempt {reminder.retry_count + 1}/{reminder.max_retries}"
    )
    return "\n\n".join(lines)


async def send_reminder_message(
    bot, chat_id: int, reminder: Reminder, *, tz: str = "UTC", snooze_min: int = 15
) -> int:
    """Send one nag with Done and Snooze buttons. Returns the message id."""
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Done", callback_data=f"{CALLBACK_PREFIX}{reminder.id}"),
        InlineKeyboardButton(
            f"💤 Snooze {snooze_min}m", callback_data=f"{SNOOZE_PREFIX}{reminder.id}"
        ),
    ]])
    message = await bot.send_message(
        chat_id=chat_id,
        text=_compose(reminder, tz),
        reply_markup=keyboard,
    )
    return message.message_id


async def handle_callback(update, context, *, db: Database, settings: Settings) -> None:
    """Inline button tap — either '✅ Done' or '💤 Snooze'."""
    query = update.callback_query
    chat_id = settings.chat_id
    if query.message.chat_id != chat_id:
        logger.warning("ignoring callback from unauthorised chat %s", query.message.chat_id)
        await query.answer("Not authorised.")
        return

    data = query.data or ""
    for prefix in (CALLBACK_PREFIX, SNOOZE_PREFIX):
        if data.startswith(prefix):
            break
    else:
        await query.answer()
        return

    try:
        reminder_id = int(data[len(prefix):])
    except ValueError:
        await query.answer()
        return

    await query.answer()
    now = utcnow()
    original = query.message.text or ""

    if prefix == SNOOZE_PREFIX:
        with db.session() as session:
            try:
                reminder = snooze_reminder(
                    session,
                    reminder_id,
                    default_minutes=settings.default_snooze_min,
                    max_snoozes=settings.max_snoozes,
                    tz=settings.timezone,
                    now=now,
                )
            except ServiceError as exc:
                # A cap or a stale button is normal user behaviour, not a bug.
                await query.edit_message_text(text=f"{original}\n\n💤 {exc}")
                return
            local_due = to_local_naive(reminder.due_at, settings.timezone)
        await query.edit_message_text(text=f"{original}\n\n💤 Snoozed until {local_due:%H:%M}")
        return

    with db.session() as session:
        acked = ack_reminder(session, reminder_id, now=now, tz=settings.timezone)

    local_now = to_local_naive(now, settings.timezone)
    suffix = f"✅ Done at {local_now:%H:%M}" if acked else "(already resolved)"
    # Passing no reply_markup drops the buttons, so the message cannot be re-tapped.
    await query.edit_message_text(text=f"{original}\n\n{suffix}")


async def handle_text(update, context, *, db: Database, settings: Settings) -> None:
    """Any plain-text reply counts as an ack (spec §5) — no intent parsing."""
    chat_id = settings.chat_id
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
        ack_reminder(session, target.id, now=now, tz=settings.timezone)

    await update.message.reply_text(f"✅ Marked “{title}” done.")

    if message_id is not None:
        local_now = to_local_naive(now, settings.timezone)
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"⏰ {title}\n\n✅ Done at {local_now:%H:%M}",
            )
        except Exception:
            # The original may be too old to edit; the ack itself already stuck.
            logger.info("could not edit message %s for reminder ack", message_id)


def build_application(settings: Settings, db: Database) -> Application:
    """Wire the long-polling Telegram application.

    The chat filter is a second guard in front of the per-handler check, so an
    unauthorised chat is dropped before any handler body runs.
    """
    application = Application.builder().token(settings.bot_token).build()
    application.add_handler(
        CallbackQueryHandler(partial(handle_callback, db=db, settings=settings))
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Chat(chat_id=settings.chat_id),
            partial(handle_text, db=db, settings=settings),
        )
    )
    return application
