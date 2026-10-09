"use strict";

function text(tag, value, className) {
  const node = document.createElement(tag);
  node.textContent = value || "";
  if (className) node.className = className;
  return node;
}

function renderCard(card) {
  const article = document.createElement("article");
  article.className = "board-card";
  const link = document.createElement("a");
  link.className = "board-card-main";
  link.href = `/ui/tasks/${encodeURIComponent(card.id)}`;
  link.append(text("span", card.external_id, "board-card-id"));
  link.append(text("strong", card.title));
  article.append(link, text("span", card.project, "board-card-project"));
  const facts = document.createElement("p");
  facts.textContent = `${card.harness || "not started"} · ${card.tier} · ${card.waiting_on} · ${card.age.label}`;
  article.append(facts);
  if (card.actions.length) {
    const actions = document.createElement("div");
    actions.className = "board-card-actions";
    card.actions.forEach((action, index) => {
      const anchor = document.createElement("a");
      anchor.className = `lat-btn ${index === 0 ? "lat-btn--primary" : "lat-btn--ghost"}`;
      anchor.href = `/ui/tasks/${encodeURIComponent(card.id)}#actions`;
      anchor.textContent = action.label;
      actions.append(anchor);
    });
    article.append(actions);
  }
  return article;
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-confirm='true']");
  if (!button || button.dataset.confirmed === "true") return;
  event.preventDefault();
  button.dataset.confirmed = "true";
  button.textContent = `Confirm ${button.textContent}`;
  window.setTimeout(() => {
    button.dataset.confirmed = "false";
    button.textContent = button.textContent.replace(/^Confirm /, "");
  }, 5000);
});

document.querySelectorAll("details.board-lane").forEach((lane) => {
  lane.addEventListener("toggle", async () => {
    if (!lane.open || lane.dataset.loaded === "true") return;
    const key = lane.dataset.lane;
    const target = lane.querySelector(`[data-cards="${key}"]`);
    target.append(text("p", "Loading...", "admin-empty"));
    const response = await fetch(`/ui/board?lane=${encodeURIComponent(key)}`, {
      headers: {Accept: "application/json"},
    });
    if (!response.ok) {
      target.replaceChildren(text("p", "Could not load this lane.", "admin-empty"));
      return;
    }
    const documentBody = await response.json();
    const found = documentBody.lanes.find((item) => item.key === key);
    target.replaceChildren(...found.cards.map(renderCard));
    if (!found.cards.length) target.append(text("p", "No cards.", "admin-empty"));
    lane.dataset.loaded = "true";
  });
});
