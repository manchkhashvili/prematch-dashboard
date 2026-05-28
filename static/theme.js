/* Light/dark theme toggle for the prematch dashboard.
 *
 * Loaded in <head> with a regular (non-defer) <script> tag so it runs
 * synchronously BEFORE the body paints — this is what prevents a dark
 * flash on light-mode page loads. The button is injected later, once
 * the DOM is ready.
 *
 * Theme is persisted in localStorage under "theme" ("dark" | "light").
 * Default = "dark". The choice applies via [data-theme] on <html>;
 * style.css defines :root vs :root[data-theme="light"].
 *
 * To enable theming on a new page: just include this script in <head>.
 */
(function () {
  "use strict";

  const KEY = "theme";

  function getStored() {
    try {
      return localStorage.getItem(KEY);
    } catch (e) {
      return null;  // private mode / disabled
    }
  }

  function setStored(value) {
    try {
      localStorage.setItem(KEY, value);
    } catch (e) {
      /* swallow */
    }
  }

  function apply(theme) {
    if (theme === "light") {
      document.documentElement.setAttribute("data-theme", "light");
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
  }

  // 1. Apply stored theme IMMEDIATELY (before paint) — no flash.
  const initial = getStored() === "light" ? "light" : "dark";
  apply(initial);

  // 2. Inject the toggle button into <header> when the DOM is ready.
  //    Sits inside header but not inside .status — gives it its own slot.
  function inject() {
    const header = document.querySelector("header");
    if (!header) return;
    if (document.getElementById("theme-toggle")) return;

    const btn = document.createElement("button");
    btn.id = "theme-toggle";
    btn.type = "button";
    btn.title = "Toggle light/dark theme";
    btn.textContent = (document.documentElement.getAttribute("data-theme") === "light")
      ? "Dark"   // label = what clicking will switch TO
      : "Light";
    btn.addEventListener("click", () => {
      const current = document.documentElement.getAttribute("data-theme");
      const next = current === "light" ? "dark" : "light";
      apply(next);
      setStored(next);
      btn.textContent = next === "light" ? "Dark" : "Light";
    });
    header.appendChild(btn);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", inject);
  } else {
    inject();
  }
})();
