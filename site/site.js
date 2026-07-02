
const buttons = Array.from(document.querySelectorAll(".filter-button"));
const cards = Array.from(document.querySelectorAll(".article-card"));
const listenButtons = Array.from(document.querySelectorAll(".listen-button"));
const cardListenButtons = Array.from(document.querySelectorAll(".speak-card-button"));
const statusNode = document.querySelector(".listen-status");
const synth = window.speechSynthesis;

function setStatus(message) {
  if (statusNode) {
    statusNode.textContent = message;
  }
}

function stopReading() {
  if (!synth) {
    setStatus("Speech is not supported in this browser.");
    return;
  }
  synth.cancel();
  setStatus("Playback stopped.");
}

function buildSpeechTextFromCard(card) {
  const button = card.querySelector(".speak-card-button");
  if (!button) {
    return "";
  }

  const parts = [
    button.dataset.speechMeta,
    button.dataset.speechTitle,
    button.dataset.speechSummary,
  ].filter(Boolean);

  return parts.join(". ");
}

function speakText(text, label) {
  if (!synth || typeof SpeechSynthesisUtterance === "undefined") {
    setStatus("Speech is not supported in this browser.");
    return;
  }

  const normalized = text.trim();
  if (!normalized) {
    setStatus("Nothing available to read.");
    return;
  }

  synth.cancel();
  const utterance = new SpeechSynthesisUtterance(normalized);
  utterance.rate = 1;
  utterance.pitch = 1;
  utterance.onstart = () => setStatus(`Reading ${label}.`);
  utterance.onend = () => setStatus(`Finished reading ${label}.`);
  utterance.onerror = () => setStatus(`Unable to read ${label}.`);
  synth.speak(utterance);
}

function getVisibleCards() {
  return cards.filter((card) => card.dataset.hidden !== "true");
}

for (const button of buttons) {
  button.addEventListener("click", () => {
    const filter = button.dataset.filter;

    for (const other of buttons) {
      other.classList.toggle("is-active", other === button);
    }

    for (const card of cards) {
      const visible = filter === "all" || card.dataset.group === filter;
      card.dataset.hidden = String(!visible);
    }
  });
}

for (const button of listenButtons) {
  button.addEventListener("click", () => {
    const target = button.dataset.listenTarget;

    if (target === "stop") {
      stopReading();
      return;
    }

    if (target === "lead") {
      const leadCard = document.querySelector(".article-card--featured");
      speakText(leadCard ? buildSpeechTextFromCard(leadCard) : "", "the lead story");
      return;
    }

    const visibleCards = getVisibleCards();
    const text = visibleCards.map(buildSpeechTextFromCard).filter(Boolean).join(". ");
    speakText(text, "the visible stories");
  });
}

for (const button of cardListenButtons) {
  button.addEventListener("click", () => {
    const card = button.closest(".article-card");
    speakText(card ? buildSpeechTextFromCard(card) : "", "the selected story");
  });
}
