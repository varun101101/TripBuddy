// Log-in / sign-up page. Clerk draws the forms (email + password, Google,
// email verification codes and "Forgot password?").
// /login?mode=signup shows sign-up; ?next=/some/page returns there afterwards.

const params = new URLSearchParams(window.location.search);

function safeNext() {
  const next = params.get("next") || "";
  // Only same-site paths, never another website.
  return next.startsWith("/") && !next.startsWith("//") && !next.startsWith("/login")
    ? next
    : "/my-trips";
}

function setStatus(text) {
  const box = document.getElementById("authStatus");
  box.textContent = text;
  box.classList.toggle("hidden", !text);
}

async function start() {
  const account = window.TripBuddyAccount;
  if (!account.enabled) {
    setStatus("Accounts aren't set up yet. Add CLERK_PUBLISHABLE_KEY to .env and restart the app. " +
      "You can still plan as a guest.");
    return;
  }

  const clerk = await account.clerk;
  if (!clerk) {
    setStatus("Couldn't reach the sign-in service. Check your internet connection. " +
      "You can still plan as a guest.");
    return;
  }

  const next = safeNext();
  if (clerk.isSignedIn) {
    window.location.replace(next);
    return;
  }

  const signup = params.get("mode") === "signup";
  const nextParam = encodeURIComponent(next);
  document.getElementById("authTitle").textContent = signup ? "Create your account" : "Welcome back";
  setStatus("");

  const mount = document.getElementById("clerkMount");
  if (signup) {
    clerk.mountSignUp(mount, {
      fallbackRedirectUrl: next,
      signInUrl: `/login?next=${nextParam}`,
      signInFallbackRedirectUrl: next,
    });
  } else {
    clerk.mountSignIn(mount, {
      fallbackRedirectUrl: next,
      signUpUrl: `/login?mode=signup&next=${nextParam}`,
      signUpFallbackRedirectUrl: next,
    });
  }

  // Backup for the redirect: as soon as someone is signed in, go on.
  clerk.addListener(({ user }) => {
    if (user) window.location.replace(next);
  });
}

start();
