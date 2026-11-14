const byId = (id) => document.getElementById(id);
let populated = false;
let busy = false;
window.addEventListener('demo-run-reset', () => {
  byId('edge-result').textContent = '';
  byId('edge-predictions').textContent = '';
  refresh();
});
async function refresh() {
  try {
    const response = await fetch('/api/edge/status', {cache: 'no-store', signal: AbortSignal.timeout(10000)});
    if (response.status === 404) throw new Error('The running station is outdated. Restart it with .venv/bin/python -m emulators.demo, then refresh this page.');
    if (!response.ok) throw new Error(`Edge status failed (HTTP ${response.status}). Check the station log and restart the demo.`);
    const state = await response.json();
    byId('edge-state').textContent = state.connected ? 'Connected' : 'Offline';
    byId('edge-panel').dataset.connected = String(state.connected);
    byId('edge-description').textContent = state.backend === 'off'
      ? 'Launch with .venv/bin/python -m emulators.demo to connect sound detection.'
      : `${state.label} · ${state.platform}${state.backend === 'arm64' ? ` · ${state.profile}` : ''}`;
    byId('edge-controls').hidden = !state.connected;
    if (state.error) byId('edge-result').textContent = state.error;
    if (state.connected && !populated) {
      for (const node of state.nodes) {
        const option = new Option(node.name, node.id);
        option.selected = node.id === 5;
        byId('edge-node').add(option);
      }
      populated = true;
      byId('edge-panel').open = !location.pathname.startsWith('/incident/') && byId('tab-results').getAttribute('aria-selected') !== 'true';
    }
    if (state.connected) {
      const last = state.recent.at(-1);
      byId('edge-metrics').textContent = `${(state.model_bytes / 1024).toFixed(1)} KiB model · ${state.windows} windows classified` +
        (last ? ` · ${last.latency_ms.toFixed(2)} ms inference / ${last.roundtrip_ms.toFixed(2)} ms round trip` : '');
      byId('edge-predictions').textContent = state.recent.slice(-4).map(p => `${p.sound} ${Math.round(p.confidence * 100)}%`).join(' → ');
    }
  } catch (error) {
    byId('edge-state').textContent = 'Unavailable';
    byId('edge-panel').dataset.connected = 'false';
    byId('edge-controls').hidden = true;
    byId('edge-description').textContent = error.name === 'TimeoutError' ? 'Edge worker is not responding. Restart the station to reconnect.' : error.message;
  }
}
byId('edge-play').addEventListener('click', async () => {
  if (busy) return;
  busy = true;
  byId('edge-panel').classList.add('edge-running');
  byId('edge-play').disabled = true;
  byId('edge-result').textContent = 'Classifying recording in the edge worker…';
  try {
    const response = await fetch('/api/edge/play', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({sound: byId('edge-sound').value, node_id: Number(byId('edge-node').value)}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'Detection failed');
    byId('edge-result').textContent = `${result.emitted} signed detection(s) emitted. ${result.note}`;
    if (result.emitted) byId('tab-feed').click();
    await refresh();
  } catch (error) {
    byId('edge-result').textContent = error.message;
  } finally {
    busy = false;
    byId('edge-panel').classList.remove('edge-running');
    byId('edge-play').disabled = false;
  }
});
async function poll() {
  await refresh();
  setTimeout(poll, 1500);
}
poll();
