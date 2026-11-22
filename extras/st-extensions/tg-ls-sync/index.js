// TG 本地设置同步：有些酒馆助手脚本（比如抗截断脚本）把设置存在浏览器本地（localStorage），不跟着数据文件走。
// 这里把它们存进 settings.json 的 extension_settings.tglsync，各设备（手机、云端页面）打开时互相套用。
// 规则：哪边改了用哪边；只增改不删除；套用了别处的设置就刷新一次页面（让脚本读到新设置）。
const SKIP = /^(tglsync\.|ar6\.log$)|userInput$|^TH-Panel|NavLock|NavOpened/i, MAX = 200000;  // 这些各设备本来就该不一样
const ctx = () => SillyTavern.getContext();
const readJ = (k, d) => { try { return JSON.parse(localStorage.getItem(k) || 'null') || d; } catch (e) { return d; } };

function collect() {
  const o = {};
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    if (SKIP.test(k)) continue;
    const v = localStorage.getItem(k);
    if (v != null && v.length <= MAX) o[k] = v;
  }
  return o;
}

function sync(first) {
  const es = ctx().extensionSettings;
  const S = es.tglsync || (es.tglsync = { data: {} });
  const R = S.data || {}, L = collect(), seen = readJ('tglsync.seen', {});
  let applied = 0, pushed = 0;
  for (const k of new Set([...Object.keys(L), ...Object.keys(R)])) {
    const localChanged = L[k] !== undefined && L[k] !== seen[k];
    const remoteChanged = R[k] !== undefined && R[k] !== seen[k];
    if (localChanged) {                      // 这台设备上改过：上传（两边都改了也以正在用的这台为准）
      if (R[k] !== L[k]) { R[k] = L[k]; pushed++; }
      seen[k] = L[k];
    } else if (remoteChanged) {              // 别处改过：写进本地
      localStorage.setItem(k, R[k]);
      seen[k] = R[k]; applied++;
    }
  }
  S.data = R;
  localStorage.setItem('tglsync.seen', JSON.stringify(seen));
  if (pushed) { S.at = Date.now(); ctx().saveSettingsDebounced(); }
  if (applied || pushed) console.log('[TG本地设置同步] 写入本地', applied, '项，上传', pushed, '项');
  const last = Number(sessionStorage.getItem('tglsync.reloadAt') || 0);
  if (applied && first && Date.now() - last > 60000) {
    sessionStorage.setItem('tglsync.reloadAt', String(Date.now()));
    location.reload();
  }
}

let started = false;
function start() {
  if (started) return;
  started = true;
  try { sync(true); } catch (e) { console.warn('[TG本地设置同步]', e); }
  setInterval(() => { try { sync(false); } catch (e) { console.warn('[TG本地设置同步]', e); } }, 15000);
}
ctx().eventSource.once(ctx().eventTypes.APP_READY, start);
setTimeout(start, 30000);                  // 万一错过了 APP_READY
