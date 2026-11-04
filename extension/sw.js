// Social Publish MCP - service worker.
// A generic Chrome DevTools Protocol relay: keeps a WebSocket to the local social-publish-mcp server, opens tabs for
// its jobs (one tab per job, all in one "Social Publish" window and tab group), attaches chrome.debugger to them and
// forwards CDP commands. All the site logic lives in the server's Python drivers.
// The server recognises this extension by its chrome-extension:// Origin, so there is nothing to paste.
// Every copy (Chrome profile, Brave, Edge...) has its own persistent browserId, so several can be connected at once;
// the server decides which one runs a job, and the popup shows and changes that choice.
const DEFAULT_PORT = 8797;
const VERSION = chrome.runtime.getManifest().version;
const PROTOCOL = '1.3';
const GROUP_TITLE = 'Social Publish';

let ws = null, state = 'disconnected', lastError = '', serverVersion = '', backoff = 1000, retryTimer = null, pingTimer = null;
const tabs = new Map(); // tabId -> { attached, openedAt, url, keep }
let roster = null; // the server's view: { you, browsers, default, routes, needs_choice }
let scanning = false;

// am I the browser that posts? 'on' = yes, 'idle' = another browser does, 'pick' = several connected and no choice yet
function role() {
  if (!roster) return 'on';
  if (roster.needs_choice) return 'pick';
  const me = (roster.browsers || []).find(b => b.id === roster.you);
  if (me && (me.is_default || (me.routes || []).length)) return 'on';
  return (roster.browsers || []).filter(b => b.connected).length > 1 ? 'idle' : 'on';
}

function setState(s, err) {
  state = s;
  if (err !== undefined) lastError = err;
  const busy = s === 'connected' && tabs.size > 0;
  const r = role();
  chrome.action.setBadgeText({ text: busy ? String(tabs.size) : s === 'connected' ? r : 'off' });
  chrome.action.setBadgeBackgroundColor({
    color: s !== 'connected' ? '#b42318' : busy || r === 'pick' ? '#b7791f' : r === 'idle' ? '#667085' : '#1a7f37',
  });
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

// who this copy is: a random id kept in this profile's storage, and the profile's Google account when Chrome shares it
async function identity() {
  let { browserId } = await chrome.storage.local.get('browserId');
  if (!browserId) {
    browserId = crypto.randomUUID().replace(/-/g, '').slice(0, 8);
    await chrome.storage.local.set({ browserId });
  }
  let email = '';
  try { email = (await chrome.identity.getProfileUserInfo({ accountStatus: 'ANY' })).email || ''; } catch { /* optional */ }
  const brands = (navigator.userAgentData && navigator.userAgentData.brands) || [];
  const brand = (brands.find(b => !/not.?a.?brand|chromium/i.test(b.brand)) || brands[0] || {}).brand || 'Chromium';
  return { browserId, email, brand };
}

async function openSocket() {
  clearTimeout(retryTimer);
  const { port = DEFAULT_PORT } = await chrome.storage.local.get('port');
  const me = await identity();
  setState('connecting');
  let sock;
  try { sock = new WebSocket(`ws://127.0.0.1:${port}`); } catch (e) { setState('disconnected', String(e)); retry(); return; }
  ws = sock;
  sock.onopen = () => send(sock, { type: 'hello', role: 'extension', version: VERSION, ...me });
  sock.onmessage = ev => {
    let msg; try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === 'welcome') {
      backoff = 1000; serverVersion = msg.server || ''; setState('connected', '');
      if (msg.browser_id && msg.browser_id !== me.browserId) chrome.storage.local.set({ browserId: msg.browser_id }); // a copied profile
      clearInterval(pingTimer); // app-level traffic keeps the MV3 worker alive (Chrome 116+)
      pingTimer = setInterval(() => send(sock, { type: 'ping' }), 20000);
    } else if (msg.type === 'roster') { roster = msg; scanning = false; setState(state); }
    else if (msg.type === 'error') { lastError = msg.error; scanning = false; }
    else if (msg.id !== undefined) handle(sock, msg); // every request runs concurrently, answered by id
  };
  sock.onclose = ev => {
    if (ws !== sock) return; // an older socket we already replaced
    clearInterval(pingTimer);
    ws = null;
    roster = null;
    // 4002 = the server swapped in a newer socket from this same worker: its tabs are still in use
    if (ev.code !== 4002) closeJobTabs(); // the server's jobs died with the connection: no bar or empty tab left behind
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
    case 'keep_tab': if (tabs.has(msg.tabId)) tabs.get(msg.tabId).keep = true; return {};
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

function closeJobTabs() {
  for (const [tabId, t] of tabs) {
    if (t.attached) chrome.debugger.detach({ tabId }).catch(() => {});
    if (!t.keep) chrome.tabs.remove(tabId).catch(() => {}); // keep = a dry run the user asked to look at
  }
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
    } else if (msg.type === 'useBrowser') {
      lastError = ''; send(ws, { type: 'use_browser', browser: msg.browser, platforms: msg.platforms });
    } else if (msg.type === 'setLabel') {
      send(ws, { type: 'set_label', label: msg.label });
    } else if (msg.type === 'scan') {
      lastError = ''; scanning = true; send(ws, { type: 'scan' });
    } else if (msg.type === 'test') {
      try { test = { ok: true, text: await selfTest() }; } catch (e) { test = { ok: false, text: String((e && e.message) || e) }; }
    }
    const { port = DEFAULT_PORT } = await chrome.storage.local.get('port');
    sendResponse({ state, lastError, port, version: VERSION, serverVersion, id: chrome.runtime.id, tabs: await listTabs(), test, roster, scanning, role: role() });
  })();
  return true;
});

chrome.alarms.create('socialPublishKeepAlive', { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(a => { if (a.name === 'socialPublishKeepAlive') connect(); });
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();
