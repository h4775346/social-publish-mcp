const el = id => document.getElementById(id);
let portTouched = false;

const VIEW = { // service-worker state -> [card class, title]
  connected: ['ok', 'Connected'],
  connecting: ['busy', 'Connecting…'],
  disconnected: ['bad', 'Not connected'],
  'not allowed': ['bad', 'Not allowed'],
};

const PLATFORMS = [['youtube', 'YouTube'], ['tiktok', 'TikTok'], ['facebook', 'Facebook']];
let renaming = false;

function make(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

// one row per browser: its name (editable for this one), its role, and a button per platform that routes it here
function browserRow(b, you, defaultId) {
  const me = b.id === you;
  const row = make('div', 'br' + (me ? ' me' : ''));
  const top = make('div', 'top');
  let name;
  if (me) {
    name = make('input', 'name');
    name.value = b.label;
    name.title = 'Rename this browser';
    name.onfocus = () => { renaming = true; };
    name.onkeydown = e => { if (e.key === 'Enter') name.blur(); };
    name.onblur = () => { renaming = false; if (name.value.trim() && name.value !== b.label) send({ type: 'setLabel', label: name.value }); };
  } else name = make('span', 'name', b.label);
  top.append(name);
  if (me) top.append(make('span', 'tag', 'this one'));
  if (!b.connected) top.append(make('span', 'tag', 'offline'));
  else if (b.is_default) top.append(make('span', 'tag live', 'default'));
  else if (b.connected) {
    const use = make('button', 'ghost', 'Use');
    use.title = 'Make this the default browser';
    use.onclick = () => send({ type: 'useBrowser', browser: b.id });
    top.append(use);
  }
  row.append(top);
  const who = [b.email, b.brand, b.jobs ? `${b.jobs} job${b.jobs > 1 ? 's' : ''} running` : ''].filter(Boolean).join(' · ');
  if (who) row.append(make('div', 'who', who));
  const plats = make('div', 'plats');
  for (const [key, title] of PLATFORMS) {
    const routed = (b.routes || []).includes(key);
    const inherited = b.is_default && !routed;
    const acc = (b.accounts || {})[key];
    const btn = make('button', 'plat' + (routed || inherited ? ' on' : ''), title);
    if (acc && acc.logged_in) btn.append(make('span', 'ok', ' ✓'));
    btn.title = acc ? (acc.logged_in ? `Logged in: ${acc.account || 'yes'}` : 'Not logged in here') + '. Click to post ' + title + ' from here.' : 'Post ' + title + ' from this browser';
    btn.disabled = !b.connected;
    btn.onclick = () => send(routed ? { type: 'useBrowser', browser: 'none', platforms: [key] } : { type: 'useBrowser', browser: b.id, platforms: [key] });
    plats.append(btn);
  }
  row.append(plats);
  return row;
}

function showBrowsers(s) {
  const r = s.roster;
  el('scan').disabled = s.state !== 'connected' || s.scanning;
  el('scan').textContent = s.scanning ? 'Scanning…' : 'Scan accounts';
  if (!r) {
    el('pick').textContent = '';
    el('browsers').replaceChildren(make('div', 'empty', 'Connects when the server is running'));
    return;
  }
  const n = r.browsers.filter(b => b.connected).length;
  el('pick').textContent = r.needs_choice ? `${n} browsers connected. Scan accounts, or press Use on one, so posts go from the right account.` : '';
  if (!renaming) el('browsers').replaceChildren(...r.browsers.map(b => browserRow(b, r.you, r.default)));
}

function show(s) {
  if (!s) return;
  const retrying = s.state === 'connecting' && s.lastError;
  let [cls, title] = retrying ? VIEW.disconnected : VIEW[s.state] || ['bad', s.state];
  if (cls === 'ok' && s.role === 'pick') { cls = 'busy'; title = 'Choose a browser'; }
  if (cls === 'ok' && s.role === 'idle') title = 'Standby: another browser posts';
  if (cls === 'ok' && s.tabs.length) { cls = 'busy'; title = `Publishing (${s.tabs.length} tab${s.tabs.length > 1 ? 's' : ''})`; }
  el('status').className = 'card ' + cls;
  el('title').textContent = title;
  el('sub').textContent = `ws://127.0.0.1:${s.port}` + (s.serverVersion && cls !== 'bad' ? ` · server ${s.serverVersion}` : '');
  el('err').textContent = cls === 'bad' || s.state === 'connected' ? s.lastError || '' : '';
  showBrowsers(s);
  const items = s.tabs.map(t => {
    const li = document.createElement('li');
    li.textContent = `${t.attached ? '●' : '○'} ${t.title || t.url}`;
    li.title = t.url;
    return li;
  });
  if (!items.length) {
    const li = document.createElement('li');
    li.className = 'empty';
    li.textContent = 'No jobs running';
    items.push(li);
  }
  el('tabs').replaceChildren(...items);
  if (!portTouched) el('port').value = s.port;
  if (s.test) el('testout').textContent = (s.test.ok ? 'OK: ' : 'Failed: ') + s.test.text;
  el('meta').textContent = 'id ' + s.id;
  el('meta').title = s.id;
  el('ver').textContent = 'v' + s.version;
}

const send = msg => chrome.runtime.sendMessage(msg).then(show);

el('port').oninput = () => { portTouched = true; };
el('port').onkeydown = e => { if (e.key === 'Enter') el('save').click(); };
el('save').onclick = () => { portTouched = false; send({ type: 'setPort', port: el('port').value }); };
el('reconnect').onclick = () => send({ type: 'reconnect' });
el('scan').onclick = () => send({ type: 'scan' });
el('test').onclick = () => { el('testout').textContent = 'Testing…'; send({ type: 'test' }); };

send({ type: 'getState' });
setInterval(() => send({ type: 'getState' }), 1500);
