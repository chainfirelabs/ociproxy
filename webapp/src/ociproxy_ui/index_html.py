"""Single-page dashboard. Vanilla JS + fetch, no build step (distroless-friendly)."""

INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OCIProxy</title>
<style>
  :root {
    --bg: #0b0b0d; --card: #141417; --card2: #1e1e25; --line: #2a2a33;
    --text: #ede6df; --dim: #9c8e83; --accent: #c84e23; --green: #46a758;
    --red: #e5484d; --amber: #f6931d;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 14px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  code, .mono { font-family: "JetBrains Mono", "Fira Code", ui-monospace, monospace; }
  header {
    max-width: 1180px; margin: 0 auto;  /* same column as main */
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
    padding: 14px 22px; border-bottom: 1px solid var(--line);
    position: sticky; top: 0; background: var(--bg); z-index: 5;
  }
  header h1 { font-size: 17px; margin: 0; letter-spacing: .3px; }
  .logo { height: 34px; width: 34px; border-radius: 7px; display: block; }
  .badge {
    padding: 2px 9px; border-radius: 10px; font-size: 12px;
    background: var(--card2); border: 1px solid var(--line); color: var(--dim);
  }
  .badge.primary { color: var(--amber); border-color: var(--amber); }
  .badge.replica { color: var(--accent); border-color: var(--accent); }
  .spacer { flex: 1; }
  button {
    background: var(--card2); color: var(--text); border: 1px solid var(--line);
    border-radius: 6px; padding: 5px 12px; cursor: pointer; font-size: 13px;
  }
  button:hover { border-color: var(--accent); }
  button.danger:hover { border-color: var(--red); color: var(--red); }
  button:disabled { opacity: .5; cursor: default; }
  #sync-now { min-width: 100px; flex-shrink: 0; }
  #sync-toast {
    position: fixed; right: 20px; bottom: 20px; z-index: 20;
    max-width: min(420px, calc(100vw - 40px)); padding: 14px 16px;
    background: var(--card); border: 1px solid var(--green); border-radius: 10px;
    box-shadow: 0 8px 30px #0008; display: flex; align-items: start; gap: 16px;
    overflow-wrap: anywhere;
  }
  #sync-toast[hidden] { display: none; }
  #sync-toast.error { border-color: var(--red); }
  #sync-toast button { flex-shrink: 0; padding: 0 6px; }
  label.auto { color: var(--dim); font-size: 13px; user-select: none; }
  #updated { color: var(--dim); font-size: 12px; }
  main { max-width: 1180px; margin: 0 auto; padding: 20px 22px 60px; }
  #err {
    display: none; background: #2d1618; border: 1px solid var(--red);
    color: #ffb4b0; border-radius: 8px; padding: 10px 14px; margin-bottom: 16px;
    white-space: pre-wrap;
  }
  .chips { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 22px; }
  .chip {
    background: var(--card); border: 1px solid var(--line); border-radius: 10px;
    padding: 10px 18px; min-width: 130px;
  }
  .chip .v { font-size: 20px; font-weight: 600; }
  .chip .k { color: var(--dim); font-size: 12px; text-transform: uppercase; letter-spacing: .5px; }
  section {
    background: var(--card); border: 1px solid var(--line); border-radius: 10px;
    margin-bottom: 22px; overflow: hidden;
  }
  section h2 {
    font-size: 14px; margin: 0; padding: 12px 16px;
    border-bottom: 1px solid var(--line); display: flex; align-items: center; gap: 10px;
  }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 8px 16px; border-bottom: 1px solid var(--line); }
  th { color: var(--dim); font-size: 12px; text-transform: uppercase; letter-spacing: .5px; font-weight: 500; }
  #images table { table-layout: fixed; }
  #images td { overflow-wrap: anywhere; }
  #images td:nth-child(6) { white-space: nowrap; }
  #images th:nth-child(1), #images td:nth-child(1) { width: 115px; }
  #images th:nth-child(2), #images td:nth-child(2) { width: auto; }
  #images th:nth-child(3), #images td:nth-child(3) { width: 13%; }
  #images th:nth-child(4), #images td:nth-child(4) { width: 205px; }
  #images th:nth-child(5), #images td:nth-child(5) { width: 9%; }
  #images th:nth-child(6), #images td:nth-child(6) { width: 175px; }
  tr:last-child td { border-bottom: none; }
  td.dim, .dim { color: var(--dim); }
  .yes { color: var(--green); }
  .no { color: var(--dim); }
  .hint { color: var(--dim); font-size: 12px; }
  .pager { display: flex; align-items: center; gap: 10px; padding: 10px 16px; border-top: 1px solid var(--line); }
  .pager .info { color: var(--dim); font-size: 13px; }
  .empty { padding: 18px 16px; color: var(--dim); }
  nav { display: flex; gap: 4px; }
  nav button { background: transparent; border-color: transparent; color: var(--dim); }
  nav button.active { color: var(--text); border-color: var(--line); background: var(--card2); }
  .who { color: var(--dim); font-size: 13px; max-width: 200px; overflow: hidden;
         text-overflow: ellipsis; white-space: nowrap; }
  .who b { color: var(--text); font-weight: 500; }
  .view { display: none; }
  .view.active { display: block; }
  form.inline { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; padding: 12px 16px;
                border-top: 1px solid var(--line); }
  input, select {
    background: var(--bg); color: var(--text); border: 1px solid var(--line);
    border-radius: 6px; padding: 5px 9px; font-size: 13px;
  }
  input:focus, select:focus { outline: none; border-color: var(--accent); }
  #login { max-width: 360px; margin: 12vh auto 0; }
  #login section { padding: 22px; }
  #login h2 { border: 0; padding: 0 0 14px; font-size: 17px; }
  #login input { display: block; width: 100%; margin-bottom: 10px; padding: 8px 10px; font-size: 14px; }
  #login button { width: 100%; padding: 8px; font-size: 14px; }
  .primary-btn { background: var(--accent); border-color: var(--accent); color: #fff; }
  #login .or { text-align: center; color: var(--dim); margin: 12px 0; font-size: 12px; }
  #login .err { color: #ffb4b0; font-size: 13px; min-height: 18px; margin-bottom: 6px; }
  .once { margin: 12px 16px; padding: 12px 14px; border: 1px solid var(--green); border-radius: 8px;
          background: #122017; }
  .once code { display: block; word-break: break-all; margin: 6px 0; font-size: 13px; }
  .body { padding: 12px 16px; }
  .pill { font-size: 11px; padding: 1px 7px; border-radius: 9px; border: 1px solid var(--line); color: var(--dim); }
  .pill.private { color: var(--amber); border-color: var(--amber); }
  .pill.public { color: var(--green); border-color: var(--green); }
  .pill.disabled { color: var(--red); border-color: var(--red); }
  .pill.managed { color: var(--accent); border-color: var(--accent); }
  .pill { white-space: nowrap; }
  input:disabled, select:disabled { opacity: .6; }
  #upstreams { overflow-x: auto; }
  #upstreams td:first-child { white-space: nowrap; }
  tr.locked td { color: var(--dim); }
  tr.locked td.mono { opacity: .7; }
  tr.inactive td { opacity: .5; }
  .src-actions { white-space: nowrap; text-align: right; }
  .h-actions { margin-left: auto; display: flex; gap: 6px; }
  #src-panel { display: none; border-top: 1px solid var(--line); padding: 14px 16px; }
  #src-panel h3 { margin: 0 0 12px; font-size: 13px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 10px 14px; }
  .grid label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--dim); }
  .grid label.check { flex-direction: row; align-items: center; gap: 8px; padding-top: 18px; }
  .grid input:not([type=checkbox]), .grid select { width: 100%; }
  .src-buttons { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-top: 14px; }
  #src-test-result { font-size: 13px; }
  #src-test-result.ok { color: var(--green); }
  #src-test-result.bad { color: #ffb4b0; }
</style>
</head>
<body>
<div id="login" class="view">
  <section>
    <h2><img src="/logo.png" class="logo" alt="" style="display:inline;vertical-align:middle;margin-right:8px;height:28px;width:28px"> Sign in to OCIProxy</h2>
    <div class="err" id="login-err"></div>
    <form id="login-form">
      <input id="login-user" autocomplete="username" placeholder="Username" required>
      <input id="login-pass" type="password" autocomplete="current-password" placeholder="Password" required>
      <button class="primary-btn" type="submit">Sign in</button>
    </form>
    <div id="sso" style="display:none">
      <div class="or">or</div>
      <button id="sso-btn" type="button"></button>
    </div>
  </section>
</div>

<div id="app" class="view">
<header>
  <img src="/logo.png" class="logo" alt="OCIProxy logo">
  <h1>OCIProxy</h1>
  <span id="role" class="badge">…</span>
  <nav id="nav">
    <button data-view="dash" class="active">Dashboard</button>
    <button data-view="tokens">My tokens</button>
    <button data-view="users" id="nav-users" style="display:none">Users</button>
    <button data-view="cleanup" id="nav-cleanup" style="display:none">Cleanup</button>
    <button data-view="account">Account</button>
  </nav>
  <span class="spacer"></span>
  <label class="auto"><input type="checkbox" id="auto" checked> auto-refresh</label>
  <button id="refresh">Refresh</button>
  <button id="sync-now" style="display:none" title="Sync the shared public catalog and content">Sync now</button>
  <span id="updated"></span>
  <span class="who" id="who"></span>
  <button id="logout">Sign out</button>
</header>
<div id="sync-toast" hidden>
  <span id="sync-status" role="status" aria-live="polite" aria-atomic="true"></span>
  <button id="sync-dismiss" type="button" aria-label="Dismiss sync notification">&times;</button>
</div>
<main>
  <div id="err"></div>

  <div class="view active" id="view-dash">
    <div class="chips" id="chips"></div>
    <section>
      <h2>Proxied registries &amp; Helm repos
        <span class="hint" id="src-hint">greyed-out entries come from the configuration file (Helm values / ociproxy.toml)</span>
        <span class="h-actions" id="src-add" style="display:none">
          <button type="button" data-add="oci">+ Registry</button>
          <button type="button" data-add="helm">+ Helm repo</button>
        </span>
      </h2>
      <div id="upstreams"></div>
      <div id="src-panel">
        <h3 id="src-title"></h3>
        <form id="src-form" autocomplete="off">
          <div class="grid">
            <label>Type<select id="src-kind"><option value="oci">Registry (OCI / Docker)</option><option value="helm">Helm repo (index.yaml)</option></select></label>
            <label>Name<input id="src-name" required maxlength="64" placeholder="e.g. quay"></label>
            <label class="oci-only">Path prefix<input id="src-prefix" maxlength="64" placeholder="defaults to the name"></label>
            <label>Base URL<input id="src-url" required placeholder="https://registry.example.com"></label>
            <label class="oci-only">Official namespace<input id="src-ns" maxlength="64" placeholder="e.g. library (Docker Hub)"></label>
            <label>Visibility<select id="src-vis"><option value="private">private</option><option value="public">public</option></select></label>
            <label>Sharing<select id="src-sharing"><option value="local">This node only</option><option value="public">Publish to public cluster catalog</option></select></label>
            <label>Username<input id="src-user" autocomplete="off" placeholder="optional"></label>
            <label>Password / token<input id="src-pass" type="password" autocomplete="new-password" placeholder="optional"></label>
            <label class="check oci-only"><input type="checkbox" id="src-default"> Default for unprefixed pulls</label>
            <label class="check" id="src-clear-wrap"><input type="checkbox" id="src-clear"> Remove stored credentials</label>
          </div>
          <div class="hint" id="src-usage" style="margin-top:10px"></div>
          <div class="src-buttons">
            <button type="submit" class="primary-btn" id="src-save">Save</button>
            <button type="button" id="src-test">Test connection</button>
            <button type="button" id="src-cancel">Cancel</button>
            <span id="src-test-result"></span>
          </div>
        </form>
      </div>
    </section>
    <section>
      <h2>Stored images <span class="hint" id="img-hint"></span></h2>
      <div id="images"></div>
      <div class="pager" id="pager"></div>
    </section>
  </div>

  <div class="view" id="view-cleanup">
    <section>
      <h2>Cache cleanup <span class="hint">Private sources and local pushes are managed on their own node.</span></h2>
      <div id="cleanup-summary" style="padding:12px 16px"></div>
      <form class="inline" id="cleanup-preview-form">
        <select id="cleanup-scope" aria-label="Cleanup scope">
          <option value="local">This node</option>
          <option value="public" id="cleanup-public-option">Primary + all public replicas</option>
        </select>
        <input id="cleanup-source" placeholder="Filter source / repository" aria-label="Filter source or repository">
        <input id="cleanup-age" type="number" min="0" value="0" aria-label="Unused for at least this many days" title="Unused for at least this many days">
        <label><input id="cleanup-pushed" type="checkbox"> Include local pushes</label>
        <button type="submit">Preview selected cleanup</button>
      </form>
      <div id="cleanup-entries" style="overflow:auto;max-height:440px"></div>
      <div id="cleanup-preview" hidden style="padding:16px">
        <pre id="cleanup-preview-text" style="white-space:pre-wrap"></pre>
        <button id="cleanup-confirm" class="danger" type="button">Confirm cleanup</button>
      </div>
    </section>
    <section>
      <h2>Automatic cleanup <span class="hint">Local mirrored cache only. Pins and local pushes are protected.</span></h2>
      <form class="inline" id="cleanup-policy-form">
        <label>Unused days (0 = off) <input id="cleanup-retention" type="number" min="0" step="any" required></label>
        <label>Disk high % (0 = off) <input id="cleanup-high" type="number" min="0" max="100" step="any" required></label>
        <label>Disk low % <input id="cleanup-low" type="number" min="0" max="99.99" step="any" required></label>
        <label>Max cache GiB (0 = off) <input id="cleanup-max" type="number" min="0" step="any" required></label>
        <button type="submit">Save policy</button>
        <button id="cleanup-run" type="button">Run cleanup now</button>
      </form>
      <p class="hint" style="padding:0 16px 12px">Disk pressure removes least recently used mirrored content. Shared layers and the blob grace period may delay reclaimed space. If background maintenance is disabled, use Run cleanup now.</p>
    </section>
    <section><h2>Cleanup requests</h2><div id="cleanup-jobs" style="padding:12px 16px"></div></section>
  </div>

  <div class="view" id="view-tokens">
    <section>
      <h2>Personal access tokens <span class="hint">use as the password for docker login / helm registry login</span></h2>
      <div id="token-once"></div>
      <div id="tokens"></div>
      <form class="inline" id="token-form">
        <input id="token-name" placeholder="Token name, e.g. laptop or ci-runner" maxlength="64" required style="min-width:260px">
        <input id="token-days" type="number" min="1" max="3650" placeholder="Expires in days (blank = never)" style="width:230px">
        <button type="submit">Create token</button>
      </form>
    </section>
  </div>

  <div class="view" id="view-users">
    <section>
      <h2>Users <span class="hint">OIDC users are created on first sign-in; their role follows provider groups</span></h2>
      <div id="users"></div>
      <form class="inline" id="user-form">
        <input id="nu-name" placeholder="Username" required>
        <select id="nu-role"><option>reader</option><option>pusher</option><option>admin</option></select>
        <input id="nu-pass" type="password" placeholder="Password (min 10 chars)" autocomplete="new-password" required>
        <button type="submit">Create local user</button>
      </form>
    </section>
    <section id="user-tokens-section" style="display:none">
      <h2>Tokens of <span class="mono" id="ut-name"></span></h2>
      <div id="user-tokens"></div>
    </section>
  </div>

  <div class="view" id="view-account">
    <section>
      <h2>Account</h2>
      <div class="body" id="account-info"></div>
      <form class="inline" id="pw-form" style="display:none">
        <input id="pw-current" type="password" placeholder="Current password" autocomplete="current-password" required>
        <input id="pw-new" type="password" placeholder="New password (min 10 chars)" autocomplete="new-password" required>
        <button type="submit">Change password</button>
      </form>
    </section>
  </div>
</main>
</div>
<script>
const PAGE = 25;
let page = 0;
let timer = null;
let me = null;

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function fmtBytes(n) {
  if (n == null) return '-';
  const u = ['B','KB','MB','GB','TB'];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (i === 0 ? v : v.toFixed(v >= 100 ? 0 : 1)) + ' ' + u[i];
}
function fmtTime(epoch) {
  if (!epoch) return '-';
  const d = new Date(epoch * 1000);
  const p = x => String(x).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth()+1) + '-' + p(d.getDate()) +
         ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}
function showErr(msg) {
  const e = document.getElementById('err');
  e.textContent = msg; e.style.display = 'block';
}
function clearErr() {
  const e = document.getElementById('err');
  e.style.display = 'none'; e.textContent = '';
}
async function api(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign({'X-Requested-With': 'ociproxy'}, opts.headers || {});
  if (opts.json !== undefined) {
    opts.body = JSON.stringify(opts.json);
    opts.headers['Content-Type'] = 'application/json';
    delete opts.json;
  }
  const res = await fetch(path, opts);
  let body = null;
  try { body = await res.json(); } catch (e) { /* non-JSON */ }
  if (res.status === 401 && !path.startsWith('/api/auth/')) { showLogin(); throw new Error('signed out'); }
  if (!res.ok) {
    const msg = (body && (body.error || (body.errors && body.errors[0] && body.errors[0].message))) ||
                (res.status + ' ' + res.statusText);
    throw new Error(msg);
  }
  return body;
}

function renderOverview(o) {
  const role = document.getElementById('role');
  role.textContent = o.role + (o.node_error ? ' (unreachable)' : '');
  role.className = 'badge ' + o.role;
  role.title = o.node_error ? o.node_error
    : 'node ' + (o.node_id || '') + (o.primary_node_id && o.primary_node_id !== o.node_id
        ? ' · primary ' + o.primary_node_id : '');
  const reps = o.replicas || [];
  const chips = [
    { v: o.blobs ? o.blobs.count : 0, k: 'blobs' },
    { v: fmtBytes(o.blobs ? o.blobs.total_size : 0), k: 'stored' },
    { v: o.manifests, k: 'manifests' },
  ];
  if (!o.primary) chips.push({ v: reps.length, k: 'live replicas' });
  document.getElementById('chips').innerHTML = chips.map(c =>
    '<div class="chip"><div class="v">' + esc(c.v) + '</div><div class="k">' + esc(c.k) + '</div></div>'
  ).join('') + (reps.length
    ? '<div class="chip" style="min-width:260px"><div class="k" style="margin-bottom:4px">' +
      'replicas syncing from this primary</div>' +
      reps.map(r => '<div class="mono" style="font-size:12px">' + esc(r.base_url) +
        ' <span class="dim" title="#: how far it has applied the primary changelog · last fetch: last time it fetched missing content from the primary">synced to #' +
        esc(r.acked_serial) +
        (r.last_pull_at ? ' · last fetch ' + ago(r.last_pull_at) : ' · no fetches yet') +
        '</span></div>').join('') + '</div>'
    : '') + (o.primary ? primaryChip(o.primary) : '');
}

function primaryChip(p) {
  let state;
  if (!p.registered) state = '<span style="color:var(--amber)">not connected</span>';
  else if (p.serial == null || p.synced_serial == null) state = 'connected';
  else if (p.synced_serial >= p.serial) state = 'up to date';
  else state = (p.serial - p.synced_serial) + ' change(s) behind';
  return '<div class="chip" style="min-width:260px"><div class="k" style="margin-bottom:4px">syncing from primary</div>' +
    '<div class="mono" style="font-size:12px">' + esc(p.url) + ' <span class="dim">' + state +
    (p.registered && p.last_contact_at ? ' · last contact ' + ago(p.last_contact_at) : '') +
    '</span></div></div>';
}

function ago(epoch) {
  const s = Math.max(0, Date.now() / 1000 - epoch);
  if (s < 60) return Math.round(s) + 's ago';
  if (s < 3600) return Math.round(s / 60) + 'm ago';
  if (s < 86400) return (s / 3600).toFixed(1) + 'h ago';
  return (s / 86400).toFixed(1) + 'd ago';
}

let sources = [];
let managedSources = true;
let editing = null;  // {kind, name} while editing an existing entry

function renderUpstreams(items) {
  sources = items;
  const admin = me && me.role === 'admin';
  document.getElementById('src-add').style.display = admin && managedSources ? '' : 'none';
  const el = document.getElementById('upstreams');
  if (!items.length) { el.innerHTML = '<div class="empty">No upstreams configured.</div>'; return; }
  el.innerHTML = '<table><tr><th>Type</th><th>Name</th><th>Path</th><th>Base URL</th>' +
    '<th>Default</th><th>Creds</th><th>Visibility</th><th>Source</th><th></th></tr>' +
    items.map((u, i) => {
      const cls = u.managed ? (u.active ? '' : 'inactive') : 'locked';
      let source = u.managed ? '<span class="pill managed">UI</span>'
        : '<span class="pill" title="Defined in the configuration file: change it there">&#128274; config</span>';
      if (u.managed && u.shadowed_by) source += ' <span class="hint" title="A configuration entry with the same name or prefix takes precedence">shadowed by ' + esc(u.shadowed_by) + '</span>';
      else if (u.managed && !u.active) source += ' <span class="hint">inactive</span>';
      const creds = u.credentials_unreadable ? '<span class="pill disabled" title="Encrypted with an older node secret: re-enter them">unreadable</span>'
        : (u.has_credentials ? '<span class="yes">set</span>' : '<span class="no">none</span>');
      const path = u.kind === 'helm' ? '/helm/' + u.name + '/' : '/' + u.prefix + '/…';
      const actions = admin && u.managed
        ? (managedSources ? '<button data-edit="' + i + '">Edit</button> ' : '') +
          '<button class="danger" data-del="' + i + '">Delete</button>' : '';
      return '<tr class="' + cls + '"><td>' + (u.kind === 'helm' ? 'Helm repo' : 'Registry') + '</td>' +
        '<td class="mono">' + esc(u.name) + '</td>' +
        '<td class="mono">' + esc(path) + (u.official_namespace
          ? '<div class="hint" title="Single-name images get this namespace, e.g. nginx → ' + esc(u.official_namespace) + '/nginx">official ns: ' + esc(u.official_namespace) + '</div>' : '') + '</td>' +
        '<td class="mono dim">' + esc(u.base_url) + '</td>' +
        '<td>' + (u.default ? '<span class="yes">yes</span>' : '<span class="no">no</span>') + '</td>' +
        '<td>' + creds + '</td>' +
        '<td><span class="pill ' + esc(u.visibility) + '">' + esc(u.visibility || '-') + '</span><div class="hint">' + (u.sharing === 'public' ? 'Shared public catalog' : 'This node only') + '</div></td>' +
        '<td>' + source + '</td><td class="src-actions">' + actions + '</td></tr>';
    }).join('') + '</table>';
}

function srcKindChanged() {
  const oci = document.getElementById('src-kind').value === 'oci';
  document.querySelectorAll('#src-form .oci-only').forEach(e => e.style.display = oci ? '' : 'none');
  document.getElementById('src-url').placeholder = oci ? 'https://registry.example.com' : 'https://charts.example.com';
  srcUsage();
}
function srcUsage() {
  const oci = document.getElementById('src-kind').value === 'oci';
  const name = document.getElementById('src-name').value || '<name>';
  const prefix = document.getElementById('src-prefix').value || name;
  const host = location.host;
  document.getElementById('src-usage').textContent = oci
    ? 'Pull with: docker pull ' + host + '/' + prefix + '/<repo>:<tag>'
    : 'Use with: helm repo add ' + name + ' ' + location.protocol + '//' + host + '/helm/' + name;
}
function openSourceForm(kind, item) {
  editing = item ? {kind: item.kind, name: item.name} : null;
  const f = id => document.getElementById(id);
  f('src-title').textContent = item ? 'Edit ' + (item.kind === 'helm' ? 'Helm repo ' : 'registry ') + item.name
                                    : (kind === 'helm' ? 'Add a Helm repo' : 'Add a registry');
  f('src-kind').value = item ? item.kind : kind;
  f('src-kind').disabled = !!item;
  f('src-name').value = item ? item.name : '';
  f('src-name').disabled = !!item;
  f('src-prefix').value = item && item.kind === 'oci' && item.prefix !== item.name ? item.prefix : '';
  f('src-url').value = item ? item.base_url : '';
  f('src-ns').value = item ? (item.official_namespace || '') : '';
  f('src-vis').value = item ? (item.own_visibility || 'private') : 'private';
  f('src-sharing').value = item ? (item.sharing || 'local') : 'local';
  f('src-sharing').disabled = !!item;
  f('src-default').checked = !!(item && item.default);
  f('src-user').value = '';
  f('src-pass').value = '';
  f('src-pass').placeholder = item && (item.has_credentials || item.credentials_unreadable)
    ? 'stored (leave blank to keep)' : 'optional';
  f('src-clear').checked = false;
  f('src-clear-wrap').style.display = item && (item.has_credentials || item.credentials_unreadable) ? '' : 'none';
  f('src-test-result').textContent = '';
  f('src-panel').style.display = 'block';
  srcKindChanged();
  srcSharingChanged();
  (item ? f('src-url') : f('src-name')).focus();
}
function closeSourceForm() {
  editing = null;
  document.getElementById('src-panel').style.display = 'none';
}
function sourceBody() {
  const v = id => document.getElementById(id).value.trim();
  const oci = v('src-kind') === 'oci';
  const body = {kind: v('src-kind'), name: v('src-name'), base_url: v('src-url'),
                visibility: v('src-vis') || 'private', sharing: v('src-sharing')};
  if (oci) {
    body.prefix = v('src-prefix') || null;
    body.official_namespace = v('src-ns') || null;
    body.default = document.getElementById('src-default').checked;
  }
  const user = v('src-user'), pass = document.getElementById('src-pass').value;
  if (document.getElementById('src-clear').checked) body.credentials = null;
  else if (user || pass) body.credentials = {username: user, password: pass};
  return body;
}
function srcSharingChanged() {
  const shared = document.getElementById('src-sharing').value === 'public';
  if (shared) document.getElementById('src-vis').value = 'public';
  document.getElementById('src-vis').disabled = shared;
  document.getElementById('src-user').disabled = shared;
  document.getElementById('src-pass').disabled = shared;
  if (shared) { document.getElementById('src-user').value = ''; document.getElementById('src-pass').value = ''; }
}
document.getElementById('src-sharing').addEventListener('change', srcSharingChanged);
document.getElementById('src-kind').addEventListener('change', srcKindChanged);
document.getElementById('src-name').addEventListener('input', srcUsage);
document.getElementById('src-prefix').addEventListener('input', srcUsage);
document.getElementById('src-cancel').addEventListener('click', closeSourceForm);
document.getElementById('src-add').addEventListener('click', e => {
  if (e.target.dataset && e.target.dataset.add) openSourceForm(e.target.dataset.add, null);
});
document.getElementById('upstreams').addEventListener('click', async e => {
  const d = e.target.dataset || {};
  if (d.edit !== undefined) { openSourceForm(null, sources[Number(d.edit)]); return; }
  if (d.del === undefined) return;
  const u = sources[Number(d.del)];
  if (!confirm('Delete ' + u.name + (u.sharing === 'public' ? '? Remove it from the shared public catalog?' : '? Remove its local cache?'))) return;
  try {
    await api('/api/sources/' + u.kind + '/' + encodeURIComponent(u.name), {method: 'DELETE'});
    if (editing && editing.kind === u.kind && editing.name === u.name) closeSourceForm();
    await loadUpstreams();
  } catch (x) { showErr(x.message); }
});
document.getElementById('src-form').addEventListener('submit', async e => {
  e.preventDefault();
  const body = sourceBody();
  try {
    if (editing) {
      await api('/api/sources/' + editing.kind + '/' + encodeURIComponent(editing.name), {method: 'PUT', json: body});
    } else {
      await api('/api/sources', {method: 'POST', json: body});
    }
    closeSourceForm();
    clearErr();
    await loadUpstreams();
  } catch (x) { showErr(x.message); }
});
document.getElementById('src-test').addEventListener('click', async () => {
  const out = document.getElementById('src-test-result');
  const body = sourceBody();
  delete body.visibility; delete body.prefix; delete body.official_namespace; delete body.default;
  if (body.credentials === null) delete body.credentials;
  out.className = ''; out.textContent = 'testing…';
  try {
    const r = await api('/api/sources/test', {method: 'POST', json: body});
    out.className = r.ok ? 'ok' : 'bad';
    out.textContent = (r.ok ? '✓ ' : '✗ ') + r.detail + ' (' + r.url + ')';
  } catch (x) { out.className = 'bad'; out.textContent = '✗ ' + x.message; }
});

function renderImages(data) {
  const el = document.getElementById('images');
  const total = data.total || 0;
  document.getElementById('img-hint').textContent = total + ' manifest(s)';
  if (!total) { el.innerHTML = '<div class="empty">Nothing stored yet.</div>'; }
  else {
    el.innerHTML = '<table><tr><th>Upstream</th><th>Repository</th><th>Reference</th>' +
      '<th>Digest</th><th>Size</th><th>Stored</th></tr>' +
      data.items.map(m =>
        '<tr><td class="mono">' + esc(m.upstream) + '</td>' +
        '<td class="mono">' + esc(m.repo) + '</td>' +
        '<td class="mono">' + esc(m.reference) + '</td>' +
        '<td class="mono dim" title="' + esc(m.digest) + '">' + esc(m.digest.slice(0, 19)) + '…</td>' +
        '<td>' + fmtBytes(m.size) + '</td>' +
        '<td class="dim">' + fmtTime(m.stored_at) + '</td></tr>'
      ).join('') + '</table>';
  }
  const from = total ? page * PAGE + 1 : 0;
  const to = Math.min((page + 1) * PAGE, total);
  document.getElementById('pager').innerHTML =
    '<button id="prev" ' + (page === 0 ? 'disabled' : '') + '>&larr; Prev</button>' +
    '<button id="next" ' + (to >= total ? 'disabled' : '') + '>Next &rarr;</button>' +
    '<span class="info">showing ' + from + '–' + to + ' of ' + total + '</span>';
  document.getElementById('prev').onclick = () => { if (page > 0) { page--; loadImages(); } };
  document.getElementById('next').onclick = () => { if (to < total) { page++; loadImages(); } };
}

async function loadOverview() {
  const o = await api('/api/overview');
  managedSources = o.managed_sources !== false;
  renderOverview(o);
}
let syncToastTimer;
function dismissSyncToast() {
  clearTimeout(syncToastTimer);
  document.getElementById('sync-toast').hidden = true;
}
function showSyncToast(message, error = false) {
  clearTimeout(syncToastTimer);
  const toast = document.getElementById('sync-toast');
  toast.hidden = false;
  toast.classList.toggle('error', error);
  document.getElementById('sync-status').textContent = message;
  if (!error) syncToastTimer = setTimeout(dismissSyncToast, 6000);
}
document.getElementById('sync-dismiss').onclick = dismissSyncToast;
document.getElementById('sync-now').onclick = async () => {
  const button = document.getElementById('sync-now');
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  dismissSyncToast();
  try {
    const result = await api('/api/sync', {method: 'POST'});
    showSyncToast(result.message);
    await refreshAll();
    setTimeout(refreshAll, 1000);
  } catch (e) {
    showSyncToast('Sync request failed: ' + (e.message || e), true);
  } finally {
    button.disabled = false;
    button.removeAttribute('aria-busy');
  }
};
async function loadUpstreams() {
  const d = await api('/api/upstreams');
  renderUpstreams(d.items || []);
}
async function loadImages() {
  renderImages(await api('/api/images?limit=' + PAGE + '&offset=' + (page * PAGE)));
}

async function refreshAll() {
  if (!me) return;
  try {
    const view = currentView();
    if (view === 'dash') await Promise.all([loadOverview(), loadUpstreams(), loadImages()]);
    else if (view === 'cleanup') await loadCleanup();
    else if (view === 'tokens') await loadTokens();
    else if (view === 'users') await loadUsers();
    else if (view === 'account') await loadAccount();
    clearErr();
    document.getElementById('updated').textContent = 'updated ' + fmtTime(Date.now() / 1000).slice(11);
  } catch (e) {
    if (me) showErr(String(e.message || e));
  }
}

// -- cache cleanup -------------------------------------------------------------
let cleanupData = null, cleanupPreview = null, cleanupOffset = 0;
function invalidateCleanupPreview() {
  cleanupPreview = null;
  document.getElementById('cleanup-preview').hidden = true;
}
function renderCleanupStatus(data) {
  document.getElementById('cleanup-summary').textContent =
    fmtBytes(data.cache.total_size) + ' cached · disk ' + data.disk.used_percent.toFixed(1) + '% used · ' +
    fmtBytes(data.disk.free) + ' free · background interval ' + data.gc_interval + 's · blob grace ' +
    data.blob_grace + 's' + (data.last_run && data.last_run.pressure_remaining ? ' · disk/cache limit still exceeded; protected content or grace may prevent reclamation' : '');
  document.getElementById('cleanup-jobs').innerHTML = data.jobs.length ? data.jobs.map(job => {
    const peers = Object.entries(job.replicas).map(([id, result]) => esc(id) + ': ' + esc(result.status) + (result.status === 'pending' && !result.online ? ' (offline)' : '')).join(' · ');
    return '<p><span class="mono">' + esc(job.id.slice(0, 12)) + '</span> · ' + esc(job.scope) +
      ' · ' + esc(fmtTime(job.created_at)) + ' · ' + esc(job.result.deleted) + ' entries removed · ' + esc(job.result.protected || 0) + ' protected · ' + esc(job.result.changed || 0) + ' changed · ' +
      esc(fmtBytes(job.result.bytes || 0)) + ' reclaimed' + (peers ? '<br>' + peers : '') + '</p>';
  }).join('') : '<p class="dim">No cleanup requests yet.</p>';
}
function renderCleanupEntries() {
  if (!cleanupData) return;
  invalidateCleanupPreview();
  const filter = document.getElementById('cleanup-source').value.toLowerCase();
  const age = Number(document.getElementById('cleanup-age').value || 0) * 86400;
  const pushed = document.getElementById('cleanup-pushed').checked;
  const shared = document.getElementById('cleanup-scope').value === 'public';
  const entries = cleanupData.entries.map((row, index) => ({row, index})).filter(({row}) =>
    row.target.join('/').toLowerCase().includes(filter) && (!row.pushed || (pushed && !shared)) &&
    (!shared || row.public) && (!age || row.last_used < Date.now()/1000 - age));
  document.getElementById('cleanup-entries').innerHTML = '<table><thead><tr><th><input id="cleanup-all" type="checkbox" aria-label="Select all visible cached entries"></th><th>Source / repository / reference</th><th>Kind</th><th>Last used</th><th>Protection</th></tr></thead><tbody>' +
    entries.map(({row, index}) => '<tr><td><input type="checkbox" data-clean-index="' + index + '" aria-label="Select ' + esc(row.target.join('/')) + '"></td><td class="mono">' + esc(row.target.join('/')) + '</td><td>' +
      (row.pushed ? 'Local push' : row.public ? 'Shared public' : 'Local private') + '</td><td>' + esc(fmtTime(row.last_used)) +
      '</td><td><button type="button" data-pin-index="' + index + '">' + (row.pinned ? 'Unpin' : 'Pin') + '</button>' +
      (row.protected ? ' protected' : '') + '</td></tr>').join('') + '</tbody></table>' +
    '<p class="hint" style="padding:0 16px">Showing ' + entries.length + ' of ' + cleanupData.total + ' cached entries (up to 2000 per page). Image indexes include cached platform manifests during preview.</p>' +
    '<div style="padding:12px 16px"><button type="button" id="cleanup-prev" ' + (cleanupOffset === 0 ? 'disabled' : '') + '>Previous</button> ' +
    '<button type="button" id="cleanup-next" ' + (cleanupOffset + 2000 >= cleanupData.total ? 'disabled' : '') + '>Next</button></div>';
  document.getElementById('cleanup-prev').onclick = () => { cleanupOffset = Math.max(0, cleanupOffset - 2000); loadCleanup().catch(e => showErr(e.message)); };
  document.getElementById('cleanup-next').onclick = () => { cleanupOffset += 2000; loadCleanup().catch(e => showErr(e.message)); };
  document.getElementById('cleanup-all').onchange = e => {
    document.querySelectorAll('[data-clean-index]').forEach(box => box.checked = e.target.checked);
    invalidateCleanupPreview();
  };
}
async function loadCleanup() {
  cleanupData = await api('/api/cleanup?offset=' + cleanupOffset);
  renderCleanupStatus(cleanupData);
  document.getElementById('cleanup-public-option').disabled = cleanupData.role !== 'primary';
  if (cleanupData.role !== 'primary') document.getElementById('cleanup-scope').value = 'local';
  document.getElementById('cleanup-retention').value = cleanupData.policy.manifest_max_idle_days;
  document.getElementById('cleanup-high').value = cleanupData.policy.disk_high_percent;
  document.getElementById('cleanup-low').value = cleanupData.policy.disk_low_percent;
  document.getElementById('cleanup-max').value = cleanupData.policy.max_cache_bytes / (1024 ** 3);
  renderCleanupEntries();
}
for (const id of ['cleanup-source', 'cleanup-age', 'cleanup-pushed', 'cleanup-scope']) {
  document.getElementById(id).addEventListener('input', () => {
    const shared = document.getElementById('cleanup-scope').value === 'public';
    document.getElementById('cleanup-pushed').disabled = shared;
    if (shared) document.getElementById('cleanup-pushed').checked = false;
    renderCleanupEntries();
  });
}
document.getElementById('cleanup-entries').addEventListener('change', invalidateCleanupPreview);
document.getElementById('cleanup-entries').addEventListener('click', async e => {
  const index = e.target.dataset.pinIndex;
  if (index == null || !cleanupData) return;
  const row = cleanupData.entries[Number(index)];
  e.target.disabled = true;
  try {
    await api('/api/cleanup/pin', {method:'POST', json:{target:row.target, pinned:!row.pinned}});
    await loadCleanup();
  } catch (error) { showErr(error.message); e.target.disabled = false; }
});
document.getElementById('cleanup-preview-form').onsubmit = async e => {
  e.preventDefault();
  invalidateCleanupPreview();
  const targets = Array.from(document.querySelectorAll('[data-clean-index]:checked')).map(box => cleanupData.entries[Number(box.dataset.cleanIndex)].target);
  try {
    const result = await api('/api/cleanup/preview', {method:'POST', json:{targets,
      scope:document.getElementById('cleanup-scope').value, include_pushed:document.getElementById('cleanup-pushed').checked}});
    cleanupPreview = result;
    document.getElementById('cleanup-preview-text').textContent = result.count + ' cached entries selected; ' + result.blocked + ' protected/excluded.\\n' +
      fmtBytes(result.reclaimable_bytes) + ' currently reclaimable; ' + fmtBytes(result.potential_bytes) + ' potentially unreferenced after cleanup.\\n' +
      result.note + (result.contains_pushed ? '\\nIncludes locally pushed content: removal may be permanent.' : '') + '\\n\\n' +
      result.targets.map(target => target.slice(0,3).join('/') + ' @ ' + target[3]).join('\\n');
    document.getElementById('cleanup-preview').hidden = false;
  } catch (error) { showErr(error.message); }
};
document.getElementById('cleanup-confirm').onclick = async e => {
  if (!cleanupPreview) return;
  const message = cleanupPreview.contains_pushed ? 'Permanently remove the selected local pushes and clean this cache?' :
    'Remove the previewed cache entries? Mirrored content can download again on the next pull.';
  if (!confirm(message)) return;
  e.target.disabled = true;
  try {
    const result = await api('/api/cleanup/execute', {method:'POST', json:{preview_id:cleanupPreview.preview_id}});
    showSyncToast('Cleanup requested: ' + result.result.deleted + ' local entries removed; ' + fmtBytes(result.result.bytes) + ' reclaimed. Replica results appear below.');
    await loadCleanup();
  } catch (error) { showErr(error.message); invalidateCleanupPreview(); }
  finally { e.target.disabled = false; }
};
document.getElementById('cleanup-policy-form').onsubmit = async e => {
  e.preventDefault();
  try {
    await api('/api/cleanup/policy', {method:'POST', json:{
      manifest_max_idle_days:Number(document.getElementById('cleanup-retention').value),
      disk_high_percent:Number(document.getElementById('cleanup-high').value),
      disk_low_percent:Number(document.getElementById('cleanup-low').value),
      max_cache_bytes:Math.round(Number(document.getElementById('cleanup-max').value) * 1024 ** 3)}});
    showSyncToast('Local cleanup policy saved.');
  } catch (error) { showErr(error.message); }
};
document.getElementById('cleanup-run').onclick = async e => {
  e.target.disabled = true;
  try {
    const result = await api('/api/cleanup/run', {method:'POST', json:{}});
    showSyncToast('Cleanup finished: ' + result.deleted + ' entries removed; ' + fmtBytes(result.bytes) + ' reclaimed.' +
      (result.pressure_remaining ? ' Disk/cache limit remains exceeded.' : ''));
    await loadCleanup();
  } catch (error) { showErr(error.message); }
  finally { e.target.disabled = false; }
};

// -- views --------------------------------------------------------------------

function show(id) {
  for (const v of ['login', 'app']) document.getElementById(v).classList.toggle('active', v === id);
}
function currentView() {
  const b = document.querySelector('#nav button.active');
  return b ? b.dataset.view : 'dash';
}
function selectView(name) {
  document.querySelectorAll('#nav button').forEach(b => b.classList.toggle('active', b.dataset.view === name));
  document.querySelectorAll('main > .view').forEach(v => v.classList.toggle('active', v.id === 'view-' + name));
  clearErr();
  refreshAll();
}
document.getElementById('nav').addEventListener('click', e => {
  if (e.target.dataset && e.target.dataset.view) selectView(e.target.dataset.view);
});

async function showLogin() {
  me = null;
  if (timer) { clearInterval(timer); timer = null; }
  show('login');
  try {
    const cfg = await api('/api/auth/config');
    if (cfg.oidc) {
      document.getElementById('sso').style.display = 'block';
      document.getElementById('sso-btn').textContent = 'Sign in with ' + cfg.provider;
    }
  } catch (e) { /* the form still works */ }
  document.getElementById('login-user').focus();
}
document.getElementById('sso-btn').addEventListener('click', () => { location.href = '/auth/oidc/start'; });
document.getElementById('login-form').addEventListener('submit', async e => {
  e.preventDefault();
  const err = document.getElementById('login-err');
  err.textContent = '';
  try {
    await api('/api/auth/login', {method: 'POST', json: {
      username: document.getElementById('login-user').value,
      password: document.getElementById('login-pass').value}});
    document.getElementById('login-pass').value = '';
    await boot();
  } catch (x) { err.textContent = x.message; }
});
document.getElementById('logout').addEventListener('click', async () => {
  try { await api('/api/auth/logout', {method: 'POST'}); } finally { showLogin(); }
});

async function boot() {
  try { me = await api('/api/me'); } catch (e) { return; }
  show('app');
  const who = document.getElementById('who');
  who.innerHTML = '<b>' + esc(me.username) + '</b> · ' + esc(me.role);
  who.title = me.username + ' (' + me.role + ')';
  document.getElementById('nav-users').style.display = me.role === 'admin' ? '' : 'none';
  document.getElementById('nav-cleanup').style.display = me.role === 'admin' ? '' : 'none';
  document.getElementById('sync-now').style.display = me.role === 'admin' ? '' : 'none';
  if (['users', 'cleanup'].includes(currentView()) && me.role !== 'admin') selectView('dash');
  await refreshAll();
  setAuto();
}

// -- tokens -------------------------------------------------------------------

function tokenRows(items, revokeAttr) {
  if (!items.length) return '<div class="empty">No tokens.</div>';
  return '<table><tr><th>Name</th><th>ID</th><th>Created</th><th>Expires</th><th>Last used</th><th></th></tr>' +
    items.map(t => '<tr><td>' + esc(t.name) + '</td><td class="mono dim">' + esc(t.id) + '</td>' +
      '<td class="dim">' + fmtTime(t.created_at) + '</td>' +
      '<td class="dim">' + (t.expires_at ? fmtTime(t.expires_at) : 'never') + '</td>' +
      '<td class="dim">' + (t.last_used_at ? ago(t.last_used_at) : 'never') + '</td>' +
      '<td><button class="danger" ' + revokeAttr + '="' + esc(t.id) + '">Revoke</button></td></tr>').join('') +
    '</table>';
}
async function loadTokens() {
  me = await api('/api/me');
  const form = document.getElementById('token-form');
  if (me.source === 'config') {
    document.getElementById('tokens').innerHTML = '<div class="empty">The bootstrap admin from the configuration ' +
      'can&#39;t hold tokens. Create a user for day-to-day use.</div>';
    form.style.display = 'none';
    return;
  }
  form.style.display = '';
  document.getElementById('tokens').innerHTML = tokenRows(me.tokens || [], 'data-revoke');
}
document.getElementById('tokens').addEventListener('click', async e => {
  const id = e.target.dataset && e.target.dataset.revoke;
  if (!id || !confirm('Revoke this token? Anything using it stops working.')) return;
  try { await api('/api/me/tokens/' + encodeURIComponent(id), {method: 'DELETE'}); await loadTokens(); }
  catch (x) { showErr(x.message); }
});
document.getElementById('token-form').addEventListener('submit', async e => {
  e.preventDefault();
  const days = document.getElementById('token-days').value;
  try {
    const t = await api('/api/me/tokens', {method: 'POST', json: {
      name: document.getElementById('token-name').value,
      expires_days: days ? Number(days) : null}});
    document.getElementById('token-once').innerHTML = '<div class="once"><b>Copy this token now: ' +
      'it won&#39;t be shown again.</b><code id="new-token">' + esc(t.token) + '</code>' +
      '<button type="button" id="copy-token">Copy</button> ' +
      '<span class="hint">e.g. <span class="mono">docker login &lt;registry&gt; -u ' + esc(me.username) +
      '</span> and paste it as the password</span></div>';
    document.getElementById('copy-token').onclick = () => navigator.clipboard.writeText(t.token);
    document.getElementById('token-name').value = '';
    document.getElementById('token-days').value = '';
    await loadTokens();
  } catch (x) { showErr(x.message); }
});

// -- users (admin) ------------------------------------------------------------

async function loadUsers() {
  const d = await api('/api/users');
  const el = document.getElementById('users');
  if (!d.items.length) { el.innerHTML = '<div class="empty">No users yet.</div>'; return; }
  el.innerHTML = '<table><tr><th>User</th><th>Role</th><th>Source</th><th>Status</th><th>Tokens</th><th></th></tr>' +
    d.items.map(u => {
      const self = u.username === me.username;
      const n = esc(u.username);
      return '<tr><td class="mono">' + n + (self ? ' <span class="dim">(you)</span>' : '') + '</td>' +
        '<td><select data-role="' + n + '"' + (self ? ' disabled' : '') + '>' +
          ['reader', 'pusher', 'admin'].map(r => '<option' + (r === u.role ? ' selected' : '') + '>' + r + '</option>').join('') +
        '</select></td>' +
        '<td class="dim">' + esc(u.source) + '</td>' +
        '<td>' + (u.disabled ? '<span class="pill disabled">disabled</span>' : '<span class="yes">active</span>') + '</td>' +
        '<td><button data-tokens="' + n + '">' + esc(u.tokens) + '</button></td>' +
        '<td style="white-space:nowrap">' + (self ? '' :
          '<button data-toggle="' + n + '" data-disabled="' + (u.disabled ? '1' : '') + '">' + (u.disabled ? 'Enable' : 'Disable') + '</button> ' +
          (u.source === 'local' ? '<button data-reset="' + n + '">Reset password</button> ' : '') +
          '<button class="danger" data-delete="' + n + '">Delete</button>') + '</td></tr>';
    }).join('') + '</table>';
}
const usersEl = document.getElementById('users');
usersEl.addEventListener('change', async e => {
  const name = e.target.dataset && e.target.dataset.role;
  if (!name) return;
  try { await api('/api/users/' + encodeURIComponent(name), {method: 'PATCH', json: {role: e.target.value}}); }
  catch (x) { showErr(x.message); }
  await loadUsers();
});
usersEl.addEventListener('click', async e => {
  const d = e.target.dataset || {};
  try {
    if (d.toggle) {
      await api('/api/users/' + encodeURIComponent(d.toggle), {method: 'PATCH', json: {disabled: !d.disabled}});
    } else if (d.reset) {
      const pw = prompt('New password for ' + d.reset + ' (min 10 characters):');
      if (!pw) return;
      await api('/api/users/' + encodeURIComponent(d.reset), {method: 'PATCH', json: {password: pw}});
    } else if (d.delete) {
      if (!confirm('Delete ' + d.delete + ' and all of their tokens?')) return;
      await api('/api/users/' + encodeURIComponent(d.delete), {method: 'DELETE'});
      document.getElementById('user-tokens-section').style.display = 'none';
    } else if (d.tokens) {
      await loadUserTokens(d.tokens);
      return;
    } else return;
    await loadUsers();
  } catch (x) { showErr(x.message); }
});
async function loadUserTokens(name) {
  const d = await api('/api/users/' + encodeURIComponent(name) + '/tokens');
  document.getElementById('user-tokens-section').style.display = '';
  document.getElementById('ut-name').textContent = name;
  const el = document.getElementById('user-tokens');
  el.dataset.user = name;
  el.innerHTML = tokenRows(d.items, 'data-admin-revoke');
}
document.getElementById('user-tokens').addEventListener('click', async e => {
  const id = e.target.dataset && e.target.dataset.adminRevoke;
  if (!id || !confirm('Revoke this token?')) return;
  try {
    await api('/api/tokens/' + encodeURIComponent(id), {method: 'DELETE'});
    await loadUserTokens(document.getElementById('user-tokens').dataset.user);
    await loadUsers();
  } catch (x) { showErr(x.message); }
});
document.getElementById('user-form').addEventListener('submit', async e => {
  e.preventDefault();
  try {
    await api('/api/users', {method: 'POST', json: {
      username: document.getElementById('nu-name').value,
      role: document.getElementById('nu-role').value,
      password: document.getElementById('nu-pass').value}});
    document.getElementById('nu-name').value = '';
    document.getElementById('nu-pass').value = '';
    clearErr();
    await loadUsers();
  } catch (x) { showErr(x.message); }
});

// -- account ------------------------------------------------------------------

async function loadAccount() {
  me = await api('/api/me');
  const info = {local: 'Local account.', oidc: 'Signed in through single sign-on; your role follows your groups ' +
    'there. Use a personal access token for docker / helm.', config: 'Bootstrap admin from the configuration ' +
    '(push_username). Change its password in the environment.'}[me.source] || '';
  document.getElementById('account-info').innerHTML = '<span class="mono">' + esc(me.username) + '</span> · ' +
    esc(me.role) + '<br><span class="dim">' + esc(info) + '</span>';
  document.getElementById('pw-form').style.display = me.source === 'local' ? '' : 'none';
}
document.getElementById('pw-form').addEventListener('submit', async e => {
  e.preventDefault();
  try {
    await api('/api/me/password', {method: 'POST', json: {
      current: document.getElementById('pw-current').value,
      new: document.getElementById('pw-new').value}});
    document.getElementById('pw-current').value = '';
    document.getElementById('pw-new').value = '';
    clearErr();
    alert('Password changed.');
  } catch (x) { showErr(x.message); }
});

// -- refresh ------------------------------------------------------------------

document.getElementById('refresh').addEventListener('click', refreshAll);

function setAuto() {
  if (timer) { clearInterval(timer); timer = null; }
  if (document.getElementById('auto').checked) {
    timer = setInterval(() => {
      if (currentView() === 'dash') refreshAll();
      else if (currentView() === 'cleanup') api('/api/cleanup').then(renderCleanupStatus).catch(e => showErr(e.message));
    }, 20000);
  }
}
document.getElementById('auto').addEventListener('change', setAuto);

boot().then(() => { if (!me) showLogin(); });
</script>
</body>
</html>
"""
