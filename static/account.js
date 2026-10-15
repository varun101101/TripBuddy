// Accounts in the browser, powered by Clerk. Loaded on every page via templates/_clerk.html.
//
// - Loads Clerk (if CLERK_PUBLISHABLE_KEY is set) and draws the account bar:
//   "Log in / Sign up" for guests, "My trips" + Clerk's account menu when signed in.
// - Adds the signed-in user's Clerk session token to every /api request
//   ("Authorization: Bearer ..."), so the server knows whose trips to save.
// - If Clerk isn't set up or can't be reached (no internet), everything keeps
//   working in guest mode.
//
// Other scripts can use:
//   await window.TripBuddyAccount.ready   -> { id, name, email } or null for guests
//   await window.TripBuddyAccount.clerk   -> the loaded Clerk object, or null

(function () {
  const config = window.TRIPBUDDY_CLERK || null;
  const LOAD_TIMEOUT_MS = 10000;
  const nativeFetch = window.fetch.bind(window);

  function pageLoaded() {
    if (document.readyState === "complete") return Promise.resolve();
    return new Promise((resolve) => window.addEventListener("load", resolve, { once: true }));
  }

  function withTimeout(promise, ms) {
    return Promise.race([promise, new Promise((resolve) => setTimeout(() => resolve(null), ms))]);
  }

  async function loadClerk() {
    if (!config) return null;
    await pageLoaded(); // Clerk's scripts are deferred; they're ready once the page has loaded
    if (!window.Clerk) {
      console.warn("TripBuddy: couldn't load Clerk (offline?). Continuing in guest mode.");
      return null;
    }
    try {
      await window.Clerk.load({
        ui: { ClerkUI: window.__internal_ClerkUICtor },
        signInUrl: "/login",
        signUpUrl: "/login?mode=signup",
        afterSignOutUrl: "/",
      });
      return window.Clerk;
    } catch (error) {
      console.warn("TripBuddy: Clerk failed to start. Continuing in guest mode.", error);
      return null;
    }
  }

  function toUser(clerkUser) {
    const email = clerkUser.primaryEmailAddress ? clerkUser.primaryEmailAddress.emailAddress : "";
    return {
      id: clerkUser.id,
      name: clerkUser.fullName || clerkUser.firstName || clerkUser.username || email.split("@")[0] || "Traveller",
      email,
    };
  }

  const clerkReady = withTimeout(loadClerk(), LOAD_TIMEOUT_MS);
  const ready = clerkReady.then((clerk) => (clerk && clerk.isSignedIn && clerk.user ? toUser(clerk.user) : null));
  window.TripBuddyAccount = { enabled: Boolean(config), clerk: clerkReady, ready };

  // Attach the session token to TripBuddy's own API calls.
  window.fetch = async function (input, init) {
    if (typeof input === "string" && input.startsWith("/api/")) {
      const clerk = await clerkReady;
      if (clerk && clerk.session) {
        try {
          const token = await clerk.session.getToken();
          if (token) {
            const headers = new Headers((init && init.headers) || {});
            headers.set("Authorization", `Bearer ${token}`);
            init = { ...(init || {}), headers };
          }
        } catch (error) {
          console.warn("TripBuddy: couldn't get a session token; sending as a guest.", error);
        }
      }
    }
    return nativeFetch(input, init);
  };

  function link(href, text, className) {
    const a = document.createElement("a");
    a.href = href;
    a.textContent = text;
    a.className = className;
    return a;
  }

  async function renderBar() {
    const bar = document.getElementById("accountBar");
    if (!bar || !config) return;
    const clerk = await clerkReady;
    const user = await ready;
    bar.replaceChildren();
    if (!clerk) return; // Clerk unreachable: stay quiet, guest mode still works

    if (!user) {
      const next = encodeURIComponent(window.location.pathname + window.location.search);
      bar.append(
        link(`/login?next=${next}`, "Log in", "account-btn ghost"),
        link(`/login?mode=signup&next=${next}`, "Sign up", "account-btn")
      );
      return;
    }

    const onMyTrips = window.location.pathname === "/my-trips";
    const name = document.createElement("span");
    name.className = "account-name";
    name.textContent = user.name;
    const menu = document.createElement("span");
    menu.className = "account-menu";
    bar.append(
      onMyTrips ? link("/", "Plan a trip", "account-btn ghost") : link("/my-trips", "My trips", "account-btn ghost"),
      name,
      menu
    );
    // Clerk's own account menu: manage account, Google link, password, sign out
    clerk.mountUserButton(menu);
  }

  renderBar();
})();
