const API = "/api/reminders";
const POLL_MS = 10000;
const UNDO_MS = 6000;

let config = { timezone: "UTC", default_snooze_min: 15, max_snoozes: 20 };
let reminders = [];
let editingId = null;
let filterText = "";
const pendingDeletes = new Map();   // id -> timeout handle

// --- formatting ----------------------------------------------------------

/** YYYY-MM-DD for an instant, in the server's timezone. */
function dayKey(date) {
  return new Intl.DateTimeFormat("en-CA", { timeZone: config.timezone }).format(date);
}

/** Absolute time in the server's timezone. */
function formatAbsolute(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString([], {
    timeZone: config.timezone,
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

/** "in 2h" / "3 days ago" — the reading that actually answers "is this urgent?". */
function formatRelative(iso, now = new Date()) {
  if (!iso) return "";
  const seconds = Math.round((new Date(iso) - now) / 1000);
  const units = [
    ["day", 86400], ["hour", 3600], ["minute", 60], ["second", 1],
  ];
  const relative = new Intl.RelativeTimeFormat([], { numeric: "auto", style: "narrow" });
  for (const [unit, size] of units) {
    if (Math.abs(seconds) >= size || unit === "second") {
      return relative.format(Math.round(seconds / size), unit);
    }
  }
  return "";
}

const FREQ_NOUNS = { DAILY: "day", WEEKLY: "week", MONTHLY: "month", YEARLY: "year" };
const DAY_NAMES = {
  MO: "Mon", TU: "Tue", WE: "Wed", TH: "Thu", FR: "Fri", SA: "Sat", SU: "Sun",
};

/** "FREQ=WEEKLY;BYDAY=MO,WE" -> "every week on Mon, Wed". */
function describeRecurrence(rule) {
  if (!rule) return "";
  const parts = Object.fromEntries(
    rule.split(";").filter(Boolean).map((chunk) => {
      const [key, value] = chunk.split("=");
      return [key.toUpperCase(), (value || "").toUpperCase()];
    })
  );
  const noun = FREQ_NOUNS[parts.FREQ];
  if (!noun) return rule;
  const interval = Number(parts.INTERVAL || 1);
  let text = interval === 1 ? `every ${noun}` : `every ${interval} ${noun}s`;
  if (parts.BYDAY) {
    const days = parts.BYDAY.split(",").map((code) => DAY_NAMES[code] || code);
    text += ` on ${days.join(", ")}`;
  }
  return text;
}

// --- toasts --------------------------------------------------------------

function toast(message, { error = false, actionLabel = null, onAction = null } = {}) {
  const element = document.createElement("div");
  element.className = error ? "toast error" : "toast";

  const text = document.createElement("span");
  text.textContent = message;                       // textContent, never innerHTML
  element.append(text);

  const dismiss = () => element.remove();

  if (actionLabel) {
    const action = document.createElement("button");
    action.textContent = actionLabel;
    action.addEventListener("click", () => { onAction(); dismiss(); });
    element.append(action);
  }

  document.getElementById("toasts").append(element);
  setTimeout(dismiss, error ? 6000 : UNDO_MS);
  return element;
}

/** Turn a failed response into the server's own message where there is one. */
async function reportFailure(response, fallback) {
  const body = await response.json().catch(() => null);
  const detail = body && body.detail;
  toast(typeof detail === "string" ? detail : fallback, { error: true });
}

// --- data ----------------------------------------------------------------

async function loadConfig() {
  const response = await fetch("/api/config");
  if (!response.ok) return;
  config = await response.json();
  const hint = document.getElementById("due-hint");
  hint.textContent =
    `Times are read in ${config.timezone}. Try "tomorrow at 9am" or "in 2 hours".`;
}

async function loadReminders() {
  const response = await fetch(API);
  if (!response.ok) {
    toast("Could not load reminders.", { error: true });
    return;
  }
  reminders = await response.json();
  render();
}

// --- rendering -----------------------------------------------------------

function groupOf(reminder, now) {
  if (reminder.status !== "pending") return "done";
  const due = new Date(reminder.due_at);
  if (due < now) return "overdue";
  return dayKey(due) === dayKey(now) ? "today" : "upcoming";
}

function matchesFilter(reminder) {
  if (!filterText) return true;
  const haystack = `${reminder.title} ${reminder.note || ""}`.toLowerCase();
  return haystack.includes(filterText);
}

function render() {
  const now = new Date();
  const buckets = { overdue: [], today: [], upcoming: [], done: [] };
  for (const reminder of reminders) {
    if (pendingDeletes.has(reminder.id)) continue;
    if (!matchesFilter(reminder)) continue;
    buckets[groupOf(reminder, now)].push(reminder);
  }

  let total = 0;
  for (const section of document.querySelectorAll(".group")) {
    const bucket = buckets[section.dataset.group];
    const cards = section.querySelector(".cards");
    cards.replaceChildren(...bucket.map((r) => buildCard(r, now)));
    section.hidden = bucket.length === 0;
    total += bucket.length;
  }
  document.getElementById("empty").hidden = total > 0;
}

function span(text, className) {
  const element = document.createElement("span");
  element.textContent = text;
  if (className) element.className = className;
  return element;
}

function actionButton(label, className, handler) {
  const button = document.createElement("button");
  button.textContent = label;
  if (className) button.className = className;
  button.addEventListener("click", handler);
  return button;
}

function buildCard(reminder, now) {
  const card = document.createElement("div");
  card.className = `card ${reminder.status}`;
  if (reminder.status === "pending" && new Date(reminder.due_at) < now) {
    card.classList.add("is-overdue");
  }

  const head = document.createElement("div");
  head.className = "card-head";
  head.append(span(reminder.title, "title"));
  if (reminder.recurrence) {
    head.append(span(describeRecurrence(reminder.recurrence), "badge repeat"));
  }
  head.append(span(reminder.status, "badge"));
  card.append(head);

  if (reminder.note) {
    const note = document.createElement("p");
    note.className = "note";
    note.textContent = reminder.note;
    card.append(note);
  }

  const meta = document.createElement("div");
  meta.className = "meta";
  meta.append(
    span(formatRelative(reminder.due_at, now), "relative"),
    span(formatAbsolute(reminder.due_at)),
    span(`sent ${reminder.retry_count}/${reminder.max_retries}`),
    span(`every ${reminder.retry_interval_min}m`),
  );
  if (reminder.snooze_count > 0) {
    meta.append(span(`snoozed ${reminder.snooze_count}×`));
  }
  card.append(meta);

  const actions = document.createElement("div");
  actions.className = "actions";
  if (reminder.status === "pending") {
    actions.append(
      actionButton("✅ Done", null, () => completeReminder(reminder)),
      actionButton(`💤 ${config.default_snooze_min}m`, null, () => snoozeReminder(reminder)),
      actionButton("Edit", "ghost", () => startEdit(reminder)),
    );
  }
  actions.append(actionButton("Delete", "danger", () => deleteReminder(reminder)));
  card.append(actions);

  return card;
}

// --- actions -------------------------------------------------------------

async function completeReminder(reminder) {
  const response = await fetch(`${API}/${reminder.id}/complete`, { method: "POST" });
  if (!response.ok) return reportFailure(response, "Could not complete that reminder.");
  const updated = await response.json();
  toast(
    updated.status === "pending"
      ? `Done — next on ${formatAbsolute(updated.due_at)}.`
      : `Done: ${updated.title}`
  );
  loadReminders();
}

async function snoozeReminder(reminder) {
  const response = await fetch(`${API}/${reminder.id}/snooze`, { method: "POST" });
  if (!response.ok) return reportFailure(response, "Could not snooze that reminder.");
  const updated = await response.json();
  toast(`Snoozed until ${formatAbsolute(updated.due_at)}.`);
  loadReminders();
}

/** Deferred delete: the card goes now, the request goes in UNDO_MS. */
function deleteReminder(reminder) {
  const handle = setTimeout(() => commitDelete(reminder.id), UNDO_MS);
  pendingDeletes.set(reminder.id, handle);
  render();

  toast(`Deleted “${reminder.title}”.`, {
    actionLabel: "Undo",
    onAction: () => {
      clearTimeout(pendingDeletes.get(reminder.id));
      pendingDeletes.delete(reminder.id);
      render();
    },
  });
}

async function commitDelete(id, keepalive = false) {
  pendingDeletes.delete(id);
  const response = await fetch(`${API}/${id}`, { method: "DELETE", keepalive });
  if (!response.ok && !keepalive) {
    toast("Delete failed.", { error: true });
  }
  if (!keepalive) loadReminders();
}

// Closing the tab must not silently cancel a delete the user already confirmed.
window.addEventListener("pagehide", () => {
  for (const [id, handle] of pendingDeletes) {
    clearTimeout(handle);
    commitDelete(id, true);
  }
});

// --- the form ------------------------------------------------------------

function form() {
  return document.getElementById("create-form");
}

/** Add an option for a rule the preset list does not contain (e.g. one
 *  created over MCP), so editing never silently drops it. */
function ensureRecurrenceOption(rule) {
  const select = document.getElementById("recurrence");
  if (!rule || [...select.options].some((option) => option.value === rule)) return;
  const option = document.createElement("option");
  option.value = rule;
  option.textContent = describeRecurrence(rule);
  select.append(option);
}

function startEdit(reminder) {
  editingId = reminder.id;
  const f = form();
  f.title.value = reminder.title;
  f.note.value = reminder.note || "";
  f.due_at.value = new Date(reminder.due_at).toISOString();
  ensureRecurrenceOption(reminder.recurrence);
  f.recurrence.value = reminder.recurrence || "";
  f.recur_from.value = reminder.recur_from;
  f.retry_interval_min.value = reminder.retry_interval_min;
  f.max_retries.value = reminder.max_retries;

  document.getElementById("submit-button").textContent = "Save changes";
  document.getElementById("cancel-edit").hidden = false;
  f.scrollIntoView({ behavior: "smooth", block: "start" });
  f.title.focus();
}

function cancelEdit() {
  editingId = null;
  form().reset();
  resetDefaults();
  document.getElementById("submit-button").textContent = "Add reminder";
  document.getElementById("cancel-edit").hidden = true;
}

async function submitForm(event) {
  event.preventDefault();
  const f = event.target;
  const payload = {
    title: f.title.value.trim(),
    note: f.note.value.trim() || null,
    due_at: f.due_at.value.trim(),
    recurrence: f.recurrence.value || null,
    recur_from: f.recur_from.value,
    retry_interval_min: Number(f.retry_interval_min.value),
    max_retries: Number(f.max_retries.value),
  };
  if (!payload.due_at) {
    toast("Give it a due time.", { error: true });
    return;
  }

  const editing = editingId !== null;
  const response = await fetch(editing ? `${API}/${editingId}` : API, {
    method: editing ? "PATCH" : "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    return reportFailure(response, "Could not save that reminder.");
  }

  const saved = await response.json();
  // Echo the resolved time: a natural-language misparse has to be visible now,
  // not days later as a reminder that never arrived.
  toast(`${editing ? "Updated" : "Added"} “${saved.title}” for ${formatAbsolute(saved.due_at)}.`);
  cancelEdit();
  loadReminders();
}

function resetDefaults() {
  const inFifteen = new Date(Date.now() + 15 * 60 * 1000);
  inFifteen.setSeconds(0, 0);
  document.getElementById("due_at").value = inFifteen.toISOString();
  document.getElementById("recurrence").value = "";
  document.getElementById("recur_from").value = "schedule";
  document.getElementById("retry_interval_min").value = 15;
  document.getElementById("max_retries").value = 4;
}

// --- theme ---------------------------------------------------------------

function applyTheme(theme) {
  if (theme) {
    document.documentElement.dataset.theme = theme;
  } else {
    delete document.documentElement.dataset.theme;
  }
}

function toggleTheme() {
  // Three states, cycled: OS preference -> light -> dark -> OS preference.
  const current = localStorage.getItem("theme");
  const next = current === null ? "light" : current === "light" ? "dark" : null;
  if (next === null) {
    localStorage.removeItem("theme");
  } else {
    localStorage.setItem("theme", next);
  }
  applyTheme(next);
}

// --- wiring --------------------------------------------------------------

function isTyping(target) {
  return ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}

function wireShortcuts() {
  const dialog = document.getElementById("shortcuts");
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (dialog.open) dialog.close();
      else if (editingId !== null) cancelEdit();
      else if (document.activeElement === document.getElementById("search")) {
        document.getElementById("search").blur();
      }
      return;
    }
    if (isTyping(event.target) || event.metaKey || event.ctrlKey || event.altKey) return;

    if (event.key === "n") {
      event.preventDefault();
      cancelEdit();
      document.getElementById("title").focus();
    } else if (event.key === "/") {
      event.preventDefault();
      document.getElementById("search").focus();
    } else if (event.key === "?") {
      event.preventDefault();
      dialog.showModal();
    }
  });
  document.getElementById("help-button").addEventListener("click", () => dialog.showModal());
  document.getElementById("shortcuts-close").addEventListener("click", () => dialog.close());
}

function wire() {
  form().addEventListener("submit", submitForm);
  document.getElementById("cancel-edit").addEventListener("click", cancelEdit);
  document.getElementById("refresh").addEventListener("click", loadReminders);
  document.getElementById("theme-toggle").addEventListener("click", toggleTheme);
  document.getElementById("search").addEventListener("input", (event) => {
    filterText = event.target.value.trim().toLowerCase();
    render();
  });
  wireShortcuts();
}

async function start() {
  applyTheme(localStorage.getItem("theme"));
  wire();
  resetDefaults();
  await loadConfig();
  await loadReminders();
  setInterval(loadReminders, POLL_MS);
}

start();
