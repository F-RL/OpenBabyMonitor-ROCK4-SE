// Notify mode: live readings from the cry detector, alerts, and Notify settings.
// app.js calls notifyStart() / notifyStop() when the mode changes.
'use strict';

const NOTIFY_POLL_MS = 2000;
const NOTIFY_STALE_S = 30;  // no reading for this long: the detector is not running

const ALERT_TEXTS = {
  crying: 'Crying detected',
  babbling: 'Babbling detected',
  crying_and_babbling: 'Crying and babbling detected',
  crying_or_babbling: 'Baby sounds detected',
  sound: 'Loud sound detected',
};

const SETTING_LABELS = {
  model: ['Detection method', {
    large_network: 'Large neural network (most accurate)',
    small_network: 'Small neural network',
    sound_level_threshold: 'Loudness only (any sound)',
  }],
  notify_on_crying: ['Alert on crying'],
  notify_on_babbling: ['Alert on babbling'],
  notify_and_or: ['When both are on, alert on', { or: 'either one', and: 'both together' }],
  probability_threshold: ['Certainty needed per recording (%)'],
  fraction_threshold: ['Share of recent recordings that must match (%)'],
  consecutive_recordings: ['Number of recent recordings considered (~2 s each)'],
  min_notification_interval: ['Minimum time between alerts (s)'],
  min_sound_contrast: ['Minimum loudness above background (dB)'],
  background_loudness_level_offset: ['Background level calibration (dB)'],
  amplification: ['Amplification before the network'],
  interval: ['Pause between recordings (s)'],
  autoplay_on_notify: ['Switch to Listen when an alert arrives'],
};
// Settings that do nothing with the loudness-only method.
const NETWORK_ONLY = ['notify_on_crying', 'notify_on_babbling', 'notify_and_or',
  'probability_threshold', 'amplification'];

const notifyEl = document.getElementById('notify');
const alertEl = document.getElementById('alert');
const alertText = document.getElementById('alert-text');
const alertTime = document.getElementById('alert-time');
const readingState = document.getElementById('reading-state');
const enableAlertsBtn = document.getElementById('enable-alerts');
const alertsState = document.getElementById('alerts-state');
const settingsForm = document.getElementById('notify-settings');
const settingsMessage = document.getElementById('settings-message');

let notifyTimer = null;
let notifyLastId = null;
let notifySettings = null;
let notifyOnAutoplay = null;
let audioCtx = null;
let wakeLock = null;

async function notifyApi(method, path, body) {
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

// --- Readings and alerts ---

function showReading(status) {
  const probsEl = document.getElementById('reading-probabilities');
  if (!status) {
    readingState.textContent = 'Waiting for the first recording…';
    return;
  }
  readingState.textContent = status.age > NOTIFY_STALE_S
    ? `No recording for ${Math.round(status.age)} s: the detector may have stopped.`
    : 'Listening.';
  document.getElementById('reading-background').textContent = `${status.background.toFixed(1)} dB`;
  document.getElementById('reading-contrast').textContent = `${status.contrast.toFixed(1)} dB`;
  probsEl.hidden = !status.probabilities;
  if (status.probabilities) {
    for (const [name, p] of Object.entries(status.probabilities)) {
      document.getElementById(`p-${name}`).value = p;
      document.getElementById(`p-${name}-text`).textContent = `${Math.round(p * 100)}%`;
    }
  }
}

async function pollNotify() {
  try {
    const query = notifyLastId === null ? '' : `?after=${notifyLastId}`;
    const data = await notifyApi('GET', `/api/notify${query}`);
    notifyLastId = data.last_id;
    showReading(data.status);
    if (data.events.length) raiseAlert(data.events[data.events.length - 1]);
  } catch (err) {
    readingState.textContent = `Could not reach the monitor: ${err.message}`;
  }
}

function raiseAlert(event) {
  const text = ALERT_TEXTS[event.kind] || event.kind;
  alertText.textContent = text;
  alertTime.textContent = `at ${new Date(event.time * 1000).toLocaleTimeString()}`;
  alertEl.hidden = false;
  playAlarm();
  if (navigator.vibrate) navigator.vibrate([400, 200, 400, 200, 400]);
  showSystemNotification(text);
  if (notifySettings && notifySettings.autoplay_on_notify && notifyOnAutoplay) {
    notifyOnAutoplay();
  }
}

document.getElementById('alert-dismiss').addEventListener('click', () => {
  alertEl.hidden = true;
});

// Three short beeps. Browsers only allow sound after the user has tapped something,
// which is what the "Enable alarm" button is for.
function playAlarm() {
  if (!audioCtx || audioCtx.state !== 'running') return;
  const start = audioCtx.currentTime + 0.05;
  for (let i = 0; i < 3; i++) {
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.frequency.value = 880;
    gain.gain.setValueAtTime(0.0001, start + i * 0.6);
    gain.gain.exponentialRampToValueAtTime(0.5, start + i * 0.6 + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, start + i * 0.6 + 0.4);
    osc.connect(gain).connect(audioCtx.destination);
    osc.start(start + i * 0.6);
    osc.stop(start + i * 0.6 + 0.45);
  }
}

function showSystemNotification(text) {
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  try {
    new Notification('Baby monitor', { body: text, tag: 'babymonitor', renotify: true });
  } catch (err) {
    // Android Chrome only allows notifications from a service worker; the page
    // alert, sound and vibration still work.
  }
}

async function enableAlerts() {
  const notes = [];
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    await audioCtx.resume();
    playAlarm();
    notes.push('alarm sound on');
  } catch (err) {
    notes.push('no sound');
  }
  if ('Notification' in window && Notification.permission === 'default') {
    try { await Notification.requestPermission(); } catch (err) { /* ignored */ }
  }
  if ('Notification' in window && Notification.permission === 'granted') {
    notes.push('notifications on');
  }
  await keepScreenOn();
  if (wakeLock) notes.push('screen kept on');
  alertsState.textContent = notes.join(', ');
}

// A phone stops running the page when its screen turns off, so keep it on while
// the page is visible in Notify mode.
async function keepScreenOn() {
  if (!('wakeLock' in navigator) || wakeLock) return;
  try {
    wakeLock = await navigator.wakeLock.request('screen');
    wakeLock.addEventListener('release', () => { wakeLock = null; });
  } catch (err) {
    wakeLock = null;
  }
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && notifyTimer && audioCtx) keepScreenOn();
});

enableAlertsBtn.addEventListener('click', enableAlerts);

// --- Settings form ---

function buildSettingsForm(data) {
  notifySettings = data.values;
  settingsForm.replaceChildren();
  for (const [name, spec] of Object.entries(data.spec)) {
    const [labelText, valueLabels] = SETTING_LABELS[name] || [name];
    const row = document.createElement('label');
    row.className = `setting setting-${spec.type}`;
    row.dataset.name = name;
    const caption = document.createElement('span');
    caption.textContent = labelText;
    let input;
    if (spec.type === 'choice') {
      input = document.createElement('select');
      for (const value of spec.values) {
        const option = document.createElement('option');
        option.value = value;
        option.textContent = (valueLabels && valueLabels[value]) || value;
        input.append(option);
      }
      input.value = data.values[name];
    } else if (spec.type === 'bool') {
      input = document.createElement('input');
      input.type = 'checkbox';
      input.checked = data.values[name];
    } else {
      input = document.createElement('input');
      input.type = 'number';
      input.min = spec.min;
      input.max = spec.max;
      input.step = spec.type === 'int' ? 1 : 0.1;
      input.value = data.values[name];
      input.required = true;
    }
    input.name = name;
    input.addEventListener('change', () => saveSetting(name, input, spec));
    if (spec.type === 'bool') row.append(input, caption);
    else row.append(caption, input);
    settingsForm.append(row);
  }
  updateDisabledSettings();
}

function updateDisabledSettings() {
  const loudnessOnly = notifySettings.model === 'sound_level_threshold';
  for (const name of NETWORK_ONLY) {
    const input = settingsForm.elements[name];
    if (input) input.disabled = loudnessOnly;
  }
  const andOr = settingsForm.elements.notify_and_or;
  if (andOr && !loudnessOnly) {
    andOr.disabled = !(notifySettings.notify_on_crying && notifySettings.notify_on_babbling);
  }
}

async function saveSetting(name, input, spec) {
  let value;
  if (spec.type === 'bool') value = input.checked;
  else if (spec.type === 'choice') value = input.value;
  else value = Number(input.value);
  try {
    const data = await notifyApi('PUT', '/api/notify/settings', { [name]: value });
    notifySettings = data.values;
    settingsMessage.textContent = 'Saved. The detector picks it up with the next recording.';
    updateDisabledSettings();
  } catch (err) {
    settingsMessage.textContent = `Not saved: ${err.message}`;
    // Put the stored value back.
    if (spec.type === 'bool') input.checked = notifySettings[name];
    else input.value = notifySettings[name];
  }
}

async function loadSettings() {
  try {
    buildSettingsForm(await notifyApi('GET', '/api/notify/settings'));
  } catch (err) {
    settingsMessage.textContent = `Could not load settings: ${err.message}`;
  }
}

// --- Called by app.js ---

function notifyStart(onAutoplay) {
  notifyOnAutoplay = onAutoplay;
  notifyEl.hidden = false;
  if (notifyTimer) return;
  notifyLastId = null;
  loadSettings();
  pollNotify();
  notifyTimer = setInterval(pollNotify, NOTIFY_POLL_MS);
  if (audioCtx) keepScreenOn();
}

function notifyStop() {
  notifyEl.hidden = true;
  if (notifyTimer) {
    clearInterval(notifyTimer);
    notifyTimer = null;
  }
  if (wakeLock) {
    wakeLock.release().catch(() => {});
    wakeLock = null;
  }
}
