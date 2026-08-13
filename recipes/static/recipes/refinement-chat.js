(() => {
  const chat = document.querySelector("[data-refinement-status-url]");
  const chatPanel = document.querySelector("#agent-chat");
  const thread = chatPanel?.querySelector(".chat-thread");
  if (thread) thread.scrollTop = thread.scrollHeight;
  const reply = chat?.querySelector("[data-active-refinement-reply]");
  const recipeForm = document.querySelector("#recipe-form");
  if (!chat || !reply) return;

  let formIsDirty = false;
  recipeForm?.addEventListener("input", () => { formIsDirty = true; });
  recipeForm?.addEventListener("change", () => { formIsDirty = true; });

  const poll = async () => {
    try {
      const response = await fetch(chat.dataset.refinementStatusUrl, {
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error("status request failed");
      const result = await response.json();
      if (result.status === "pending" || result.status === "processing") {
        window.setTimeout(poll, 3000);
        return;
      }
      if (result.status === "completed" && !formIsDirty) {
        window.location.reload();
        return;
      }
      reply.className = `chat-message is-agent status-${result.status}`;
      reply.textContent = result.status === "completed"
        ? "Готово. Обновите страницу, чтобы увидеть новый рецепт — в форме есть несохранённые изменения."
        : `Не получилось: ${result.error || "Гермес не смог переработать рецепт."}`;
    } catch (error) {
      window.setTimeout(poll, 5000);
    }
  };

  window.setTimeout(poll, 2000);
})();
