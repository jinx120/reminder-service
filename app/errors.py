class ServiceError(Exception):
    """Base for every error the business layer raises at its callers.

    Adapters (HTTP router, MCP tools, Telegram handlers) map these to their
    own vocabulary. Nothing below this layer knows about HTTP or MCP.
    """


class ReminderNotFound(ServiceError):
    """No reminder with that id."""


class ReminderNotPending(ServiceError):
    """The reminder is already acked or expired, so it cannot be changed."""


class InvalidRecurrence(ServiceError):
    """The recurrence rule is outside the supported RRULE subset."""


class SnoozeLimitReached(ServiceError):
    """MAX_SNOOZES exceeded for this occurrence."""


class InvalidField(ServiceError):
    """An update named a field that is not editable, or nulled a required one.

    A ServiceError rather than a bare ValueError so it reaches the adapters'
    single error-mapping table instead of surfacing as a 500.
    """


class InvalidTime(ServiceError):
    """A date/time or duration could not be understood.

    Deliberately an error rather than a guess: silently scheduling something
    for the wrong day surfaces days later as a missed reminder.
    """
