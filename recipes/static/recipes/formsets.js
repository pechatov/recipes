(() => {
  const allowedImageTypes = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
  };
  const maxImageSize = 10 * 1024 * 1024;
  const definitions = {
    ingredients: { list: "ingredient-forms", template: "ingredient-empty-form" },
    steps: { list: "step-forms", template: "step-empty-form" },
  };

  function updateOrder(prefix) {
    const rows = document.querySelectorAll(`#${definitions[prefix].list} .formset-row`);
    rows.forEach((row, index) => {
      const number = row.querySelector(".formset-number");
      if (number) number.textContent = `Шаг ${index + 1}`;
    });
  }

  function addForm(prefix) {
    const total = document.querySelector(`#id_${prefix}-TOTAL_FORMS`);
    const index = Number(total.value);
    const html = document.getElementById(definitions[prefix].template).innerHTML.replaceAll("__prefix__", index);
    document.getElementById(definitions[prefix].list).insertAdjacentHTML("beforeend", html);
    total.value = index + 1;
    updateOrder(prefix);
  }

  function setPasteStatus(zone, message, isError = false) {
    const status = zone.querySelector("[data-image-paste-status]");
    status.textContent = message;
    status.classList.toggle("is-error", isError);
  }

  function showImagePreview(zone, file) {
    const preview = zone.querySelector("[data-image-preview]");
    const wrapper = zone.querySelector("[data-image-preview-wrap]");
    if (preview.dataset.objectUrl) URL.revokeObjectURL(preview.dataset.objectUrl);
    const objectUrl = URL.createObjectURL(file);
    preview.src = objectUrl;
    preview.dataset.objectUrl = objectUrl;
    wrapper.classList.remove("is-empty");
  }

  function setImageFile(zone, sourceFile) {
    const extension = allowedImageTypes[sourceFile.type];
    if (!extension) {
      setPasteStatus(zone, "Поддерживаются только JPG, PNG и WebP", true);
      return false;
    }
    if (sourceFile.size > maxImageSize) {
      setPasteStatus(zone, "Изображение больше 10 МБ", true);
      return false;
    }
    const input = zone.closest(".field").querySelector("input[type=file]");
    const file = new File(
      [sourceFile],
      `clipboard-${Date.now()}.${extension}`,
      { type: sourceFile.type, lastModified: Date.now() },
    );
    const transfer = new DataTransfer();
    transfer.items.add(file);
    input.files = transfer.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
    showImagePreview(zone, file);
    setPasteStatus(zone, `Вставлено: ${file.name}`);
    return true;
  }

  document.addEventListener("paste", event => {
    const zone = event.target.closest?.("[data-image-paste-zone]");
    if (!zone) return;
    const imageItem = [...(event.clipboardData?.items || [])]
      .find(item => item.kind === "file" && item.type.startsWith("image/"));
    if (!imageItem) {
      setPasteStatus(zone, "В буфере нет изображения", true);
      return;
    }
    event.preventDefault();
    const file = imageItem.getAsFile();
    if (file) setImageFile(zone, file);
  });

  document.addEventListener("change", event => {
    if (event.target.matches('input[type="checkbox"][name$="-clear"]')) {
      const zone = event.target.closest(".field").querySelector("[data-image-paste-zone]");
      const preview = zone.querySelector("[data-image-preview]");
      if (event.target.checked) {
        if (preview.dataset.objectUrl) URL.revokeObjectURL(preview.dataset.objectUrl);
        preview.removeAttribute("src");
        delete preview.dataset.objectUrl;
        zone.querySelector("[data-image-preview-wrap]").classList.add("is-empty");
        setPasteStatus(zone, "Фотография будет удалена после сохранения");
      }
      return;
    }
    if (!event.target.matches("input[data-image-input]")) return;
    const file = event.target.files?.[0];
    if (!file) return;
    const zone = event.target.closest(".field").querySelector("[data-image-paste-zone]");
    if (allowedImageTypes[file.type] && file.size <= maxImageSize) {
      showImagePreview(zone, file);
      setPasteStatus(zone, `Выбрано: ${file.name}`);
    }
  });

  document.addEventListener("click", event => {
    const zone = event.target.closest?.("[data-image-paste-zone]");
    if (zone) zone.focus();
  });

  document.querySelectorAll(".add-form").forEach(button => {
    button.addEventListener("click", () => addForm(button.dataset.prefix));
  });

  const form = document.getElementById("recipe-form");
  if (form) {
    form.addEventListener("submit", () => {
      updateOrder("ingredients");
      updateOrder("steps");
    });
  }

  updateOrder("ingredients");
  updateOrder("steps");
})();
