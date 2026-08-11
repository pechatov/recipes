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

  const pantryToggle = container.querySelector("[data-pantry-toggle]");
  const pantryCheckboxes = [...container.querySelectorAll("[data-pantry-checkbox]")];
  if (pantryToggle && pantryCheckboxes.length) {
    const updatePantryToggle = () => {
      const checkedCount = pantryCheckboxes.filter(checkbox => checkbox.checked).length;
      pantryToggle.checked = checkedCount === pantryCheckboxes.length;
      pantryToggle.indeterminate = checkedCount > 0 && checkedCount < pantryCheckboxes.length;
    };
    pantryToggle.addEventListener("change", () => {
      const shouldCheck = pantryToggle.checked;
      pantryCheckboxes.forEach(checkbox => {
        checkbox.checked = shouldCheck;
        checkbox.dispatchEvent(new Event("change"));
      });
      updatePantryToggle();
    });
    pantryCheckboxes.forEach(checkbox => checkbox.addEventListener("change", updatePantryToggle));
    updatePantryToggle();
  }

  const storeDialog = document.querySelector("[data-store-dialog]");
  const openStoreDialog = document.querySelector("[data-store-dialog-open]");
  if (storeDialog && openStoreDialog) {
    openStoreDialog.addEventListener("click", () => storeDialog.showModal());
    storeDialog.querySelectorAll("[data-store-dialog-close]").forEach(button => {
      button.addEventListener("click", () => storeDialog.close());
    });
    storeDialog.addEventListener("click", event => {
      if (event.target === storeDialog) storeDialog.close();
    });

    const priorityList = storeDialog.querySelector("[data-store-priority-list]");
    let draggedRow = null;
    priorityList.querySelectorAll("[data-store-row]").forEach(row => {
      row.addEventListener("dragstart", () => {
        draggedRow = row;
        row.classList.add("is-dragging");
      });
      row.addEventListener("dragend", () => {
        row.classList.remove("is-dragging");
        draggedRow = null;
      });
    });
    priorityList.addEventListener("dragover", event => {
      event.preventDefault();
      if (!draggedRow) return;
      const rows = [...priorityList.querySelectorAll("[data-store-row]:not(.is-dragging)")];
      const nextRow = rows.find(row => {
        const bounds = row.getBoundingClientRect();
        return event.clientY < bounds.top + bounds.height / 2;
      });
      priorityList.insertBefore(draggedRow, nextRow || null);
    });
  }

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
