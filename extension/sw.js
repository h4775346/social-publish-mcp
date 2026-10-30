// Social Publish MCP - service worker.
// A generic Chrome DevTools Protocol relay: keeps a WebSocket to the local social-publish-mcp server, opens tabs for
// its jobs (one tab per job, all in one "Social Publish" window and tab group), attaches chrome.debugger to them and
// forwards CDP commands. All the site logic lives in the server's Python drivers.
// The server recognises this extension by its chrome-extension:// Origin, so there is nothing to paste.
const DEFAULT_PORT = 8797;
const VERSION = chrome.runtime.getManifest().version;
const PROTOCOL = '1.3';
const GROUP_TITLE = 'Social Publish';

let ws = null, state = 'disconnected', lastError = '', serverVersion = '', backoff = 1000, retryTimer = null, pingTimer = null;
const tabs = new Map(); // tabId -> { attached, openedAt, url }

function setState(s, err) {
  state = s;
  if (err !== undefined) lastError = err;
  const busy = s === 'connected' && tabs.size > 0;
  chrome.action.setBadgeText({ text: busy ? String(tabs.size) : s === 'connected' ? 'on' : 'off' });
  chrome.action.setBadgeBackgroundColor({ color: s === 'connected' ? (busy ? '#b7791f' : '#1a7f37') : '#b42318' });
}

function retry(delay) {
  clearTimeout(retryTimer);
  retryTimer = setTimeout(connect, delay || backoff);
  backoff = Math.min(backoff * 2, 30000);
}

const send = (sock, obj) => { if (sock && sock.readyState === WebSocket.OPEN) sock.send(JSON.stringify(obj)); };

let connecting = false; // connect() awaits storage before creating the socket: block concurrent calls (alarm + retry timer)

async function connect() {
  if (connecting || (ws && ws.readyState <= WebSocket.OPEN)) return; // CONNECTING or OPEN
  connecting = true;
  try { await openSocket(); } finally { connecting = false; }
}

async function openSocket() {
  clearTimeout(retryTimer);
  const { port = DEFAULT_PORT } = await chrome.storage.local.get('port');
  setState('connecting');
  let sock;
  try { sock = new WebSocket(`ws://127.0.0.1:${port}`); } catch (e) { setState('disconnected', String(e)); retry(); return; }
  ws = sock;
  sock.onopen = () => send(sock, { type: 'hello', role: 'extension', version: VERSION });
  sock.onmessage = ev => {
    let msg; try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === 'welcome') {
      backoff = 1000; serverVersion = msg.server || ''; setState('connected', '');
      clearInterval(pingTimer); // app-level traffic keeps the MV3 worker alive (Chrome 116+)
      pingTimer = setInterval(() => send(sock, { type: 'ping' }), 20000);
    } else if (msg.type === 'error') lastError = msg.error;
    else if (msg.id !== undefined) handle(sock, msg); // every request runs concurrently, answered by id
  };
  sock.onclose = ev => {
    if (ws !== sock) return; // an older socket we already replaced
    clearInterval(pingTimer);
    ws = null;
    // 4002 = the server swapped in a newer socket from this same worker: its tabs are still in use
    if (ev.code !== 4002) detachAll(); // the server's jobs died with the connection; drop the "debugging this browser" bar
    if (ev.code === 4004) { setState('not allowed', lastError || 'the server pinned another extension id; see extensions.json in ~/.social-publish-mcp'); retry(60000); }
    else { setState('disconnected', ev.reason || 'server not running: start your MCP client (Claude, Cursor...) with social-publish-mcp configured'); retry(); }
  };
}

// ------------------------------------------------------------------ requests from the server
async function handle(sock, msg) {
  let res;
  try { res = { ok: true, result: await dispatch(msg) }; }
  catch (e) { res = { ok: false, error: String((e && e.message) || e) }; }
  send(sock, { type: 'response', id: msg.id, ...res });
}

async function dispatch(msg) {
  switch (msg.type) {
    case 'open_tab': return openTab(msg.url || 'about:blank', !!msg.active);
    case 'cdp': return cdp(msg.tabId, msg.method, msg.params || {});
    case 'close_tab': return closeTab(msg.tabId);
    case 'activate_tab': await chrome.tabs.update(msg.tabId, { active: true }); return {};
    case 'list_tabs': return { tabs: await listTabs() };
    default: throw new Error('unknown request type ' + msg.type);
  }
}

function cdp(tabId, method, params) {
  if (!tabs.has(tabId)) throw new Error(`tab ${tabId} is not managed by Social Publish (closed?)`);
  if (!tabs.get(tabId).attached) throw new Error(`the debugger is no longer attached to tab ${tabId}`);
  return chrome.debugger.sendCommand({ tabId }, method, params).then(r => r || {});
}

async function jobWindow() {
  const { windowId } = await chrome.storage.session.get('windowId');
  if (windowId !== undefined) {
    try { await chrome.windows.get(windowId); return windowId; } catch { /* closed */ }
  }
  return null;
}

async function openTab(url, active) {
  let tab, windowId = await jobWindow();
  if (windowId === null) {
    const win = await chrome.windows.create({ url: 'about:blank', focused: active, type: 'normal' });
    windowId = win.id;
    tab = win.tabs[0];
    await chrome.storage.session.set({ windowId });
  } else {
    tab = await chrome.tabs.create({ windowId, url: 'about:blank', active });
  }
  tabs.set(tab.id, { attached: false, openedAt: Date.now(), url });
  await group(tab.id, windowId);
  try {
    await chrome.debugger.attach({ tabId: tab.id }, PROTOCOL);
    tabs.get(tab.id).attached = true;
    for (const m of ['Page.enable', 'Runtime.enable', 'DOM.enable']) await chrome.debugger.sendCommand({ tabId: tab.id }, m, {});
    // parallel jobs share one window, so most job tabs are in the background: let them behave as focused
    await chrome.debugger.sendCommand({ tabId: tab.id }, 'Emulation.setFocusEmulationEnabled', { enabled: true }).catch(() => {});
    if (url && url !== 'about:blank') await chrome.debugger.sendCommand({ tabId: tab.id }, 'Page.navigate', { url });
  } catch (e) {
    tabs.delete(tab.id);
    chrome.tabs.remove(tab.id).catch(() => {});
    throw new Error('cannot attach the debugger: ' + ((e && e.message) || e) + ' (is DevTools open on that tab, or another debugger extension active?)');
  }
  setState(state);
  return { tabId: tab.id, windowId };
}

async function group(tabId, windowId) {
  try {
    const [existing] = await chrome.tabGroups.query({ windowId, title: GROUP_TITLE });
    const groupId = await chrome.tabs.group(existing ? { tabIds: [tabId], groupId: existing.id } : { tabIds: [tabId], createProperties: { windowId } });
    if (!existing) await chrome.tabGroups.update(groupId, { title: GROUP_TITLE, color: 'blue', collapsed: false });
  } catch { /* grouping is cosmetic */ }
}

async function closeTab(tabId) {
  const t = tabs.get(tabId);
  tabs.delete(tabId);
  if (t && t.attached) await chrome.debugger.detach({ tabId }).catch(() => {});
  await chrome.tabs.remove(tabId).catch(() => {});
  setState(state);
  return {};
}

async function listTabs() {
  const out = [];
  for (const [tabId, t] of tabs) {
    try {
      const tab = await chrome.tabs.get(tabId);
      out.push({ tabId, url: tab.url, title: tab.title, attached: t.attached, active: tab.active, openedAt: t.openedAt });
    } catch { tabs.delete(tabId); }
  }
  return out;
}

function detachAll() {
  for (const [tabId, t] of tabs) if (t.attached) chrome.debugger.detach({ tabId }).catch(() => {});
  tabs.clear();
  setState(state);
}

function lost(tabId, event, reason) {
  const t = tabs.get(tabId);
  if (!t) return;
  if (event === 'tab_closed') tabs.delete(tabId); else t.attached = false;
  send(ws, { type: 'event', event, tabId, reason: reason || '' });
  setState(state);
}

chrome.debugger.onDetach.addListener((source, reason) => { if (source.tabId !== undefined) lost(source.tabId, 'detached', reason); });
chrome.tabs.onRemoved.addListener(tabId => lost(tabId, 'tab_closed', 'closed'));

// ------------------------------------------------------------------ popup
async function selfTest() {
  // attaches to a blank tab and evaluates 1+1: proves the debugger permission works, touches no website
  const { tabId } = await openTab('about:blank', false);
  try {
    const r = await cdp(tabId, 'Runtime.evaluate', { expression: '1+1', returnByValue: true });
    if (!r.result || r.result.value !== 2) throw new Error('unexpected answer ' + JSON.stringify(r));
    return 'debugger OK' + (state === 'connected' ? `, server ${serverVersion} connected` : ', but the server is not connected');
  } finally { await closeTab(tabId); }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    let test;
    if (msg.type === 'setPort') {
      const port = parseInt(msg.port, 10);
      if (port >= 1024 && port <= 65535) await chrome.storage.local.set({ port });
      if (ws) ws.close(); ws = null; backoff = 1000; await connect();
    } else if (msg.type === 'reconnect') {
      backoff = 1000; await connect();
    } else if (msg.type === 'test') {
      try { test = { ok: true, text: await selfTest() }; } catch (e) { test = { ok: false, text: String((e && e.message) || e) }; }
    }
    const { port = DEFAULT_PORT } = await chrome.storage.local.get('port');
    sendResponse({ state, lastError, port, version: VERSION, serverVersion, id: chrome.runtime.id, tabs: await listTabs(), test });
  })();
  return true;
});

chrome.alarms.create('socialPublishKeepAlive', { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(a => { if (a.name === 'socialPublishKeepAlive') connect(); });
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();
