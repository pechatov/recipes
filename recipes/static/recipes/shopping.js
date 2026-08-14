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

  const storeSelect = document.querySelector("[data-store-select]");
  if (storeSelect) {
    const updateStoreLinks = () => {
      const option = storeSelect.selectedOptions[0];
      const storeName = option.textContent.trim();
      const storeBrand = option.dataset.searchBrand;
      const storePlace = option.dataset.searchPlace;
      container.querySelectorAll("[data-store-search]").forEach(link => {
        const query = encodeURIComponent(link.dataset.searchQuery.trim());
        const parameters = new URLSearchParams();
        if (storePlace) {
          parameters.set("placeSlug", storePlace);
          parameters.set("relatedBrandSlug", storeBrand);
        }
        parameters.set("query", link.dataset.searchQuery.trim());
        link.href = `https://eda.yandex.ru/retail/${storeBrand}/search?${parameters}`;
        link.textContent = `Найти в ${storeName} ↗`;
      });
    };
    storeSelect.addEventListener("change", updateStoreLinks);
    updateStoreLinks();

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
