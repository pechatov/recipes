(() => {
  const player = document.querySelector("[data-recipe-video]");
  if (!player) return;

  document.addEventListener("click", event => {
    const link = event.target.closest?.("[data-video-start]");
    if (!link) return;

    const seconds = Number.parseInt(link.dataset.videoStart, 10);
    if (!Number.isSafeInteger(seconds) || seconds < 0) return;

    event.preventDefault();
    const separator = player.dataset.embedUrl.includes("?") ? "&" : "?";
    player.src = `${player.dataset.embedUrl}${separator}start=${seconds}&autoplay=1&rel=0`;
    document.getElementById("recipe-video")?.scrollIntoView({
      behavior: "smooth",
      block: "center",
    });
  });
})();
