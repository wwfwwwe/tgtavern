const TAG = '[worldinfo-dedupe]';
if (!globalThis.__worldinfoDedupeInstalled) {
    globalThis.__worldinfoDedupeInstalled = true;
    const inflight = new Map();
    let saved = 0;
    const getUrl = (input) => typeof input === 'string' ? input : (input?.url ?? String(input ?? ''));
    const base = globalThis.fetch;
    const wrapped = async function (input, init) {
        const url = getUrl(input);
        const method = String(init?.method ?? 'GET').toUpperCase();
        if (url.includes('/api/worldinfo/get') && method === 'POST' && typeof init?.body === 'string') {
            const key = init.body;
            let p = inflight.get(key);
            if (p) {
                saved++;
                console.debug(TAG, 'shared', key, 'saved total', saved);
            } else {
                const { signal, ...rest } = init;
                p = base(input, rest).then(async (r) => ({
                    text: await r.text(),
                    status: r.status,
                    statusText: r.statusText,
                    type: r.headers.get('content-type') || 'application/json',
                })).finally(() => inflight.delete(key));
                inflight.set(key, p);
            }
            const v = await p;
            return new Response(v.text, { status: v.status, statusText: v.statusText, headers: { 'content-type': v.type } });
        }
        return base(input, init);
    };
    globalThis.fetch = wrapped;
    window.fetch = wrapped;
    globalThis.__worldinfoDedupeStats = () => ({ saved, inflight: inflight.size });
}
