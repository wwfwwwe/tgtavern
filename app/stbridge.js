(() => {
// TG bot <-> 无头酒馆页面的桥。用 page.evaluate(本文件) 注入，挂在 window.__TGB2__ 上。
// 改了这个文件要把 V 加 1（bot 看到版本不同会重新注入）
const V = 3;
if (window.__TGB2__ && window.__TGB2__.v === V) return 'already';
const c = () => SillyTavern.getContext();
const sleep = ms => new Promise(r => setTimeout(r, ms));
const S = window.__TGB2__ = {
  v: V, gen: 0, ended: 0, ver: 0, boot: performance.timeOrigin, inj: Date.now(),
  own: 0, chatIO: 0, charIO: performance.timeOrigin, err: '', errAt: 0,
};
const ev = c().eventSource, T = c().eventTypes;
ev.on(T.GENERATION_STARTED, (type, o, dry) => { if (!dry && type !== 'quiet') S.gen++; });
const fin = () => { S.ended = S.gen; S.ver++; };
if (T.GENERATION_ENDED) ev.on(T.GENERATION_ENDED, fin);
if (T.GENERATION_STOPPED) ev.on(T.GENERATION_STOPPED, fin);
for (const k of ['MESSAGE_SENT', 'MESSAGE_RECEIVED', 'MESSAGE_EDITED', 'MESSAGE_DELETED', 'MESSAGE_SWIPED',
  'MESSAGE_UPDATED', 'CHAT_CHANGED', 'MORE_MESSAGES_LOADED']) if (T[k]) ev.on(T[k], () => { S.ver++; });

// 记下页面自己什么时候存过设置/聊天/角色卡：bot 拿它和文件修改时间比，判断是不是别处（手机、网页）改过
const mark = (u) => {
  const W = window.__TGB2__; if (!W) return;
  u = String(u || ''); const t = Date.now();
  if (/\/api\/(settings\/save|worldinfo\/(edit|create|import|delete)|presets\/(save|delete))/.test(u)) W.own = t;
  else if (/\/api\/chats\/(save|get)\b/.test(u)) W.chatIO = t;
  else if (/\/api\/characters\/(all|get|edit|create|merge-attributes)\b/.test(u)) W.charIO = t;
};
if (!window.__tgbHooks) {
  window.__tgbHooks = 1;
  const of = window.fetch;
  window.fetch = function (u, o) {
    const p = of.apply(this, arguments);
    p.then(() => mark(u && u.url ? u.url : u), () => {});
    return p;
  };
  const oo = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (m, u) { this.addEventListener('loadend', () => mark(u)); return oo.apply(this, arguments); };
  if (window.toastr && toastr.error) {
    const oe = toastr.error.bind(toastr);
    toastr.error = function (msg, title, ...r) {
      const W = window.__TGB2__;
      if (W) {
        W.err = ((title ? title + '：' : '') + String(msg || '')).replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 500);
        W.errAt = Date.now();
      }
      return oe(msg, title, ...r);
    };
  }
}

const saveSettings = async () => { await (await import('/script.js')).saveSettings(); };
const lastMsg = () => { const ch = c().chat || []; return ch[ch.length - 1] || {}; };
const lastSid = () => lastMsg().swipe_id || 0;

S.io = () => ({ boot: S.boot, inj: S.inj, own: S.own, chatIO: S.chatIO, charIO: S.charIO });

S.status = async () => {
  const m = await import('/script.js');
  const stop = document.querySelector('#mes_stop');
  const busy = !!m.is_send_press || (!!stop && getComputedStyle(stop).display !== 'none');
  const ch = c().chat || [], L = ch[ch.length - 1] || {};
  return {
    busy, press: !!m.is_send_press, gen: S.gen, ended: S.ended, boot: S.boot, ver: S.ver, len: ch.length,
    chat: c().getCurrentChatId() || '', err: S.err, errAt: S.errAt,
    last: { u: !!L.is_user, mes: String(L.mes || ''), r: String((L.extra && L.extra.reasoning) || ''),
      sid: L.swipe_id || 0, n: (L.swipes || []).length || 1 },
  };
};

S.getChat = () => {
  const ctx = c(), ch = ctx.chat || [], md = ctx.chatMetadata || {};
  return {
    id: ctx.getCurrentChatId() || '', ver: S.ver,
    avatar: ((ctx.characters || [])[ctx.characterId] || {}).avatar || '',
    vars: md.variables || {}, note: { text: md.note_prompt || '', depth: md.note_depth, freq: md.note_interval },
    msgs: ch.map(x => ({
      u: !!x.is_user, s: !!x.is_system, mes: String(x.mes || ''), r: String((x.extra && x.extra.reasoning) || ''),
      sid: x.swipe_id || 0, n: (x.swipes || []).length || 1, d: String(x.send_date || ''),
    })),
  };
};

// 让页面停在这张卡（和指定的聊天）上。cardMt = 卡文件的修改时间（毫秒），比页面读卡的时间新就先重读角色列表
S.ensure = async (avatar, want, cardMt) => {
  const find = () => (c().characters || []).findIndex(x => x && x.avatar === avatar);
  let id = find();
  if (id < 0 || (cardMt && cardMt > S.charIO + 3000)) {
    await c().getCharacters();
    id = find();
    if (id >= 0 && String(c().characterId) === String(id)) await c().selectCharacterById(id); // 同一张卡：重新载入资料，免得旧资料写回卡里
  }
  if (id < 0) throw new Error('酒馆里找不到这张卡：' + avatar);
  if (String(c().characterId) !== String(id)) await c().selectCharacterById(id);
  if (want && c().getCurrentChatId() !== want) await c().openCharacterChat(want);
  return { chat: c().getCurrentChatId() || '', chatIO: S.chatIO, len: (c().chat || []).length };
};

S.reloadChat = async () => { await c().reloadCurrentChat(); return (c().chat || []).length; };

S.send = async (text) => {
  const ta = document.querySelector('#send_textarea');
  ta.value = text;
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  document.querySelector('#send_but').click();
  return true;
};

S.swipe = async (d) => {
  const before = lastSid();
  const el = document.querySelector('#chat .last_mes ' + (d < 0 ? '.swipe_left' : '.swipe_right'));
  if (!el) throw new Error('找不到翻页按钮');
  el.click();
  for (let k = 0; k < 50 && lastSid() === before; k++) await sleep(100);
  await sleep(300);
  return lastSid();
};

// 翻到第 k 个候选回复（不会在最后一个上再右翻，那样会触发生成）
S.swipeTo = async (k) => {
  for (let t = 0; t < 80; t++) {
    const m = lastMsg(), cur = m.swipe_id || 0, n = (m.swipes || []).length || 1;
    if (cur === k || k < 0 || k >= n) break;
    const got = await S.swipe(k < cur ? -1 : 1);
    if (got === cur) break;
  }
  return lastSid();
};

S.regen = async () => {
  const m = lastMsg(), n = (m.swipes || []).length || 1;
  if ((m.swipe_id || 0) < n - 1) await S.swipeTo(n - 1);
  const el = document.querySelector('#chat .last_mes .swipe_right');
  if (!el) throw new Error('找不到重新生成（右翻）按钮');
  el.click();
  return true;
};

S.cont = async () => {
  c().executeSlashCommandsWithOptions('/continue').catch(e => { S.err = String((e && e.message) || e); S.errAt = Date.now(); });
  return true;
};

S.stop = async () => {
  try { c().stopGeneration(); } catch (e) {}
  return true;
};

S.cut = async (range) => {
  await c().executeSlashCommandsWithOptions('/cut ' + range);
  await sleep(300);
  return (c().chat || []).length;
};

S.edit = async (i, t) => {
  const ctx = c(), m = (ctx.chat || [])[i];
  if (!m) throw new Error('第 ' + (i + 1) + ' 条不存在');
  m.mes = t;
  if (Array.isArray(m.swipes) && m.swipes.length) m.swipes[m.swipe_id || 0] = t;
  try { ctx.updateMessageBlock(i, m); } catch (e) {}
  try { await ctx.eventSource.emit(ctx.eventTypes.MESSAGE_EDITED, i); } catch (e) {}
  await ctx.saveChat();
  return true;
};

S.branch = async (i) => {
  const before = c().getCurrentChatId();
  let b = null;
  try { b = await import('/scripts/bookmarks.js'); } catch (e) {}
  if (b && typeof b.branchChat === 'function') await b.branchChat(Number(i));
  else await c().executeSlashCommandsWithOptions('/branch-create ' + i);
  for (let k = 0; k < 40 && c().getCurrentChatId() === before; k++) await sleep(250);
  if (c().getCurrentChatId() === before) throw new Error('开分支没成功');
  return c().getCurrentChatId();
};

S.newChat = async () => {
  const m = await import('/script.js');
  await m.doNewChat({ deleteCurrentChat: false });
  await sleep(300);
  return c().getCurrentChatId() || '';
};

// 主动消息：给这张卡新开一个聊天（开场白 = 搭讪，第 gi 个开场白），然后把卡的「当前聊天」指回 back
S.greetChat = async (avatar, gi, back, cardMt) => {
  await S.ensure(avatar, '', cardMt);
  const name = await S.newChat();
  const n = (c().chat || []).length ? ((lastMsg().swipes || []).length || 1) : 0;
  if (n > 1 && gi % n) await S.swipeTo(gi % n);
  await c().saveChat();
  const data = S.getChat();
  if (back && back !== name) {
    try { await c().openCharacterChat(back); } catch (e) { console.warn('[tgb] 指回原聊天失败', e); }
  }
  return { name, data };
};

S.setPreset = async (name) => {
  const o = c().chatCompletionSettings;
  const pm = c().getPresetManager ? c().getPresetManager('openai') : null;
  if (pm && pm.findPreset) {
    const v = pm.findPreset(name);
    if (v === undefined || v === null || v === -1) throw new Error('酒馆里没有这个预设：' + name);
    pm.selectPreset(v);
  } else if (window.TavernHelper && TavernHelper.loadPreset) {
    if (!(await TavernHelper.loadPreset(name))) throw new Error('切换预设失败：' + name);
  } else throw new Error('页面里找不到切换预设的接口');
  for (let k = 0; k < 80 && o.preset_settings_openai !== name; k++) await sleep(250);
  await sleep(800);
  await saveSettings();
  return o.preset_settings_openai;
};

S.toggle = async (ids, on) => {
  let n = 0, done = false;
  try {
    const om = await import('/scripts/openai.js');
    const pm = om.promptManager;
    if (pm && pm.getPromptOrderForCharacter) {
      const ord = pm.getPromptOrderForCharacter(pm.activeCharacter || { id: 100001 });
      for (const e of ord) if (ids.includes(e.identifier)) { e.enabled = on; n++; }
      if (pm.saveServiceSettings) await pm.saveServiceSettings();
      try { if (pm.render) pm.render(false); } catch (e) {}
      done = n > 0;
    }
  } catch (e) { console.warn('[tgb] promptManager', e); }
  if (!done && window.TavernHelper && TavernHelper.updatePresetWith) {
    await TavernHelper.updatePresetWith('in_use', p => {
      for (const x of p.prompts || []) if (ids.includes(x.id)) { x.enabled = on; n++; }
      return p;
    });
    done = true;
  }
  if (!done) throw new Error('页面里找不到改预设条目的接口');
  await saveSettings();
  return n;
};

S.wiSet = async (name, on) => {
  const TH = window.TavernHelper;
  if (!TH || !TH.getGlobalWorldbookNames) throw new Error('页面里没有酒馆助手（TavernHelper），改不了世界书');
  const g = [...(await TH.getGlobalWorldbookNames())];
  if (on) {
    if (!g.includes(name)) { g.push(name); await TH.rebindGlobalWorldbooks(g); }
  } else {
    if (g.includes(name)) await TH.rebindGlobalWorldbooks(g.filter(x => x !== name));
    let cb = null;
    try { cb = await TH.getCharWorldbookNames('current'); } catch (e) {}
    if (cb && (cb.primary === name || (cb.additional || []).includes(name))) {
      await TH.rebindCharWorldbooks('current', {
        primary: cb.primary === name ? null : cb.primary,
        additional: (cb.additional || []).filter(x => x !== name),
      });
    }
  }
  await saveSettings();
  return true;
};

S.setApi = async (src, url, keyId) => {
  const o = c().chatCompletionSettings;
  if (src === 'custom') {
    if (keyId) {
      let ok = false;
      try { const sec = await import('/scripts/secrets.js'); if (sec.rotateSecret) { await sec.rotateSecret('api_key_custom', keyId); ok = true; } } catch (e) {}
      if (!ok) {
        const r = await fetch('/api/secrets/rotate', { method: 'POST', headers: c().getRequestHeaders(),
          body: JSON.stringify({ key: 'api_key_custom', id: keyId }) });
        if (!r.ok) throw new Error('切换酒馆里的密钥失败 HTTP ' + r.status);
      }
    }
    $('#custom_api_url_text').val(url).trigger('input');
    o.custom_url = url;
  }
  $('#chat_completion_source').val(src).trigger('change');
  for (let k = 0; k < 40 && o.chat_completion_source !== src; k++) await sleep(250);
  await sleep(500);
  await saveSettings();
  return { src: o.chat_completion_source, url: o.custom_url };
};

S.setModel = async (m) => {
  const o = c().chatCompletionSettings, src = o.chat_completion_source;
  if (src === 'custom') {
    $('#custom_model_id').val(m).trigger('input');
    o.custom_model = m;
  } else if (src === 'vertexai') {
    const s = $('#model_vertexai_select');
    if (!s.find('option').filter((_, x) => x.value === m).length) s.append(new Option(m, m));
    s.val(m).trigger('change');
    o.vertexai_model = m;
  } else throw new Error('bot 只会切 Vertex 和 Custom 的模型，酒馆现在用的是 ' + src);
  await sleep(300);
  await saveSettings();
  return { src, model: m };
};

S.vertexModels = () => [...document.querySelectorAll('#model_vertexai_select option')].map(x => x.value).filter(Boolean);

S.note = async (text, depth, freq) => {
  const md = c().chatMetadata;
  if (text !== null && text !== undefined) md.note_prompt = text;
  if (depth !== null && depth !== undefined) md.note_depth = depth;
  if (freq !== null && freq !== undefined) md.note_interval = freq;
  if (text !== null || depth !== null || freq !== null) await (await import('/script.js')).saveMetadata();
  return { text: md.note_prompt || '', depth: md.note_depth, freq: md.note_interval };
};

return 'installed';
})()
