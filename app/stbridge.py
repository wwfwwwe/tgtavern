"""stbridge — bot 经 CDP（默认 127.0.0.1:9222）操作无头浏览器里常开的酒馆页面（tavpage.py 起的那个）。

页面里的函数在 stbridge.js（注入后挂在 window.__TGB2__ 上）。这里负责：
  连上浏览器、找到酒馆页面、等页面就绪、注入/升级桥；
  页面在刷新时自动等它好了再重试（只重试读状态这类不怕重复的操作）；
  新鲜度：设置/世界书/预设在别处（手机、网页）改过 -> 先刷新页面；当前聊天文件在别处改过 -> 先重读聊天。
"""
import asyncio, logging, os, re
from pathlib import Path

log = logging.getLogger("tgtavern")
JS_F = Path(__file__).resolve().parent / "stbridge.js"
READY = ("!!(window.SillyTavern && SillyTavern.getContext && SillyTavern.getContext().characters"
         " && SillyTavern.getContext().characters.length > 0)")
SAFE = {"io", "status", "getChat", "vertexModels", "reloadChat", "ensure"}          # 页面刷新打断了也可以放心重试的
TRANSIENT = ("Execution context was destroyed", "Target closed", "has been closed", "navigat",
             "Connection closed", "Browser closed", "Cannot find context", "frame was detached", "Most likely")


def _mt(p):
    try:
        return os.stat(p).st_mtime
    except OSError:
        return 0.0


class Bridge:
    def __init__(self, st_dir, cdp="http://127.0.0.1:9222", match=":8000"):
        self.st = Path(st_dir)
        self.cdp = cdp
        self.match = match                                       # 酒馆页面地址里的特征（默认端口 :8000）
        self._pw = self._br = self._page = None
        self._conn = asyncio.Lock()
        self._js = None
        self._v = None

    def js(self):
        src = JS_F.read_text(encoding="utf-8")
        if src != self._js:
            self._js = src
            m = re.search(r"const V = (\d+);", src)
            self._v = int(m.group(1)) if m else 0
        return self._js

    async def _get_page(self):
        async with self._conn:
            if self._page is not None and not self._page.is_closed():
                return self._page
            from playwright.async_api import async_playwright
            if self._pw is None:
                self._pw = await async_playwright().start()
            if self._br is None or not self._br.is_connected():
                self._br = await self._pw.chromium.connect_over_cdp(self.cdp, timeout=30000)
            pages = [p for c in self._br.contexts for p in c.pages if self.match in p.url]
            if not pages:
                raise RuntimeError("无头浏览器里没有酒馆页面（tavpage 在跑吗？）")
            self._page = pages[0]
            return self._page

    async def _ready(self, page):
        js = self.js()
        st = await page.evaluate(f"() => ({READY}) ? ((window.__TGB2__ && window.__TGB2__.v === {self._v}) ? 2 : 1) : 0")
        if st == 2:
            return
        if st == 0:
            await page.wait_for_function(READY, timeout=300000)
        r = await page.evaluate(js)
        if r == "installed":
            io = await page.evaluate("() => window.__TGB2__.io()")
            if io["inj"] - io["boot"] > 120000:
                # 页面早就开着了、之前没装过桥：不知道这段时间里设置有没有在别处改过，刷新一次最稳
                await self.reload("第一次接上这个页面，刷新一下保证设置是最新的", page)

    async def call(self, fn, *args, timeout=600):
        if not re.fullmatch(r"[A-Za-z_]\w*", fn):
            raise ValueError(fn)
        for attempt in range(5):
            called = False
            try:
                page = await self._get_page()
                await self._ready(page)
                called = True
                return await asyncio.wait_for(page.evaluate(f"a => window.__TGB2__.{fn}(...a)", list(args)), timeout)
            except Exception as e:
                msg = str(e)
                if "closed" in msg or "Connection" in msg:
                    self._page = None
                    if "Browser" in msg or "Connection" in msg:
                        self._br = None
                transient = any(k in msg for k in TRANSIENT) or "Connection" in msg
                # 还没真正调用就断了：什么都重试；调用途中断了：只重试不怕重复的（发送、翻页这些可能已经执行过了）
                if not transient or attempt == 4 or (called and fn not in SAFE):
                    raise RuntimeError(clean(msg)) from None
                log.warning("酒馆页面在刷新/断开，3 秒后重试 %s：%s", fn, clean(msg)[:120])
                await asyncio.sleep(3)

    async def reload(self, why="", page=None):
        page = page or await self._get_page()
        log.info("刷新酒馆页面：%s", why)
        try:
            await page.reload(timeout=180000, wait_until="domcontentloaded")
        except Exception as e:
            log.warning("刷新页面出错（继续等它加载）：%s", e)
        await page.wait_for_function(READY, timeout=300000)
        await page.evaluate(self.js())

    def _newest(self):
        m = _mt(self.st / "settings.json")
        for d in ("worlds", "OpenAI Settings"):
            try:
                with os.scandir(self.st / d) as it:
                    for e in it:
                        if e.name.endswith(".json"):
                            m = max(m, e.stat().st_mtime)
            except OSError:
                pass
        return m

    async def fresh(self):
        """设置/世界书/预设比页面上次读、上次自己存的都新 -> 是别处改的，刷新页面"""
        io = await self.call("io")
        base = max(io["inj"], io["own"]) / 1000 + 3
        if self._newest() > base:
            await self.reload("设置/世界书/预设在别处（手机或网页）改过")

    async def ensure(self, avatar, want, card_mtime):
        """页面停到这张卡、这个聊天上；聊天文件在别处改过就重读"""
        r = await self.call("ensure", avatar, want or "", card_mtime * 1000)
        if r.get("chat"):
            p = self.st / "chats" / Path(avatar).stem / f"{r['chat']}.jsonl"
            if _mt(p) > r["chatIO"] / 1000 + 3:
                await self.call("reloadChat")
        return r


def clean(msg):
    """playwright 的报错去掉堆栈和前缀，只留页面里抛出的那句话"""
    m = re.search(r"Error: ([^\n]+)", msg)
    s = m.group(1) if m else msg.split("\n")[0]
    return re.sub(r"^page\.evaluate:\s*", "", s).strip()
