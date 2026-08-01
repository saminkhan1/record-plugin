const dot = document.getElementById("dot");
const stateLabel = document.getElementById("state");
const shot = document.getElementById("shot");
const hint = document.getElementById("hint");

function send(action) {
  return chrome.runtime.sendMessage({ action });
}

function paint(status) {
  const chromeSegmentActive = !!status.chromeSegmentActive;
  const nativeActive = !!status.native?.active;
  dot.classList.toggle("live", chromeSegmentActive);
  stateLabel.textContent = chromeSegmentActive
    ? `Chrome segment active: ${status.activeTarget?.title || status.activeTarget?.url || "active tab"}`
    : nativeActive
      ? "Record recording ready"
      : "No active Record recording";
  shot.disabled = !chromeSegmentActive;
  hint.textContent = nativeActive
    ? "Only the active Chrome tab and its frames are written into the active local Record bundle."
    : "Start a Record recording first, then use Chrome to activate the Chrome segment.";
  hint.className = "hint";
}

async function refresh() {
  try {
    const status = await send("status");
    paint(status);
  } catch (err) {
    stateLabel.textContent = "Unavailable";
    hint.textContent = String(err?.message || err);
    hint.className = "hint error";
  }
}

shot.addEventListener("click", async () => {
  shot.disabled = true;
  try {
    await send("screenshot");
    hint.textContent = "Screenshot captured.";
    hint.className = "hint";
  } catch (err) {
    hint.textContent = String(err?.message || err);
    hint.className = "hint error";
  } finally {
    shot.disabled = false;
  }
});

refresh();
setInterval(refresh, 1500);
