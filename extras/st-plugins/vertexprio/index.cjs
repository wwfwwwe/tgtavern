const fs = require('fs'), path = require('path'), https = require('https');
const LOG = path.join(__dirname, 'log.txt');
const log = (...a) => fs.appendFile(LOG, new Date().toISOString() + ' ' + a.join(' ') + '\n', () => {});
const H = {'X-Vertex-AI-LLM-Request-Type':'shared','X-Vertex-AI-LLM-Shared-Request-Type':'priority'};
const isV = h => /(^|\.)aiplatform\.googleapis\.com$/.test(String(h || '').split(':')[0]);
function sniff(req) {
  req.on('response', res => { let tail = '';
    res.on('data', c => { tail = (tail + c.toString()).slice(-4096); });
    res.on('end', () => { const m = [...tail.matchAll(/"trafficType"\s*:\s*"(\w+)"/g)].pop();
      const p = [...tail.matchAll(/"promptTokenCount"\s*:\s*(\d+)/g)].pop(); log('HTTP', res.statusCode, 'trafficType', m ? m[1] : '?', 'prompt', p ? p[1] : '?'); }); });
}
const oreq = https.request;
https.request = function (...a) {
  try {
    const isU = typeof a[0] === 'string' || a[0] instanceof URL;
    let o = isU ? (a[1] && typeof a[1] === 'object' ? a[1] : null) : a[0];
    const host = (o && (o.hostname || o.host)) || (isU ? new URL(a[0]).hostname : '');
    if (isV(host)) {
      if (!o) { o = {}; a.splice(1, 0, o); }
      const h = {}; for (const k in (o.headers || {})) if (k.toLowerCase() !== 'accept-encoding') h[k] = o.headers[k];
      o.headers = Object.assign(h, H, {'Accept-Encoding': 'identity'});
      const req = oreq.apply(this, a); sniff(req); log('patched', host, JSON.stringify(Object.keys(o.headers))); return req;
    }
  } catch (e) { log('patch error', e && e.stack); }
  return oreq.apply(this, a);
};
require('module').syncBuiltinESMExports();
module.exports = {
  info: { id: 'vertexprio', name: 'Vertex Priority', description: 'Vertex 高优车道 header' },
  init: async () => log('loaded'),
  exit: async () => { https.request = oreq; },
};
