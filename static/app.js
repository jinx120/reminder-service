const API = "/api/reminders";
const POLL_MS = 10000;

let activeFilter = "all";

/** Format a datetime-local input value from a Date, in the browser's local zone. */
function toLocalInputValue(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
         `T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** Render a UTC ISO string from the API as local time. */
function formatLocal(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString([], {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

function showError(message) {
  document.getElementById("error").textContent = message || "";
}

async function loadReminders() {
  const url = activeFilter === "all" ? API : `${API}?status=${activeFilter}`;
  const response = await fetch(url);
  if (!response.ok) {
    showError("Could not load reminders.");
    return;
  }
  render(await response.json());
}

function render(reminders) {
  const list = document.getElementById("list");
  if (reminders.length === 0) {
    list.innerHTML = `<p class="empty">Nothing here.</p>`;
    return;
  }
  list.innerHTML = "";
  for (const reminder of reminders) {
    const card = document.createElement("div");
    card.className = `card ${reminder.status}`;

    const head = document.createElement("div");
    head.className = "card-head";
    const title = document.createElement("span");
    title.className = "title";
    title.textContent = reminder.title;           // textContent, never innerHTML
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = reminder.status;
    head.append(title, badge);
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
      spanText(`due ${formatLocal(reminder.due_at)}`),
      spanText(`sent ${reminder.retry_count}/${reminder.max_retries}`),
      spanText(`last ${formatLocal(reminder.last_sent_at)}`),
      spanText(`every ${reminder.retry_interval_min}m`),
    );

    const remove = document.createElement("button");
    remove.textContent = "delete";
    remove.addEventListener("click", () => deleteReminder(reminder.id, reminder.title));
    meta.append(remove);

    card.append(meta);
    list.append(card);
  }
}

function spanText(text) {
  const span = document.createElement("span");
  span.textContent = text;
  return span;
}

async function deleteReminder(id, title) {
  if (!confirm(`Delete “${title}”?`)) return;
  const response = await fetch(`${API}/${id}`, { method: "DELETE" });
  if (!response.ok) {
    showError("Delete failed.");
    return;
  }
  loadReminders();
}

async function submitForm(event) {
  event.preventDefault();
  showError("");
  const form = event.target;
  const dueLocal = form.due_at.value;
  if (!dueLocal) {
    showError("Pick a due time.");
    return;
  }
  const payload = {
    title: form.title.value.trim(),
    note: form.note.value.trim() || null,
    // The input is local time; toISOString converts it to the UTC the API wants.
    due_at: new Date(dueLocal).toISOString(),
    retry_interval_min: Number(form.retry_interval_min.value),
    max_retries: Number(form.max_retries.value),
  };

  const response = await fetch(API, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => null);
    showError(detail ? JSON.stringify(detail.detail) : "Could not create reminder.");
    return;
  }
  form.reset();
  resetDefaults();
  loadReminders();
}

function resetDefaults() {
  const inFifteen = new Date(Date.now() + 15 * 60 * 1000);
  document.getElementById("due_at").value = toLocalInputValue(inFifteen);
  document.getElementById("retry_interval_min").value = 15;
  document.getElementById("max_retries").value = 4;
}

function wireFilters() {
  document.getElementById("filters").addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.id === "refresh") {
      loadReminders();
      return;
    }
    activeFilter = button.dataset.status;
    for (const other of document.querySelectorAll("#filters button[data-status]")) {
      other.setAttribute("aria-pressed", String(other === button));
    }
    loadReminders();
  });
}

document.getElementById("create-form").addEventListener("submit", submitForm);
wireFilters();
resetDefaults();
loadReminders();
setInterval(loadReminders, POLL_MS);
