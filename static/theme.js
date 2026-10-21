// Light/dark mode toggle. Dark is TripBuddy's original look and the default;
// the choice is remembered in this browser. templates/_theme.html applies a saved
// "light" before the page is drawn, so there's no dark flash on load.

(function () {
  const KEY = "tripbuddy-theme";
  const root = document.documentElement;
  const ICONS = {
    // Shown in dark mode: switch to light
    sun: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
    // Shown in light mode: switch to dark
    moon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>',
  };

  const button = document.createElement("button");
  button.type = "button";
  button.className = "theme-toggle";

  const current = () => (root.dataset.theme === "light" ? "light" : "dark");

  function update() {
    const light = current() === "light";
    const label = light ? "Switch to dark mode" : "Switch to light mode";
    button.innerHTML = light ? ICONS.moon : ICONS.sun;
    button.setAttribute("aria-label", label);
    button.title = label;
  }

  function apply(theme) {
    if (theme === "light") root.dataset.theme = "light";
    else delete root.dataset.theme;
    update();
  }

  button.addEventListener("click", () => {
    const next = current() === "light" ? "dark" : "light";
    apply(next);
    try { localStorage.setItem(KEY, next); } catch (e) { /* private mode: just this page */ }
  });

  // Keep other open TripBuddy tabs in step
  window.addEventListener("storage", (event) => {
    if (event.key === KEY) apply(event.newValue === "light" ? "light" : "dark");
  });

  function place() {
    const bar = document.getElementById("accountBar");
    if (bar) {
      bar.prepend(button);
    } else {
      button.classList.add("floating");
      document.body.appendChild(button);
    }
  }

  update();
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", place);
  else place();
})();
