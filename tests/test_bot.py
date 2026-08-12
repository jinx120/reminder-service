from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.bot import handle_callback, handle_text, send_reminder_message
from app.models import Notification, Reminder, ReminderStatus

NOW = datetime(2026, 8, 12, 12, 0, 0)
CHAT_ID = 987654321


def add(db, **overrides) -> int:
    fields = dict(title="Take pills", due_at=NOW - timedelta(hours=1))
    fields.update(overrides)
    with db.session() as s:
        reminder = Reminder(**fields)
        s.add(reminder)
        s.commit()
        s.refresh(reminder)
        return reminder.id


def load(db, reminder_id: int) -> Reminder:
    with db.session() as s:
        return s.get(Reminder, reminder_id)


def fake_callback_update(data: str, chat_id: int = CHAT_ID):
    query = SimpleNamespace(
        data=data,
        message=SimpleNamespace(chat_id=chat_id, text="⏰ Take pills"),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    return SimpleNamespace(callback_query=query)


def fake_text_update(text: str, chat_id: int = CHAT_ID):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        message=SimpleNamespace(text=text, reply_text=AsyncMock()),
    )


def fake_context():
    return SimpleNamespace(bot=SimpleNamespace(edit_message_text=AsyncMock()))


async def test_send_builds_a_message_with_a_done_button():
    bot = SimpleNamespace(send_message=AsyncMock(
        return_value=SimpleNamespace(message_id=4242)))
    reminder = Reminder(id=7, title="Take pills", note="the blue ones",
                        due_at=NOW, retry_count=1, max_retries=4)

    message_id = await send_reminder_message(bot, CHAT_ID, reminder)

    assert message_id == 4242
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == CHAT_ID
    assert "Take pills" in kwargs["text"]
    assert "the blue ones" in kwargs["text"]
    assert "2/4" in kwargs["text"]
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.callback_data == "ack:7"
    assert "Done" in button.text
    assert "parse_mode" not in kwargs


async def test_send_omits_the_note_line_when_there_is_no_note():
    bot = SimpleNamespace(send_message=AsyncMock(
        return_value=SimpleNamespace(message_id=1)))
    reminder = Reminder(id=1, title="Bare", note=None, due_at=NOW,
                        retry_count=0, max_retries=4)
    await send_reminder_message(bot, CHAT_ID, reminder)
    assert bot.send_message.await_args.kwargs["text"].count("\n\n") == 1


async def test_button_tap_acks_the_reminder(db):
    reminder_id = add(db)
    update = fake_callback_update(f"ack:{reminder_id}")

    await handle_callback(update, fake_context(), db=db, chat_id=CHAT_ID)

    assert load(db, reminder_id).status == ReminderStatus.acked.value
    update.callback_query.answer.assert_awaited()
    edited = update.callback_query.edit_message_text.await_args.kwargs["text"]
    assert "Done" in edited


async def test_button_tap_from_an_unauthorised_chat_changes_nothing(db):
    reminder_id = add(db)
    update = fake_callback_update(f"ack:{reminder_id}", chat_id=111222333)

    await handle_callback(update, fake_context(), db=db, chat_id=CHAT_ID)

    assert load(db, reminder_id).status == ReminderStatus.pending.value
    update.callback_query.edit_message_text.assert_not_awaited()


async def test_second_button_tap_is_harmless(db):
    reminder_id = add(db)
    await handle_callback(fake_callback_update(f"ack:{reminder_id}"),
                          fake_context(), db=db, chat_id=CHAT_ID)

    update = fake_callback_update(f"ack:{reminder_id}")
    await handle_callback(update, fake_context(), db=db, chat_id=CHAT_ID)

    assert load(db, reminder_id).status == ReminderStatus.acked.value
    assert "already" in update.callback_query.edit_message_text.await_args.kwargs["text"]


async def test_malformed_callback_data_is_ignored(db):
    update = fake_callback_update("garbage")
    await handle_callback(update, fake_context(), db=db, chat_id=CHAT_ID)
    update.callback_query.edit_message_text.assert_not_awaited()


async def test_text_reply_acks_the_most_recently_notified_reminder(db):
    add(db, title="old", last_sent_at=NOW - timedelta(minutes=40))
    newest_id = add(db, title="new", last_sent_at=NOW - timedelta(minutes=5))
    update = fake_text_update("done")

    await handle_text(update, fake_context(), db=db, chat_id=CHAT_ID)

    assert load(db, newest_id).status == ReminderStatus.acked.value
    assert "new" in update.message.reply_text.await_args.args[0]


async def test_text_reply_edits_the_original_message_when_known(db):
    reminder_id = add(db, last_sent_at=NOW - timedelta(minutes=5))
    with db.session() as s:
        s.add(Notification(reminder_id=reminder_id, sent_at=NOW - timedelta(minutes=5),
                           telegram_message_id=555))
        s.commit()
    context = fake_context()

    await handle_text(fake_text_update("done"), context, db=db, chat_id=CHAT_ID)

    kwargs = context.bot.edit_message_text.await_args.kwargs
    assert kwargs["chat_id"] == CHAT_ID
    assert kwargs["message_id"] == 555
    assert "Done" in kwargs["text"]


async def test_text_reply_with_nothing_pending_says_so(db):
    update = fake_text_update("hello?")
    await handle_text(update, fake_context(), db=db, chat_id=CHAT_ID)
    assert "Nothing pending" in update.message.reply_text.await_args.args[0]


async def test_text_reply_ignores_reminders_never_sent(db):
    reminder_id = add(db, last_sent_at=None)
    await handle_text(fake_text_update("done"), fake_context(), db=db, chat_id=CHAT_ID)
    assert load(db, reminder_id).status == ReminderStatus.pending.value


async def test_text_from_an_unauthorised_chat_is_ignored(db):
    reminder_id = add(db, last_sent_at=NOW - timedelta(minutes=5))
    update = fake_text_update("done", chat_id=111222333)

    await handle_text(update, fake_context(), db=db, chat_id=CHAT_ID)

    assert load(db, reminder_id).status == ReminderStatus.pending.value
    update.message.reply_text.assert_not_awaited()
