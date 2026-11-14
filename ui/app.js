const statusEl = document.getElementById("status");
const modelEl = document.getElementById("model");

async function loadHealth() {
  try {
    const res = await fetch("/api/health");
    if (!res.ok) throw new Error(res.status);
    const data = await res.json();
    modelEl.textContent = data.model;
    modelEl.hidden = false;
    statusEl.textContent = "Listening";
  } catch {
    statusEl.textContent = "Station offline";
  }
}

loadHealth();
