// Main page: mode buttons and the stream player.
'use strict';

const LABELS = {
  standby: 'Standby: nothing is running.',
  notify: 'Notify: listening for crying.',
  listen: 'Listen: live sound.',
  observe: 'Observe: live video and sound.',
};
const STREAM_RETRY_MS = 1000;
const STREAM_START_TIMEOUT_MS = 20000;

const buttons = document.querySelectorAll('[data-mode]');
const statusEl = document.getElementById('status');
const playerEl = document.getElementById('player');
const video = document.getElementById('video');
const unmuteBtn = document.getElementById('unmute');

let session = null;   // current WHEP session
let playToken = 0;    // bumps on every mode change, so stale retries give up

async function api(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
    credentials: 'same-origin',
  });
  if (response.status === 401) {
    window.location.href = '/login';
    throw new Error('logged out');
  }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function showMode(state) {
  buttons.forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.mode === state.mode)));
  statusEl.textContent = LABELS[state.mode] || state.mode;
  if (state.mode === 'notify') notifyStart(() => setMode('listen'));
  else notifyStop();
  startPlayer(state);
}

async function setMode(mode) {
  buttons.forEach((b) => { b.disabled = true; });
  statusEl.textContent = `Switching to ${mode}…`;
  try {
    showMode(await api('PUT', '/api/mode', { mode }));
  } catch (err) {
    statusEl.textContent = `Could not switch mode: ${err.message}`;
  } finally {
    buttons.forEach((b) => { b.disabled = false; });
  }
}

async function stopPlayer() {
  if (session) {
    const old = session;
    session = null;
    await old.stop();
  }
  playerEl.hidden = true;
  unmuteBtn.hidden = true;
}

async function startPlayer(state) {
  const token = ++playToken;
  await stopPlayer();
  if (!state.stream) return;

  playerEl.hidden = false;
  playerEl.classList.toggle('audio-only', state.mode === 'listen');
  const url = `${window.WHEP_BASE}/${state.stream}/whep`;
  const deadline = Date.now() + STREAM_START_TIMEOUT_MS;

  // The stream unit needs a moment after a mode switch before MediaMTX has the stream.
  while (token === playToken) {
    try {
      const s = await whepPlay(url, video);
      if (token !== playToken) { s.stop(); return; }
      session = s;
      statusEl.textContent = LABELS[state.mode];
      watchConnection(s, state, token);
      await startPlayback();
      return;
    } catch (err) {
      if (Date.now() > deadline) {
        statusEl.textContent = `Stream unavailable: ${err.message}`;
        return;
      }
      await new Promise((r) => setTimeout(r, STREAM_RETRY_MS));
    }
  }
}

// Reconnect if the connection drops, e.g. Wi-Fi roaming or the stream restarting.
function watchConnection(s, state, token) {
  s.pc.addEventListener('connectionstatechange', () => {
    if (token !== playToken) return;
    if (['failed', 'disconnected', 'closed'].includes(s.pc.connectionState)) {
      statusEl.textContent = 'Connection lost, reconnecting…';
      startPlayer(state);
    }
  });
}

// Browsers block sound until the user has interacted with the page; fall back to
// muted playback with a button to turn sound on.
async function startPlayback() {
  video.muted = false;
  try {
    await video.play();
    unmuteBtn.hidden = true;
  } catch (err) {
    video.muted = true;
    unmuteBtn.hidden = false;
    video.play().catch(() => {});
  }
}

unmuteBtn.addEventListener('click', () => {
  video.muted = false;
  video.play().catch(() => {});
  unmuteBtn.hidden = true;
});

buttons.forEach((b) => b.addEventListener('click', () => setMode(b.dataset.mode)));

// The board has no clock battery; without internet its clock is set from browsers.
api('POST', '/api/system/time', { time: Date.now() / 1000 }).catch(() => {});

api('GET', '/api/mode')
  .then(showMode)
  .catch((err) => { statusEl.textContent = `Could not read mode: ${err.message}`; });
