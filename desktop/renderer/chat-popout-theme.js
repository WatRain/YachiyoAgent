"use strict";

const requestedTheme = new URLSearchParams(window.location.search).get("theme");
document.documentElement.dataset.theme = requestedTheme === "light" || requestedTheme === "dark"
  ? requestedTheme
  : (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
