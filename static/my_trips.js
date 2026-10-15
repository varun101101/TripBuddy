// "My trips": group trips and solo plans saved to the signed-in account.

const $ = (id) => document.getElementById(id);

const GROUP_STATUS = {
  collecting: ["Collecting members", "wait"],
  planning: ["Agents are planning", "busy"],
  awaiting_votes: ["Waiting for votes", "wait"],
  processing: ["Updating the plan", "busy"],
  completed: ["Plan approved", "good"],
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

function row(href, title, [label, tone], meta) {
  const a = el("a", "trip-row");
  a.href = href;
  a.append(el("h3", "", title), el("span", `trip-status ${tone}`, label), el("p", "trip-meta", meta));
  return a;
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
    groups.appendChild(row(
      `/trip/${encodeURIComponent(t.trip_id)}`,
      t.title,
      GROUP_STATUS[t.status] || [t.status, ""],
      `${role} · ${people}${days} · updated ${niceDate(t.updated_at)}`
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
      `Created ${niceDate(p.created_at)}`
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
    $("errorBox").textContent = error.message;
    $("errorBox").classList.remove("hidden");
  }
}

start();
