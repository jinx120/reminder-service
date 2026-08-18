from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot import SNOOZE_PREFIX, handle_callback, handle_text, send_reminder_message
from app.models import Notification, Reminder, ReminderStatus
from app.timeutil import utcnow

NOW = datetime(2026, 8, 12, 12, 0, 0)
CHAT_ID = 987654321


@pytest.fixture
def settings(settings):
    return replace(settings, chat_id=CHAT_ID, bot_token="123:abc")


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


class FakeBot:
    """Minimal stand-in for telegram.Bot — records sends, never touches the network."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=len(self.sent))


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
    buttons = kwargs["reply_markup"].inline_keyboard[0]
    assert buttons[0].callback_data == "ack:7"
    assert "Done" in buttons[0].text
    assert "parse_mode" not in kwargs


async def test_send_omits_the_note_line_when_there_is_no_note():
    bot = SimpleNamespace(send_message=AsyncMock(
        return_value=SimpleNamespace(message_id=1)))
    reminder = Reminder(id=1, title="Bare", note=None, due_at=NOW,
                        retry_count=0, max_retries=4)
    await send_reminder_message(bot, CHAT_ID, reminder)
    assert bot.send_message.await_args.kwargs["text"].count("\n\n") == 1


async def test_button_tap_acks_the_reminder(db, settings):
    reminder_id = add(db)
    update = fake_callback_update(f"ack:{reminder_id}")

    await handle_callback(update, fake_context(), db=db, settings=settings)

    assert load(db, reminder_id).status == ReminderStatus.acked.value
    update.callback_query.answer.assert_awaited()
    edited = update.callback_query.edit_message_text.await_args.kwargs["text"]
    assert "Done" in edited


async def test_button_tap_from_an_unauthorised_chat_changes_nothing(db, settings):
    reminder_id = add(db)
    update = fake_callback_update(f"ack:{reminder_id}", chat_id=111222333)

    await handle_callback(update, fake_context(), db=db, settings=settings)

    assert load(db, reminder_id).status == ReminderStatus.pending.value
    update.callback_query.edit_message_text.assert_not_awaited()


async def test_second_button_tap_is_harmless(db, settings):
    reminder_id = add(db)
    await handle_callback(fake_callback_update(f"ack:{reminder_id}"),
                          fake_context(), db=db, settings=settings)

    update = fake_callback_update(f"ack:{reminder_id}")
    await handle_callback(update, fake_context(), db=db, settings=settings)

    assert load(db, reminder_id).status == ReminderStatus.acked.value
    assert "already" in update.callback_query.edit_message_text.await_args.kwargs["text"]


async def test_malformed_callback_data_is_ignored(db, settings):
    update = fake_callback_update("garbage")
    await handle_callback(update, fake_context(), db=db, settings=settings)
    update.callback_query.edit_message_text.assert_not_awaited()


async def test_text_reply_acks_the_most_recently_notified_reminder(db, settings):
    add(db, title="old", last_sent_at=NOW - timedelta(minutes=40))
    newest_id = add(db, title="new", last_sent_at=NOW - timedelta(minutes=5))
    update = fake_text_update("done")

    await handle_text(update, fake_context(), db=db, settings=settings)

    assert load(db, newest_id).status == ReminderStatus.acked.value
    assert "new" in update.message.reply_text.await_args.args[0]


async def test_text_reply_edits_the_original_message_when_known(db, settings):
    reminder_id = add(db, last_sent_at=NOW - timedelta(minutes=5))
    with db.session() as s:
        s.add(Notification(reminder_id=reminder_id, sent_at=NOW - timedelta(minutes=5),
                           telegram_message_id=555))
        s.commit()
    context = fake_context()

    await handle_text(fake_text_update("done"), context, db=db, settings=settings)

    kwargs = context.bot.edit_message_text.await_args.kwargs
    assert kwargs["chat_id"] == CHAT_ID
    assert kwargs["message_id"] == 555
    assert "Done" in kwargs["text"]


async def test_text_reply_with_nothing_pending_says_so(db, settings):
    update = fake_text_update("hello?")
    await handle_text(update, fake_context(), db=db, settings=settings)
    assert "Nothing pending" in update.message.reply_text.await_args.args[0]


async def test_text_reply_ignores_reminders_never_sent(db, settings):
    reminder_id = add(db, last_sent_at=None)
    await handle_text(fake_text_update("done"), fake_context(), db=db, settings=settings)
    assert load(db, reminder_id).status == ReminderStatus.pending.value


async def test_text_from_an_unauthorised_chat_is_ignored(db, settings):
    reminder_id = add(db, last_sent_at=NOW - timedelta(minutes=5))
    update = fake_text_update("done", chat_id=111222333)

    await handle_text(update, fake_context(), db=db, settings=settings)

    assert load(db, reminder_id).status == ReminderStatus.pending.value
    update.message.reply_text.assert_not_awaited()


async def test_send_offers_both_done_and_snooze():
    bot = FakeBot()
    reminder = Reminder(id=7, title="t", due_at=datetime(2026, 8, 15, 9, 0))
    await send_reminder_message(bot, CHAT_ID, reminder, snooze_min=15)

    buttons = bot.sent[-1]["reply_markup"].inline_keyboard[0]
    assert [b.callback_data for b in buttons] == ["ack:7", "snooze:7"]
    assert "15" in buttons[1].text


async def test_send_shows_the_recurrence_in_the_body():
    bot = FakeBot()
    reminder = Reminder(id=1, title="bins", due_at=datetime(2026, 8, 15, 9, 0),
                        recurrence="FREQ=WEEKLY;BYDAY=TU")
    await send_reminder_message(bot, CHAT_ID, reminder)
    assert "FREQ=WEEKLY;BYDAY=TU" in bot.sent[-1]["text"]


async def test_send_omits_the_recurrence_line_for_a_one_shot():
    bot = FakeBot()
    reminder = Reminder(id=1, title="t", due_at=datetime(2026, 8, 15, 9, 0))
    await send_reminder_message(bot, CHAT_ID, reminder)
    assert "Repeats" not in bot.sent[-1]["text"]


async def test_snooze_button_pushes_the_reminder_out(db, settings):
    reminder_id = add(db)
    update = fake_callback_update(f"{SNOOZE_PREFIX}{reminder_id}")

    await handle_callback(update, fake_context(), db=db, settings=settings)

    with db.session() as session:
        reminder = session.get(Reminder, reminder_id)
        assert reminder.status == ReminderStatus.pending.value
        assert reminder.snooze_count == 1


async def test_snooze_button_uses_the_configured_default(db, settings):
    reminder_id = add(db)
    before = utcnow()

    await handle_callback(
        fake_callback_update(f"{SNOOZE_PREFIX}{reminder_id}"),
        fake_context(), db=db, settings=replace(settings, default_snooze_min=45),
    )

    with db.session() as session:
        due = session.get(Reminder, reminder_id).due_at
    assert timedelta(minutes=44) < due - before < timedelta(minutes=46)


async def test_snooze_beyond_the_cap_is_reported_not_crashed(db, settings):
    reminder_id = add(db, snooze_count=2)
    update = fake_callback_update(f"{SNOOZE_PREFIX}{reminder_id}")

    await handle_callback(update, fake_context(), db=db,
                          settings=replace(settings, max_snoozes=2))

    with db.session() as session:
        assert session.get(Reminder, reminder_id).snooze_count == 2


async def test_done_button_on_a_recurring_reminder_rolls_it_forward(db, settings):
    reminder_id = add(db, due_at=datetime(2026, 8, 15, 9, 0),
                      recurrence="FREQ=DAILY")

    await handle_callback(fake_callback_update(f"ack:{reminder_id}"),
                          fake_context(), db=db, settings=settings)

    with db.session() as session:
        assert session.get(Reminder, reminder_id).status == ReminderStatus.pending.value
