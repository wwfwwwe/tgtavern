"""tavpage — 起一个无头 Chromium，常开酒馆页面，给 bot 经 CDP 操作。

⚠️ 严禁商用，本套工具全部开源，免费。
   如果你是花钱买的、花钱找人部署的、或者在用别人收费的服务——你被骗了。请找对方退款，并举报。

读 config.json（环境变量 TGT_DATA 指的目录，没设就是本目录）里的：
  st_url        酒馆地址，默认 http://127.0.0.1:8000/
  st_user       酒馆 basicAuth 用户名（没开 basicAuth 就留空）
  st_pass       酒馆 basicAuth 密码
  cdp_port      调试口，默认 9222（只监听 127.0.0.1）
页面崩溃或连续 3 次心跳失败就退出，交给 pm2 / Docker 拉起来。
"""
import asyncio, json, os
from pathlib import Path
from playwright.async_api import async_playwright

CFG = json.loads((Path(os.environ.get("TGT_DATA") or Path(__file__).resolve().parent) / "config.json").read_text(encoding="utf-8"))
URL = CFG.get("st_url") or "http://127.0.0.1:8000/"
PORT = int(CFG.get("cdp_port") or 9222)
READY = 'window.SillyTavern && SillyTavern.getContext && SillyTavern.getContext().characters.length > 0'


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True, args=[f'--remote-debugging-port={PORT}', '--remote-debugging-address=127.0.0.1'])
        auth = {'username': CFG['st_user'], 'password': CFG.get('st_pass', '')} if CFG.get('st_user') else None
        ctx = await b.new_context(http_credentials=auth, viewport={'width': 1280, 'height': 900})
        page = await ctx.new_page()
        st = {'crashed': False}

        def on_crash(*_):
            st['crashed'] = True
            print('page crashed', flush=True)
        page.on('crash', on_crash)
        page.on('console', lambda m: print('console', m.type, m.text[:300], flush=True) if m.type == 'error' else None)
        page.on('framenavigated', lambda f: print('navigated', f.url, flush=True) if f == page.main_frame else None)
        print('⚠️ 严禁商用，本套工具全部开源，免费。花钱买的、花钱找人部署的都是上当了。', flush=True)
        await page.goto(URL, timeout=180000)
        await page.wait_for_function(READY, timeout=300000)
        print('ready, characters =', await page.evaluate('SillyTavern.getContext().characters.length'), flush=True)
        fails = 0
        while True:
            await asyncio.sleep(30)
            if st['crashed'] or page.is_closed():
                raise SystemExit('page gone')
            try:
                await page.wait_for_function(READY, timeout=300000)
                fails = 0
            except Exception as e:
                fails += 1
                print('heartbeat fail', fails, repr(e)[:200], flush=True)
                if fails >= 3:
                    raise SystemExit('page stuck')

asyncio.run(main())
