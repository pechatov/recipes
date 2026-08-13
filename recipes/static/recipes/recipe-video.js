(() => {
  const disclosure = document.querySelector("[data-recipe-video-disclosure]");
  const player = document.querySelector("[data-recipe-video]");
  if (!disclosure || !player) return;

  function showVideo(seconds = null, autoplay = false) {
    const parameters = new URLSearchParams({ rel: "0" });
    if (seconds !== null) parameters.set("start", String(seconds));
    if (autoplay) parameters.set("autoplay", "1");
    const source = `${player.dataset.embedUrl}?${parameters}`;
    if (player.src !== source) player.src = source;
  }

  disclosure.addEventListener("toggle", () => {
    if (disclosure.open && !player.getAttribute("src")) showVideo();
  });

  document.addEventListener("click", event => {
    const link = event.target.closest?.("[data-video-start]");
    if (!link) return;

    const seconds = Number.parseInt(link.dataset.videoStart, 10);
    if (!Number.isSafeInteger(seconds) || seconds < 0) return;

    event.preventDefault();
    disclosure.open = true;
    showVideo(seconds, true);
    requestAnimationFrame(() => {
      disclosure.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
    });
  });
})();
