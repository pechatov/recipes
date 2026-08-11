(() => {
  const toggle = document.querySelector("[data-theme-toggle]");
  if (!toggle) return;

  toggle.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    window.applyTheme(next);
    try {
      localStorage.setItem("theme", next);
    } catch (error) {
      // Хранилище заблокировано: тема поменяется, но не переживёт перезагрузку.
    }
  });
})();
