// Settings page: Wi-Fi, hotspot, region, host name, password, power.
'use strict';

const CONNECT_POLL_MS = 3000;
const COUNTRY_CODES = ('AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH ' +
  'BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW ' +
  'CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI ' +
  'GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO ' +
  'JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH ' +
  'MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA ' +
  'PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL ' +
  'SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US ' +
  'UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW').split(' ');

const $ = (id) => document.getElementById(id);
const messageEl = $('message');
let messageTimer = null;
let connectPoll = null;

async function api(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
    credentials: 'same-origin',
  });
  if (response.status === 401) {
    window.location.href = '/login?next=/settings';
    throw new Error('logged out');
  }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function say(text, isError = false) {
  messageEl.textContent = text;
  messageEl.classList.toggle('error', isError);
  messageEl.hidden = false;
  clearTimeout(messageTimer);
  messageTimer = setTimeout(() => { messageEl.hidden = true; }, isError ? 10000 : 5000);
}

// Run an action for a form/button, disabling it meanwhile and reporting the outcome.
async function act(control, work, success) {
  control.disabled = true;
  try {
    const result = await work();
    if (success) say(typeof success === 'function' ? success(result) : success);
    return result;
  } catch (err) {
    say(err.message, true);
    return null;
  } finally {
    control.disabled = false;
  }
}

function formatDuration(seconds) {
  if (seconds == null) return '–';
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return d ? `${d} d ${h} h` : h ? `${h} h ${m} min` : `${m} min`;
}

function element(tag, props = {}, ...children) {
  const el = Object.assign(document.createElement(tag), props);
  el.append(...children);
  return el;
}

// --- Status ---

async function loadSystem() {
  const s = await api('GET', '/api/system');
  $('s-hostname').textContent = s.hostname;
  const clock = new Date(s.time * 1000);
  $('s-clock').textContent = `${clock.toLocaleString()} (${s.timezone}` +
    `${s.ntp_synced ? ', synchronised' : ', set from a browser'})`;
  $('s-temperature').textContent = s.temperature_c == null ? '–' : `${s.temperature_c} °C`;
  $('s-uptime').textContent = formatDuration(s.uptime_s);
  document.forms['hostname-form'].elements.hostname.value = s.hostname;
  return s;
}

async function loadTimezones(current) {
  const { timezones } = await api('GET', '/api/system/timezones');
  const select = $('timezone');
  select.replaceChildren(...timezones.map((tz) => element('option', { value: tz, textContent: tz })));
  select.value = current;
}

function fillCountries(current) {
  let names = null;
  try { names = new Intl.DisplayNames([navigator.language], { type: 'region' }); } catch (e) { /* old browser */ }
  const options = COUNTRY_CODES
    .map((code) => ({ code, name: names ? `${names.of(code)} (${code})` : code }))
    .sort((a, b) => a.name.localeCompare(b.name));
  const select = $('country');
  if (select.options.length) return;  // filled already; keep the user's selection
  select.replaceChildren(
    element('option', { value: '', textContent: 'Not set' }),
    ...options.map((o) => element('option', { value: o.code, textContent: o.name })));
  select.value = current || '';
}

// --- Network ---

function describeNetwork(n) {
  const parts = n.devices
    .filter((d) => d.state.startsWith('connected'))
    .map((d) => (d.connection === 'babymonitor-hotspot'
      ? `own hotspot (${d.device})` : `${d.connection} (${d.device})`));
  return parts.length ? parts.join(', ') : 'not connected';
}

function showConnectState(c) {
  const el = $('connect-state');
  if (!c) { el.textContent = ''; return; }
  el.classList.toggle('error', c.state === 'failed');
  el.textContent = {
    connecting: `Joining ${c.ssid}…`,
    connected: `Joined ${c.ssid}.`,
    failed: `Could not join ${c.ssid}: ${c.error}`,
  }[c.state] || '';
}

async function loadNetwork() {
  const n = await api('GET', '/api/network');
  $('s-network').textContent = describeNetwork(n);
  showConnectState(n.connect);

  const saved = $('saved');
  saved.replaceChildren(...n.saved.map((net) => {
    const forget = element('button', { type: 'button', className: 'link', textContent: 'Forget' });
    forget.addEventListener('click', () => {
      if (!confirm(`Forget ${net.name}? The monitor won't join it automatically any more.`)) return;
      act(forget, () => api('DELETE', `/api/network/saved/${net.uuid}`), `Forgot ${net.name}.`)
        .then(loadNetwork);
    });
    return element('li', {}, element('span', { textContent: net.name }), forget);
  }));
  if (!n.saved.length) saved.append(element('li', { className: 'muted', textContent: 'None yet.' }));

  const form = document.forms['hotspot-form'];
  if (n.hotspot && !form.dataset.loaded) {
    form.elements.ssid.value = n.hotspot.ssid;
    form.elements.password.value = n.hotspot.password;
    form.elements.channel.value = String(n.hotspot.channel);
    form.dataset.loaded = '1';
  }
  $('hotspot-state').textContent = !n.wifi_device ? 'This device has no Wi-Fi.'
    : !n.hotspot ? 'Not set up yet: without a hotspot name and password the monitor can\'t fall back to its own network.'
      : n.hotspot_active ? `The hotspot "${n.hotspot.ssid}" is on.` : 'The hotspot is off.';
  $('hotspot-start').hidden = n.hotspot_active;
  $('hotspot-stop').hidden = !n.hotspot_active;
  fillCountries(n.country);

  if (n.connect && n.connect.state === 'connecting') {
    clearTimeout(connectPoll);
    connectPoll = setTimeout(() => loadNetwork().catch(() => {}), CONNECT_POLL_MS);
  }
  return n;
}

function openConnectForm(ssid, secured) {
  const form = document.forms['connect-form'];
  form.hidden = false;
  form.elements.ssid.value = ssid || '';
  form.elements.password.value = '';
  form.elements.password.placeholder = secured === false ? 'Open network: no password'
    : 'Leave empty for a saved network';
  (ssid ? form.elements.password : form.elements.ssid).focus();
}

$('scan').addEventListener('click', async (e) => {
  const result = await act(e.target, () => api('GET', '/api/network/scan'));
  if (!result) return;
  const list = $('scan-results');
  list.replaceChildren(...result.networks.map((net) => {
    const button = element('button', { type: 'button', className: 'network' },
      element('span', { textContent: net.ssid + (net.in_use ? ' (connected)' : '') }),
      element('span', { className: 'muted', textContent: `${net.security ? '🔒 ' : ''}${net.signal}%` }));
    button.addEventListener('click', () => openConnectForm(net.ssid, Boolean(net.security)));
    return element('li', {}, button);
  }));
  const other = element('button', { type: 'button', className: 'link', textContent: 'Other (hidden) network…' });
  other.addEventListener('click', () => openConnectForm('', true));
  list.append(element('li', {}, other));
});

$('connect-cancel').addEventListener('click', () => { document.forms['connect-form'].hidden = true; });

document.forms['connect-form'].addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target;
  const ssid = form.elements.ssid.value;
  const ok = await act(form.querySelector('[type=submit]'),
    () => api('POST', '/api/network/connect', { ssid, password: form.elements.password.value }),
    `Joining ${ssid}…`);
  if (ok) {
    form.hidden = true;
    loadNetwork().catch(() => {});
  }
});

document.forms['hotspot-form'].addEventListener('submit', (e) => {
  e.preventDefault();
  const f = e.target.elements;
  act(e.target.querySelector('[type=submit]'), () => api('PUT', '/api/network/hotspot', {
    ssid: f.ssid.value, password: f.password.value, channel: Number(f.channel.value),
  }), 'Hotspot saved.').then(loadNetwork);
});

$('hotspot-start').addEventListener('click', (e) => {
  if (!confirm('Turn the hotspot on? The monitor leaves its current Wi-Fi network; '
    + 'connect to the hotspot and open https://192.168.4.1')) return;
  act(e.target, () => api('POST', '/api/network/hotspot/start'), 'Hotspot on.').then(loadNetwork);
});

$('hotspot-stop').addEventListener('click', (e) => {
  if (!confirm('Turn the hotspot off? The monitor joins a saved network if one is in range; '
    + 'otherwise the hotspot comes back in about two minutes.')) return;
  act(e.target, () => api('POST', '/api/network/hotspot/stop'), 'Hotspot off.').then(loadNetwork);
});

// --- Phone notifications ---

async function loadPush() {
  const p = await api('GET', '/api/notify/push');
  $('push-off').hidden = p.enabled;
  $('push-on').hidden = !p.enabled;
  $('push-server').textContent = p.server;
  $('push-topic').textContent = p.topic;
  $('push-user').textContent = p.user;
  $('push-cert').hidden = !p.certificate;
  $('push-cert-missing').hidden = p.certificate;
}

$('push-test').addEventListener('click', (e) => {
  act(e.target, () => api('POST', '/api/notify/push/test'),
    'Test sent. It should appear on every subscribed phone within a few seconds.');
});

// --- Region, host name, password ---

document.forms['country-form'].addEventListener('submit', (e) => {
  e.preventDefault();
  const country = e.target.elements.country.value;
  if (!country) return;
  act(e.target.querySelector('[type=submit]'),
    () => api('PUT', '/api/network/country', { country }), 'Wi-Fi country saved.');
});

document.forms['timezone-form'].addEventListener('submit', (e) => {
  e.preventDefault();
  act(e.target.querySelector('[type=submit]'),
    () => api('PUT', '/api/system/timezone', { timezone: e.target.elements.timezone.value }),
    'Time zone saved.').then(loadSystem);
});

document.forms['hostname-form'].addEventListener('submit', (e) => {
  e.preventDefault();
  act(e.target.querySelector('[type=submit]'),
    () => api('PUT', '/api/system/hostname', { hostname: e.target.elements.hostname.value }),
    'Host name saved. Restart the monitor to finish.').then(loadSystem);
});

document.forms['password-form'].addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target.elements;
  if (f.new.value !== f.repeat.value) {
    say('The new passwords differ.', true);
    return;
  }
  const ok = await act(e.target.querySelector('[type=submit]'),
    () => api('POST', '/api/password', { current: f.current.value, new: f.new.value }),
    'Password changed.');
  if (ok) e.target.reset();
});

// --- Power ---

for (const [id, question, done] of [
  ['reboot', 'Restart the monitor? It is back in about a minute.', 'Restarting…'],
  ['shutdown', 'Shut the monitor down? It stays off until its power is plugged back in.', 'Shutting down…'],
]) {
  $(id).addEventListener('click', (e) => {
    if (confirm(question)) act(e.target, () => api('POST', `/api/system/${id}`), done);
  });
}

// --- Start ---

loadSystem()
  .then((s) => loadTimezones(s.timezone))
  .catch((err) => say(`Could not read device settings: ${err.message}`, true));
loadNetwork().catch((err) => say(`Could not read network settings: ${err.message}`, true));
loadPush().catch((err) => say(`Could not read notification settings: ${err.message}`, true));
