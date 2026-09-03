(() => {
  "use strict";

  const STORAGE_KEY = "birdbot-theme";
  const root = document.documentElement;

  function readTheme() {
    try {
      return localStorage.getItem(STORAGE_KEY) === "dark" ? "dark" : "light";
    } catch (_) {
      return "light";
    }
  }

  function saveTheme(theme) {
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch (_) {
      // Private browsing or a strict storage policy should not break the UI.
    }
  }

  function updateButton(theme) {
    const button = document.getElementById("theme-toggle");
    if (!button) return;
    const dark = theme === "dark";
    button.setAttribute("aria-pressed", String(dark));
    button.setAttribute("aria-label", dark ? "Switch to light mode" : "Switch to dark mode");
    const icon = document.createElement("span");
    icon.className = "theme-toggle-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = dark ? "☀" : "☾";
    const label = document.createElement("span");
    label.className = "theme-toggle-label";
    label.textContent = dark ? "Light mode" : "Dark mode";
    button.replaceChildren(icon, label);
  }

  function applyTheme(theme, persist = false) {
    const next = theme === "dark" ? "dark" : "light";
    root.dataset.theme = next;
    if (persist) saveTheme(next);
    updateButton(next);
  }

  applyTheme(root.dataset.theme || readTheme());

  document.addEventListener("click", (event) => {
    const button = event.target.closest("#theme-toggle");
    if (!button) return;
    applyTheme(root.dataset.theme === "dark" ? "light" : "dark", true);
  });
})();
