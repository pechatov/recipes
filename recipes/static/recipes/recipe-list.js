(() => {
  const grid = document.querySelector("[data-recipe-grid]");
  const input = document.querySelector("[data-recipe-search]");
  const form = document.querySelector("[data-recipe-filters]");
  if (!grid || !input || !form) return;

  const cards = [...grid.querySelectorAll("[data-recipe-card]")];
  const serverFiltered = form.dataset.serverFiltered === "true";
  const empty = document.querySelector("[data-live-empty]");
  const categoryLinks = [...document.querySelectorAll(".category-filters a")];
  const normalize = value => value
    .toLocaleLowerCase("ru")
    .normalize("NFKD")
    .replace(/\p{Diacritic}/gu, "")
    .replace(/ё/g, "е")
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim();
  const isSubsequence = (needle, haystack) => {
    let index = 0;
    for (const character of haystack) {
      if (character === needle[index]) index += 1;
      if (index === needle.length) return true;
    }
    return needle.length === 0;
  };
  const matches = (query, text) => normalize(query).split(/\s+/).filter(Boolean).every(token =>
    text.includes(token) || (token.length > 2 && isSubsequence(token, text))
  );
  const filterCards = () => {
    const query = input.value;
    let visible = 0;
    cards.forEach(card => {
      const show = matches(query, normalize(card.dataset.searchText || ""));
      card.hidden = !show;
      if (show) visible += 1;
    });
    if (empty) empty.hidden = visible !== 0;
    grid.hidden = visible === 0;
  };
  const syncCategoryLinks = () => {
    const author = form.querySelector("[data-author-filter]")?.value || "";
    categoryLinks.forEach(link => {
      const url = new URL(link.href, window.location.href);
      input.value ? url.searchParams.set("q", input.value) : url.searchParams.delete("q");
      author ? url.searchParams.set("author", author) : url.searchParams.delete("author");
      link.href = `${url.pathname}${url.search}`;
    });
  };

  input.addEventListener("input", () => {
    filterCards();
    syncCategoryLinks();
  });
  form.addEventListener("submit", event => {
    if (serverFiltered) return;
    event.preventDefault();
    filterCards();
  });
  form.querySelector("[data-author-filter]")?.addEventListener("change", () => form.submit());
  filterCards();
  syncCategoryLinks();
})();
