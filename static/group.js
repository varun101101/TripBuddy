// TripBuddy group trip page.
// /group            -> create a trip
// /trip/{trip_id}   -> join, or (for members) the live trip dashboard

const POLL_MS = 4000;

const STATUS = {
  collecting: ["Collecting members", "wait"],
  planning: ["Agents are planning", "busy"],
  awaiting_votes: ["Waiting for votes", "wait"],
  processing: ["Updating the plan", "busy"],
  completed: ["Plan approved", "good"],
  needs_changes: ["Needs changes", "bad"],
  blocked: ["Blocked by guardrail", "bad"],
};

const AGENT_LABELS = {
  flight_agent: "Flight agent",
  hotel_agent: "Hotel agent",
  weather_agent: "Weather agent",
  budget_agent: "Budget agent",
  itinerary_agent: "Itinerary agent",
};

const app = {
  tripId: null,
  memberId: null,
  view: null,
  renderedPlan: null,
  busy: false,
  timer: null,
};

// ---------- small helpers ----------
const $ = (id) => document.getElementById(id);

function storageGet(key) {
  try { return localStorage.getItem(key); } catch (e) { return null; }
}
function storageSet(key, value) {
  try { localStorage.setItem(key, value); } catch (e) { /* private mode: ignore */ }
}
function storageRemove(key) {
  try { localStorage.removeItem(key); } catch (e) { /* ignore */ }
}
const memberKey = (tripId) => `tripbuddy_member_${tripId}`;

function rupees(n) {
  return "₹" + new Intl.NumberFormat("en-IN").format(n);
}

function niceDate(iso) {
  if (!iso) return "";
  const d = new Date(iso + "T00:00:00");
  return d.toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
}

function joinNames(names) {
  if (names.length <= 1) return names.join("");
  return names.slice(0, -1).join(", ") + " and " + names[names.length - 1];
}

function el(tag, attrs = {}, text) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else node.setAttribute(k, v);
  }
  if (text !== undefined) node.textContent = text;
  return node;
}

function show(id, visible) {
  $(id).classList.toggle("hidden", !visible);
}

function showError(message) {
  const box = $("errorBox");
  box.textContent = message;
  box.classList.remove("hidden");
  box.scrollIntoView({ behavior: "smooth", block: "center" });
}

function hideError() {
  $("errorBox").classList.add("hidden");
}

async function api(method, url, body) {
  const response = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await response.json(); } catch (e) { /* non-JSON error page */ }
  if (!response.ok || data.success === false) {
    if (Array.isArray(data.detail)) {
      // Pydantic validation errors (422)
      const msgs = data.detail.map((d) => `${d.loc[d.loc.length - 1]}: ${d.msg}`);
      throw new Error("Please check: " + msgs.join("; "));
    }
    throw new Error(data.error || `Request failed (${response.status}).`);
  }
  return data;
}

// ---------- member form fields ----------
function mountMemberFields() {
  const template = $("memberFieldsTemplate");
  document.querySelectorAll(".member-fields").forEach((slot) => {
    slot.appendChild(template.content.cloneNode(true));
  });
}

function readMember(scope) {
  const get = (name) => scope.querySelector(`[name="${name}"]`);
  const nameInput = get("name");
  const name = nameInput.value.trim();
  nameInput.classList.toggle("invalid", !name);
  if (!name) {
    nameInput.focus();
    throw new Error("Please enter your name.");
  }
  const from = get("available_from").value || null;
  const to = get("available_to").value || null;
  if ((from && !to) || (!from && to)) {
    throw new Error("Give both a free-from and a free-until date, or leave both empty.");
  }
  if (from && to && from > to) {
    throw new Error("Your free-from date is after your free-until date.");
  }
  const budget = get("budget_max").value;
  return {
    name,
    origin: get("origin").value.trim() || null,
    budget_max: budget ? parseInt(budget, 10) : null,
    available_from: from,
    available_to: to,
    food_pref: get("food_pref").value,
    must_see: get("must_see").value.trim() || null,
    notes: get("notes").value.trim() || null,
  };
}

function fillMember(scope, details) {
  for (const [key, value] of Object.entries(details)) {
    const input = scope.querySelector(`[name="${key}"]`);
    if (input) input.value = value ?? (key === "food_pref" ? "no_preference" : "");
  }
}

// ---------- create ----------
async function onCreate(event) {
  event.preventDefault();
  hideError();
  const form = event.target;
  const button = form.querySelector("button[type=submit]");
  try {
    const field = (name) => form.elements[name];
    const title = field("title").value.trim();
    const description = field("description").value.trim();
    if (!title || description.length < 5) {
      throw new Error("Give the trip a title and a short description.");
    }
    const organiser = readMember(form.querySelector('[data-fields="organiser"]'));
    button.disabled = true;
    const data = await api("POST", "/api/trips", {
      title,
      description,
      trip_days: field("trip_days").value ? parseInt(field("trip_days").value, 10) : null,
      approval_rule: field("approval_rule").value,
      organiser,
    });
    storageSet(memberKey(data.trip_id), data.member_id);
    window.location.href = data.join_url;
  } catch (error) {
    showError(error.message);
    button.disabled = false;
  }
}

// ---------- join ----------
async function onJoin(event) {
  event.preventDefault();
  hideError();
  const button = event.target.querySelector("button[type=submit]");
  try {
    const details = readMember(event.target.querySelector('[data-fields="join"]'));
    button.disabled = true;
    const data = await api("POST", `/api/trips/${app.tripId}/members`, details);
    app.memberId = data.member_id;
    storageSet(memberKey(app.tripId), data.member_id);
    await loadTrip();
  } catch (error) {
    showError(error.message);
  } finally {
    button.disabled = false;
  }
}

function renderJoin(view) {
  show("createView", false);
  show("tripView", false);
  show("joinView", true);
  $("joinTitle").textContent = view.trip.title;
  $("joinDescription").textContent = view.trip.description;
  const organiser = view.members.find((m) => m.is_organiser);
  const names = view.members.map((m) => m.name);
  $("joinMembers").textContent =
    `Organised by ${organiser ? organiser.name : "someone"} · ${names.length} going: ${joinNames(names)}`;
  show("joinClosed", !view.trip.can_join);
  show("joinForm", view.trip.can_join);
}

// ---------- dashboard ----------
function renderMembers(view) {
  const list = $("membersList");
  list.replaceChildren();
  $("membersHeading").textContent = `Members (${view.members.length})`;
  for (const m of view.members) {
    const card = el("article", { class: "member-card" + (m.is_you ? " you" : "") });
    const h = el("h3", {}, m.name);
    if (m.is_organiser) h.appendChild(el("span", { class: "tag" }, "Organiser"));
    if (m.is_you) h.appendChild(el("span", { class: "tag you" }, "You"));
    card.appendChild(h);
    const dl = el("dl");
    const rows = [
      ["From", m.origin || "Not given"],
      ["Budget", m.budget_max ? rupees(m.budget_max) + " max" : "Not given"],
      ["Free", m.available_from ? `${niceDate(m.available_from)} – ${niceDate(m.available_to)}` : "Flexible"],
      ["Food", m.food_label],
    ];
    if (m.must_see) rows.push(["Must see", m.must_see]);
    if (m.notes) rows.push(["Note", m.notes]);
    for (const [k, v] of rows) {
      dl.appendChild(el("dt", {}, k));
      dl.appendChild(el("dd", {}, v));
    }
    card.appendChild(dl);
    list.appendChild(card);
  }
}

function renderRules(view) {
  const p = view.preview;
  const dl = $("rulesList");
  dl.replaceChildren();
  const add = (label, value) => {
    dl.appendChild(el("dt", {}, label));
    dl.appendChild(el("dd", {}, value));
  };
  add("Travellers", `${p.group_size} (${joinNames(p.member_names)})`);
  add("Budget", p.budget_per_person
    ? `${rupees(p.budget_per_person)} per person, set by ${joinNames(p.budget_set_by)} (the lowest limit)`
    : "No budget given");
  add("Dates", p.window_start
    ? `${niceDate(p.window_start)} – ${niceDate(p.window_end)} (${p.window_days} days everyone is free)`
    : "Flexible");
  if (p.trip_days) add("Trip length", `${p.trip_days} days`);
  const origins = Object.entries(p.origins).map(([city, names]) => `${city} (${joinNames(names)})`);
  add("Starting from", origins.length ? origins.join(" · ") : "Not given");
  add("Meals", p.meal_rules.length ? p.meal_rules.join(". ") : "No restrictions");
  add("Must see", p.must_see.length
    ? p.must_see.map((e) => `${e.place} (${joinNames(e.requested_by)})`).join(" · ")
    : "None listed");

  const fillList = (id, items) => {
    const ul = $(id);
    ul.replaceChildren(...items.map((t) => el("li", {}, t)));
  };
  fillList("conflictList", p.conflicts);
  fillList("warningList", p.warnings);

  // Planning controls
  const controls = $("planControls");
  controls.replaceChildren();
  const trip = view.trip;
  if (!trip.can_edit) return;
  if (view.you.is_organiser) {
    const tooFew = view.members.length < 2;
    const button = el("button", { class: "primary-btn", type: "button", id: "planBtn" },
      trip.status === "collecting" ? "Generate group plan" : "Plan again");
    button.disabled = tooFew || p.conflicts.length > 0 || app.busy;
    button.addEventListener("click", onPlan);
    controls.appendChild(button);
    let hint = "The agents start only when you press this.";
    if (tooFew) hint = "Share the link: a group trip needs at least 2 members.";
    else if (p.conflicts.length) hint = "Fix the clash in red first (members can edit their details).";
    controls.appendChild(el("span", {}, hint));
  } else {
    const organiser = view.members.find((m) => m.is_organiser);
    controls.appendChild(el("span", {},
      `Waiting for ${organiser ? organiser.name : "the organiser"} to start planning. You can still edit your details.`));
  }
}

function renderPlan(view) {
  const plan = view.plan;
  const status = view.trip.status;

  // Execution plan
  const hasRouting = plan && (plan.selected_agents.length || plan.supervisor_reasoning);
  show("executionSection", Boolean(hasRouting));
  if (hasRouting) {
    $("supervisorReasoning").textContent = plan.supervisor_reasoning || "";
    $("agentChips").replaceChildren(
      ...plan.selected_agents.map((a) => el("span", { class: "agent-chip" }, AGENT_LABELS[a] || a))
    );
    const badge = $("guardrailBadge");
    badge.textContent = plan.guardrail_allowed ? "Guardrail passed" : "Guardrail blocked";
    badge.classList.toggle("blocked", !plan.guardrail_allowed);
  }

  // Draft / final
  const answer = plan && plan.answer;
  const showPlan = Boolean(answer) && status !== "planning";
  show("planSection", showPlan);
  if (!showPlan) return;

  let title = "Draft plan";
  if (status === "completed") title = "Final group plan";
  else if (status === "needs_changes") title = "Planning stopped";
  else if (status === "blocked") title = "Request blocked";
  else if (status === "awaiting_votes" || status === "processing") {
    title = `Draft plan · round ${view.trip.current_round}`;
  }
  $("planTitle").textContent = title;
  $("pdfTitle").textContent = view.trip.title;
  const meta = [];
  if (plan.vote_summary && status === "completed") meta.push(`Vote: ${plan.vote_summary}`);
  if (plan.llm_calls) meta.push(`${plan.llm_calls} model calls`);
  $("planMeta").textContent = meta.join(" · ");

  if (app.renderedPlan !== answer) {
    const box = $("planBox");
    if (typeof marked !== "undefined") {
      box.innerHTML = marked.parse(answer);
      box.style.whiteSpace = "";
    } else {
      box.textContent = answer;  // offline: show the raw text, keeping line breaks
      box.style.whiteSpace = "pre-wrap";
    }
    app.renderedPlan = answer;
  }
}

function renderVote(view) {
  const trip = view.trip;
  const open = trip.status === "awaiting_votes";
  show("voteSection", open);
  if (!open) return;

  const maxRounds = trip.max_revisions + 1;
  const lastRound = trip.current_round >= maxRounds;
  $("roundLabel").textContent = `Group vote · round ${trip.current_round} of up to ${maxRounds}`;
  $("voteRule").textContent = lastRound
    ? `${trip.approval_rule_label}. This is the last round: after it, the plan is finalised either way, applying everyone's requests where the group rules allow.`
    : `${trip.approval_rule_label}. If the draft isn't approved, the itinerary agent revises it using everyone's requests.`;

  const list = $("voteStatus");
  list.replaceChildren();
  const votesByName = Object.fromEntries(view.votes.map((v) => [v.name, v]));
  for (const m of view.members) {
    const v = votesByName[m.name];
    const li = el("li", { class: v ? v.decision : "" });
    li.appendChild(document.createTextNode(
      v ? `${m.name}: ${v.decision === "approve" ? "approved" : "asked for changes"}` : `${m.name}: not voted yet`
    ));
    if (v && v.feedback) li.appendChild(el("span", { class: "feedback" }, `“${v.feedback}”`));
    list.appendChild(li);
  }

  // Everyone has voted but the trip is still waiting: the last attempt to
  // update the plan failed. Votes are locked now, so offer a retry instead.
  const stuck = view.waiting_for.length === 0;
  show("voteFeedback", !stuck);
  show("approveBtn", !stuck);
  show("reviseBtn", !stuck);
  show("retryBtn", stuck);

  const mine = view.you.my_vote;
  if (stuck) {
    $("myVoteNote").textContent =
      "Everyone has voted, but the plan couldn't be updated (the AI service may have timed out). Votes are locked; press Try again.";
  } else {
    $("myVoteNote").textContent = mine
      ? `You ${mine.decision === "approve" ? "approved" : "asked for changes"}. You can change your vote until everyone has voted.`
      : "";
  }
  $("approveBtn").disabled = app.busy;
  $("reviseBtn").disabled = app.busy;
  $("retryBtn").disabled = app.busy;
}

function renderDashboard(view) {
  show("createView", false);
  show("joinView", false);
  show("tripView", true);

  const trip = view.trip;
  $("tripTitle").textContent = trip.title;
  $("tripDescription").textContent = trip.description;
  const you = view.you.is_organiser ? `${view.you.name} (organiser)` : view.you.name;
  $("tripMeta").textContent =
    `${trip.approval_rule_label}${trip.trip_days ? ` · ${trip.trip_days} days` : ""} · You are ${you}`;

  const [label, tone] = STATUS[trip.status] || [trip.status, ""];
  const pill = $("statusPill");
  pill.textContent = label;
  pill.className = `trip-status ${tone}`;

  $("shareLink").value = `${window.location.origin}/trip/${trip.trip_id}`;
  show("editMeBtn", trip.can_edit);
  if (!trip.can_edit) show("editForm", false);

  renderMembers(view);
  renderRules(view);
  renderPlan(view);
  renderVote(view);

  const working = app.busy || trip.status === "planning" || trip.status === "processing";
  show("workingBox", working);
  $("workingTitle").textContent = trip.status === "processing" || (app.busy && trip.status === "awaiting_votes")
    ? "Everyone has voted. Updating the plan…"
    : "The agents are planning your trip";
}

async function loadTrip({ silent = false } = {}) {
  const query = app.memberId ? `?member_id=${encodeURIComponent(app.memberId)}` : "";
  try {
    const view = await api("GET", `/api/trips/${app.tripId}${query}`);
    app.view = view;
    if (!view.you) {
      if (app.memberId) {
        // Saved key no longer matches (e.g. database was reset)
        storageRemove(memberKey(app.tripId));
        app.memberId = null;
      }
      renderJoin(view);
    } else {
      renderDashboard(view);
    }
    if (!silent) hideError();
  } catch (error) {
    if (!silent) showError(error.message);
  }
}

// ---------- actions ----------
async function onPlan() {
  hideError();
  app.busy = true;
  renderDashboard(app.view);
  try {
    const view = await api("POST", `/api/trips/${app.tripId}/plan`, { member_id: app.memberId });
    app.view = view;
  } catch (error) {
    showError(error.message);
  } finally {
    app.busy = false;
    await loadTrip({ silent: true });
  }
}

async function onVote(decision) {
  hideError();
  const feedback = $("voteFeedback").value.trim();
  if (decision === "revise" && !feedback) {
    showError("Write what should change before asking for changes.");
    $("voteFeedback").focus();
    return;
  }
  const remaining = app.view.waiting_for.filter((n) => n !== app.view.you.name);
  const isLast = remaining.length === 0;
  app.busy = isLast;  // the last vote runs the agents, which takes a while
  $("approveBtn").disabled = true;
  $("reviseBtn").disabled = true;
  if (isLast) renderDashboard(app.view);
  try {
    await api("POST", `/api/trips/${app.tripId}/vote`, {
      member_id: app.memberId, decision, feedback,
    });
    $("voteFeedback").value = "";
  } catch (error) {
    showError(error.message);
  } finally {
    app.busy = false;
    await loadTrip({ silent: true });
  }
}

async function onRetry() {
  // Re-send this member's own (locked) vote; the server just continues the plan.
  hideError();
  const mine = app.view.you.my_vote;
  if (!mine) return;
  app.busy = true;
  renderDashboard(app.view);
  try {
    await api("POST", `/api/trips/${app.tripId}/vote`, {
      member_id: app.memberId, decision: mine.decision, feedback: mine.feedback || "",
    });
  } catch (error) {
    showError(error.message);
  } finally {
    app.busy = false;
    await loadTrip({ silent: true });
  }
}

async function onEditSave(event) {
  event.preventDefault();
  hideError();
  try {
    const details = readMember(event.target.querySelector('[data-fields="edit"]'));
    await api("POST", `/api/trips/${app.tripId}/members`, { ...details, member_id: app.memberId });
    show("editForm", false);
    await loadTrip();
  } catch (error) {
    showError(error.message);
  }
}

function openEdit() {
  const form = $("editForm");
  fillMember(form, app.view.you.details);
  show("editForm", true);
  form.scrollIntoView({ behavior: "smooth", block: "start" });
}

function copyText(text, button) {
  navigator.clipboard.writeText(text).then(() => {
    const old = button.textContent;
    button.textContent = "Copied!";
    setTimeout(() => { button.textContent = old; }, 1400);
  }).catch(() => showError("Could not copy."));
}

function downloadPdf() {
  if (typeof html2pdf === "undefined") {
    showError("The PDF library didn't load. Check your internet connection.");
    return;
  }
  const slug = (app.view.trip.title || "trip").toLowerCase().replace(/[^a-z0-9]+/g, "-");
  const title = $("pdfTitle");
  title.style.display = "block";
  html2pdf()
    .set({
      margin: 0.5,
      filename: `tripbuddy-${slug}.pdf`,
      image: { type: "jpeg", quality: 0.98 },
      html2canvas: { scale: 2, useCORS: true, backgroundColor: "#ffffff" },
      jsPDF: { unit: "in", format: "a4", orientation: "portrait" },
      pagebreak: { mode: ["avoid-all", "css", "legacy"] },
    })
    .from($("pdfContent"))
    .save()
    .catch(() => showError("Could not create the PDF."))
    .finally(() => { title.style.display = ""; });
}

// ---------- start ----------
function start() {
  mountMemberFields();
  $("createForm").addEventListener("submit", onCreate);
  $("joinForm").addEventListener("submit", onJoin);
  $("editForm").addEventListener("submit", onEditSave);
  $("editMeBtn").addEventListener("click", openEdit);
  $("cancelEditBtn").addEventListener("click", () => show("editForm", false));
  $("approveBtn").addEventListener("click", () => onVote("approve"));
  $("reviseBtn").addEventListener("click", () => onVote("revise"));
  $("retryBtn").addEventListener("click", onRetry);
  $("copyLinkBtn").addEventListener("click", (e) => copyText($("shareLink").value, e.target));
  $("copyPlanBtn").addEventListener("click", (e) => copyText($("planBox").innerText, e.target));
  $("downloadBtn").addEventListener("click", downloadPdf);

  const match = window.location.pathname.match(/^\/trip\/([^/]+)/);
  if (!match) {
    show("createView", true);
    return;
  }
  app.tripId = decodeURIComponent(match[1]);
  app.memberId = storageGet(memberKey(app.tripId));
  loadTrip();
  app.timer = setInterval(() => {
    if (app.busy || document.hidden) return;
    if (app.view && app.view.trip && app.view.trip.status === "completed") return;
    loadTrip({ silent: true });
  }, POLL_MS);
}

document.addEventListener("DOMContentLoaded", start);
