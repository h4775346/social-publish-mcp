const el = id => document.getElementById(id);
let portTouched = false;

const VIEW = { // service-worker state -> [card class, title]
  connected: ['ok', 'Connected'],
  connecting: ['busy', 'Connecting…'],
  disconnected: ['bad', 'Not connected'],
  'not allowed': ['bad', 'Not allowed'],
};

function show(s) {
  if (!s) return;
  const retrying = s.state === 'connecting' && s.lastError;
  let [cls, title] = retrying ? VIEW.disconnected : VIEW[s.state] || ['bad', s.state];
  if (cls === 'ok' && s.tabs.length) { cls = 'busy'; title = `Publishing (${s.tabs.length} tab${s.tabs.length > 1 ? 's' : ''})`; }
  el('status').className = 'card ' + cls;
  el('title').textContent = title;
  el('sub').textContent = `ws://127.0.0.1:${s.port}` + (s.serverVersion && cls !== 'bad' ? ` · server ${s.serverVersion}` : '');
  el('err').textContent = cls === 'bad' ? s.lastError || '' : '';
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
el('test').onclick = () => { el('testout').textContent = 'Testing…'; send({ type: 'test' }); };

send({ type: 'getState' });
setInterval(() => send({ type: 'getState' }), 1500);
