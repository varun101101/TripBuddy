// "My trips": group trips and solo plans saved to the signed-in account.

const $ = (id) => document.getElementById(id);

const GROUP_STATUS = {
  collecting: ["Collecting members", "wait"],
  planning: ["Agents are planning", "busy"],
  awaiting_votes: ["Waiting for votes", "wait"],
  processing: ["Updating the plan", "busy"],
  completed: ["Final plan", "good"],
  needs_changes: ["Needs changes", "bad"],
  blocked: ["Blocked by guardrail", "bad"],
};

const SOLO_STATUS = {
  draft: ["Draft · needs your approval", "wait"],
  final: ["Final plan", "good"],
  blocked: ["Blocked by guardrail", "bad"],
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function niceDate(iso) {
  return new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
}

function showError(message) {
  $("errorBox").textContent = message;
  $("errorBox").classList.remove("hidden");
}

// One saved trip: the link to open it, its status, and a delete button.
// `remove` says what deleting does: { label, confirm, url, emptyText, emptyLink, emptyHref }.
function row(href, title, [label, tone], meta, remove) {
  const item = el("div", "trip-row");
  const link = el("a", "trip-link");
  link.href = href;
  link.append(el("h3", "", title), el("p", "trip-meta", meta));

  const side = el("div", "trip-side");
  const button = el("button", "trip-delete", remove.label);
  button.type = "button";
  button.title = remove.title;
  button.addEventListener("click", () => deleteRow(item, button, remove));
  side.append(el("span", `trip-status ${tone}`, label), button);

  item.append(link, side);
  return item;
}

async function deleteRow(item, button, remove) {
  if (!window.confirm(remove.confirm)) return;
  button.disabled = true;
  button.textContent = remove.label === "Remove" ? "Removing…" : "Deleting…";
  try {
    const response = await fetch(remove.url, { method: "DELETE" });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.success) throw new Error(data.error || "Couldn't delete this trip.");
    const list = item.parentElement;
    item.remove();
    if (!list.querySelector(".trip-row")) {
      list.appendChild(empty(remove.emptyText, remove.emptyLink, remove.emptyHref));
    }
    $("errorBox").classList.add("hidden");
  } catch (error) {
    showError(error.message);
    button.disabled = false;
    button.textContent = remove.label;
  }
}

function empty(text, linkText, href) {
  const box = el("div", "empty-state", text + " ");
  const a = el("a", "", linkText);
  a.href = href;
  box.appendChild(a);
  return box;
}

function render(data) {
  const groups = $("groupList");
  groups.replaceChildren();
  if (!data.group_trips.length) {
    groups.appendChild(empty("No group trips yet.", "Start one and share the link →", "/group"));
  }
  for (const t of data.group_trips) {
    const role = t.is_organiser ? "Organiser" : "Member";
    const people = `${t.members} ${t.members === 1 ? "person" : "people"}`;
    const days = t.trip_days ? ` · ${t.trip_days} days` : "";
    const groupEmpty = { emptyText: "No group trips yet.", emptyLink: "Start one and share the link →", emptyHref: "/group" };
    groups.appendChild(row(
      `/trip/${encodeURIComponent(t.trip_id)}`,
      t.title,
      GROUP_STATUS[t.status] || [t.status, ""],
      `${role} · ${people}${days} · updated ${niceDate(t.updated_at)}`,
      t.is_organiser
        ? {
            ...groupEmpty,
            label: "Delete",
            title: "Delete this trip for everyone in the group",
            confirm: `Delete "${t.title}" for everyone in the group?\n\nThe plan, members and votes will be removed. This can't be undone.`,
            url: `/api/me/trips/${encodeURIComponent(t.trip_id)}`,
          }
        : {
            ...groupEmpty,
            label: "Remove",
            title: "Remove this trip from My trips",
            confirm: `Remove "${t.title}" from your trips?\n\nYou stay in the group and your vote still counts; it just won't be listed here.`,
            url: `/api/me/trips/${encodeURIComponent(t.trip_id)}`,
          }
    ));
  }

  const solo = $("soloList");
  solo.replaceChildren();
  if (!data.solo_plans.length) {
    solo.appendChild(empty("No solo plans yet.", "Plan a trip →", "/"));
  }
  for (const p of data.solo_plans) {
    solo.appendChild(row(
      `/?plan=${encodeURIComponent(p.thread_id)}`,
      p.title,
      SOLO_STATUS[p.status] || [p.status, ""],
      `Created ${niceDate(p.created_at)}`,
      {
        label: "Delete",
        title: "Delete this plan",
        confirm: `Delete the plan "${p.title}"?\n\nThis can't be undone.`,
        url: `/api/me/plans/${encodeURIComponent(p.thread_id)}`,
        emptyText: "No solo plans yet.",
        emptyLink: "Plan a trip →",
        emptyHref: "/",
      }
    ));
  }

  $("groupSection").classList.remove("hidden");
  $("soloSection").classList.remove("hidden");
}

async function start() {
  const account = window.TripBuddyAccount;
  const user = await account.ready;
  if (!user) {
    $("loggedOut").classList.remove("hidden");
    $("myTripsIntro").textContent = "Log in to see the trips and plans saved to your account.";
    if (!account.enabled || !(await account.clerk)) {
      $("loggedOutText").textContent = account.enabled
        ? "Couldn't reach the sign-in service. Check your internet connection and reload."
        : "Accounts aren't set up yet: add CLERK_PUBLISHABLE_KEY to .env and restart the app.";
      $("loggedOutActions").classList.add("hidden");
    }
    return;
  }
  try {
    const response = await fetch("/api/me/trips");
    const data = await response.json();
    if (!response.ok || !data.success) throw new Error(data.error || "Couldn't load your trips.");
    render(data);
  } catch (error) {
    showError(error.message);
  }
}

start();
