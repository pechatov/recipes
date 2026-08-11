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
  const editDistance = (left, right) => {
    if (left === right) return 0;
    let previousPrevious = null;
    let previous = Array.from({length: right.length + 1}, (_, index) => index);
    for (let leftIndex = 1; leftIndex <= left.length; leftIndex += 1) {
      const current = [leftIndex];
      for (let rightIndex = 1; rightIndex <= right.length; rightIndex += 1) {
        current.push(Math.min(
          current[current.length - 1] + 1,
          previous[rightIndex] + 1,
          previous[rightIndex - 1] + (left[leftIndex - 1] === right[rightIndex - 1] ? 0 : 1)
        ));
        if (
          previousPrevious
          && leftIndex > 1
          && rightIndex > 1
          && left[leftIndex - 1] === right[rightIndex - 2]
          && left[leftIndex - 2] === right[rightIndex - 1]
        ) {
          current[current.length - 1] = Math.min(
            current[current.length - 1],
            previousPrevious[rightIndex - 2] + 1
          );
        }
      }
      previousPrevious = previous;
      previous = current;
    }
    return previous[previous.length - 1];
  };
  const tokenMatches = (token, text) => {
    if (text.includes(token)) return true;
    if (token.length <= 2) return false;
    const words = text.split(/\s+/).filter(Boolean);
    if (words.some(word => isSubsequence(token, word))) return true;
    const maximumDistance = token.length <= 5 ? 1 : token.length <= 8 ? 2 : 3;
    return words.some(word =>
      Math.abs(token.length - word.length) <= maximumDistance
      && editDistance(token, word) <= maximumDistance
    );
  };
  const matches = (query, text) => normalize(query).split(/\s+/).filter(Boolean).every(token =>
    tokenMatches(token, text)
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
