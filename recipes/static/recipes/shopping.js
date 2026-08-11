(() => {
  const container = document.querySelector("[data-shopping-list]");
  if (!container) return;

  const rows = [...container.querySelectorAll("[data-shopping-item]")];
  rows.forEach(row => {
    const checkbox = row.querySelector("[data-item-checkbox]");
    const update = () => row.classList.toggle("is-checked-off", !checkbox.checked);
    checkbox.addEventListener("change", update);
    update();
  });

  const copyButton = container.querySelector("[data-copy-list]");
  copyButton.addEventListener("click", async () => {
    const selected = rows
      .filter(row => row.querySelector("[data-item-checkbox]").checked)
      .map(row => `• ${row.querySelector("[data-item-text]").dataset.itemText.trim()}`)
      .join("\n");
    await navigator.clipboard.writeText(selected);
    const toast = document.querySelector("[data-toast]");
    toast.classList.add("visible");
    window.setTimeout(() => toast.classList.remove("visible"), 1800);
  });
})();
