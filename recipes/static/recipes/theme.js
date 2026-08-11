(() => {
  const toggle = document.querySelector("[data-theme-toggle]");
  if (!toggle) return;

  const meta = document.querySelector("[data-theme-color]");
  toggle.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    meta.content = next === "light" ? "#fbf7f0" : "#191512";
    localStorage.setItem("theme", next);
  });
})();
