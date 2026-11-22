#!/usr/bin/env python3
"""tgtavern — 在 Telegram 里玩酒馆（SillyTavern）。

⚠️ 严禁商用，本套工具全部开源，免费。
   如果你是花钱买的、花钱找人部署的、或者在用别人收费的服务——你被骗了。请找对方退款，并举报。

生成由无头浏览器里常开的酒馆页面完成（tavpage.py + stbridge），bot 只负责转发和 TG 里的操作。
同时直接读取酒馆的 data/default-user：
  characters/        角色卡 PNG/JSON
  OpenAI Settings/   对话补全预设（按 prompt_order 拼接）
  worlds/            世界书（关键词、常驻、深度插入、递归、预算、粘性/冷却）
  settings.json      全局世界书、用户人设、全局正则；bot 选的预设如果正好是酒馆当前预设，
                     就读这里的实时条目开关和预设正则（酒馆里点完不用保存）
  secrets.json       Vertex 服务账号
文件改动按修改时间自动重新加载。

config.json 可选项（不写就用默认值）：
  fold_tags    折叠显示的标签，默认 think/thinking/summary/总结/状态栏 等
  unwrap_tags  只去掉标签、保留内容，默认 content/正文
  hide_tags    整段隐藏，默认空
  note_depth   作者注释默认深度 4
  summary_keep / summary_every / summary_words / summary_batch / summary_prompt / summary_template
  tz           显示时间用的时区，默认 Asia/Shanghai
  proactive_gap_hours / proactive_roster / proactive_roster_hour / proactive_quiet_min /
  proactive_pool / proactive_new_pool / proactive_ignore_limit / proactive_small_max_chars /
  proactive_small_max_wi / proactive_scan_parallel / proactive_scan_sec / aux_timeout_sec /
  proactive_prompt / proactive_prompt_first / proactive_prompt_follow   主动消息（/dm）
"""
import asyncio, base64, collections, contextvars, copy, faulthandler, hashlib, html, json, logging, os, random, re, struct, sys, threading, time, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import regex as re2  # 兜底：支持 \p{Han} 等 Python re 不认的写法
except ImportError:
    re2 = None
try:
    import tg_html  # 美化正则的 HTML 输出 -> TG 格式（同目录 tg_html.py；没有就退回旧的折叠显示）
except ImportError:
    tg_html = None

from telegram import Update, InlineKeyboardButton as Btn, InlineKeyboardMarkup, BotCommand
from telegram.constants import ChatAction
from telegram.error import BadRequest, RetryAfter
from telegram.ext import (Application, CommandHandler, MessageHandler, CallbackQueryHandler,
                          ContextTypes, TypeHandler, filters)

BASE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("TGT_DATA") or BASE)      # config.json、state.json 等放这里（Docker 里是 /data）
DATA.mkdir(parents=True, exist_ok=True)
CFG = json.loads((DATA / "config.json").read_text(encoding="utf-8"))
ST = Path(CFG.get("st_user_dir", "/st/data/default-user"))
CHATS = DATA / "chats"; CHATS.mkdir(exist_ok=True); (CHATS / "archive").mkdir(exist_ok=True)
STATE_F = DATA / "state.json"
ALLOWED = set(int(x) for x in CFG.get("allowed_ids", []))
MODELS = CFG.get("models") or ["gemini-2.5-pro"]
TZ = ZoneInfo(CFG.get("tz", "Asia/Shanghai"))
NOTICE = ("⚠️ 严禁商用，本套工具全部开源，免费。\n"
          "如果你是花钱买的、花钱找人部署的、或者在用别人收费的服务——你被骗了。请找对方退款，并举报。")

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("tgtavern")
faulthandler.enable()

# 最近的日志留在内存里，给监控 bot 的 /botlog 用
LOG_RING = collections.deque(maxlen=400)


class RingHandler(logging.Handler):
    def emit(self, record):
        try:
            LOG_RING.append(self.format(record))
        except Exception:
            pass


_rh = RingHandler()
_rh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logging.getLogger().addHandler(_rh)
CRASH_F = DATA / "last_crash.txt"
STATS = {"started": time.time(), "last_update": None, "updates": 0, "restart_reason": None}
try:
    STATS["restart_reason"] = CRASH_F.read_text(encoding="utf-8").strip() or None
    CRASH_F.unlink()
except Exception:
    pass


def die(code, reason):
    """记下原因再退出，pm2 会自动拉起来；下次启动时报告原因"""
    try:
        CRASH_F.write_text(reason, encoding="utf-8")
    except Exception:
        pass
    logging.shutdown()
    os._exit(code)


# ---------------------------------------------------------------- 卡死检测
INFLIGHT = {}                                   # 正在处理的消息：key -> [名字, 开始时间, 最近一次有进展的时间]
CUR = contextvars.ContextVar("cur_update", default=None)


LAST_BEAT = [time.monotonic()]


def beat():
    """有进展了（流式收到字、发出消息等），重置这条消息的卡死计时"""
    LAST_BEAT[0] = time.monotonic()
    k = CUR.get()
    if k in INFLIGHT:
        INFLIGHT[k][2] = LAST_BEAT[0]


def stall_limit():
    return int(CFG.get("stall_sec") or max(600, int(CFG.get("timeout_sec", 300)) * 2))


def dump_tasks():
    """列出每个任务的 await 链（卡在哪个函数哪一行）"""
    lines = []
    for t in asyncio.all_tasks():
        chain, c = [], t.get_coro()
        while c is not None:
            f = getattr(c, "cr_frame", None) or getattr(c, "ag_frame", None) or getattr(c, "gi_frame", None)
            if f is not None:
                chain.append(f"{os.path.basename(f.f_code.co_filename)}:{f.f_lineno} {f.f_code.co_name}")
            c = getattr(c, "cr_await", None) or getattr(c, "ag_await", None) or getattr(c, "gi_yieldfrom", None)
        lines.append(f"[{t.get_name()}] " + " -> ".join(chain))
    return "\n".join(lines)


async def stall_watch(app):
    """处理一条消息超过 stall_sec 秒没有任何进展 -> 记下卡在哪，然后退出让 pm2 重启"""
    while True:
        await asyncio.sleep(20)
        now = time.monotonic()
        for k, (name, start, last) in list(INFLIGHT.items()):
            if now - max(last, LAST_BEAT[0]) > stall_limit():     # 别的消息还在动（比如在排队等生成）就不算卡死
                log.error("卡死：%s 已经 %d 秒没有进展（总共 %d 秒），各任务卡在：\n%s",
                          name, now - last, now - start, dump_tasks())
                try:
                    for uid in ALLOWED:
                        await asyncio.wait_for(app.bot.send_message(
                            chat_id=uid, text=f"⚠️ bot 处理「{name}」卡住了，已记下日志，正在自动重启，稍等几秒再发消息"), 10)
                except Exception:
                    pass
                die(3, f"处理「{name}」卡住 {int(now - last)} 秒，自动重启")


def loop_guard(loop, wait=90, every=15):
    """事件循环被同步代码卡死（整个 bot 不动）-> 打印所有线程堆栈后退出"""
    while True:
        ev = threading.Event()
        loop.call_soon_threadsafe(ev.set)
        if not ev.wait(wait):
            log.error("事件循环卡死超过 %d 秒，线程堆栈：", wait)
            faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
            sys.stderr.flush()
            die(4, f"整个 bot 冻住超过 {wait} 秒，自动重启")
        time.sleep(every)


def tracked(name, fn):
    async def w(update, ctx):
        k = f"{name}:{id(update)}:{time.monotonic()}"
        now = time.monotonic()
        INFLIGHT[k] = [name, now, now]
        tok = CUR.set(k)
        try:
            return await fn(update, ctx)
        finally:
            INFLIGHT.pop(k, None)
            CUR.reset(tok)
    return w


async def log_update(update, ctx):
    """每收到一条更新都记一行（判断是没收到还是收到了卡住）"""
    STATS["last_update"] = time.time(); STATS["updates"] += 1
    try:
        if update.callback_query:
            kind = "按钮 " + (update.callback_query.data or "")
        elif update.edited_message:
            kind = "编辑消息"
        elif update.effective_message and update.effective_message.text:
            t = update.effective_message.text
            kind = ("命令 " + t.split()[0]) if t.startswith("/") else f"消息（{len(t)} 字）"
        else:
            kind = "其他更新"
        log.info("收到 %s", kind)
    except Exception:
        pass


class HealthHandler(BaseHTTPRequestHandler):
    """127.0.0.1 上的健康检查接口，单独线程，bot 卡死时也能回应"""
    def _send(self, code, body, ctype="application/json"):
        b = body.encode("utf-8")
        self.send_response(code); self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            now, mono = time.time(), time.monotonic()
            info = {
                "ok": True, "uptime": now - STATS["started"], "updates": STATS["updates"],
                "last_update_age": (now - STATS["last_update"]) if STATS["last_update"] else None,
                "inflight": [{"name": n, "age": mono - st, "idle": mono - max(la, LAST_BEAT[0])}
                             for n, st, la in list(INFLIGHT.values())],
                "restart_reason": STATS["restart_reason"], "started": STATS["started"],
                "char": STATE.get("char"), "api": STATE.get("api"),
                "model": (STATE.get("models") or {}).get(STATE.get("api") or "vertex"),
            }
            return self._send(200, json.dumps(info, ensure_ascii=False))
        if u.path == "/log":
            n = int((parse_qs(u.query).get("n") or ["30"])[0])
            return self._send(200, "\n".join(list(LOG_RING)[-max(1, min(n, 400)):]), "text/plain")
        self._send(404, "{}")

    def do_POST(self):
        if urlparse(self.path).path == "/restart":
            self._send(200, '{"ok": true}')
            log.warning("收到监控 bot 的重启指令")
            threading.Timer(0.5, die, (5, "从监控 bot 手动重启")).start()
            return
        self._send(404, "{}")

    def log_message(self, *a):
        pass


def start_health():
    port = int(CFG.get("health_port", 9102))
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), HealthHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log.info("健康检查接口：127.0.0.1:%d", port)
    except Exception as e:
        log.warning("健康检查接口没开起来：%s", e)


async def on_error(update, ctx):
    log.error("处理出错：%s", ctx.error, exc_info=ctx.error)


# ---------------------------------------------------------------- utils
def rjson(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def wjson(p, obj):
    p = Path(p); tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def est_tokens(s):
    return int(len(s.encode("utf-8")) / 3.5) + 1


def now_ms():
    return int(time.time() * 1000)


class MtimeCache:
    """按文件修改时间缓存解析结果，文件变了自动重读。"""
    def __init__(self, loader):
        self.loader, self.data = loader, {}

    def get(self, path):
        path = str(path)
        try:
            mt = os.stat(path).st_mtime_ns
        except FileNotFoundError:
            self.data.pop(path, None); return None
        hit = self.data.get(path)
        if hit and hit[0] == mt:
            return hit[1]
        try:
            val = self.loader(path)
        except Exception as e:
            log.warning("load fail %s: %s", path, e); val = None
        self.data[path] = (mt, val)
        return val


# ---------------------------------------------------------------- character cards
def read_png_card(path):
    texts = {}
    with open(path, "rb") as f:
        if f.read(8) != b"\x89PNG\r\n\x1a\n":
            return None
        while True:
            h = f.read(8)
            if len(h) < 8:
                break
            ln, typ = struct.unpack(">I4s", h)
            if typ == b"tEXt":
                data = f.read(ln); f.seek(4, 1)
                k, _, v = data.partition(b"\x00")
                texts[k.decode("latin1").lower()] = v
            elif typ == b"IEND":
                break
            else:
                f.seek(ln + 4, 1)
    raw = texts.get("ccv3") or texts.get("chara")
    if not raw:
        return None
    return json.loads(base64.b64decode(raw).decode("utf-8"))


def load_card(path):
    j = read_png_card(path) if path.lower().endswith(".png") else rjson(path)
    if not j:
        return None
    d = j["data"] if isinstance(j.get("data"), dict) else j
    ext = d.get("extensions") or {}
    return {
        "file": os.path.basename(path),
        "name": d.get("name") or j.get("name") or Path(path).stem,
        "description": d.get("description", ""),
        "personality": d.get("personality", ""),
        "scenario": d.get("scenario", ""),
        "first_mes": d.get("first_mes", ""),
        "mes_example": d.get("mes_example", ""),
        "alternate_greetings": d.get("alternate_greetings") or [],
        "system_prompt": d.get("system_prompt", ""),
        "post_history_instructions": d.get("post_history_instructions", ""),
        "depth_prompt": ext.get("depth_prompt") or {},
        "world": ext.get("world", ""),
        "character_book": d.get("character_book"),
        "regex_scripts": ext.get("regex_scripts") or [],
        "helper": any((("tavern" in k.lower() and "helper" in k.lower()) or "slash" in k.lower())
                      for k, v in ext.items() if v),           # 酒馆助手（JS-Slash-Runner）的卡内脚本
        "chat": str(j.get("chat") or d.get("chat") or ""),
    }


card_cache = MtimeCache(load_card)


def list_cards():
    d = ST / "characters"
    out = []
    for p in sorted(d.iterdir()) if d.exists() else []:
        if p.suffix.lower() in (".png", ".json") and p.is_file():
            c = card_cache.get(p)
            if c:
                out.append(c)
    return out


def get_card(fname):
    return card_cache.get(ST / "characters" / fname) if fname else None


# ---------------------------------------------------------------- settings / secrets
settings_cache = MtimeCache(rjson)


def st_settings():
    return settings_cache.get(ST / "settings.json") or {}


def st_oai():
    s = st_settings()
    return s.get("oai_settings") or (s if "prompt_order" in s else {})


def st_cur_preset():
    """酒馆当前选中的对话补全预设名"""
    return st_oai().get("preset_settings_openai") or st_settings().get("preset_settings_openai") or ""


def st_secret(name):
    s = rjson(ST / "secrets.json", {}) or {}
    v = s.get(name)
    if isinstance(v, list):  # 新版 secrets 格式: [{value, active, ...}]
        pick = next((x for x in v if x.get("active")), v[0] if v else None)
        v = pick.get("value") if pick else None
    return v


secrets_cache = MtimeCache(rjson)


def st_custom_keys():
    """酒馆里存的 Custom（OpenAI 兼容）密钥里，备注写的是地址的那些：[{id, url, value, active}]"""
    v = (secrets_cache.get(ST / "secrets.json") or {}).get("api_key_custom")
    out = []
    for x in v if isinstance(v, list) else []:
        if not isinstance(x, dict) or not x.get("value"):
            continue
        lb = str(x.get("label") or "").strip()
        if re.match(r"^https?://\S+$", lb):
            out.append({"id": str(x.get("id") or ""), "url": lb, "value": x["value"], "active": bool(x.get("active"))})
    return out


def vertex_sa():
    v = st_secret("vertexai_service_account_json")
    if isinstance(v, str):
        v = json.loads(v)
    if not v:
        raise RuntimeError("secrets.json 里没找到 vertexai_service_account_json")
    return v


# ---------------------------------------------------------------- macros
MACRO_RE = re.compile(r"\{\{((?:(?!\{\{|\}\}).)*)\}\}", re.S)
TRIM = "\x00TRIM\x00"


def roll(expr):
    m = re.match(r"\s*(\d*)d(\d+)\s*([+-]\s*\d+)?", expr)
    if not m:
        try:
            return str(random.randint(1, int(expr)))
        except Exception:
            return expr
    n, s = int(m.group(1) or 1), int(m.group(2))
    return str(sum(random.randint(1, s) for _ in range(n)) + int((m.group(3) or "0").replace(" ", "")))


def _var_path(path):
    path = re.sub(r"\[(['\"])(.*?)\1\]", r".\2", path)
    path = re.sub(r"\[(\d+)\]", r".\1", path)
    return [x for x in path.split(".") if x != ""]


def tavern_var(ctx, scope, path):
    """按路径取变量：MVU 的状态树（stat_data/display_data/delta_data）和 {{setvar}} 的变量都能取"""
    tree = ctx.get("mvu_tree") or {}
    src = ctx.get("gvars", {}) if scope == "global" else ctx.get("vars", {})
    if path in src:
        return src[path]
    keys = _var_path(path)
    for root in (tree, src):
        cur, ok = root, True
        for k in keys:
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            elif isinstance(cur, list) and k.isdigit() and int(k) < len(cur):
                cur = cur[int(k)]
            else:
                ok = False; break
        if ok and keys:
            return cur
    return None


def fmt_var(v, inline=False):
    if not isinstance(v, (dict, list)):
        return str(v)
    if inline:
        return json.dumps(v, ensure_ascii=False)
    try:
        import yaml
        return yaml.safe_dump(v, allow_unicode=True, sort_keys=False).strip()
    except Exception:
        return json.dumps(v, ensure_ascii=False, indent=2)


def macro(text, ctx):
    if not text or "{{" not in text:
        return text or ""
    lv, gv = ctx.setdefault("vars", {}), ctx.setdefault("gvars", {})

    def num(x):
        try:
            return float(x) if "." in str(x) else int(x)
        except Exception:
            return None

    def sub(m):
        body = m.group(1); low = body.strip().lower()
        if low.startswith("//"):
            return ""
        simple = {
            "char": ctx.get("char", ""), "user": ctx.get("user", ""), "description": ctx.get("description", ""),
            "personality": ctx.get("personality", ""), "scenario": ctx.get("scenario", ""),
            "persona": ctx.get("persona", ""), "mesexamples": ctx.get("mesExamples", ""),
            "mesexamplesraw": ctx.get("mesExamples", ""), "charprompt": ctx.get("charPrompt", ""),
            "charjailbreak": ctx.get("charJailbreak", ""), "original": ctx.get("original", ""),
            "newline": "\n", "trim": TRIM, "noop": "", "model": ctx.get("model", ""),
            "lastmessage": ctx.get("lastMessage", ""), "lastusermessage": ctx.get("lastUser", ""),
            "lastcharmessage": ctx.get("lastChar", ""), "input": ctx.get("lastUser", ""),
            "charversion": "", "group": ctx.get("char", ""), "groupnotmuted": ctx.get("char", ""),
            "summary": ctx.get("summary", ""),
        }
        if low in simple:
            return simple[low]
        now = datetime.datetime.now(TZ)
        if low == "time": return now.strftime("%H:%M")
        if low == "date": return now.strftime("%Y-%m-%d")
        if low == "weekday": return now.strftime("%A")
        if low == "isotime": return now.strftime("%H:%M")
        if low == "isodate": return now.strftime("%Y-%m-%d")
        if low in ("idle_duration", "lastgenerationtype"): return ""
        if low.startswith(("random", "pick")):
            rest = body.split("::", 1)[1] if "::" in body else body.split(":", 1)[1] if ":" in body else ""
            opts = rest.split("::") if "::" in body else rest.split(",")
            return random.choice(opts) if opts else ""
        if low.startswith("roll"):
            rest = re.split(r"::|:", body, 1)
            return roll(rest[1]) if len(rest) > 1 else ""
        parts = body.split("::")
        op = parts[0].strip().lower()
        store = gv if "global" in op else lv
        op = op.replace("global", "")
        if op == "setvar" and len(parts) >= 3:
            store[parts[1].strip()] = "::".join(parts[2:]); return ""
        if op == "getvar" and len(parts) >= 2:
            return str(store.get(parts[1].strip(), ""))
        if op == "addvar" and len(parts) >= 3:
            k, v = parts[1].strip(), "::".join(parts[2:])
            a, b = num(store.get(k, 0) or 0), num(v)
            store[k] = (a + b) if a is not None and b is not None else str(store.get(k, "")) + v
            return ""
        if op in ("incvar", "decvar") and len(parts) >= 2:
            k = parts[1].strip(); a = num(store.get(k, 0) or 0) or 0
            store[k] = a + (1 if op == "incvar" else -1); return str(store[k])
        # 酒馆助手 / MVU 的变量宏：{{get_message_variable::stat_data.世界.时间}}、{{format_message_variable::stat_data}}
        mv = re.match(r"^(get|format)_(message|chat|character|global|preset|script)_variable$", op0 := parts[0].strip().lower())
        if mv and len(parts) >= 2:
            val = tavern_var(ctx, mv.group(2), parts[1].strip())
            if val is None:
                return ""
            return fmt_var(val) if mv.group(1) == "format" else (val if isinstance(val, str) else fmt_var(val, inline=True))
        return m.group(0)

    for _ in range(20):
        new = MACRO_RE.sub(sub, text)
        if new == text:
            break
        text = new
    if TRIM in text:
        text = re.sub(r"\s*" + TRIM + r"\s*", "", text)
    return text


# ---------------------------------------------------------------- regex（照酒馆的正则引擎）
REPL_TOK = re.compile(r"\$(\d+)|\$<([^>]+)>")
HTML_LIKE = re.compile(r"<\s*/?\s*[a-zA-Z!][^>]*>|```")
P_USER, P_AI, P_WI, P_REASON = 1, 2, 5, 6


def js_regex(s):
    """'/pat/flags' -> (compiled, global?)"""
    m = re.match(r"^/(.*)/([a-z]*)$", s, re.S)
    pat, flags = (m.group(1), m.group(2)) if m else (s, "")
    pat = pat.replace("[^]", r"[\s\S]")
    pat = re.sub(r"\(\?<([A-Za-z_]\w*)>", r"(?P<\1>", pat)
    pat = re.sub(r"\\k<([A-Za-z_]\w*)>", r"(?P=\1)", pat)
    f = 0
    if "i" in flags: f |= re.I
    if "s" in flags: f |= re.S
    if "m" in flags: f |= re.M
    try:
        return re.compile(pat, f), ("g" in flags)
    except re.error:
        if re2 is None:
            raise
        return re2.compile(pat, f | re2.V0), ("g" in flags)


_rx_cache = {}


def compile_rx(s):
    if s not in _rx_cache:
        try:
            _rx_cache[s] = js_regex(s)
        except Exception as e:
            log.warning("regex skip %r: %s", s[:80], e); _rx_cache[s] = None
    return _rx_cache[s]


def regex_scripts(card, preset=None):
    ext = (st_settings().get("extension_settings") or {})
    scripts = list(ext.get("regex") or [])
    if preset:
        scripts += ((preset.get("extensions") or {}).get("regex_scripts") or [])
    if card:
        scripts += card.get("regex_scripts") or []
    return [s for s in scripts if isinstance(s, dict) and not s.get("disabled")]


def mctx_for(card, chat=None):
    """正则里用的宏上下文（拷贝变量，正则不会改动聊天里的变量）"""
    return {"char": card["name"] if card else "", "user": user_name(st_settings()),
            "vars": dict((chat or {}).get("vars") or {}), "gvars": dict(STATE.get("gvars") or {}),
            "mvu_tree": (chat or {}).get("mvu_tree") or {}}


def run_script(s, text, mc, tg=None):
    """tg：显示时翻译 HTML 美化用的回调 tg(script, match, 替换结果, 用到的捕获组) -> 最终替换成的文字"""
    find = s.get("findRegex") or ""
    sub = s.get("substituteRegex") or 0
    if sub:
        if sub == 2:
            find = macro(find, dict(mc, char=re.escape(mc.get("char", "")), user=re.escape(mc.get("user", ""))))
        else:
            find = macro(find, mc)
    c = compile_rx(find)
    if not c:
        return text
    rx, g = c
    rep = re.sub(r"\{\{match\}\}", "$0", s.get("replaceString") or "", flags=re.I)
    trims = [macro(t, mc) for t in (s.get("trimStrings") or []) if t]

    def fn(m):
        used = []

        def grp(mm):
            try:
                v = m.group(int(mm.group(1))) if mm.group(1) is not None else m.group(mm.group(2))
            except Exception:
                v = ""
            v = v or ""
            for t in trims:
                v = v.replace(t, "")
            if tg and not getattr(tg, "raw", False):
                v = v.strip(); used.append(v)    # 只给显示翻译用：去掉捕获组两头的换行
            return v
        out = macro(REPL_TOK.sub(grp, rep), mc)
        return tg(s, m, out, used) if tg else out

    return rx.sub(fn, text, count=0 if g else 1)


# 正文里的思考块（<thinking>…</thinking>，没闭合就到结尾）。思考里常会「提到」<imgthink> <image> 这类标签名，
# 像「隐藏imgthink」这种 <imgthink>[\s\S]*?</imgthink> 的正则会从提到的地方一直吃到后面真正的 </imgthink>，
# 把中间的正文整段吞掉（酒馆里也一样）。所以显示和发送时先把思考块护住，正则只处理思考块外面的部分
THINK_SPAN = re.compile(r"<\s*(think|thinking|reasoning)\s*>[\s\S]*?(?:<\s*/\s*\1\s*>|$)", re.I)
THINK_PH = re.compile(r"\x00TH(\d+)\x00")


def apply_regex(text, card, placement, mode, depth=None, preset=None, chat=None):
    if not text or placement != P_AI or mode not in ("display", "prompt") or not THINK_SPAN.search(text):
        return _apply_regex(text, card, placement, mode, depth, preset, chat)
    kept = []

    def hold(m):
        kept.append(m.group(0)); return f"\x00TH{len(kept) - 1}\x00"
    out = _apply_regex(THINK_SPAN.sub(hold, text), card, placement, mode, depth, preset, chat)
    return THINK_PH.sub(lambda m: kept[int(m.group(1))] if int(m.group(1)) < len(kept) else "", out)


def html_translate_on():
    return tg_html is not None and CFG.get("html_translate", True)


_tg_cache = collections.OrderedDict()


def tg_translate(out, used):
    """tg_html.translate 带缓存（流式时同一块会反复显示）"""
    k = (out, tuple(used))
    if k in _tg_cache:
        _tg_cache.move_to_end(k); return _tg_cache[k]
    try:
        r = tg_html.translate(out, used)
    except Exception as e:
        log.warning("html translate fail: %s", e); r = None
    _tg_cache[k] = r
    if len(_tg_cache) > 300:
        _tg_cache.popitem(last=False)
    return r


def tg_block(s, m, out, used, total):
    """一处美化正则命中：翻得了就换成 TG 格式（短的直接内嵌，长的放进折叠块），翻不了就保持原文走旧的标签折叠"""
    if tg_html.is_fragment(out):               # 半截 HTML：原样插进去，等拼完整后由 tokenize/图片处理；吃掉的字照样留
        kh = keep_hidden(total)(s, m, out, used)
        if kh == out or kh == m.group(0):
            return kh
        return kh[len(out):] + out              # 留下的字放在前面，免得被插进没写完的标签里
    r = tg_translate(out, used)
    if r is None:
        return m.group(0)
    title, body = r
    if not title and not body:
        return keep_hidden(total)(s, m, "", used) if not used else ""   # 纯装饰；原文里要是有字也不吞
    if (total > 500 and len(m.group(0)) > total * 0.7) or (not title and "\n" not in body and len(body) <= 120):
        return body                             # 包住整段正文的美化，或一小段行内美化：不折叠
    t = FIND_TAG.search(s.get("findRegex") or "")
    if not title:
        title = "📌 " + _label(s.get("scriptName"), t.group(1) if t else "美化")
    out_txt = f"\n<tgfold>**{title}**\n{body}</tgfold>\n"
    if not used:                 # 模板不用捕获组（从变量读值）：原文里有、模板里没有的字，单独留一份
        extra = keep_hidden(total)(s, m, "", used)
        if extra:
            out_txt += extra
    return out_txt


def not_swallow_on():
    return CFG.get("show_hidden", True)


def _plain(s):
    return re.sub(r"\s+", "", re.sub(r"<[^<>\n]*>|```[\w-]*", "", s or ""))


def keep_hidden(total):
    """显示正则删掉的内容不吞：酒馆里这些常交给面板/变量显示，bot 里没有面板，就放进「🙈 隐藏内容」折叠块。
    只删标签不删字的、图片提示词、包住大半条消息的（远楼层隐藏这类）不管：前两种照删，最后一种干脆不删"""
    def cb(sc, m, out, used):
        whole = m.group(0)
        gone = _plain(whole)
        kept = _plain(out)
        if len(gone) < 6 or gone in kept:
            return out
        miss = sum(len(w) for w in re.findall(r"[0-9A-Za-z\u00c0-\uffff]{2,}", gone) if w not in kept)
        if miss < 6:
            return out
        t = FIND_TAG.search(sc.get("findRegex") or "")
        tag = t.group(1).lower() if t else ""
        if tag in ("imgthink", "image") or tag in {x.lower() for x in CFG.get("hide_tags") or []}:
            return out
        if total > 200 and len(whole) > total * 0.7:
            return whole
        body = re.sub(r"<[^<>\n]*>", "", whole).strip()
        body = re.sub(r"\n{3,}", "\n\n", body)
        label = _label(sc.get("scriptName"), tag or "隐藏")
        return f"{out}\n<tgfold>**🙈 {label}**\n{body}</tgfold>\n"
    cb.raw = True
    return cb


def _apply_regex(text, card, placement, mode, depth=None, preset=None, chat=None):
    """placement: 1=用户输入 2=AI输出 5=世界书 6=思考过程
    mode: store（存进记录）| prompt（只发给模型）| wi（世界书条目）| display（只改显示，只用不含 HTML 的）"""
    if not text:
        return text
    mc = None
    total = len(text)
    for s in regex_scripts(card, preset):
        if placement not in (s.get("placement") or []):
            continue
        md, po = bool(s.get("markdownOnly")), bool(s.get("promptOnly"))
        if mode == "store" and (md or po): continue
        if mode == "prompt" and not po: continue
        if mode == "wi" and md: continue
        tg = None
        if mode == "display":
            if not md:
                continue
            if HTML_LIKE.search(s.get("replaceString") or ""):
                if not html_translate_on():
                    continue
                tg = lambda sc, m, out, used: tg_block(sc, m, out, used, total)
            elif not_swallow_on():
                tg = keep_hidden(total)
        if depth is not None and mode in ("prompt", "display"):     # 酒馆显示时也看楼层深度（如「10 楼外隐藏」）
            mn, mx = s.get("minDepth"), s.get("maxDepth")
            if isinstance(mn, int) and mn >= 0 and depth < mn: continue
            if isinstance(mx, int) and mx >= 0 and depth > mx: continue
        if mc is None:
            mc = mctx_for(card, chat)
        try:
            text = run_script(s, text, mc, tg)
        except Exception as e:
            log.warning("regex apply fail %s: %s", s.get("scriptName"), e)
    return text


# ---------------------------------------------------------------- reasoning（思考过程单独存，不发回给模型）
THINK_BLOCK = re.compile(r"<\s*(think|thinking|reasoning)\s*>([\s\S]*?)<\s*/\s*\1\s*>", re.I)
ORPHAN_THINK = re.compile(r"^((?:(?!<\s*(?:think|thinking|reasoning)\b)[\s\S])*?)<\s*/\s*(think|thinking|reasoning)\s*>", re.I)


def body_of(buf):
    """流式中途：去掉思考过程后的正文；思考还没结束算没有正文"""
    r, b = split_reasoning(buf)
    if not r and re.match(r"\s*<\s*(think|thinking|reasoning)\s*>", b, re.I):
        return ""
    return b.strip()


def split_reasoning(text):
    m = ORPHAN_THINK.match(text)
    if m:
        return m.group(1).strip(), text[m.end():].lstrip()
    m = THINK_BLOCK.search(text)
    if m and not text[:m.start()].strip():
        return m.group(2).strip(), (text[:m.start()] + text[m.end():]).lstrip()
    return "", text


# ---------------------------------------------------------------- world info
POS_BEFORE, POS_AFTER, POS_ANTOP, POS_ANBOT, POS_DEPTH, POS_EMTOP, POS_EMBOT = range(7)


def norm_entry(e, book):
    return {
        "book": book, "uid": e.get("uid"), "comment": e.get("comment", ""),
        "key": [k for k in (e.get("key") or []) if str(k).strip()],
        "key2": [k for k in (e.get("keysecondary") or []) if str(k).strip()],
        "content": e.get("content") or "", "constant": bool(e.get("constant")),
        "selective": e.get("selective", True), "logic": e.get("selectiveLogic") or 0,
        "order": e.get("order", 100) or 0, "position": e.get("position", 0) or 0,
        "depth": e.get("depth", 4) if e.get("depth") is not None else 4,
        "role": e.get("role") or 0, "disable": bool(e.get("disable")),
        "prob": e.get("probability", 100) if e.get("probability") is not None else 100,
        "useProb": e.get("useProbability", True),
        "scanDepth": e.get("scanDepth"), "case": e.get("caseSensitive"),
        "whole": e.get("matchWholeWords"), "excludeRec": bool(e.get("excludeRecursion")),
        "preventRec": bool(e.get("preventRecursion")), "delayRec": bool(e.get("delayUntilRecursion")),
        "group": e.get("group") or "", "groupOverride": bool(e.get("groupOverride")),
        "groupWeight": e.get("groupWeight", 100) or 100, "ignoreBudget": bool(e.get("ignoreBudget")),
        "delay": e.get("delay") or 0, "sticky": int(e.get("sticky") or 0), "cooldown": int(e.get("cooldown") or 0),
    }


def load_book(path):
    j = rjson(path) or {}
    name = Path(path).stem
    ents = j.get("entries") or {}
    ents = ents.values() if isinstance(ents, dict) else ents
    return [norm_entry(e, name) for e in ents if (e.get("content") or "").strip()]


book_cache = MtimeCache(load_book)


def convert_char_book(cb, name):
    out = []
    for e in (cb or {}).get("entries") or []:
        if not e.get("enabled", True):
            continue
        ex = e.get("extensions") or {}
        pos = ex.get("position")
        if pos is None:
            pos = 1 if e.get("position") == "after_char" else 0
        out.append(norm_entry({
            "uid": e.get("id"), "comment": e.get("comment") or e.get("name", ""),
            "key": e.get("keys"), "keysecondary": e.get("secondary_keys"), "content": e.get("content"),
            "constant": e.get("constant"), "selective": e.get("selective", True),
            "selectiveLogic": ex.get("selectiveLogic"), "order": e.get("insertion_order", 100),
            "position": pos, "depth": ex.get("depth"), "role": ex.get("role"),
            "probability": ex.get("probability"), "useProbability": ex.get("useProbability", True),
            "scanDepth": ex.get("scan_depth"), "caseSensitive": e.get("case_sensitive"),
            "matchWholeWords": ex.get("match_whole_words"), "group": ex.get("group"),
            "excludeRecursion": ex.get("exclude_recursion"), "preventRecursion": ex.get("prevent_recursion"),
            "sticky": ex.get("sticky"), "cooldown": ex.get("cooldown"), "delay": ex.get("delay"),
        }, name + "(内嵌)"))
    return out


def wi_settings():
    s = st_settings()
    w = s.get("world_info_settings") or {}
    wi = w.get("world_info") or s.get("world_info") or {}
    return w, wi


def active_books(card):
    base = base_books(card)
    ov = STATE.get("wi_ov") or {}
    names = [n for n in base if ov.get(n, True)] + [n for n, v in ov.items() if v and n not in base]
    seen, entries = set(), []
    for n in names:
        if n in seen: continue
        seen.add(n)
        b = book_cache.get(ST / "worlds" / f"{n}.json")
        if b: entries += b
    if card and not card.get("world") and card.get("character_book") and ov.get(card["name"] + "(内嵌)", True):
        entries += convert_char_book(card["character_book"], card["name"])
    return entries


_ww_cache = {}


def key_match(key, buf, buf_low, case, whole):
    key = str(key)
    if len(key) > 2 and key.startswith("/") and re.match(r"^/.+/[a-z]*$", key, re.S):
        c = compile_rx(key)
        return bool(c and c[0].search(buf))
    k = key.strip()
    if not case:
        k, hay = k.lower(), buf_low
    else:
        hay = buf
    if not whole or len(k.split()) > 1:
        return k in hay
    if k not in hay:
        return False
    rx = _ww_cache.get(k)
    if rx is None:
        rx = _ww_cache[k] = re.compile(r"(?:^|\W)" + re.escape(k) + r"(?:$|\W)", re.ASCII)
    return bool(rx.search(hay))


def entry_match(e, buf, low, gcase, gwhole):
    case = gcase if e["case"] is None else e["case"]
    whole = gwhole if e["whole"] is None else e["whole"]
    if not any(key_match(k, buf, low, case, whole) for k in e["key"]):
        return False
    if not e["selective"] or not e["key2"]:
        return True
    hits = [key_match(k, buf, low, case, whole) for k in e["key2"]]
    lg = e["logic"]
    if lg == 0: return any(hits)          # AND ANY
    if lg == 1: return not all(hits)      # NOT ALL
    if lg == 2: return not any(hits)      # NOT ANY
    if lg == 3: return all(hits)          # AND ALL
    return True


def entry_key(e):
    return f"{e['book']}:{e['uid']}"






# ---------------------------------------------------------------- presets
preset_cache = MtimeCache(rjson)


def list_presets():
    d = ST / "OpenAI Settings"
    return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []


def get_preset(name):
    return (preset_cache.get(ST / "OpenAI Settings" / f"{name}.json") if name else None) or {}


def effective_preset(name):
    """bot 选的预设如果正好是酒馆当前在用的，就用酒馆 settings.json 里的实时条目和开关、预设正则"""
    p = dict(get_preset(name))
    if name and name == st_cur_preset():
        o = st_oai()
        live = False
        for k in ("prompts", "prompt_order"):
            if o.get(k):
                p[k] = o[k]; live = True
        ex = o.get("extensions")
        if isinstance(ex, dict) and "regex_scripts" in ex:
            p["extensions"] = dict(p.get("extensions") or {}, regex_scripts=ex["regex_scripts"])
        p["_live"] = live
    return p


def cur_preset():
    name = st_cur_preset()
    STATE["preset"] = name
    return effective_preset(name)


def preset_order(p):
    po = p.get("prompt_order") or []
    if isinstance(po, list) and po:
        for cid in (100001, 100000):
            for x in po:
                if x.get("character_id") == cid:
                    return x.get("order") or []
        return po[0].get("order") or []
    return [{"identifier": i, "enabled": True} for i in
            ("main", "worldInfoBefore", "personaDescription", "charDescription", "charPersonality",
             "scenario", "enhanceDefinitions", "nsfw", "worldInfoAfter", "dialogueExamples",
             "chatHistory", "jailbreak")]


def toggles_for(pname):
    return {}                    # 条目开关直接改酒馆，bot 自己不再另存一份


def prompt_entries(pname):
    """当前预设的所有条目和生效状态（含 bot 里 /toggle 的改动）"""
    p = effective_preset(pname)
    ov = toggles_for(pname)
    prompts = {x.get("identifier"): x for x in (p.get("prompts") or [])}
    out = []
    for o in preset_order(p):
        ident = o.get("identifier")
        pr = prompts.get(ident) or {}
        base = bool(o.get("enabled", True))
        out.append({"id": ident, "name": pr.get("name") or ident, "base": base,
                    "on": ov.get(ident, base), "ov": ident in ov, "content": pr.get("content") or ""})
    return out


DEFAULT_PROMPTS = {
    "main": "Write {{char}}'s next reply in a fictional chat between {{char}} and {{user}}.",
}
ROLE_MAP = {0: "system", 1: "user", 2: "assistant", "system": "system", "user": "user", "assistant": "assistant"}


def persona_desc(s):
    pu = s.get("power_user") or {}
    av = s.get("user_avatar") or ""
    pd = (pu.get("persona_descriptions") or {}).get(av) or {}
    return pd.get("description") or pu.get("persona_description") or ""


def user_name(s):
    if CFG.get("user_name"): return CFG["user_name"]
    pu = s.get("power_user") or {}
    av = s.get("user_avatar") or ""
    return (pu.get("personas") or {}).get(av) or s.get("username") or "User"










# ---------------------------------------------------------------- Vertex
_client = None


def client():
    global _client
    if _client is None:
        from google import genai
        from google.oauth2 import service_account
        sa = vertex_sa()
        creds = service_account.Credentials.from_service_account_info(
            sa, scopes=["https://www.googleapis.com/auth/cloud-platform"])
        _client = genai.Client(vertexai=True, project=sa["project_id"], location=CFG.get("location", "global"),
                               credentials=creds,
                               http_options={"timeout": int(CFG.get("timeout_sec", 300)) * 1000, "headers": {"X-Vertex-AI-LLM-Request-Type": "shared", "X-Vertex-AI-LLM-Shared-Request-Type": "priority"}})
    return _client


def to_gemini(msgs, use_sys):
    from google.genai import types
    sys_parts, i = [], 0
    if use_sys:
        while i < len(msgs) and msgs[i]["role"] == "system" and msgs[i].get("id") != "newchat":
            sys_parts.append(msgs[i]["content"]); i += 1
    contents = []
    for m in msgs[i:]:
        role = "model" if m["role"] == "assistant" else "user"
        if contents and contents[-1]["role"] == role:
            contents[-1]["text"] += "\n\n" + m["content"]
        else:
            contents.append({"role": role, "text": m["content"]})
    if not contents or contents[0]["role"] != "user":
        contents.insert(0, {"role": "user", "text": "[Start a new Chat]"})
    return ("\n\n".join(sys_parts) or None,
            [types.Content(role=c["role"], parts=[types.Part(text=c["text"])]) for c in contents])


def gemini_cfg(msgs, preset):
    from google.genai import types
    sys_inst, contents = to_gemini(msgs, preset.get("use_makersuite_sysprompt", True))
    th = CFG.get("safety_threshold", "OFF")
    cats = ["HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT"]
    cfg = types.GenerateContentConfig(
        system_instruction=sys_inst,
        temperature=preset.get("temperature", 1.0),
        top_p=preset.get("top_p", 0.95) or None,
        top_k=preset.get("top_k") or None,
        max_output_tokens=int(CFG.get("max_tokens") or preset.get("openai_max_tokens") or 4096),
        safety_settings=[types.SafetySetting(category=c, threshold=th) for c in cats],
    )
    return contents, cfg




async def generate(msgs, preset, model):
    contents, cfg = gemini_cfg(msgs, preset)
    r = await client().aio.models.generate_content(model=model, contents=contents, config=cfg)
    texts = []
    for cand in r.candidates or []:
        for part in (cand.content.parts if cand.content and cand.content.parts else []):
            if part.text and not getattr(part, "thought", False):
                texts.append(part.text)
        break
    text = "".join(texts).strip()
    if not text:
        fb = getattr(r, "prompt_feedback", None)
        fr = r.candidates[0].finish_reason if r.candidates else None
        raise RuntimeError(f"空回复 finish_reason={fr} prompt_feedback={fb}")
    return text


# ---------------------------------------------------------------- OpenAI 兼容（中转站）
import httpx

APIS_F = DATA / "apis.json"


def st_api_name(url):
    """酒馆密钥的显示名：地址去掉 https:// 和结尾的 /v1"""
    u = urlparse(url)
    path = u.path.rstrip("/")
    for suf in ("/chat/completions", "/models"):
        if path.endswith(suf): path = path[: -len(suf)]
    return u.netloc + ("" if path in ("", "/v1") else path)


def get_apis():
    apis = [{"name": "vertex", "type": "vertex", "models": MODELS}]
    apis += CFG.get("apis") or []
    apis += rjson(APIS_F, []) or []
    # 酒馆里存的 Custom 密钥（备注 = 中转站地址）直接列出来：在酒馆里加/删密钥，这里自动跟着变
    names = {a["name"] for a in apis}
    for k in st_custom_keys():
        nm, n = st_api_name(k["url"]), 2
        while nm in names:
            nm = f"{st_api_name(k['url'])} ({n})"; n += 1
        names.add(nm)
        apis.append({"name": nm, "type": "openai", "base_url": k["url"], "st_key_id": k["id"],
                     "st_active": k["active"]})
    return apis


def cur_api():
    """酒馆现在用的 API：Vertex，或者 Custom（按地址对上 /api 列表里的那条）"""
    apis = get_apis()
    o = st_oai()
    if o.get("chat_completion_source") == "custom":
        u = (o.get("custom_url") or "").strip()
        if u:
            nu = api_url({"base_url": u})
            same = [a for a in apis if a.get("type") == "openai" and a.get("base_url") and api_url(a) == nu]
            hit = next((a for a in same if a.get("st_active")), None) or (same[0] if same else None)
            if hit:
                return hit
            return {"name": st_api_name(u), "type": "openai", "base_url": u}
    return apis[0]


def api_key(a):
    if a.get("st_key_id") is not None:          # 酒馆里的那条密钥（每次现读，酒馆里改了 key 也跟着变）
        k = next((x for x in st_custom_keys() if x["id"] == a["st_key_id"]), None)
        return k["value"] if k else ""
    return a.get("key") or (st_secret(a["key_from_st"]) if a.get("key_from_st") else "") or ""


def api_url(a):
    u = a["base_url"].rstrip("/")
    for suf in ("/chat/completions", "/models"):
        if u.endswith(suf): u = u[: -len(suf)]
    return u


def cur_model():
    o = st_oai()
    src = o.get("chat_completion_source") or ""
    if src == "custom":
        return o.get("custom_model") or ""
    if src == "vertexai":
        return o.get("vertexai_model") or ""
    return o.get(f"{src}_model") or ""


_model_cache = {}


async def list_models(a):
    if a["type"] == "vertex":
        ms = list(a.get("models") or [])
        try:                                         # 酒馆模型下拉框里的（含 vertex-gemini4 扩展加的）
            for m in await SB.call("vertexModels", timeout=30):
                if m not in ms:
                    ms.append(m)
        except Exception as e:
            log.warning("读酒馆的 Vertex 模型列表失败：%s", e)
        return ms
    if a.get("models_fixed"):
        return list(a.get("models") or [])
    hit = _model_cache.get(a["name"])
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(api_url(a) + "/models", headers={"Authorization": f"Bearer {api_key(a)}"})
        r.raise_for_status()
        ids = sorted({m.get("id") for m in (r.json().get("data") or []) if m.get("id")})
    ids = list(a.get("models") or []) + [i for i in ids if i not in (a.get("models") or [])]
    _model_cache[a["name"]] = (time.time(), ids)
    return ids


def openai_body(msgs, preset, model, stream):
    body = {
        "model": model,
        "messages": [{"role": m["role"], "content": m["content"]} for m in msgs],
        "temperature": preset.get("temperature", 1.0),
        "max_tokens": int(CFG.get("max_tokens") or preset.get("openai_max_tokens") or 4096),
        "stream": stream,
    }
    if preset.get("top_p") not in (None, 1, 1.0): body["top_p"] = preset["top_p"]
    for k_src, k_dst in (("freq_pen", "frequency_penalty"), ("pres_pen", "presence_penalty")):
        if preset.get(k_src): body[k_dst] = preset[k_src]
    return body


def with_reasoning(msg_or_delta, content):
    r = msg_or_delta.get("reasoning_content") or msg_or_delta.get("reasoning") or ""
    return (f"<think>{r}</think>\n" if r else "") + (content or "")


async def generate_openai(a, msgs, preset, model):
    async with httpx.AsyncClient(timeout=int(CFG.get("timeout_sec", 300))) as c:
        r = await c.post(api_url(a) + "/chat/completions", json=openai_body(msgs, preset, model, False),
                         headers={"Authorization": f"Bearer {api_key(a)}"})
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:500]}")
    if "text/event-stream" in r.headers.get("content-type", ""):   # 中转站无视 stream=false 照样流式返回
        parts = []
        for line in r.text.splitlines():
            if line.startswith("data:") and line[5:].strip() not in ("", "[DONE]"):
                try:
                    d = (json.loads(line[5:]).get("choices") or [{}])[0].get("delta") or {}
                except Exception:
                    continue
                parts.append(with_reasoning(d, d.get("content")))
        text = "".join(parts)
        text = re.sub(r"</think>\n<think>", "", text)
        if not text.strip():
            raise RuntimeError("空回复")
        return text.strip()
    j = r.json()
    if j.get("error"):
        raise RuntimeError(str(j["error"])[:500])
    ch = (j.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    if not (msg.get("content") or "").strip():
        raise RuntimeError(f"空回复 finish_reason={ch.get('finish_reason')} {str(j)[:300]}")
    return with_reasoning(msg, msg.get("content")).strip()




async def stream_openai(a, msgs, preset, model, tmo=None):
    tmo = int(tmo or CFG.get("timeout_sec", 300))
    got, in_reason = False, False
    async with httpx.AsyncClient(timeout=httpx.Timeout(tmo, connect=30)) as c:
        async with c.stream("POST", api_url(a) + "/chat/completions", json=openai_body(msgs, preset, model, True),
                            headers={"Authorization": f"Bearer {api_key(a)}"}) as r:
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {(await r.aread()).decode('utf-8', 'replace')[:500]}")
            if "text/event-stream" not in r.headers.get("content-type", ""):
                j = json.loads(await r.aread())  # 中转站没开流式，退回整段
                if j.get("error"): raise RuntimeError(str(j["error"])[:500])
                msg = (j.get("choices") or [{}])[0].get("message") or {}
                if (msg.get("content") or "").strip(): yield with_reasoning(msg, msg.get("content")); return
                raise RuntimeError(f"空回复 {str(j)[:300]}")
            async for line in r.aiter_lines():
                if not line.startswith("data:"): continue
                data = line[5:].strip()
                if data == "[DONE]": break
                try:
                    j = json.loads(data)
                except Exception:
                    continue
                if j.get("error"): raise RuntimeError(str(j["error"])[:500])
                for ch in j.get("choices") or []:
                    d = ch.get("delta") or {}
                    rs = d.get("reasoning_content") or d.get("reasoning")
                    if rs:
                        if not in_reason: in_reason = True; yield "<think>"
                        yield rs
                    t = d.get("content")
                    if t:
                        if in_reason: in_reason = False; yield "</think>\n"
                        got = True; yield t
    if in_reason:
        yield "</think>\n"
    if not got:
        raise RuntimeError("空回复（流式没有收到正文）")




# ---------------------------------------------------------------- state / chats
STATE = rjson(STATE_F, {}) or {}
STATE.setdefault("models", {})
if not STATE.get("preset"):
    STATE["preset"] = st_cur_preset() or (list_presets() or [""])[0]


def save_state(): wjson(STATE_F, STATE)




def load_chat(card):
    """最近一次从页面拉到的这张卡的聊天（同步函数里用；要最新的用 await pull_chat）"""
    return CHAT_CACHE.get(card["file"]) if card else None


async def chat_now(card):
    """先用内存里上次拉到的；bot 刚重启还没拉过，就从酒馆拉一次"""
    c = load_chat(card)
    if c is None and card and not LOCK.locked():
        async with LOCK:
            c = await pull_chat(card)
    return c


def save_chat(card, chat):
    """只存 bot 这边的东西：每条消息对应的 TG 消息、插图（聊天内容本身在酒馆里）"""
    if not card or not chat or not chat.get("id"):
        return
    mp = {}
    for m in (chat.get("messages") or [])[-80:]:
        ent = {k: m[k] for k in ("tg", "imgs", "img_tg", "img_sid") if m.get(k)}
        if ent and m.get("fp"):
            mp[m["fp"]] = ent
    tgmap()[f"{card['file']}|{chat['id']}"] = mp
    save_state()


def meaningful(chat):
    return bool(chat) and any(m["role"] == "user" for m in chat.get("messages") or [])






def fmt_ts(ts):
    if ts > 1e11: ts /= 1000
    return datetime.datetime.fromtimestamp(ts, TZ).strftime("%m-%d %H:%M")




def swipes_of(m):
    sw = m.get("swipes")
    if not sw:
        sw = m["swipes"] = [m["content"]]
    return sw


def set_swipe(m, i):
    sw = swipes_of(m)
    rs = m.setdefault("rs", [])
    rs += [""] * (len(sw) - len(rs))
    m["sid"] = i; m["content"] = sw[i]; m["reasoning"] = rs[i]


def find_by_tg(chat, mid):
    for i, m in enumerate(chat["messages"]):
        if mid in (m.get("tg") or []):
            return i
    return None


# ---------------------------------------------------------------- 排版：转成 TG 的 HTML
# 标签怎么显示，从「只改显示」的美化正则里自动读：正则管哪个标签、正则叫什么名字
HTML_TAGS = {"b", "i", "u", "s", "em", "strong", "br", "p", "div", "span", "font", "small", "big", "sub", "sup",
             "hr", "center", "code", "pre", "a", "img", "ul", "ol", "li", "table", "tr", "td", "th", "tbody",
             "thead", "h1", "h2", "h3", "h4", "h5", "h6", "style", "script", "details", "summary", "blockquote",
             "q", "del", "ins", "mark", "html", "head", "body", "meta", "link", "title", "section", "header",
             "footer", "nav", "article", "aside", "main", "button", "input", "label", "svg", "path"}
THINK_TAGS = {"think", "thinking", "reasoning"}
TAG_NAME = r"[A-Za-z_][\w-]{0,40}|[一-鿿][一-鿿\w]{0,15}"
GEN_OPEN = re.compile(r"```|<\s*(" + TAG_NAME + r")(?:\s[^<>\n]*)?>")
ANY_TAG = re.compile(r"<\s*/?\s*(?:" + TAG_NAME + r")(?:\s[^<>\n]*)?/?\s*>")
STRAY_CLOSE = re.compile(r"<\s*/\s*(?:" + TAG_NAME + r")\s*>")
FIND_TAG = re.compile(r"<\\?/?\s*(" + TAG_NAME + r")")


def _label(name, tag):
    nm = re.split(r"[：:]", name or "")[-1].strip()
    nm = re.sub(r"[（(][^）)]*[）)]", "", nm)
    nm = re.sub(r"^\s*(?:\[[^\]]{0,12}\]|【[^】]{0,12}】)\s*", "", nm)    # 「[🦋美化]梦境选项框」-> 梦境选项框
    nm = re.sub(r"^(显示|美化)\s*|\s*(美化|显示)$", "", nm).strip()
    return (nm or tag)[:16]


def tag_rules(card=None, preset=None):
    """标签名(小写) -> (动作, 标题)。动作：fold 折叠 / quote 引用 / options 选项 / unwrap 去标签 / hide 隐藏"""
    r = {t: ("fold", "💭 思考") for t in THINK_TAGS}
    r.update({"content": ("unwrap", ""), "正文": ("unwrap", ""), "image": ("unwrap", ""), "imgthink": ("hide", "")})
    r["tgfold"] = ("fold", "")                # 美化翻译出来的块，标题已经在块的第一行
    for s in regex_scripts(card, preset):
        if not s.get("markdownOnly") or P_AI not in (s.get("placement") or []):
            continue
        if not HTML_LIKE.search(s.get("replaceString") or ""):
            continue                      # 不产生 HTML 的显示正则照常执行，不用管
        m = FIND_TAG.search(s.get("findRegex") or "")
        if not m:
            continue
        tag = m.group(1).lower()
        if tag in HTML_TAGS or tag in THINK_TAGS or tag in ("content", "正文"):
            continue
        lb = _label(s.get("scriptName"), m.group(1))
        key = (lb + " " + tag).lower()
        if "选项" in key or "option" in key:
            r[tag] = ("options", "🎯 " + lb)
        elif "内心" in key or "心声" in key or "mind" in key:
            r[tag] = ("quote", "💭 " + lb)
        elif any(w in key for w in ("总结", "记事", "摘要", "summary")):
            r[tag] = ("fold", "📝 " + lb)
        else:
            r[tag] = ("fold", "📌 " + lb)
    for t in CFG.get("fold_tags") or []: r[t.lower()] = ("fold", "📌 " + t)
    for t in CFG.get("unwrap_tags") or []: r[t.lower()] = ("unwrap", "")
    for t in CFG.get("hide_tags") or []: r[t.lower()] = ("hide", "")
    return r


def _next_block(t, start, rules, self_tag):
    """没闭合的块到哪结束：下一个已知块开始的地方（跟酒馆正则里的「或者遇到下一个标签」一样）"""
    for m in GEN_OPEN.finditer(t, start):
        if m.group(0) == "```":
            continue
        low = m.group(1).lower()
        if low != self_tag and low in rules:
            return m.start(), True
    return len(t), False


def tokenize(t, rules=None):
    """切成 文本 / 代码块 / 折叠块等。每段 (kind, body, extra, closed)"""
    rules = rules if rules is not None else tag_rules()
    segs, i, n = [], 0, len(t)
    while i < n:
        m = GEN_OPEN.search(t, i)
        if not m:
            segs.append(("text", t[i:], None, True)); break
        if m.start() > i:
            segs.append(("text", t[i:m.start()], None, True))
        if m.group(0) == "```":
            nl = t.find("\n", m.end())
            lang, bs = (t[m.end():], n) if nl == -1 else (t[m.end():nl], nl + 1)
            lang = re.sub(r"[^\w+#-]", "", lang)[:20]
            e = t.find("```", bs)
            if e == -1:
                segs.append(("code", t[bs:], lang, False)); i = n
            else:
                segs.append(("code", t[bs:e], lang, True)); i = e + 3
            continue
        tag = m.group(1); low = tag.lower()
        act = rules.get(low)
        if act is None and low in HTML_TAGS:
            if low == "br": segs.append(("text", "\n", None, True))
            i = m.end(); continue
        cm = re.compile(r"<\s*/\s*" + re.escape(tag) + r"\s*>", re.I).search(t, m.end())
        if act is None:
            if not cm:
                i = m.end(); continue         # 不认识又没闭合的标签：只去掉这个标记
            act = ("fold", "📌 " + tag)
        kind, label = act
        if kind == "unwrap":
            i = m.end(); continue
        if cm:
            end, nxt, closed = cm.start(), cm.end(), True
        else:
            end, closed = _next_block(t, m.end(), rules, low); nxt = end
        if kind != "hide":
            segs.append((kind, t[m.end():end], (tag, label), closed))
        i = nxt
    return segs


def inline_html(s):
    s = html.escape(s, quote=False)
    codes = []

    def keep(m):
        codes.append(m.group(1)); return f"\x00{len(codes) - 1}\x00"
    s = re.sub(r"`([^`\n]+)`", keep, s)
    s = re.sub(r"\*\*(?![\s*])([^*\n]+?)(?<!\s)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<!\*)\*(?![\s*])([^*\n]+?)(?<![\s*])\*(?!\*)", r"<i>\1</i>", s)
    s = re.sub(r"~~(?![\s~])([^~\n]+?)~~", r"<s>\1</s>", s)
    return re.sub(r"\x00(\d+)\x00", lambda m: "<code>" + codes[int(m.group(1))] + "</code>", s)


def md_quotes(h):
    """Markdown 的「> 开头」行 -> TG 引用块（酒馆里显示成引用，bot 以前原样露出 >）"""
    out, buf = [], []
    for ln in h.split("\n"):
        mq = re.match(r"^\s*&gt;\s?(.*)$", ln)
        if mq:
            buf.append(mq.group(1)); continue
        if buf:
            out.append("<blockquote>" + "\n".join(buf) + "</blockquote>"); buf = []
        out.append(ln)
    if buf:
        out.append("<blockquote>" + "\n".join(buf) + "</blockquote>")
    return "\n".join(out)


def clean_inner(body):
    """块里面的内容：去掉嵌套的标签标记和 ``` 行"""
    body = ANY_TAG.sub("", body)
    body = re.sub(r"^\s*```[\w+#-]*\s*$", "", body, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def parse_options(body):
    out = []
    for ln in clean_inner(body).split("\n"):
        ln = ln.strip()
        ln = re.sub(r"^(?:[-*•·>]+|\d{1,2}\s*[.、)）:：]|[A-Ha-h]\s*[.、)）:：]|[①-⑩]|选项\s*\d+\s*[.、:：]?)\s*", "", ln).strip()
        if not ln or (ln.endswith(("：", ":")) and len(ln) < 16):
            continue
        out.append(ln)
    return out[:8]


def render_seg(kind, body, extra, closed, cont=False):
    """一段 -> TG HTML。cont=True 表示是被切到下一条消息里的后半段"""
    if kind == "text":
        return md_quotes(inline_html(STRAY_CLOSE.sub("", body)))
    if kind == "code":
        b = html.escape(body.strip("\n"), quote=False)
        if not b.strip():
            return ""
        return f'<pre><code class="language-{extra}">{b}</code></pre>' if extra else f"<pre>{b}</pre>"
    tag, label = extra
    more = "" if closed else "…"
    if kind == "options":
        opts = parse_options(body)
        inner = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(opts)) if opts else clean_inner(body)
    else:
        inner = clean_inner(body)
    if not inner and closed and not cont:
        return ""                             # 空块不显示
    inner = ("\n" + inline_html(inner)) if inner else ""
    bq = "<blockquote expandable>" if kind == "fold" else "<blockquote>"
    head = label + ("（续）" if cont else "") + more
    if not head:
        return f"{bq}{inner.lstrip(chr(10))}</blockquote>"
    return f"{bq}<b>{html.escape(head, quote=False)}</b>{inner}</blockquote>"


def split_raw(t, limit=3500, rules=None, carry_tags=True):
    parts = []
    while len(t) > limit:
        cut = t.rfind("\n\n", 0, limit)
        if cut < limit // 2: cut = t.rfind("\n", 0, limit)
        if cut < limit // 2: cut = limit
        parts.append(t[:cut]); t = t[cut:].lstrip("\n")
    parts.append(t)
    if not carry_tags:
        return parts
    fixed, carry = [], ""
    for k, p in enumerate(parts):
        p = carry + p; carry = ""
        if k < len(parts) - 1:   # 被切开的代码块/折叠块：这块补上结尾，下块补上开头
            segs = tokenize(p, rules)
            if segs and not segs[-1][3]:
                kind, _, extra, _ = segs[-1]
                if kind == "code":
                    p += "\n```"; carry = "```" + (extra or "") + "\n"
                elif kind in ("fold", "quote", "options"):
                    p += f"</{extra[0]}>"; carry = f"<{extra[0]}>"
        fixed.append(p)
    return fixed


CHUNK = 3500        # 一条 TG 消息放多少字（上限 4096，留点余量给标题）


def _cut(body, n):
    """在 n 字以内找个好切点（空行 > 换行），找不到返回 None"""
    for sep in ("\n\n", "\n"):
        c = body.rfind(sep, 0, n)
        if c >= n * 0.3:
            return c
    return None


def render_chunks(t, rules=None):
    """原文 -> [(html, 纯文本备用)]，每块都在 TG 长度限制内。
    先对整段切好块（标签配对看的是全文），再按长度分到几条消息里；长的块从中间切开，后半段标「（续）」"""
    rules = rules if rules is not None else tag_rules()
    t = re.sub(r"\n{3,}", "\n\n", t or "").strip()
    msgs, cur, room = [], [], CHUNK

    def flush():
        nonlocal cur, room
        if cur:
            msgs.append(cur)
        cur, room = [], CHUNK

    for kind, body, extra, closed in tokenize(t, rules):
        cont = False
        while True:
            over = 0 if kind == "text" else 30
            if len(body) + over <= room:
                cur.append((kind, body, extra, closed, cont)); room -= len(body) + over
                break
            c = _cut(body, room - over) if room - over > 300 else None
            if c is None:
                if cur:
                    flush(); continue         # 这条放不下了，整段挪到下一条
                c = room - over               # 一整条都放不下又没有换行：硬切
            cur.append((kind, body[:c], extra, True, cont))
            flush()
            body, cont = body[c:].lstrip("\n"), True
    flush()
    out = []
    for segs in msgs:
        h = re.sub(r"\n{3,}", "\n\n", "".join(render_seg(*x) for x in segs)).strip()
        raw = "".join(x[1] for x in segs).strip()
        if h or raw.strip():
            out.append((h or html.escape(ANY_TAG.sub("", raw)) or "（空）", raw))
    return out or [("（空）", "（空）")]


def message_options(card, m, preset=None):
    """这条回复里的行动选项（给按钮用）"""
    if m["role"] != "assistant":
        return []
    rules = tag_rules(card, preset)
    if not any(a[0] == "options" for a in rules.values()):
        return []
    opts = []
    for kind, body, _, _ in tokenize(m.get("content") or "", rules):
        if kind == "options":
            opts = parse_options(body)
    return opts


MVU_BLOCK = re.compile(r"<\s*UpdateVariable\s*>([\s\S]*?)<\s*/\s*UpdateVariable\s*>", re.I)
MVU_CALL = re.compile(r"_\.(set|add|insert|assign|remove|delete|unset)\s*\(([\s\S]*?)\)\s*;?[ \t]*(?://[^\n]*)?$", re.M)
MVU_PH = re.compile(r"\x00MV(\d+)\x00")


def _js_args(src):
    """'a', 'b, c', 3 -> ['a', 'b, c', '3']（按顶层逗号切，去掉引号）"""
    out, cur, q, depth, i = [], "", None, 0, 0
    while i < len(src):
        ch = src[i]
        if q:
            if ch == "\\" and i + 1 < len(src):
                cur += src[i + 1]; i += 2; continue
            if ch == q:
                q = None
            else:
                cur += ch
        elif ch in "'\"`":
            q = ch
        elif ch in "[{(":
            depth += 1; cur += ch
        elif ch in "]})":
            depth -= 1; cur += ch
        elif ch == "," and depth == 0:
            out.append(cur.strip()); cur = ""
        else:
            cur += ch
        i += 1
    if cur.strip():
        out.append(cur.strip())
    return out


def mvu_lines(body):
    """MVU 的变量更新命令 -> 「路径：新值」行（酒馆里这些是状态栏面板从变量里读出来显示的，bot 没有变量，直接列出来）"""
    lines = []
    for m in MVU_CALL.finditer(body):
        op, args = m.group(1).lower(), _js_args(m.group(2))
        if not args:
            continue
        path = re.sub(r"\[0\]$", "", args[0]).strip()
        val = args[-1] if len(args) > 1 else ""
        if op == "set":
            lines.append(f"{path}：{val}")
        elif op == "add":
            lines.append(f"{path}：{'' if val.startswith(('-', '+')) else '+'}{val}")
        elif op in ("insert", "assign"):
            lines.append(f"{path}：+ {val}")
        else:
            lines.append(f"{path}：删除 {val}".rstrip())
    return lines


def _mvu_path(p):
    p = re.sub(r"\[(['\"])(.*?)\1\]", r".\2", p.strip())
    p = re.sub(r"\[\d+\]$", "", p)
    return re.sub(r"^stat_data\.", "", p.strip(". "))


_mvu_ops_cache = collections.OrderedDict()


def mvu_ops(text):
    """一条回复里的变量操作 [(op, 路径, 值)]（带缓存，回放整段聊天时用）"""
    k = hashlib.md5((text or "").encode("utf-8", "ignore")).hexdigest()
    if k in _mvu_ops_cache:
        return _mvu_ops_cache[k]
    ops = []
    for b in MVU_BLOCK.finditer(text or ""):
        for m in MVU_CALL.finditer(b.group(1)):
            args = _js_args(m.group(2))
            if args:
                ops.append((m.group(1).lower(), _mvu_path(args[0]), args[-1] if len(args) > 1 else ""))
    _mvu_ops_cache[k] = ops
    if len(_mvu_ops_cache) > 2000:
        _mvu_ops_cache.popitem(last=False)
    return ops


def _mvu_flat(obj, pre="", out=None):
    """初始变量 -> {路径: 值}；MVU 的 [值, "说明"] 只取值，$ 开头的元信息跳过"""
    out = {} if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not str(k).startswith("$"):
                _mvu_flat(v, f"{pre}.{k}" if pre else str(k), out)
    elif isinstance(obj, list) and len(obj) == 2 and not isinstance(obj[0], (dict, list)) and isinstance(obj[1], str):
        out[pre] = obj[0]
    elif pre:
        out[pre] = obj if not isinstance(obj, (dict, list)) else json.dumps(obj, ensure_ascii=False)
    return out


_mvu_init_cache = {}


def mvu_init(card):
    """卡的初始变量：绑定世界书里注释带 initvar 的条目（JSON 或 YAML，禁用的也算）"""
    try:
        _, paths = card_book_paths(card)
        sig = (card.get("file"), tuple((str(p), p.stat().st_mtime_ns) for p in paths if p.exists()))
    except Exception:
        return {}
    if _mvu_init_cache.get(card.get("file"), (None,))[0] == sig:
        return _mvu_init_cache[card["file"]][1]
    ents = []
    for p in paths:
        try:
            ents += load_book(p)
        except Exception:
            pass
    if card.get("character_book"):
        try:
            ents += convert_char_book(card["character_book"], card["name"])
        except Exception:
            pass
    init = {}
    for e in ents:
        cm = (e.get("comment") or "").lower().replace(" ", "")
        named = any(w in cm for w in ("initvar", "init_var", "初始变量", "变量初始", "初始化变量", "变量初始化"))
        if not named and "stat_data" not in (e.get("content") or "")[:200]:
            continue
        data = _parse_vars(e.get("content") or "")
        if isinstance(data, dict):
            _mvu_flat(data.get("stat_data", data), "", init)
    _mvu_init_cache[card["file"]] = (sig, init)
    return init


def _num(v):
    try:
        f = float(str(v).strip())
        return int(f) if f.is_integer() else f
    except Exception:
        return None


def mvu_apply(state, ops):
    for op, path, val in ops:
        if op == "set":
            state[path] = val
        elif op == "add":
            a, b = _num(state.get(path, 0)), _num(val)
            state[path] = (a + b) if a is not None and b is not None else val
        elif op in ("insert", "assign"):
            old = str(state.get(path, "") or "")
            state[path] = f"{old}、{val}" if old else val
        else:
            if val and path in state and str(val) in str(state[path]):
                state[path] = str(state[path]).replace(str(val), "").strip("、, ")
            else:
                state.pop(path, None)
    return state


INITVAR_BLOCK = re.compile(r"<\s*init_?var\s*>([\s\S]*?)<\s*/\s*init_?var\s*>", re.I)


def _parse_vars(src):
    src = re.sub(r"^\s*```\w*\s*$", "", src or "", flags=re.M).strip()
    try:
        return json.loads(src)
    except Exception:
        try:
            import yaml
            return yaml.safe_load(src)
        except Exception:
            return None


def greeting_raw(card, m):
    """开场白的原文（存进聊天时宏已经执行掉了，{{setvar}} 只能从卡里重新拿）"""
    greets = [g for g in [card.get("first_mes") or ""] + list(card.get("alternate_greetings") or []) if g.strip()]
    sid = m.get("sid") or 0
    return greets[sid] if 0 <= sid < len(greets) else (greets[0] if greets else "")


def mvu_state(card, chat, idx):
    """回放第 0 条到第 idx 条消息里的变量更新，得到这条消息时的完整状态，和这条改了哪些"""
    state = dict(mvu_init(card))
    changed = set()
    msgs = (chat or {}).get("messages") or []
    if msgs and msgs[0].get("greeting"):        # 开场白里的 <initvar> 初始变量块
        for b in INITVAR_BLOCK.finditer((msgs[0].get("content") or "") + "\n" + greeting_raw(card, msgs[0])):
            d = _parse_vars(b.group(1))
            if isinstance(d, dict):
                _mvu_flat(d.get("stat_data", d), "", state)
    for j, mm in enumerate(msgs[:idx + 1]):
        if mm.get("role") != "assistant":
            continue
        ops = mvu_ops(mm.get("content") or "")
        if j == idx:
            changed = {p for _, p, _ in ops}
        mvu_apply(state, ops)
    return state, changed


def mvu_tree(state, changed):
    """扁平状态 -> 酒馆助手消息变量的样子：{stat_data: {...}, display_data: {...}, delta_data: {本轮改的}}"""
    def nest(items):
        root = {}
        for path, v in items:
            ks = _var_path(path)
            cur = root
            for k in ks[:-1]:
                if not isinstance(cur.get(k), dict):
                    cur[k] = {}
                cur = cur[k]
            if ks:
                cur[ks[-1]] = v
        return root
    full = nest(state.items())
    return {"stat_data": full, "display_data": full, "delta_data": nest((k, v) for k, v in state.items() if k in changed)}


def mvu_panel(state, changed):
    """完整状态 -> 折叠块；按第一级分组，这一轮变了的标 🔸"""
    groups = collections.OrderedDict()
    for path, v in state.items():
        head, _, rest = path.partition(".")
        groups.setdefault(head if rest else "", []).append((rest or head, path, v))
    lines = []
    for head, items in groups.items():
        if head:
            lines.append(f"**{head}**")
        for name, path, v in items:
            mark = "🔸" if path in changed else "• "
            lines.append(f"{mark}{name}：{v}")
    title = "📊 状态" + ("（🔸 本轮更新）" if changed else "")
    return f"\n<tgfold>**{title}**\n" + "\n".join(lines) + "</tgfold>\n"


VAR_MACRO = re.compile(r"\{\{\s*(?:set|add|inc|dec)(?:global)?var\s*::", re.I)


def var_state(card, chat, idx, preset=None):
    """{{setvar}} 一派的卡：把会设变量的正则在第 0..idx 条消息上重放一遍，得到这条消息时的局部变量；
    返回 (变量, 这一条改了哪些)。酒馆里这些是显示正则顺手执行的宏，bot 以前不留结果"""
    sc = [s for s in regex_scripts(card, preset) if VAR_MACRO.search(s.get("replaceString") or "")]
    msgs0 = (chat or {}).get("messages") or []
    if not sc and not (msgs0 and msgs0[0].get("greeting") and VAR_MACRO.search(greeting_raw(card, msgs0[0]))):
        return {}, set()
    mc = {"char": card["name"], "user": user_name(st_settings()), "vars": {}, "gvars": {}}
    before = {}
    msgs = (chat or {}).get("messages") or []
    for j, mm in enumerate(msgs[:idx + 1]):
        if j == 0 and mm.get("greeting"):
            macro(greeting_raw(card, mm), mc)   # 开场白里的 {{setvar}}（存的时候已经执行掉了，从卡里重放）
        if j == idx:
            before = dict(mc["vars"])
        pl = P_AI if mm.get("role") == "assistant" else P_USER
        t = mm.get("content") or ""
        for s in sc:
            if pl not in (s.get("placement") or []):
                continue
            try:
                t = run_script(s, t, mc)
            except Exception:
                pass
    v = dict(mc["vars"])
    for k, val in mc["gvars"].items():          # 全局变量（setglobalvar）放「全局」组
        v["全局." + k] = val
    return v, {k for k in mc["vars"] if before.get(k) != mc["vars"][k]}


def _strip_common_head(state):
    """「变量.清溪.时间」这类都带同一个前缀的，去掉前缀再分组"""
    heads = {k.split(".", 1)[0] for k in state if "." in k}
    if len(heads) == 1 and all("." in k for k in state):
        h = heads.pop() + "."
        return {k[len(h):]: v for k, v in state.items()}, h
    return state, ""


def display_text(card, m, preset=None, streaming=False, depth=0, chat=None, idx=None):
    """先把 MVU 变量更新块拿出来（很多卡的显示正则会把它删掉，交给面板读变量显示），再跑正则，最后作为「状态变化」折叠块放回去"""
    kept = []
    content = m.get("content") or ""
    if m["role"] == "assistant":
        def hold(mm):
            ls = mvu_lines(mm.group(1))
            if not ls:
                return mm.group(0)
            kept.append(ls); return f"\x00MV{len(kept) - 1}\x00"
        content = MVU_BLOCK.sub(hold, content)
    vstate, vchanged = {}, set()
    if chat is not None and idx is not None:
        try:
            vstate, vchanged = var_state(card, chat, idx, preset)
        except Exception as e:
            log.warning("var state fail: %s", e)
    # 显示正则里的 {{getvar}}（变量状态栏模板）用重放出来的变量填
    vtree = {}
    if chat is not None and idx is not None:
        try:
            st0, ch0 = mvu_state(card, chat, idx)
            vtree = mvu_tree(st0, ch0) if st0 else {}
        except Exception:
            pass
    body = _display_text(card, dict(m, content=content), preset, streaming, depth,
                         vars_ctx={"vars": vstate, "mvu_tree": vtree} if (vstate or vtree) else None)
    if chat is not None and idx is not None and m["role"] == "assistant":
        try:
            state, changed = mvu_state(card, chat, idx)
        except Exception as e:
            log.warning("mvu state fail: %s", e); state, changed = {}, set()
        if vstate:
            vs, h = _strip_common_head(vstate)
            shown = re.sub(r"\s+", "", re.sub(r"<[^<>]*>", "", body))
            for k, v in vs.items():
                sv = re.sub(r"\s+", "", str(v))
                if len(sv) >= 2 and sv in shown and h + k not in vchanged:
                    continue                    # 卡自带的状态栏已经显示了这个值，不重复（这一轮改过的照样列出来）
                if k not in state:
                    state[k] = v
                    if h + k in vchanged:
                        changed.add(k)
        if state:                      # 这张卡用变量：显示继承下来的完整状态，代替只有本轮变化的块
            panel = mvu_panel(state, changed)
            return MVU_PH.sub("", body).rstrip() + "\n" + panel
    if not kept:
        return body
    used = set()

    def put(mm):
        i = int(mm.group(1)); used.add(i)
        return "\n<tgfold>**📊 状态变化**\n" + "\n".join(kept[i]) + "</tgfold>\n" if i < len(kept) else ""
    body = MVU_PH.sub(put, body)
    for i, ls in enumerate(kept):      # 被别的正则连带删掉的，补在最后，不吞
        if i not in used:
            body += "\n<tgfold>**📊 状态变化**\n" + "\n".join(ls) + "</tgfold>"
    return body


IMG_RAW = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']?((?:https?:)?//[^\"'\s>]+)[^>]*>", re.I)


def _display_text(card, m, preset=None, streaming=False, depth=0, vars_ctx=None):
    """一条消息在 TG 里显示的内容：思考过程（折叠）+ 正文，套上只改显示的正则，画图标记去掉"""
    body = apply_regex(m.get("content") or "", card, P_AI if m["role"] == "assistant" else P_USER,
                       "display", depth=depth, preset=preset, chat=vars_ctx)
    if m["role"] == "assistant" and c8():
        body = strip_image_tags(body, streaming)
    body = re.sub(r"<!--[\s\S]*?(?:-->|$)", "", body)       # HTML 注释在酒馆里本来就不显示
    body = IMG_RAW.sub(lambda mm: f"\n🖼 {mm.group(1)}\n", body)   # 正文/拼起来的美化里的图片：TG 显示不了，留链接
    r = m.get("reasoning") or ""
    if r:
        r = apply_regex(r, card, P_REASON, "display", depth=depth, preset=preset)
        body = f"<think>{r}</think>\n{body}"
    return body


# ---------------------------------------------------------------- telegram 发送/同步
def allowed(update: Update):
    u = update.effective_user
    return u and (not ALLOWED or u.id in ALLOWED)


STOP_KB = InlineKeyboardMarkup([[Btn("⏹ 停止", callback_data="stop")]])


def kb_sig(k):
    return None if k is None else tuple((b.text, b.callback_data, b.web_app.url if b.web_app else None)
                                        for row in k.inline_keyboard for b in row)


async def tg_call(fn, *a, **k):
    for _ in range(4):
        try:
            return await fn(*a, **k)
        except RetryAfter as e:
            ra = e.retry_after                       # 新版是 timedelta，旧版是秒数
            await asyncio.sleep((ra.total_seconds() if hasattr(ra, "total_seconds") else float(ra)) + 0.5)
    return await fn(*a, **k)


async def put_msg(bot, chat_id, mid, html_text, raw, kb):
    """mid=None 发新消息，否则编辑；HTML 解析失败就退回纯文本。返回 message_id"""
    beat()
    use_html = True
    for _ in range(4):
        txt, pm = (html_text, "HTML") if use_html else ((raw or "（空）")[:4096], None)
        try:
            if mid is None:
                m = await tg_call(bot.send_message, chat_id=chat_id, text=txt, parse_mode=pm, reply_markup=kb)
                return m.message_id
            await tg_call(bot.edit_message_text, text=txt, chat_id=chat_id, message_id=mid, parse_mode=pm,
                          reply_markup=kb)
            return mid
        except BadRequest as e:
            s = str(e).lower()
            if "not modified" in s:
                return mid
            if use_html and ("parse" in s or "entit" in s or "tag" in s or "too long" in s):
                log.warning("HTML 显示失败，改用纯文本：%s", e); use_html = False; continue
            if mid is not None and ("not found" in s or "can't be edited" in s or "to edit" in s):
                mid = None; continue
            raise
    raise RuntimeError("消息发送失败")


async def del_msgs(bot, chat_id, ids):
    for mid in ids or []:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass


async def sync_msgs(bot, chat_id, ids, chunks, kb, cache=None):
    """把 chunks 显示到 ids 这几条消息上：改已有的、补发新的、删掉多余的。返回新的 ids。
    cache={"shown": [...]} 时跳过没变的块（流式用）"""
    ids = list(ids or [])
    shown = cache["shown"] if cache is not None else []
    out, last = [], len(chunks) - 1
    for i, (h, raw) in enumerate(chunks):
        k = kb if i == last else None
        mid = ids[i] if i < len(ids) else None
        sig = (h, kb_sig(k))
        if mid is not None and i < len(shown) and shown[i] == sig:
            out.append(mid); continue
        mid = await put_msg(bot, chat_id, mid, h, raw, k)
        out.append(mid)
        if i < len(shown): shown[i] = sig
        else: shown.append(sig)
    await del_msgs(bot, chat_id, ids[len(chunks):])
    del shown[len(chunks):]
    return out


def msg_kb(m):
    kb = _msg_kb(m)
    rows = list(kb.inline_keyboard) if kb else []
    if m["role"] == "assistant":
        opts = message_options(cur_card(), m, cur_preset())
        if opts:                      # 行动选项：点数字就当作你选了它
            btns = [Btn(f"选 {i + 1}", callback_data=f"opt:{i}") for i in range(len(opts))]
            rows = [btns[k:k + 4] for k in range(0, len(btns), 4)] + rows
    if m["role"] == "assistant" and STATE.get("img_on", True) and c8().get("mode") == "comfyui" \
            and image_prompts(m.get("content") or ""):
        has = bool((m.get("imgs") or {}).get(str(m.get("sid", 0))))
        rows.append([Btn("🖼 重新出图" if has else "🖼 出图", callback_data="img")])
    return InlineKeyboardMarkup(rows) if rows else None


def _msg_kb(m):
    sw = swipes_of(m); n, i = len(sw), m.get("sid", 0)
    if m.get("greeting"):
        if n <= 1: return None
        return InlineKeyboardMarkup([[Btn("◀", callback_data="sw:-1"), Btn(f"开场白 {i + 1}/{n}", callback_data="noop"),
                                      Btn("▶", callback_data="sw:1")]])
    if n <= 1:
        return InlineKeyboardMarkup([[Btn("🔄 重新生成", callback_data="regen"), Btn("⏩ 继续", callback_data="cont")]])
    return InlineKeyboardMarkup([[Btn("◀", callback_data="sw:-1"), Btn(f"{i + 1}/{n}", callback_data="noop"),
                                  Btn("🔄" if i == n - 1 else "▶", callback_data="sw:1"), Btn("⏩", callback_data="cont")]])


async def show_msg(bot, chat_id, card, chat, idx, new=False):
    """把第 idx 条消息显示出来（最后一条才带按钮）。new=True 时发成新消息"""
    m = chat["messages"][idx]
    kb = kb_for(card, m) if idx == len(chat["messages"]) - 1 and m["role"] == "assistant" else None
    ids = [] if new else m.get("tg")
    pr = cur_preset()
    chunks = render_chunks(display_text(card, m, pr, depth=len(chat["messages"]) - 1 - idx, chat=chat, idx=idx),
                           tag_rules(card, pr))
    if m.get("pro"):                  # 主动消息：开头标上是谁发来的
        tag = "（你好久没理 TA，从头开始了）" if m.get("pro_reset") else ""
        chunks[0] = (f"<b>💌 {html.escape(card['name'], quote=False)}</b>{tag}\n" + chunks[0][0], f"💌 {card['name']}{tag}\n" + chunks[0][1])
    m["tg"] = await sync_msgs(bot, chat_id, ids, chunks, kb)
    await sync_photos(bot, chat_id, m, new)
    save_chat(card, chat)


def kb_for(card, m):
    """主动消息只给一个「切过去」按钮；当前角色的回复用平常的按钮"""
    if m.get("pro") and card:
        return pro_kb(card)
    if not card or card["file"] == STATE.get("char"):
        return msg_kb(m)
    return None


async def drop_kb(bot, chat_id, m):
    if m and m.get("tg"):
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=m["tg"][-1], reply_markup=None)
        except Exception:
            pass


async def refresh_last_kb(bot, chat_id, chat, card=None):
    """最后一条是 AI 回复时，把按钮补回去"""
    if chat["messages"] and chat["messages"][-1]["role"] == "assistant":
        m = chat["messages"][-1]
        if m.get("tg"):
            try:
                await bot.edit_message_reply_markup(chat_id=chat_id, message_id=m["tg"][-1], reply_markup=kb_for(card, m))
            except Exception:
                pass


async def send_plain(bot, chat_id, text, kb=None):
    for i in range(0, max(len(text), 1), 4000):
        last = i + 4000 >= len(text)
        await tg_call(bot.send_message, chat_id=chat_id, text=text[i:i + 4000] or "（空）",
                      reply_markup=kb if last else None)


async def send_pre(bot, chat_id, text):
    """原文放进代码块，点一下就能复制"""
    for p in split_raw(text or "（空）", 3500, carry_tags=False):
        await put_msg(bot, chat_id, None, "<pre>" + html.escape(p, quote=False) + "</pre>", p, None)


def cur_card():
    return get_card(STATE.get("char"))


def cmd_arg(update):
    t = update.effective_message.text or ""
    parts = t.split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def strip_name(card, s):
    return re.sub(r"^\s*" + re.escape(card["name"]) + r"\s*[:：]\s*", "", s)


# ---------------------------------------------------------------- 生成
LOCK = asyncio.Lock()


async def typing_loop(bot, chat_id):
    try:
        while True:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass
    except Exception:
        pass










# ---------------------------------------------------------------- 自动摘要（前情提要）








# ---------------------------------------------------------------- 插图（照 st-chatu8 的流程；配置直接读酒馆 settings.json 里的 chatu8 设置）
SIZE_RX = re.compile(r",?\s*(\d{2,4})x(\d{2,4})(?=[;\s]|$)", re.I)
C8_CLIENT = "533ef3a3-39c0-4e39-9ced-37d290f371f8"
IMG_BUSY = set()
IMG_EPOCH = [0]          # 每来一条新指令加 1；出图循环发现变了就停


def img_rounds():
    try:
        return max(1, min(5, int(STATE.get("img_rounds", CFG.get("img_rounds", 2)) or 2)))
    except Exception:
        return 2


def bump_img():
    IMG_EPOCH[0] += 1


async def comfy_unqueue(pids):
    """把还在 ComfyUI 队列里没开始的任务删掉（已经在画的那张让它画完）"""
    if not pids:
        return
    try:
        async with httpx.AsyncClient(timeout=15) as h:
            await h.post(comfy_url() + "/queue", json={"delete": pids})
    except Exception as e:
        log.warning("comfy unqueue fail: %s", e)


def c8():
    return (st_settings().get("extension_settings") or {}).get("st-chatu8") or {}


def c8_tags():
    c = c8()
    return c.get("startTag") or "image###", c.get("endTag") or "###"


def c8_fix(text):
    """chatu8 的换行修正"""
    t = (text or "").replace("\n###", "###")
    return re.sub(r"<image>\n*###\n*", "<image>image###", t)


def image_prompts(text):
    s, e = c8_tags()
    rx = re.compile(re.escape(s) + r"([\s\S]*?)" + re.escape(e))
    return [m.group(1).strip() for m in rx.finditer(c8_fix(text)) if m.group(1).strip()]


def strip_image_tags(text, streaming=False):
    s, e = c8_tags()
    t = re.sub(re.escape(s) + r"[\s\S]*?" + re.escape(e), "", c8_fix(text))
    if streaming:
        t = re.sub(re.escape(s) + r"[\s\S]*$", "", t)
    return re.sub(r"<image>\s*</image>", "", t)


def strip_cn(text):
    """去掉全角括号里的中文注释（chatu8 的 stripChineseAnnotations）"""
    text = text or ""
    while True:
        new = re.sub(r"（[^（）]*）", "", text)
        if new == text: return new
        text = new


def _base_tag(tag):
    b = tag.lower()
    while True:
        p = b
        b = re.sub(r"^[(\[{<]+", "", b); b = re.sub(r"[)\]}>]+$", "", b)
        b = re.sub(r"^[\d.]+::", "", b); b = re.sub(r"::$", "", b)
        b = re.sub(r":(?:\d*\.\d+|\d+)$", "", b).strip()
        if b == p: return b


def dedup_tags(s):
    if not s: return ""
    ph = []
    s = re.sub(r"\$([^$]+)\$", lambda m: (ph.append(m.group(0)), f"__J{len(ph) - 1}__")[1], s)
    seen = {}
    for t in [x.strip() for x in s.split(",") if x.strip()]:
        t = re.sub(r"__J(\d+)__", lambda m: ph[int(m.group(1))], t)
        b = _base_tag(t)
        if b not in seen or (seen[b].lower() == _base_tag(seen[b]) and t.lower() != b):
            seen[b] = t
    return ", ".join(seen.values())


def _js_rx(pat):
    try:
        return re.compile(re.sub(r"\(\?<([A-Za-z_]\w*)>", r"(?P<\1>", pat), re.I)
    except re.error:
        try:
            return re2.compile(pat, re2.I | re2.V0) if re2 else re.compile(re.escape(pat), re.I)
        except Exception:
            return re.compile(re.escape(pat), re.I)


def _js_expand(rep, m):
    def g(mm):
        k = mm.group(1)
        if k == "$": return "$"
        if k == "&": return m.group(0)
        try:
            return (m.group(int(k)) if k.isdigit() else m.group(k[1:-1])) or ""
        except Exception:
            return mm.group(0)
    return re.sub(r"\$(\$|&|\d{1,2}|<[^>]+>)", g, rep)


def _split_rule(line):
    t = (line or "").strip()
    if not t: return None
    if re.match(r"re\s*\(", t, re.I):
        depth, inb, end, i = 0, False, -1, 0
        while i < len(t):
            ch = t[i]
            if ch == "\\": i += 2; continue
            if inb:
                if ch == "]": inb = False
            elif ch == "[": inb = True
            elif ch == "(": depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0: end = i; break
            i += 1
        if end != -1:
            eq = t.find("=", end + 1)
            if eq != -1: return t[:eq].strip(), t[eq + 1:].strip()
    eq = t.find("=")
    return (t[:eq].strip(), t[eq + 1:].strip()) if eq != -1 else None


def _if_cond(raw):
    tr = raw.rstrip()
    if not tr.endswith(")"): return raw, None
    depth, op = 0, -1
    for i in range(len(tr) - 1, -1, -1):
        if tr[i] == ")": depth += 1
        elif tr[i] == "(":
            depth -= 1
            if depth == 0: op = i; break
    if op <= 0: return raw, None
    head = tr[:op]
    m = re.search(r"@if\s*$", head, re.I)
    cond = tr[op + 1:-1].strip()
    if not m or not cond: return raw, None
    return head[:m.start()].rstrip(), cond


def _eval_cond(src, hay):
    hay = hay.lower(); pos = [0]

    def ws():
        while pos[0] < len(src) and src[pos[0]].isspace(): pos[0] += 1

    def p_or():
        v = p_and(); ws()
        while src.startswith("||", pos[0]):
            pos[0] += 2; r = p_and(); v = v or r; ws()
        return v

    def p_and():
        v = p_un(); ws()
        while src.startswith("&&", pos[0]):
            pos[0] += 2; r = p_un(); v = v and r; ws()
        return v

    def p_un():
        ws()
        if pos[0] < len(src) and src[pos[0]] == "!":
            pos[0] += 1; return not p_un()
        return p_prim()

    def p_prim():
        ws()
        if pos[0] < len(src) and src[pos[0]] == "(":
            pos[0] += 1; v = p_or(); ws()
            if pos[0] >= len(src) or src[pos[0]] != ")": raise ValueError("缺右括号")
            pos[0] += 1; return v
        if pos[0] < len(src) and src[pos[0]] == '"':
            pos[0] += 1; buf = ""
            while pos[0] < len(src) and src[pos[0]] != '"':
                if src[pos[0]] == "\\" and pos[0] + 1 < len(src): buf += src[pos[0] + 1]; pos[0] += 2
                else: buf += src[pos[0]]; pos[0] += 1
            if pos[0] >= len(src): raise ValueError("缺右引号")
            pos[0] += 1; return buf.lower() in hay
        st = pos[0]
        while pos[0] < len(src):
            ch = src[pos[0]]
            if ch in "()!" or ch.isspace() or src.startswith("&&", pos[0]) or src.startswith("||", pos[0]): break
            pos[0] += 1
        w = src[st:pos[0]].strip()
        if not w: raise ValueError("空触发词")
        return w.lower() in hay

    v = p_or(); ws()
    if pos[0] < len(src): raise ValueError("多余内容")
    return v


INS_KEYS = ("前置前", "前置后", "后置前", "后置后", "最后置")


def rule_replace(prompt, c):
    """chatu8 的提示词替换规则：触发词=类型|内容，类型是「替换」或插入位置"""
    rid = c.get("prompt_replace_id")
    rules = (((c.get("prompt_replace") or {}).get(rid) or {}).get("text")) or ""
    ins = {k: [] for k in INS_KEYS}
    mod, allp = prompt, prompt
    for line in rules.split("\n"):
        sp = _split_rule(line)
        if not sp: continue
        trig, body = sp
        if not trig or "|" not in body: continue
        typ, raw = body.split("|", 1)[0].strip(), body.split("|", 1)[1]
        val, cond = _if_cond(raw)
        vre = re.fullmatch(r"\s*re\s*\(([\s\S]*)\)\s*", val, re.I)
        vcontent = vre.group(1) if vre else val
        tre = re.fullmatch(r"\s*re\s*\(([\s\S]*)\)\s*", trig, re.I)

        def ok():
            if not cond: return True
            try: return _eval_cond(cond, allp)
            except Exception: return False
        if tre:
            rx = _js_rx(tre.group(1))
            if not rx.search(allp) or not ok(): continue
            if typ == "替换":
                mod = rx.sub((lambda m: _js_expand(vcontent, m)) if vre else (lambda m: vcontent), mod)
            elif typ in ins:
                for m in rx.finditer(allp):
                    ins[typ].append(_js_expand(vcontent, m) if vre else vcontent)
        else:
            for t in [x.strip() for x in trig.split("|") if x.strip()]:
                if t.lower() not in allp.lower() or not ok(): continue
                if typ == "替换":
                    rx = re.compile(re.escape(t), re.I)
                    mod = rx.sub((lambda m: _js_expand(vcontent, m)) if vre else (lambda m: vcontent), mod)
                elif typ in ins:
                    ins[typ].append(vcontent)
    return mod, {k: ", ".join(v) for k, v in ins.items()}


def _lora_fix(s, c, clip_skip):
    def f(m):
        name, params = m.group(1), m.group(2)
        if not clip_skip: return m.group(0)
        p = params.split(":")
        if str(c.get("weilin_lora_fix")) == "true":
            return f"<lora:{name}:{p[0] or '1'}:{(p[1] if len(p) > 1 else '') or '1'}:{(p[2] if len(p) > 2 else '') or '1'}>"
        return f"<lora:{name}:{p[0] or '1'}:{(p[1] if len(p) > 1 else '') or '1'}>"
    return re.sub(r"<lora:([^:>]+):([^>]+)>", f, s)


def c8_payload(raw, c):
    """一个 image### 里的提示词 -> 填工作流用的参数（照 chatu8 的 generateComfyUIImage）"""
    tag = raw.replace("，", ",").replace("；", ";").replace("：", ":")
    w, h = c.get("comfyui_width") or 1024, c.get("comfyui_height") or 1024
    m = SIZE_RX.search(tag)
    if m:
        if str(c.get("aiAutonomousResolution")) != "false":
            w, h = int(m.group(1)), int(m.group(2))
        tag = tag[:m.start()] + tag[m.end():]
    tag = strip_cn(re.sub(r"\r?\n", ", ", tag))
    mod, ins = rule_replace(dedup_tags(tag), c)
    yushe = c.get("yushe") or {}
    yid = random.choice(list(yushe)) if str(c.get("randomYushe")) == "true" and yushe else c.get("yusheid_comfyui")
    y = yushe.get(yid)
    if not y:
        raise RuntimeError(f"chatu8 里找不到固定提示词预设「{yid}」")
    parts = [ins["前置前"], strip_cn(y.get("fixedPrompt", "")), ins["前置后"], strip_cn(mod), ins["后置前"],
             strip_cn(y.get("fixedPrompt_end", "")), ins["后置后"], strip_cn(c.get("AQT_comfyui", "")), ins["最后置"]]
    pos = ", ".join(p for p in parts if p and p.strip())
    worker = c.get("worker") if isinstance(c.get("worker"), str) else json.dumps(c.get("worker") or {})
    neg_y = strip_cn(y.get("negativePrompt", "")); ucp = c.get("UCP_comfyui", "") or ""
    neg = ucp if not neg_y else (neg_y if not ucp else ucp + ", " + neg_y)
    tags = [t.strip() for t in neg.split(",") if t.strip()]
    if any(t.lower() == "nsfw" for t in tags):
        neg = ", ".join(["nsfw"] + [t for t in tags if t.lower() != "nsfw"])
    if "WeiLinPromptUI" in worker:
        pos = _lora_fix(pos, c, True).replace("<lora:", "<wlr:").replace(".safetensors", "")
        neg = _lora_fix(neg, c, True).replace("<lora:", "<wlr:")
    elif "WeiLin" in worker:
        pos = _lora_fix(pos, c, True)
        if ".safetensors" not in pos:
            pos = re.sub(r"<lora:([^:>]+)(\.safetensors)?:([^>]+)>", lambda m: f"<lora:{m.group(1)}.safetensors:{m.group(3)}> ", pos)
    pos = re.sub(r",{2,}", ",", pos.replace("\n", ","))
    neg = re.sub(r",{2,}", ",", neg.replace("\n", ","))
    model = re.sub(r"\\{2,}", r"\\", str(c.get("MODEL_NAME") or "").strip())
    if not model or model == "连接后选择":
        raise RuntimeError("chatu8 里还没选 ComfyUI 模型")
    seed = c.get("comfyui_seed")
    if seed in (0, "0", "", -1, "-1", None):
        seed = random.randrange(10 ** 10)
    norm = lambda v: "" if v is None else str(v).strip()
    return {"prompt": pos, "negative_prompt": neg, "steps": c.get("comfyui_steps"), "sampler_name": c.get("comfyuisamplerName"),
            "width": w, "height": h, "cfg_scale": c.get("cfg_comfyui"), "seed": seed, "MODEL_NAME": model,
            "c_quanzhong": c.get("c_quanzhong"), "c_idquanzhong": c.get("c_idquanzhong"), "c_xijie": c.get("c_xijie"),
            "c_fenwei": c.get("c_fenwei"), "comfyuicankaotupian": "", "ipa": c.get("ipa"),
            "scheduler": norm(c.get("comfyui_scheduler")), "vae": norm(c.get("comfyui_vae")),
            "clip": norm(c.get("comfyuiCLIPName"))}


def c8_fill(wf, p):
    """把参数填进工作流里的 %占位符%（照 chatu8 的 replacepro）"""
    def num(v, fb=0):
        try:
            f = float(v)
            return str(int(f)) if f.is_integer() else str(f)
        except Exception:
            return str(fb)
    js = lambda v: json.dumps("" if v is None else str(v), ensure_ascii=False)
    for k in ("seed", "steps", "cfg_scale", "width", "height", "c_quanzhong", "c_idquanzhong", "c_xijie", "c_fenwei"):
        wf = wf.replace(f'"%{k}%"', num(p.get(k)))
    for k in ("sampler_name", "negative_prompt", "prompt", "MODEL_NAME", "comfyuicankaotupian", "ipa", "scheduler", "vae", "clip"):
        wf = wf.replace(f'"%{k}%"', js(p.get(k)))
    for k in ("inpaint_image", "inpaint_mask", "inpaint_positive", "inpaint_negative"):
        wf = wf.replace(f'"%{k}%"', '""')
    wf = wf.replace('"%inpaint_denoise%"', "0.75")
    obj = json.loads(wf)
    if any(isinstance(n, dict) and n.get("_skip") for n in obj.values()):
        raise RuntimeError("这个工作流里有被跳过的节点，bot 暂时不支持，换一个工作流试试")
    return obj


def comfy_url():
    return (CFG.get("comfyui_url") or c8().get("comfyuiUrl") or "").strip().rstrip("/")


async def comfy_image(raw, on_pid=None):
    """一段提示词 -> 图片 bytes。on_pid(prompt_id)：排进 ComfyUI 队列后回调（取消时用来从队列里删掉）"""
    c = c8()
    url = comfy_url()
    if not url:
        raise RuntimeError("chatu8 里没填 ComfyUI 地址")
    wf = c.get("worker")
    if not wf:
        raise RuntimeError("chatu8 里没选工作流")
    wf = wf if isinstance(wf, str) else json.dumps(wf, ensure_ascii=False)
    graph = c8_fill(wf, c8_payload(raw, c))
    tmo = int(c.get("comfyui_timeout") or 0) or 600
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15)) as h:
            r = await h.post(url + "/prompt", json={"client_id": C8_CLIENT, "prompt": graph})
            if r.status_code in (502, 503, 504, 520, 521, 522, 523, 524, 530):
                raise RuntimeError("ComfyUI 不在线（电脑或 ComfyUI 没开？）")
            if r.status_code >= 400:
                raise RuntimeError(f"ComfyUI 拒绝了请求（{r.status_code}）：{r.text[:300]}")
            pid = r.json()["prompt_id"]
            if on_pid:
                on_pid(pid)
            end = time.monotonic() + tmo
            while True:
                await asyncio.sleep(1.5)
                if time.monotonic() > end:
                    raise RuntimeError("ComfyUI 出图超时")
                hr = await h.get(f"{url}/history/{pid}")
                if hr.status_code != 200:
                    continue
                item = hr.json().get(pid)
                if not item:
                    continue
                st = item.get("status") or {}
                if st.get("status_str") == "error":
                    msg = next((m[1].get("exception_message") for m in st.get("messages") or []
                                if isinstance(m, list) and len(m) > 1 and isinstance(m[1], dict)
                                and m[1].get("exception_message")), "")
                    raise RuntimeError("ComfyUI 执行出错：" + (msg or "未知错误")[:300])
                imgs = [im for o in (item.get("outputs") or {}).values() for im in (o.get("images") or [])
                        if im.get("type") == "output"]
                if imgs:
                    im = imgs[0]
                    v = await h.get(url + "/view", params={"filename": im["filename"],
                                                           "subfolder": im.get("subfolder", ""), "type": "output"})
                    v.raise_for_status()
                    return v.content
                if st.get("completed"):
                    raise RuntimeError("工作流跑完了但没有输出图片")
    except httpx.TransportError as e:
        raise RuntimeError(f"连不上 ComfyUI（电脑或 ComfyUI 没开？）：{type(e).__name__}")


def img_on():
    return STATE.get("img_on", True) and (c8().get("mode") == "comfyui")


def maybe_images(bot, chat_id, card, chat, idx, force=False):
    """回复里有 image### 就在后台出图"""
    if not img_on():
        return
    m = chat["messages"][idx]
    sid = m.get("sid", 0)
    if not force and (m.get("imgs") or {}).get(str(sid)):
        return
    ps = image_prompts(m.get("content") or "")
    key = (chat["id"], idx, sid)
    if not ps or key in IMG_BUSY:
        return
    bump_img()
    IMG_BUSY.add(key)
    asyncio.create_task(gen_images(bot, chat_id, card, chat["id"], idx, sid, ps, key))


async def gen_images(bot, chat_id, card, cuid, idx, sid, prompts, key):
    note = None
    fids, sent, errs = [], [], []
    rounds = img_rounds()
    epoch = IMG_EPOCH[0]
    total = len(prompts) * rounds
    tasks, pids = [], {}

    try:
        note = await bot.send_message(chat_id=chat_id, text=f"🎨 出图中（{len(prompts)} 张 × {rounds} 轮）…",
                                      disable_notification=True)
        # 按 1 2 3 1 2 3 的顺序一轮一轮出；始终比正在等的那张多排一轮进 ComfyUI 队列，显卡不空等。
        # 你一发新指令（新消息、重新生成、翻页、继续…），后面的轮次就不出了，已经排队的从 ComfyUI 队列里删掉
        order = [p for _ in range(rounds) for p in prompts]
        made = 0

        def ensure(upto):
            nonlocal made
            while made < min(upto, len(order)) and IMG_EPOCH[0] == epoch:
                i = made
                t = asyncio.create_task(comfy_image(order[i], on_pid=lambda pid, i=i: pids.__setitem__(i, pid)))
                tasks.append(t); made += 1
        for i in range(len(order)):
            if IMG_EPOCH[0] != epoch:
                break                                   # 换了新指令，不再往下出
            ensure(i + 1 + len(prompts))
            try:
                data = await tasks[i]
                if IMG_EPOCH[0] != epoch:
                    break
                pm = await tg_call(bot.send_photo, chat_id=chat_id, photo=data, disable_notification=True)
                fids.append(pm.photo[-1].file_id); sent.append(pm.message_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("image fail: %s", e); errs.append(str(e))
                if len(errs) >= len(prompts):
                    break                               # 一整轮都失败（多半是显卡没开），别再排了
    finally:
        left = [i for i, t in enumerate(tasks) if not t.done()]
        for i in left:
            tasks[i].cancel()
        if left:
            asyncio.create_task(comfy_unqueue([pids[i] for i in left if i in pids]))
        if note: await del_msgs(bot, chat_id, [note.message_id])
        IMG_BUSY.discard(key)
    if fids:
        async with LOCK:
            c = load_chat(card)
            m = c["messages"][idx] if c and c["id"] == cuid and idx < len(c["messages"]) else None
            if m is not None and sid < len(swipes_of(m)):
                m.setdefault("imgs", {})[str(sid)] = fids
                if m.get("sid", 0) == sid:
                    old = m.get("img_tg") or []
                    m["img_tg"] = sent; m["img_sid"] = str(sid)
                    await del_msgs(bot, chat_id, old)
                else:
                    await del_msgs(bot, chat_id, sent)      # 已经翻到别的回复了，图先收起来
                save_chat(card, c)
                if idx == len(c["messages"]) - 1:
                    await refresh_last_kb(bot, chat_id, c, card)
    if errs:
        kb = InlineKeyboardMarkup([[Btn("🖼 重新出图", callback_data="img")]])
        await bot.send_message(chat_id=chat_id, text="🖼 出图失败：" + errs[0][:500], reply_markup=kb,
                               disable_notification=True)


async def sync_photos(bot, chat_id, m, new=False):
    """把这条回复当前 swipe 的图片显示出来（换 swipe 时换图）"""
    if m["role"] != "assistant":
        return
    sid = str(m.get("sid", 0))
    want = (m.get("imgs") or {}).get(sid) or []
    if not new and m.get("img_sid") == sid and len(m.get("img_tg") or []) == len(want):
        return
    if not new:
        await del_msgs(bot, chat_id, m.get("img_tg"))
    ids = []
    for fid in want:
        try:
            ids.append((await tg_call(bot.send_photo, chat_id=chat_id, photo=fid, disable_notification=True)).message_id)
        except Exception as e:
            log.warning("resend photo fail: %s", e)
    m["img_tg"], m["img_sid"] = ids, sid


async def cmd_img(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    arg = cmd_arg(update)
    if arg in ("开", "on"): STATE["img_on"] = True; save_state()
    elif arg in ("关", "off"): STATE["img_on"] = False; save_state()
    elif re.fullmatch(r"(?:轮|轮数|rounds?)?\s*[1-5]", arg or ""):
        STATE["img_rounds"] = int(arg[-1]); save_state()
    elif arg in ("停", "stop"):
        bump_img()
    c = c8()
    lines = [f"插图：{'开' if STATE.get('img_on', True) else '关'}（/img 开、/img 关）",
             f"每次出图：{img_rounds()} 轮（每轮每个提示词一张，1 2 3 1 2 3；发新指令就停。/img 1～/img 5 调，/img 停 = 现在就停）",
             f"chatu8 模式：{c.get('mode') or '未设置'}" + ("" if c.get("mode") == "comfyui" else "（bot 只支持 ComfyUI）"),
             f"ComfyUI：{comfy_url() or '未设置'}", f"工作流：{c.get('workerid') or '未选'}",
             f"模型：{c.get('MODEL_NAME') or '未选'}", f"固定提示词预设：{c.get('yusheid_comfyui') or '未选'}",
             f"标记：{c8_tags()[0]}…{c8_tags()[1]}"]
    if arg in ("测试", "test"):
        try:
            async with httpx.AsyncClient(timeout=10) as h:
                r = await h.get(comfy_url() + "/system_stats")
            lines.append("连通测试：" + ("✅ 在线" if r.status_code == 200 else f"❌ HTTP {r.status_code}（电脑或 ComfyUI 没开？）"))
        except Exception as e:
            lines.append(f"连通测试：❌ {type(e).__name__}（电脑或 ComfyUI 没开？）")
    else:
        lines.append("/img 测试 = 检查能不能连上 ComfyUI")
    await update.message.reply_text("\n".join(lines))


async def redo_images(bot, chat_id):
    card = cur_card()
    if not card:
        return
    if LOCK.locked():
        await bot.send_message(chat_id=chat_id, text="还在生成中，稍等"); return
    async with LOCK:
        chat = await pull_chat(card)
    if not chat["messages"]:
        return
    idx = len(chat["messages"]) - 1
    if not image_prompts(chat["messages"][idx].get("content") or ""):
        await bot.send_message(chat_id=chat_id, text="最后一条回复里没有画图标记"); return
    if not img_on():
        await bot.send_message(chat_id=chat_id, text="插图是关着的，/img 开 打开"); return
    maybe_images(bot, chat_id, card, chat, idx, force=True)


# ---------------------------------------------------------------- 世界书开关（只在 bot 里生效）
def wi_ov():
    return {}                    # 世界书开关直接改酒馆


def all_book_names():
    d = ST / "worlds"
    return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []


def base_books(card):
    """酒馆里启用的：全局 + 卡绑定的"""
    _, wi = wi_settings()
    names = list(wi.get("globalSelect") or [])
    if card:
        if card.get("world"): names.append(card["world"])
        for cl in wi.get("charLore") or []:
            if cl.get("name") == Path(card["file"]).stem: names += cl.get("extraBooks") or []
    return list(dict.fromkeys(names))


def book_on(name, base):
    return wi_ov().get(name, name in base)


async def cmd_wi(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    q = cmd_arg(update)
    card = cur_card(); base = base_books(card)
    names = all_book_names()
    if q:
        names = [n for n in names if q.lower() in n.lower()]
    if not names:
        await update.message.reply_text("没找到世界书"); return
    names = sorted(names, key=lambda n: (n not in base, n))   # 开着的排前面
    ctx.user_data["wi"] = names
    await update.message.reply_text(
        f"共 {len(names)} 本世界书，点一下开/关（直接改酒馆：全局世界书和这张卡绑定的都算，手机上也跟着变；/wi 关键词 搜索）：",
        reply_markup=wi_kb(names, 0, base))


def wi_kb(names, page, base, per=10):
    total = max(1, (len(names) + per - 1) // per)
    page = max(0, min(page, total - 1))
    rows = []
    for i in range(page * per, min(len(names), (page + 1) * per)):
        n = names[i]; on = book_on(n, base)
        rows.append([Btn((("✅ " if on else "⬜ ") + n + (" ✎" if n in wi_ov() else ""))[:60], callback_data=f"w:{i}:{page}")])
    nav = []
    if page > 0: nav.append(Btn("◀", callback_data=f"wp:{page - 1}"))
    nav.append(Btn(f"{page + 1}/{total}", callback_data="noop"))
    if page < total - 1: nav.append(Btn("▶", callback_data=f"wp:{page + 1}"))
    if total > 1: rows.append(nav)
    return InlineKeyboardMarkup(rows)


async def wi_callback(q, ctx, data):
    names = ctx.user_data.get("wi") or []
    card = cur_card()
    if data.startswith("wp:"):
        await q.edit_message_reply_markup(wi_kb(names, int(data[3:]), base_books(card))); return
    i, page = (int(x) for x in data[2:].split(":"))
    if i >= len(names): return
    n = names[i]
    new = n not in base_books(card)
    if LOCK.locked():
        await q.message.reply_text("还在生成中，稍等"); return
    try:
        async with LOCK:
            if card:
                await pull_chat(card)            # 页面先切到这张卡，卡绑定的世界书才对得上
            else:
                await SB.fresh()
            await SB.call("wiSet", n, new)
    except Exception as e:
        await q.message.reply_text(f"改世界书失败：{err_text(e)[:300]}"); return
    await asyncio.sleep(0.5)
    try:
        await q.edit_message_reply_markup(wi_kb(names, page, base_books(cur_card())))
    except BadRequest:
        pass


# ---------------------------------------------------------------- 主动消息（名单里的角色不定时主动来找你）
PRO_CARDS_F = DATA / "pro_cards.json"           # 每张卡是单人还是多人的判断结果（按卡和绑定世界书的修改时间缓存）
PRO = {"roster_task": None, "busy": False}
WEEK = "一二三四五六日"




CLASSIFY_PROMPT = (
    "下面是一张角色扮演用的角色卡资料，以及它绑定的世界书。这是数据分类任务：只判断卡的类型，不用评价、复述或续写内容。\n"
    "single：卡的核心是一个具体角色，{{user}}主要和这一个角色一对一互动（可以有配角）。\n"
    "group：卡里有好几个固定的、同等重要、会轮流登场说话的主要角色（比如群像、后宫、多个可攻略对象）。\n"
    "world：世界观、模拟器、系统、旁白/叙事者这类卡，没有固定的主要角色，或者主要角色多到数不过来。\n"
    "卡的描述可能是空的，角色资料都写在世界书里，这时看世界书：条目标题大多是不同的人名，通常就是 group 或 world。\n"
    '只输出一行 JSON：{"type": "single / group / world", '
    '"chars": ["主要角色名", ...（single 写一个，group 列出所有主要角色，配角不算；world 留空）], '
    '"intro": "用一句话（30 字以内）介绍主角色或这群角色", "reason": "一句话理由"}\n\n')

ROSTER_PROMPT = (
    "你在帮{user}挑今天会主动给他发消息的角色。下面是他最近在玩的角色，每个附了最后几条聊天。"
    "挑出最多 {k} 个今天最适合主动来找他的：关系亲近的、剧情停在适合接话的地方的、或者很久没聊了会想他的。"
    "标着「还没聊过」的是他没在这里聊过的角色，会来跟他搭讪，可以挑一两个混进去让名单新鲜一点。"
    "{yday}只输出一行 JSON：{\"pick\": [编号, ...]}\n\n")


def pro():
    P = STATE.setdefault("pro", {})
    P.setdefault("on", True)
    P.pop("cap", None)                                   # 旧版「每天几次」的设置，已不用
    for k in ("pin", "block"):
        P.setdefault(k, [])
    P.setdefault("last", {})
    return P


def pro_key(f):
    return hashlib.md5(f.encode("utf-8")).hexdigest()[:10]


def card_by_key(k):
    return next((c for c in list_cards() if pro_key(c["file"]) == k), None)


def fmt_gap(sec):
    if sec < 3600: return f"{max(1, int(sec // 60))} 分钟"
    if sec < 86400: return f"{int(sec // 3600)} 小时"
    return f"{int(sec // 86400)} 天"


def chat_mtime(card):
    p = st_chat_file(card)
    try:
        return p.stat().st_mtime if p else 0
    except OSError:
        return 0


def cycle_start():
    """每天几点换名单（默认东京时间 7 点）；返回这一轮开始的时间"""
    now = datetime.datetime.now(TZ)
    s = now.replace(hour=int(CFG.get("proactive_roster_hour", 7)), minute=0, second=0, microsecond=0)
    return s if now >= s else s - datetime.timedelta(days=1)


def gap_hours():
    g = pro().get("gap") or CFG.get("proactive_gap_hours") or [3, 4]
    return float(g[0]), float(g[-1])








def aux_api():
    """杂活模型（挑名单、判断单人/多人卡）；没单独设就用聊天的那个"""
    x = pro().get("aux") or {}
    a = next((a for a in get_apis() if a["name"] == x.get("api")), None)
    if a and x.get("model"):
        return a, x["model"]
    return cur_api(), cur_model()


AUX_TASK = "【暂停角色扮演，执行系统任务】这一条不是剧情，请按下面的要求完成任务，最后输出要求的 JSON。\n\n"
PRESET_SKIP = {"chatHistory", "worldInfoBefore", "worldInfoAfter", "charDescription", "charPersonality", "scenario",
               "personaDescription", "dialogueExamples"}


def preset_wrap(task, char=""):
    """杂活走预设：只用预设自己写的条目（破限、后置、预填充），中间放任务；角色描述/世界书/聊天记录这些位置不放"""
    p = cur_preset(); ov = toggles_for(STATE.get("preset"))
    prompts = {x.get("identifier"): x for x in (p.get("prompts") or [])}
    ctx = {"char": char or "角色", "user": user_name(st_settings()), "vars": {}, "gvars": dict(STATE.get("gvars") or {})}
    before, after, inj, seen = [], [], [], False
    for o in preset_order(p):
        ident = o.get("identifier")
        if not ov.get(ident, o.get("enabled", True)):
            continue
        if ident == "chatHistory":
            seen = True; continue
        pr = prompts.get(ident, {})
        if ident in PRESET_SKIP or (pr.get("marker") and ident not in ("main", "jailbreak", "nsfw", "enhanceDefinitions")):
            continue
        c = macro(pr.get("content") or ("" if pr else DEFAULT_PROMPTS.get(ident, "")), ctx)
        if not c.strip():
            continue
        m = {"role": ROLE_MAP.get(pr.get("role", "system"), "system"), "content": c}
        (inj if pr.get("injection_position") == 1 else after if seen else before).append(m)
    return before + inj + [{"role": "user", "content": AUX_TASK + task}] + after, p


class AuxTimeout(RuntimeError):
    pass


def aux_tmo():
    return int(CFG.get("aux_timeout_sec", 900))


async def aux_call(prompt, max_tokens=1500, char="", use_preset=None):
    a, model = aux_api()
    if use_preset is None:
        use_preset = pro().get("aux_preset")
    if use_preset:
        msgs, p = preset_wrap(prompt, char)
        pre = {"temperature": 0.3, "openai_max_tokens": max(max_tokens, 4000),
               "use_makersuite_sysprompt": p.get("use_makersuite_sysprompt", True)}
    else:
        msgs = [{"role": "user", "content": prompt}]
        pre = {"temperature": 0.3, "openai_max_tokens": max_tokens, "use_makersuite_sysprompt": False}
    async def run():
        if a["type"] == "vertex":
            return await generate(msgs, pre, model)
        # 走流式：中转站不管是一个字一个字给还是写完一次性给都能收；等待上限用杂活自己的（「抗截断」这类模型很慢）
        return "".join([x async for x in stream_openai(a, msgs, pre, model, aux_tmo())])
    try:
        text = await asyncio.wait_for(run(), aux_tmo())
    except (asyncio.TimeoutError, httpx.TimeoutException):
        raise AuxTimeout(f"杂活模型 {model} 超过 {aux_tmo() // 60} 分钟没回完")
    return split_reasoning(text)[1].strip()


def err_text(e):
    t = str(e).strip()
    if "<" in t and re.search(r"<(html|head|title|body)\b", t, re.I):
        m = re.search(r"<title>(.*?)</title>", t, re.I | re.S)
        t = re.sub(r"<[^>]*>.*" if not m else r"<[\s\S]*", "", t).strip(" :：") + (" " + m.group(1).strip() if m else "")
    return re.sub(r"\b(\d{3}) \1\b", r"\1", re.sub(r"\s+", " ", t)).strip() or type(e).__name__


def parse_json(t, need):
    """从回复里找出带 need 这个键的 JSON（走预设时模型可能先演一段再给 JSON）"""
    t = THINK_SPAN.sub("", re.sub(r"```(?:json)?", "", t or ""))
    dec, found = json.JSONDecoder(), None
    for m in re.finditer(r"\{", t):
        try:
            obj, _ = dec.raw_decode(t, m.start())
        except ValueError:
            continue
        if isinstance(obj, dict) and need in obj:
            found = obj                         # 取最后一个
    if found is None:
        raise ValueError("模型没按格式回答：" + re.sub(r"\s+", " ", t)[:120])
    return found


def clip(s, n):
    s = re.sub(r"\n{3,}", "\n\n", (s or "").strip())
    return s if len(s) <= n else s[:n] + "…"


def card_book_paths(card):
    """卡绑定的世界书（卡里设的那本 + 额外绑定的），不含全局世界书"""
    _, wi = wi_settings()
    names = [card["world"]] if card.get("world") else []
    for cl in wi.get("charLore") or []:
        if cl.get("name") == Path(card["file"]).stem:
            names += cl.get("extraBooks") or []
    names = list(dict.fromkeys(n for n in names if n))
    return names, [ST / "worlds" / f"{n}.json" for n in names]


def card_books(card):
    names, paths = card_book_paths(card)
    ents = []
    for p in paths:
        hit = book_cache.data.get(str(p))            # 已经在内存里就直接用，不在就临时读一下（不常驻内存）
        try:
            ents += hit[1] if hit and hit[0] == os.stat(p).st_mtime_ns and hit[1] else load_book(p)
        except FileNotFoundError:
            pass
    if card.get("character_book"):
        ents += convert_char_book(card["character_book"], card["name"])
    return names, [e for e in ents if not e["disable"]], paths


def card_sig(card):
    out = []
    for p in [ST / "characters" / card["file"]] + card_book_paths(card)[1]:
        try:
            out.append(str(os.stat(p).st_mtime_ns))
        except FileNotFoundError:
            out.append("0")
    return ":".join(out)


def card_brief(card):
    """给模型判断单人/多人用的材料：卡本身 + 绑定世界书（标题和关键词全给，正文只截一部分）"""
    out = [f"【卡名】{card['name']}"]
    for lb, k, n in (("描述", "description", 2500), ("性格", "personality", 600), ("场景", "scenario", 600),
                     ("开场白", "first_mes", 1500)):
        if (card.get(k) or "").strip():
            out.append(f"【{lb}】\n{clip(card[k], n)}")
    for i, g in enumerate((card.get("alternate_greetings") or [])[:3]):
        if (g or "").strip():
            out.append(f"【备用开场白 {i + 1}】\n{clip(g, 500)}")
    names, ents, _ = card_books(card)
    if ents:
        lines, used = [], 0
        for e in ents:
            ln = f"- {e['comment'] or '（无标题）'}｜{'、'.join(str(k) for k in e['key'][:6])}"
            if used + len(ln) > 4000:
                lines.append(f"……（还有 {len(ents) - len(lines)} 条）"); break
            lines.append(ln); used += len(ln)
        out.append(f"【绑定的世界书】{'、'.join(names) or '卡内嵌'}，共 {len(ents)} 条。条目标题｜关键词：\n" + "\n".join(lines))
        top = sorted(ents, key=lambda e: (not e["constant"], -e["order"]))[:6]
        out.append("【部分条目正文】\n" + "\n\n".join(f"〔{e['comment'] or '无标题'}〕{clip(e['content'], 400)}" for e in top))
    elif not any((card.get(k) or "").strip() for k in ("description", "personality", "scenario", "first_mes")):
        out.append("（这张卡几乎没有资料）")
    return "\n\n".join(out)


async def aux_json(prompt, need, max_tokens, char=""):
    """问杂活模型要 JSON；没问明白（拒绝、空回复、格式不对）就自动带上预设再问一次。返回 (结果, 是否用了预设)"""
    first = bool(pro().get("aux_preset"))
    try:
        return parse_json(await aux_call(prompt, max_tokens, char, use_preset=first), need), first
    except AuxTimeout:
        raise                                       # 超时的不重查，带预设只会更慢
    except Exception as e:
        if first:
            raise
        log.info("杂活没问明白（%s），带预设再问一次", err_text(e)[:80])
        return parse_json(await aux_call(prompt, max_tokens, char, use_preset=True), need), True


def cache_ok(hit, sig):
    return bool(hit) and hit.get("sig") == sig and not (hit.get("type") == "multi" and "kind" not in hit)


HEAVY_TEXT = re.compile(r"<\s*(?:style|script|html|iframe|details|table)\b|```html|<div[^>]*style=", re.I)
HEAVY_WI = re.compile(r"<%|\{\{\s*(?:setvar|addvar|getvar|setglobalvar)|_\.set\(|stat_data|initvar|<UpdateVariable|"
                      r"<StatusPlaceHolder|EJS|getvar\(", re.I)


def heavy_check(card):
    """多人卡够不够「轻」：绑定世界书不大、没有美化渲染和脚本插件。返回不够轻的原因，够轻返回空字符串"""
    if any(HTML_LIKE.search(x.get("replaceString") or "") for x in card.get("regex_scripts") or [] if not x.get("disabled")):
        return "卡里有美化正则"
    if card.get("helper"):
        return "卡里有酒馆助手脚本"
    greet = "\n".join([card.get("first_mes") or "", card.get("description") or ""] + list(card.get("alternate_greetings") or []))
    if HEAVY_TEXT.search(greet):
        return "开场白/描述里有 HTML 面板"
    _, ents, _ = card_books(card)
    size = sum(len(e["content"]) for e in ents)
    if size > int(CFG.get("proactive_small_max_wi", 50000)):
        return f"世界书 {size // 1000}k 字"
    if any(HEAVY_WI.search(e["content"]) for e in ents):
        return "世界书里有变量/模板脚本"
    return ""


def small_why(r):
    """多人卡为什么不算小卡（是小卡返回空字符串）。人数上限每次现算，改 proactive_small_max_chars 不用重新判断"""
    if r.get("type") != "multi":
        return "不是多人卡"
    if r.get("kind") != "group":
        return "世界观/模拟器类"
    n = len(r.get("chars") or [])
    if not 2 <= n <= int(CFG.get("proactive_small_max_chars", 8)):
        return f"主要角色 {n} 个"
    return r.get("heavy") or ""


def is_small(r):
    return bool(r) and r.get("type") == "multi" and not small_why(r)


def can_call(r):
    """这张卡能不能主动来找你：单人卡，或者多人小卡"""
    return bool(r) and (r.get("type") == "single" or is_small(r))


async def classify(card, cache, sem):
    sig = card_sig(card)
    hit = cache.get(card["file"])
    if cache_ok(hit, sig):
        return hit
    async with sem:
        try:
            j, via = await aux_json(CLASSIFY_PROMPT + card_brief(card), "type", 600, card["name"])
            t = str(j.get("type", "")).strip().lower()
            chars = [str(x).strip()[:20] for x in (j.get("chars") or []) if str(x).strip()][:20]
            r = {"sig": sig, "type": "single" if t.startswith("s") else "multi", "v": 2,
                 "intro": str(j.get("intro") or "")[:60], "reason": str(j.get("reason") or "")[:100]}
            if r["type"] == "multi":
                r["kind"] = "world" if t.startswith("w") else "group"
                r["chars"] = chars
                r["heavy"] = heavy_check(card)
            if via: r["via"] = "预设"
        except Exception as e:
            log.warning("判断卡类型失败 %s：%s", card["name"], err_text(e))
            return {"type": "error", "reason": err_text(e)[:100]}     # 失败的不缓存，下次再判断
    cache[card["file"]] = r
    return r


def cand_text(i, card, chat, mt, preset, intro="", chars=None):
    many = f"，多人卡：{'、'.join(chars)}" if chars else ""
    if not chat:
        return f"[{i}] {card['name']}（还没聊过{many}）：{intro or clip(card.get('description') or '', 60) or '（没有简介）'}"
    rules = tag_rules(card, preset)
    lines = [f"[{i}] {card['name']}（上次聊天：{fmt_gap(time.time() - mt)}前{many}）"]
    for m in chat["messages"][-4:]:
        segs = tokenize(strip_image_tags(m.get("content") or ""), rules)
        t = re.sub(r"\s+", " ", "".join(b for k, b, _, _ in segs if k in ("text", "quote"))).strip()
        if t:
            lines.append(f"  {'他' if m['role'] == 'user' else card['name']}：{clip(t, 150)}")
    return "\n".join(lines)


async def build_roster():
    """今天的名单：常驻的 + 模型从最近在酒馆里聊过的单人卡里挑的"""
    P = pro()
    cands = []
    for c in list_cards():
        if c["file"] in P["block"]:
            continue
        p = st_chat_file(c)
        if not p:
            continue
        try:
            cands.append((p.stat().st_mtime, c, p))
        except OSError:
            continue
    cands.sort(key=lambda x: -x[0])
    pool = []
    for mt, c, p in cands:
        tail = st_tail(p, 6)
        if any(m["role"] == "user" for m in tail):
            pool.append((mt, c, {"messages": tail}))
        if len(pool) >= int(CFG.get("proactive_pool", 40)):
            break
    cands = pool
    cache = rjson(PRO_CARDS_F, {}) or {}
    sem = asyncio.Semaphore(3)
    res = await asyncio.gather(*(classify(c, cache, sem) for _, c, _ in cands))
    wjson(PRO_CARDS_F, cache)
    pin = [f for f in P["pin"] if get_card(f)]
    singles = [x for x, r in zip(cands, res) if can_call(r) and x[1]["file"] not in pin]
    stats = {"pool": len(cands), "multi": sum(r["type"] == "multi" and not is_small(r) for r in res),
             "err": sum(r["type"] == "error" for r in res)}
    # 最近没聊过的单人卡（后台已经判断过的），每天随机抽一些混进候选，来跟你搭讪
    chatted = {c["file"] for _, c, _ in cands}
    fresh = [c for c in list_cards() if c["file"] not in chatted
             and c["file"] not in P["block"] and c["file"] not in pin
             and can_call(cache.get(c["file"]))
             and cache_ok(cache[c["file"]], card_sig(c))]
    fresh = random.sample(fresh, min(len(fresh), int(CFG.get("proactive_new_pool", 15))))
    singles += [(0, c, None) for c in fresh]
    stats["new"] = len(fresh)
    k = int(CFG.get("proactive_roster", 5))
    picked, src = [], "模型挑的" if singles else ("只有常驻的" if pin else "没有可挑的单人卡")
    if singles:
        try:
            preset = cur_preset()
            yday = [get_card(f)["name"] for f in (P.get("plan") or {}).get("roster") or [] if get_card(f)]
            head = (ROSTER_PROMPT.replace("{user}", user_name(st_settings())).replace("{k}", str(k))
                    .replace("{yday}", f"尽量和昨天的名单（{'、'.join(yday)}）不完全一样。" if yday else ""))
            body = "\n\n".join(cand_text(i, c, ch, mt, preset, (cache.get(c["file"]) or {}).get("intro", ""),
                                          (cache.get(c["file"]) or {}).get("chars") if is_small(cache.get(c["file"])) else None)
                                for i, (mt, c, ch) in enumerate(singles))
            j, _ = await aux_json(head + body, "pick", 800)
            for i in j.get("pick") or []:
                if isinstance(i, int) and 0 <= i < len(singles) and singles[i][1]["file"] not in picked:
                    picked.append(singles[i][1]["file"])
            picked = picked[:k]
        except Exception as e:
            log.warning("挑名单失败，改成随机：%s", err_text(e))
            src = f"随机挑的，模型出错：{err_text(e)[:60]}"
        if not picked:
            picked = [x[1]["file"] for x in random.sample(singles, min(k, len(singles)))]
            if src == "模型挑的": src = "随机挑的"
    return pin + picked, src, stats


async def new_plan(same_day=False):
    """挑名单。same_day=True：今天之内重挑（「今天已来几条」不清零）"""
    P = pro()
    roster, src, stats = await build_roster()
    start = cycle_start().timestamp()
    plan = P.get("plan") or {}
    if not same_day or plan.get("cycle") != start:
        plan = {"cycle": start, "sent": 0}
    plan.pop("times", None)                              # 旧版排好的固定时间，已不用
    plan.update(roster=roster, src=src, stats=stats)
    P["plan"] = plan; save_state()
    return plan


async def roster_job(bot, chat_id, same_day=False, show=False):
    try:
        await new_plan(same_day)
    except Exception as e:
        log.exception("挑主动名单")
        P = pro()
        if not same_day:         # 别每分钟重试一次：这一轮记成空名单
            P["plan"] = {"cycle": cycle_start().timestamp(), "sent": 0, "roster": [],
                         "src": f"出错：{str(e)[:80]}", "stats": {}}
            save_state()
        await bot.send_message(chat_id=chat_id, text=f"💌 挑主动名单失败：{e}"[:800], disable_notification=True)
        return
    if show:
        text, kb = pro_view()
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)


async def pro_fire(bot, chat_id, force=False):
    """从名单里挑一张卡来找你。返回 True，或者没发的原因"""
    P = pro()
    quiet = int(CFG.get("proactive_quiet_min", 60)) * 60
    elig = []
    for f in (P.get("plan") or {}).get("roster") or []:
        c = get_card(f)
        if not c or f in P["block"]:
            continue
        if not force and time.time() - chat_mtime(c) < quiet:
            continue                                       # 刚聊过
        elig.append((P["last"].get(f, 0), random.random(), c))
    today = cycle_start().timestamp()
    fresh = [x for x in elig if x[0] < today]              # 每个角色一天（早 7 点到第二天 7 点）最多来一次
    if fresh or not force:
        elig = fresh                                       # 手动叫的：今天都来过了就不限
    if not elig:
        return "名单里的今天都来过了，或者刚聊过（一小时内）"
    card = random.choice(elig)[2] if force else min(elig, key=lambda x: (x[0], x[1]))[2]
    PRO["busy"] = True
    try:
        async with LOCK:
            pend = P.setdefault("pending", {})
            old = pend.pop(card["file"], None)
            if old:                                        # 上一次搭讪你没回：那个聊天删掉，换个开场白重来
                p = st_chat_file(card, old.get("chat") or "")
                if p and p.exists() and p.stem != (card.get("chat") or "") \
                        and not any(m["role"] == "user" for m in st_tail(p, 0)):
                    p.unlink()
                    log.info("主动消息：%s 上次的搭讪没回，删掉聊天 %s", card["name"], p.stem)
            gi = int(P.setdefault("gi", {}).get(card["file"], 0))
            await SB.fresh()
            r = await SB.call("greetChat", card["file"], gi, card.get("chat") or "", card_mtime(card) * 1000)
            P["gi"][card["file"]] = gi + 1
            chat = st_to_chat(card, r["data"])
            if not chat["messages"]:
                p = st_chat_file(card, r["name"])
                if p and p.exists():
                    p.unlink()
                save_state()
                return f"「{card['name']}」没有开场白，没法搭讪"
            pend[card["file"]] = {"chat": r["name"], "t": time.time()}
            P["last"][card["file"]] = time.time(); save_state()
            m = chat["messages"][-1]
            m["pro"] = True
            if old:
                m["pro_reset"] = True
            log.info("主动消息 char=%s 新聊天=%s 开场白=%d", card["name"], r["name"], gi)
            await show_msg(bot, chat_id, card, chat, len(chat["messages"]) - 1, new=True)
        return True
    finally:
        PRO["busy"] = False


async def pro_tick(bot):
    P = pro()
    if not P["on"] or not ALLOWED:
        return
    uid = next(iter(ALLOWED))
    plan = P.get("plan") or {}
    if not P.get("aux") and "roster" not in plan:
        return                                  # 第一次用：等你选好杂活模型（/dm 杂活）再开始挑
    if plan.get("cycle") != cycle_start().timestamp():
        t = PRO["roster_task"]
        if not (t and not t.done()):
            PRO["roster_task"] = asyncio.create_task(roster_job(bot, uid))
        return
    now = time.time()
    if STATS["last_update"] and STATS["last_update"] > P.get("active", 0):
        P["active"] = STATS["last_update"]; save_state()      # 你最后一次动 bot 的时间（存盘，重启不丢）
    if not P.get("active"):
        P["active"] = now; save_state()
    anchor = max(P["active"], P.get("fired", 0))
    if P.get("anchor") != anchor:                            # 你动了 bot，或者刚来过一条：重新计时
        lo, hi = gap_hours()
        P["anchor"] = anchor; P["next"] = anchor + random.uniform(lo, hi) * 3600; save_state()
    if now < P["next"] or PRO["busy"] or LOCK.locked():
        return
    lo, hi = gap_hours()                                     # 不管这次发没发成，都从现在重新计时
    P["fired"] = P["anchor"] = now; P["next"] = now + random.uniform(lo, hi) * 3600; save_state()
    r = await pro_fire(bot, uid)
    if r is True:
        plan["sent"] = plan.get("sent", 0) + 1; save_state()
    else:
        log.info("主动消息这次没发：%s", r)


SCAN_FAIL = {}          # 判断失败的卡：文件名 -> 下次再试的时间


def scan_progress():
    """(总数, 判断完, 单人, 多人小卡, 多人大卡/世界观)"""
    cache = rjson(PRO_CARDS_F, {}) or {}
    cards = list_cards()
    done = [cache[c["file"]] for c in cards if cache_ok(cache.get(c["file"]), card_sig(c))]
    small = sum(is_small(d) for d in done)
    single = sum(d["type"] == "single" for d in done)
    return len(cards), len(done), single, small, len(done) - single - small


async def scan_one():
    """找几张还没判断（或者改过）的卡，同时判断（默认 3 张）。返回 True = 判断了"""
    cache = rjson(PRO_CARDS_F, {}) or {}
    now = time.time()
    todo = []
    for c in list_cards():
        hit = cache.get(c["file"])
        if cache_ok(hit, card_sig(c)) or SCAN_FAIL.get(c["file"], 0) > now:
            continue
        todo.append(c)
        if len(todo) >= int(CFG.get("proactive_scan_parallel", 3)):
            break
    if not todo:
        return False
    sem = asyncio.Semaphore(len(todo))
    res = await asyncio.gather(*(classify(c, {}, sem) for c in todo))
    cur = rjson(PRO_CARDS_F, {}) or {}
    for c, r in zip(todo, res):
        if r["type"] == "error":
            SCAN_FAIL[c["file"]] = time.time() + 6 * 3600   # 失败的过 6 小时再试
        else:
            cur[c["file"]] = r
    wjson(PRO_CARDS_F, cur)
    return True


async def scan_loop():
    """后台每隔一会儿判断一张卡，把所有卡慢慢判断完（默认 20 秒一张）"""
    await asyncio.sleep(60)
    while True:
        gap = int(CFG.get("proactive_scan_sec", 20))
        try:
            P = pro()
            t = PRO["roster_task"]
            if P["on"] and P.get("aux") and not (t and not t.done()):
                if not await scan_one():
                    gap = 600                                # 全判断完了，十分钟看一次有没有新卡或改过的卡
        except Exception:
            log.exception("后台判断卡")
        await asyncio.sleep(gap)


async def pro_loop(app):
    await asyncio.sleep(30)
    while True:
        try:
            await pro_tick(app.bot)
        except Exception:
            log.exception("主动消息")
        await asyncio.sleep(60)


async def call_one(bot, chat_id):
    """手动叫名单里随机一个人来找你（今天还没来过的优先）"""
    P = pro(); plan = P.get("plan") or {}
    t = PRO["roster_task"]
    if PRO["busy"]:
        await bot.send_message(chat_id=chat_id, text="已经在写一条了，稍等"); return
    if t and not t.done():
        await bot.send_message(chat_id=chat_id, text="名单还在挑，好了会发给你，到时再叫"); return
    if not plan.get("roster"):
        tip = "先用 /dm 杂活 选杂活模型" if not P.get("aux") else "先在 /dm 里点 🔄 重新挑名单，或者 /dm 加 关键词"
        await bot.send_message(chat_id=chat_id, text=f"名单是空的，{tip}"); return
    await bot.send_message(chat_id=chat_id, text="💌 好，随机叫一个人来找你…", disable_notification=True)
    asyncio.create_task(fire_now(bot, chat_id))


async def fire_now(bot, chat_id):
    try:
        r = await pro_fire(bot, chat_id, force=True)
        if r is not True:
            await bot.send_message(chat_id=chat_id, text=f"💌 没发出来：{r}")
    except Exception as e:
        log.exception("主动消息")
        await bot.send_message(chat_id=chat_id, text=f"💌 出错了：{e}"[:800])


def next_text():
    P = pro()
    if not P["on"]:
        return "—（已关）"
    nx = P.get("next")
    if not nx:
        return "—"
    d = datetime.datetime.fromtimestamp(nx, TZ)
    today = datetime.datetime.now(TZ).date()
    day = "" if d.date() == today else ("明天 " if (d.date() - today).days == 1 else d.strftime("%m-%d "))
    return day + d.strftime("%H:%M")


def pro_view():
    P = pro(); plan = P.get("plan") or {}
    a, model = aux_api()
    lines = [f"💌 主动消息：{'开' if P['on'] else '关'}",
             f"你不动 bot {gap_text()}就有人来找你（你一动就重新计时）\n"
             f"下一次约 {next_text()} · 今天已来 {plan.get('sent', 0)} 条",
             f"杂活模型：{a['name']} / {model or '未选'}" + ("" if P.get("aux") else "（没单独设，跟聊天的一样）")
             + f" · 走预设：{'是' if P.get('aux_preset') else '否'}", ""]
    st = plan.get("stats") or {}
    t = PRO["roster_task"]
    if t and not t.done() and t is not asyncio.current_task():     # 挑完名单发面板时，任务本身还没结束，不算「正在挑」
        lines.append("名单正在挑…")
    elif "roster" not in plan and not P.get("aux"):
        lines.append("⚠️ 先用 /dm 杂活 选一个便宜的杂活模型，选好就开始挑名单")
    elif "roster" in plan:
        lines.append(f"今天的名单（{plan.get('src', '')}）" + (
            f"：聊过的卡 {st.get('pool', 0)} 张（多人大卡/世界观跳过 {st.get('multi', 0)} 张"
            + (f"，{st['err']} 张没判断出来" if st.get("err") else "")
            + f"）+ 没聊过的随机抽 {st.get('new', 0)} 张" if st else ""))
    if P.get("aux"):
        total, done, single, small, multi = scan_progress()
        lines.insert(3, f"全部卡判断进度：{done}/{total}（单人 {single}，多人小卡 {small}，多人大卡/世界观 {multi}）"
                     + ("" if done >= total else f"，后台每次同时判断 {CFG.get('proactive_scan_parallel', 3)} 张")
                 + (f"，{len([1 for v in SCAN_FAIL.values() if v > time.time()])} 张判断失败，6 小时后重试"
                    if any(v > time.time() for v in SCAN_FAIL.values()) else ""))
    rows = []
    for f in plan.get("roster") or []:
        c = get_card(f)
        if not c: continue
        pin = f in P["pin"]
        rows.append([Btn(("📌 " if pin else "") + c["name"][:24], callback_data="noop"),
                     Btn("取消常驻" if pin else "常驻", callback_data="dmk:" + pro_key(f)),
                     Btn("✖ 移除", callback_data="dmx:" + pro_key(f))])
    if not rows:
        lines.append("（名单是空的）")
    rows.append([Btn("🔄 重新挑名单", callback_data="dmr"), Btn("💌 随机叫一个", callback_data="dmn")])
    lines.append("\n📌 常驻 = 每天都在名单里；✖ 移除 = 以后不会再挑它\n"
                 "/dm 来 = 现在就随机叫一个（今天还没来过的优先）\n"
                 "/dm 加 关键词 · /dm 恢复 · /dm 间隔 3-4 · /dm 开 · /dm 关 · /dm 杂活 · /dm 杂活预设")
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def aux_preset_tip():
    P = pro()
    if P.get("aux_preset"):
        return (f"📜 杂活走预设：是（用 bot 现在选的预设「{STATE.get('preset')}」）\n"
                "只套预设自己写的条目（破限、后置、预填充），角色描述、世界书、聊天记录这些不放。\n"
                "之前没判断出来的卡，下次挑名单时会按新设置重新判断；已经判断好的不变。")
    return "📜 杂活走预设：否（直接把任务发给杂活模型，最省 token）"


def fmt_h(x):
    return f"{x:g}"


def gap_text():
    lo, hi = gap_hours()
    return f"{fmt_h(lo)} 小时" if lo == hi else f"{fmt_h(lo)}～{fmt_h(hi)} 小时"


def pro_kb(card):
    return InlineKeyboardMarkup([[Btn("💬 切到 TA 回复", callback_data="dmgo:" + pro_key(card["file"]))]])


async def cmd_dm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    P = pro(); arg = cmd_arg(update)
    if arg in ("来", "叫", "now"):
        await call_one(ctx.bot, update.effective_chat.id); return
    if arg in ("开", "on", "关", "off"):
        P["on"] = arg in ("开", "on"); save_state()
        await update.message.reply_text("主动消息：" + ("开" if P["on"] else "关")); return
    m = re.fullmatch(r"(?:间隔|gap)\s*(\d+(?:\.\d+)?)\s*(?:[-~～到至]\s*(\d+(?:\.\d+)?))?\s*(?:小时|h)?", arg)
    if m:
        a = float(m.group(1)); b = float(m.group(2) or a)
        if not 0.5 <= min(a, b) <= max(a, b) <= 48:
            await update.message.reply_text("间隔要在 0.5～48 小时之间"); return
        P["gap"] = [min(a, b), max(a, b)]; P.pop("anchor", None); save_state()
        await update.message.reply_text(f"好，你不动 bot {gap_text()}就有人来找你"); return
    if re.match(r"(加|add)(\s|$)", arg):
        q = re.sub(r"^(加|add)\s*", "", arg).lower()
        if not q:
            await update.message.reply_text("用法：/dm 加 关键词（搜角色名）"); return
        files = [c["file"] for c in list_cards() if q in c["name"].lower() or q in c["file"].lower()]
        if not files:
            await update.message.reply_text("没找到角色"); return
        cache = rjson(PRO_CARDS_F, {}) or {}
        ctx.user_data["dm_add"] = files
        def mark(f):
            r = cache.get(f) or {}
            return "" if r.get("type") != "multi" else (" 👥小" if is_small(r) else f" 👥（{small_why(r)}）")
        labels = [get_card(f)["name"] + mark(f) for f in files]
        await update.message.reply_text("点一下加进名单（会设成常驻）。👥小 = 多人小卡，👥（原因）= 多人大卡/世界观：",
                                        reply_markup=pager(labels, 0, "dma", "dmap")); return
    if arg in ("恢复", "restore"):
        files = [f for f in P["block"] if get_card(f)]
        if not files:
            await update.message.reply_text("没有移除过的卡"); return
        ctx.user_data["dm_blk"] = files
        await update.message.reply_text("移除过的卡，点一下恢复（之后又能被挑中）：",
                                        reply_markup=pager([get_card(f)["name"] for f in files], 0, "dmu", "dmup")); return
    m = re.fullmatch(r"(?:杂活预设|auxpreset)\s*(开|关|on|off)?", arg)
    if m:
        P["aux_preset"] = (m.group(1) in ("开", "on")) if m.group(1) else not P.get("aux_preset"); save_state()
        await update.message.reply_text(aux_preset_tip()); return
    if re.match(r"(杂活|aux)(\s|$)", arg):
        ctx.user_data["dm_q"] = re.sub(r"^(杂活|aux)\s*", "", arg).lower()
        rows = [[Btn(a["name"], callback_data=f"dmA:{i}")] for i, a in enumerate(get_apis())]
        rows.append([Btn(f"📜 杂活走预设：{'是' if P.get('aux_preset') else '否'}（点一下切换）", callback_data="dmT")])
        await update.message.reply_text("杂活模型用来挑名单、判断单人/多人卡，选个便宜快的。先选 API：\n"
                                        "（/dm 杂活 flash 这样带关键词可以筛模型）", reply_markup=InlineKeyboardMarkup(rows)); return
    text, kb = pro_view()
    await update.message.reply_text(text, reply_markup=kb)


async def pro_callback(update: Update, ctx, data):
    q = update.callback_query
    bot, chat_id = ctx.bot, update.effective_chat.id
    P = pro(); plan = P.setdefault("plan", {})
    head, _, arg = data.partition(":")
    if head == "dmgo":
        c = card_by_key(arg)
        if not c:
            await q.answer("找不到这张卡了"); return
        if LOCK.locked():
            await q.answer("还在生成中，稍等"); return
        await q.answer()
        try:
            await q.edit_message_reply_markup(None)
        except Exception:
            pass
        pend = (P.get("pending") or {}).pop(c["file"], None); save_state()
        name = (pend or {}).get("chat")
        p = st_chat_file(c, name) if name else None
        await select_char(bot, chat_id, c["file"], chat_name=name if p and p.exists() else None); return
    await q.answer()
    if head in ("dmk", "dmx"):
        c = card_by_key(arg)
        if c:
            f = c["file"]
            if head == "dmk":
                if f in P["pin"]: P["pin"].remove(f)
                else: P["pin"].append(f)
            else:
                P["pin"] = [x for x in P["pin"] if x != f]
                if f not in P["block"]: P["block"].append(f)
                plan["roster"] = [x for x in plan.get("roster") or [] if x != f]
            save_state()
        text, kb = pro_view()
        try:
            await q.edit_message_text(text, reply_markup=kb)
        except BadRequest:
            pass
    elif head == "dmr":
        t = PRO["roster_task"]
        if t and not t.done():
            await bot.send_message(chat_id=chat_id, text="正在挑了，稍等"); return
        await bot.send_message(chat_id=chat_id, text="🔄 重新挑名单中（第一次要逐张判断是不是多角色卡，会久一点）…")
        PRO["roster_task"] = asyncio.create_task(roster_job(bot, chat_id, same_day=True, show=True))
    elif head == "dmn":
        await call_one(bot, chat_id)
    elif head in ("dmap", "dmup"):
        key = "dm_add" if head == "dmap" else "dm_blk"
        files = ctx.user_data.get(key) or []
        labels = [get_card(f)["name"] if get_card(f) else f for f in files]
        await q.edit_message_reply_markup(pager(labels, int(arg), "dma" if head == "dmap" else "dmu", head))
    elif head == "dma":
        files = ctx.user_data.get("dm_add") or []
        i = int(arg)
        if i < len(files):
            f = files[i]
            P["block"] = [x for x in P["block"] if x != f]
            if f not in P["pin"]: P["pin"].append(f)
            if f not in (plan.get("roster") or []): plan["roster"] = (plan.get("roster") or []) + [f]
            save_state()
            await bot.send_message(chat_id=chat_id, text=f"📌 已把「{get_card(f)['name']}」加进名单（常驻）")
    elif head == "dmu":
        files = ctx.user_data.get("dm_blk") or []
        i = int(arg)
        if i < len(files):
            P["block"] = [x for x in P["block"] if x != files[i]]; save_state()
            await bot.send_message(chat_id=chat_id, text=f"已恢复「{get_card(files[i])['name']}」，下次挑名单时又能被挑中")
    elif head == "dmA":
        apis = get_apis(); i = int(arg)
        if i >= len(apis): return
        try:
            ms = await list_models(apis[i])
        except Exception as e:
            await bot.send_message(chat_id=chat_id, text=f"拉取模型列表失败：{e}"[:800]); return
        kw = ctx.user_data.get("dm_q") or ""
        if kw: ms = [m for m in ms if kw in m.lower()]
        if not ms:
            await bot.send_message(chat_id=chat_id, text="没有符合的模型"); return
        ctx.user_data["dm_models"] = (apis[i]["name"], ms)
        await q.edit_message_text(f"「{apis[i]['name']}」的模型（{len(ms)} 个），点一个当杂活模型：",
                                  reply_markup=pager(ms, 0, "dmM", "dmP"))
    elif head == "dmT":
        P["aux_preset"] = not P.get("aux_preset"); save_state()
        await bot.send_message(chat_id=chat_id, text=aux_preset_tip())
    elif head == "dmP":
        _, ms = ctx.user_data.get("dm_models") or ("", [])
        await q.edit_message_reply_markup(pager(ms, int(arg), "dmM", "dmP"))
    elif head == "dmM":
        name, ms = ctx.user_data.get("dm_models") or ("", [])
        i = int(arg)
        if i < len(ms):
            P["aux"] = {"api": name, "model": ms[i]}; save_state()
            await q.edit_message_text(f"杂活模型已设为：{name} / {ms[i]}")
            t = PRO["roster_task"]
            if "roster" not in plan and not (t and not t.done()):
                await bot.send_message(chat_id=chat_id, text="💌 开始挑今天的名单（第一次要逐张判断是不是多角色卡，会久一点），好了会发给你")
                PRO["roster_task"] = asyncio.create_task(roster_job(bot, chat_id, show=True))


# ---------------------------------------------------------------- handlers
HELP = ("聊天都在酒馆里：各设备、网页、TG 是同一份，TG 里打开一张卡就接着酒馆里最近的那个聊天\n\n"
        "聊天\n/chars 角色列表（/chars 关键词 搜索；/chars 全部 连多人大卡一起列）\n/new 新开聊天\n"
        "/chats 这张卡在酒馆里的所有聊天\n"
        "/regen 重新生成（旧的保留，用 ◀ ▶ 翻）\n/continue 继续写（回复被截断时用）\n/undo 撤回上一轮\n/stop 停止生成\n\n"
        "改消息（回复某条消息再发命令；不回复就是最后一条）\n/edit 新内容  改写\n/raw 拿原文（方便复制改）\n"
        "/del 删除这条\n/branch 从这条开分支\n你自己发的消息直接在 TG 里编辑就会同步\n\n"
        "酒馆设置（直接改酒馆本身，手机上也跟着变）\n/preset 切换预设\n/entries 看条目开关（/entries 关键词）\n"
        "/toggle 关键词  开关条目\n/wi 世界书开关（/wi 关键词 搜索）\n/note 作者注释\n"
        "/api 切换 API / 中转站（酒馆里存的密钥）\n/model 切换模型\n\n"
        "插图\n/img 插图设置（/img 开、/img 关、/img 测试）\n\n"
        "主动消息\n/dm 今天谁会来找你（名单、杂活模型）\n/dm 来  随机叫一个人来找你\n\n"
        "其他\n/stream 流式显示开关（只影响 TG 这边）\n/status 当前状态")


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    await update.effective_message.reply_text(NOTICE + "\n\n酒馆 TG 版\n\n" + HELP)


def pager(labels, page, pick, nav, per=10):
    total = max(1, (len(labels) + per - 1) // per)
    page = max(0, min(page, total - 1))
    rows = [[Btn(lb[:60], callback_data=f"{pick}:{i}")]
            for i, lb in list(enumerate(labels))[page * per:(page + 1) * per]]
    navr = []
    if page > 0: navr.append(Btn("◀", callback_data=f"{nav}:{page - 1}"))
    navr.append(Btn(f"{page + 1}/{total}", callback_data="noop"))
    if page < total - 1: navr.append(Btn("▶", callback_data=f"{nav}:{page + 1}"))
    if total > 1: rows.append(navr)
    return InlineKeyboardMarkup(rows)


def recent_first(items, recent, key):
    pos = {k: i for i, k in enumerate(recent)}
    return sorted(items, key=lambda x: pos.get(key(x), 10 ** 6))


def push_recent(name, val, n=10):
    r = [x for x in STATE.get(name, []) if x != val]
    STATE[name] = [val] + r[:n - 1]


def char_labels(files):
    cache = rjson(PRO_CARDS_F, {}) or {}
    out = []
    for f in files:
        c = get_card(f)
        nm = c["name"] if c else f
        r = cache.get(f) or {}
        mark = (" 👥" if is_small(r) else " 👥大") if r.get("type") == "multi" else ""   # 多人小卡 / 多人大卡
        out.append(("✅ " if f == STATE.get("char") else "") + nm + mark)
    return out


def preset_labels(ps):
    cur = st_cur_preset()
    return [("✅ " if n == cur else "") + n for n in ps]


async def cmd_chars(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """角色列表。默认只列单人卡和多人小卡（多人大卡/世界观卡藏起来）；/chars 全部 列全部"""
    if not allowed(update): return
    q = " ".join(ctx.args).strip()
    m = re.match(r"(全部|all)(\s+|$)", q, re.I)
    show_all = bool(m)
    q = (q[m.end():] if m else q).strip().lower()
    cards = list_cards()
    if q:
        cards = [c for c in cards if q in c["name"].lower() or q in c["file"].lower()]
    hidden = 0
    if not show_all:
        cache = rjson(PRO_CARDS_F, {}) or {}
        keep = []
        for c in cards:
            r = cache.get(c["file"])
            # 当前角色、还没判断过的卡（新卡、改过的卡）照常显示
            if c["file"] == STATE.get("char") or not cache_ok(r, card_sig(c)) or can_call(r):
                keep.append(c)
            else:
                hidden += 1
        cards = keep
    more = f"/chars 全部 {q}".strip()
    if not cards:
        await update.message.reply_text("没找到角色" + (f"（有 {hidden} 张多人大卡/世界观卡没列出，{more} 能找到）" if hidden else ""))
        return
    cards = recent_first(cards, STATE.get("recent_chars", []), lambda c: c["file"])
    files = [c["file"] for c in cards]
    ctx.user_data["list"] = files
    tips = []
    if hidden:
        tips.append(f"藏起了 {hidden} 张多人大卡/世界观卡，{more} 看全部")
    if not q:
        tips.append("最近用过的排在前面；/chars 关键词 搜索")
    tips.append("👥 = 多人小卡" + ("，👥大 = 多人大卡" if show_all else ""))
    await update.message.reply_text(f"共 {len(cards)} 个角色（{'；'.join(tips)}）：",
                                    reply_markup=pager(char_labels(files), 0, "c", "cp"))


async def select_char(bot, chat_id, fname, chat_name=None):
    card = get_card(fname)
    if not card:
        await bot.send_message(chat_id=chat_id, text="角色卡读取失败"); return
    old = cur_card()
    if old:
        oc = load_chat(old)
        if oc and oc["messages"]: await drop_kb(bot, chat_id, oc["messages"][-1])
    STATE["char"] = fname; push_recent("recent_chars", fname); save_state()
    try:
        async with LOCK:
            chat = await pull_chat(card, chat_name)
    except Exception as e:
        log.exception("打开角色")
        await bot.send_message(chat_id=chat_id, text=f"打开「{card['name']}」失败：{err_text(e)[:500]}"); return
    if not chat["messages"]:
        await bot.send_message(chat_id=chat_id, text=f"（{card['name']} 没有开场白，直接说话吧）"); return
    tip = "这个聊天是 TA 来搭讪时新开的" if chat_name else "接着酒馆里最近的聊天"
    await bot.send_message(chat_id=chat_id,
                           text=f"已切换到「{card['name']}」，{tip}「{chat['id']}」（{len(chat['messages'])} 条）。"
                                f"/new 新开，/chats 换别的聊天。")
    await show_msg(bot, chat_id, card, chat, len(chat["messages"]) - 1, new=True)


async def start_new(bot, chat_id, card):
    """在酒馆里给这张卡新开一个聊天（要在 LOCK 里调用）"""
    await pull_chat(card)
    name = await SB.call("newChat")
    chat = await pull_chat(card, name)
    if not chat["messages"]:
        await bot.send_message(chat_id=chat_id, text=f"（{card['name']} 没有开场白，直接说话吧）"); return
    await show_msg(bot, chat_id, card, chat, 0, new=True)


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    card = cur_card()
    if not card:
        await update.message.reply_text("先用 /chars 选角色"); return
    if LOCK.locked():
        await update.message.reply_text("还在生成中，稍等"); return
    try:
        async with LOCK:
            old = load_chat(card)
            if old and old["messages"]: await drop_kb(ctx.bot, update.effective_chat.id, old["messages"][-1])
            await start_new(ctx.bot, update.effective_chat.id, card)
    except Exception as e:
        log.exception("new"); await update.message.reply_text(f"新开聊天失败：{err_text(e)[:500]}")


async def cmd_regen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    await regen(ctx.bot, update.effective_chat.id)


async def regen(bot, chat_id):
    card = cur_card()
    if not card:
        await bot.send_message(chat_id=chat_id, text="先用 /chars 选角色"); return
    if LOCK.locked():
        await bot.send_message(chat_id=chat_id, text="还在生成中，稍等"); return
    async with LOCK:
        try:
            chat = await pull_chat(card)
            if not chat["messages"]:
                await bot.send_message(chat_id=chat_id, text="没有可以重新生成的"); return
            last = chat["messages"][-1]
            if last["role"] == "assistant" and last.get("greeting"):
                await bot.send_message(chat_id=chat_id, text="还没开始聊。换开场白用 ◀ ▶"); return
            await st_gen(bot, chat_id, card, "swipe" if last["role"] == "assistant" else "new", "", chat=chat)
        except Exception as e:
            log.exception("regen"); await gen_error(bot, chat_id, e)


async def cmd_continue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    await cont(ctx.bot, update.effective_chat.id)


async def cont(bot, chat_id):
    """继续：接着最后一条回复往下写（被截断时用）"""
    card = cur_card()
    if not card:
        await bot.send_message(chat_id=chat_id, text="先用 /chars 选角色"); return
    if LOCK.locked():
        await bot.send_message(chat_id=chat_id, text="还在生成中，稍等"); return
    async with LOCK:
        try:
            chat = await pull_chat(card)
            if not chat["messages"]:
                await bot.send_message(chat_id=chat_id, text="没有可以继续的"); return
            if chat["messages"][-1]["role"] != "assistant":
                await bot.send_message(chat_id=chat_id, text="最后一条是你的消息，用 /regen 让角色回复"); return
            await st_gen(bot, chat_id, card, "continue", chat=chat)
        except Exception as e:
            log.exception("continue"); await gen_error(bot, chat_id, e)


async def do_swipe(update: Update, ctx, d):
    q = update.callback_query
    bot, chat_id = ctx.bot, update.effective_chat.id
    card = cur_card()
    chat = await chat_now(card) if card else None
    m = chat["messages"][-1] if chat and chat["messages"] else None
    if not m or m["role"] != "assistant" or q.message.message_id not in (m.get("tg") or []):
        await q.answer("只能翻最后一条回复"); return
    if LOCK.locked():
        await q.answer("还在生成中"); return
    n, i = len(swipes_of(m)), m.get("sid", 0) + d
    if m.get("greeting"):
        i %= n
    elif i < 0:
        await q.answer("已经是第一个了"); return
    elif i >= n:
        await q.answer()
        await regen(bot, chat_id); return
    await q.answer()
    async with LOCK:
        try:
            await pull_chat(card)
            await SB.call("swipeTo", i)
            chat = await pull_chat(card)
            await del_msgs(bot, chat_id, (m.get("tg") or []) + (m.get("img_tg") or []))   # 旧的删掉，新的发在最下面
            chat["messages"][-1]["tg"], chat["messages"][-1]["img_tg"] = [], []
            await show_msg(bot, chat_id, card, chat, len(chat["messages"]) - 1, new=True)
        except Exception as e:
            log.exception("swipe"); await bot.send_message(chat_id=chat_id, text=f"翻页失败：{err_text(e)[:300]}")


async def cmd_undo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    card = cur_card()
    if not card:
        await update.message.reply_text("先用 /chars 选角色"); return
    if LOCK.locked():
        await update.message.reply_text("还在生成中，稍等"); return
    async with LOCK:
        chat = await pull_chat(card)
        ms = chat["messages"]
        cut, j = [], len(ms) - 1
        if j >= 0 and ms[j]["role"] == "assistant" and not ms[j].get("greeting"):
            cut.append(j); j -= 1
        if j >= 0 and ms[j]["role"] == "user":
            cut.append(j)
        if not cut:
            await update.message.reply_text("没有可撤回的"); return
        await SB.call("cut", f"{min(cut)}-{max(cut)}" if len(cut) > 1 else str(cut[0]))
        gone = [ms[i] for i in cut]
        chat = await pull_chat(card)
        for m in gone: await del_msgs(ctx.bot, update.effective_chat.id, (m.get("tg") or []) + (m.get("img_tg") or []))
        await refresh_last_kb(ctx.bot, update.effective_chat.id, chat)
    await update.message.reply_text(f"已撤回上一轮，剩 {len(chat['messages'])} 条")


def pick_index(update, chat, role=None):
    """回复了哪条就是哪条；没回复就取最后一条（可限定角色）"""
    r = update.effective_message.reply_to_message
    if r:
        return find_by_tg(chat, r.message_id)
    for i in range(len(chat["messages"]) - 1, -1, -1):
        if role is None or chat["messages"][i]["role"] == role:
            return i
    return None


async def need_chat(update):
    """拉回当前聊天（要在 LOCK 里调用）"""
    card = cur_card()
    if not card:
        await update.effective_message.reply_text("先用 /chars 选角色"); return None, None
    chat = await pull_chat(card)
    if not chat["messages"]:
        await update.effective_message.reply_text("现在没有聊天"); return None, None
    return card, chat


async def cmd_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    new = cmd_arg(update)
    if not new:
        await update.message.reply_text("用法：回复要改的那条消息，发 /edit 新内容\n（不回复就是改最后一条回复。先用 /raw 拿原文复制过来改最方便）")
        return
    if LOCK.locked():
        await update.message.reply_text("还在生成中，稍等"); return
    async with LOCK:
        card, chat = await need_chat(update)
        if not chat: return
        idx = pick_index(update, chat, "assistant")
        if idx is None:
            await update.message.reply_text("这条消息不在当前聊天里"); return
        role = chat["messages"][idx]["role"]
        await SB.call("edit", idx, new)
        chat = await pull_chat(card)
        await del_msgs(ctx.bot, update.effective_chat.id, [update.message.message_id])
        if role == "assistant":
            await show_msg(ctx.bot, update.effective_chat.id, card, chat, idx)
        else:
            await ctx.bot.send_message(chat_id=update.effective_chat.id,
                                       text="✏️ 已改好（TG 里你那条原消息的显示不会变，以酒馆里为准）")


async def cmd_raw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    card = cur_card()
    if LOCK.locked():
        chat = await chat_now(card) if card else None          # 正在生成：用上次拉到的
        if not chat or not chat["messages"]:
            await update.message.reply_text("现在没有聊天"); return
    else:
        async with LOCK:
            card, chat = await need_chat(update)
        if not chat: return
    idx = pick_index(update, chat, "assistant")
    if idx is None:
        await update.message.reply_text("这条消息不在当前聊天里"); return
    await send_pre(ctx.bot, update.effective_chat.id, chat["messages"][idx]["content"])


async def cmd_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    if not update.message.reply_to_message:
        await update.message.reply_text("回复要删的那条消息，再发 /del"); return
    if LOCK.locked():
        await update.message.reply_text("还在生成中，稍等"); return
    async with LOCK:
        card, chat = await need_chat(update)
        if not chat: return
        idx = pick_index(update, chat)
        if idx is None:
            await update.message.reply_text("这条消息不在当前聊天里"); return
        m = chat["messages"][idx]
        await SB.call("cut", str(idx))
        chat = await pull_chat(card)
        await del_msgs(ctx.bot, update.effective_chat.id, (m.get("tg") or []) + (m.get("img_tg") or []) + [update.message.message_id])
        await refresh_last_kb(ctx.bot, update.effective_chat.id, chat)


async def cmd_branch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    if not update.message.reply_to_message:
        await update.message.reply_text("回复想从哪条开始分支的那条消息，再发 /branch"); return
    if LOCK.locked():
        await update.message.reply_text("还在生成中，稍等"); return
    async with LOCK:
        card, chat = await need_chat(update)
        if not chat: return
        idx = pick_index(update, chat)
        if idx is None:
            await update.message.reply_text("这条消息不在当前聊天里"); return
        await drop_kb(ctx.bot, update.effective_chat.id, chat["messages"][-1])
        try:
            name = await SB.call("branch", idx)
            nc = await pull_chat(card, name)
        except Exception as e:
            await update.message.reply_text(f"开分支失败：{err_text(e)[:300]}"); return
        if nc["messages"] and nc["messages"][-1]["role"] == "assistant":
            await update.message.reply_text(f"🌿 已从第 {idx + 1} 条开出分支「{name}」，原来的聊天还在 /chats 里")
            await show_msg(ctx.bot, update.effective_chat.id, card, nc, len(nc["messages"]) - 1, new=True)
        else:
            await update.message.reply_text(f"🌿 已从第 {idx + 1} 条（你的消息）开出分支「{name}」，原来的聊天还在 /chats 里。"
                                            f"发 /regen 让角色接着回")


async def cmd_chats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    card = cur_card()
    if not card:
        await update.message.reply_text("先用 /chars 选角色"); return
    lst = st_chat_list(card)
    if not lst:
        await update.message.reply_text(f"「{card['name']}」在酒馆里还没有聊天"); return
    cur = (load_chat(card) or {}).get("id") or card.get("chat") or ""
    names = [nm for _, nm, _ in lst]
    labels = [("✅ " if nm == cur else "") + f"{fmt_ts(mt)} · {size_text(sz)} · {nm}" for mt, nm, sz in lst]
    ctx.user_data["hist"] = (names, labels)
    await update.message.reply_text(f"「{card['name']}」在酒馆里的聊天（{len(names)} 个，最近改过的在前，✅ = 现在这个），点一个切过去：",
                                    reply_markup=pager(labels, 0, "h", "hp", per=8))


async def restore_chat(bot, chat_id, name):
    card = cur_card()
    if not card or not st_chat_file(card, name).exists():
        await bot.send_message(chat_id=chat_id, text="找不到这个聊天了"); return
    async with LOCK:
        old = load_chat(card)
        if old and old["messages"]: await drop_kb(bot, chat_id, old["messages"][-1])
        try:
            chat = await pull_chat(card, name)
        except Exception as e:
            await bot.send_message(chat_id=chat_id, text=f"切换聊天失败：{err_text(e)[:300]}"); return
        await bot.send_message(chat_id=chat_id, text=f"已切到聊天「{name}」（{len(chat['messages'])} 条）")
        if chat["messages"]:
            await show_msg(bot, chat_id, card, chat, len(chat["messages"]) - 1, new=True)


async def cmd_preset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    q = " ".join(ctx.args).strip().lower()
    ps = list_presets()
    if q:
        ps = [n for n in ps if q in n.lower()]
    if not ps:
        await update.message.reply_text("没找到预设"); return
    ps = recent_first(ps, [st_cur_preset()] + STATE.get("recent_presets", []), lambda n: n)
    ctx.user_data["presets"] = ps
    tip = "" if q else "（✅ = 酒馆现在用的；最近用过的排在前面；/preset 关键词 可搜索；切换会直接改酒馆）"
    await update.message.reply_text(f"共 {len(ps)} 个预设{tip}：", reply_markup=pager(preset_labels(ps), 0, "p", "pp"))


async def cmd_entries(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    q = cmd_arg(update).lower()
    pname = st_cur_preset()
    es = prompt_entries(pname)
    if q:
        es = [e for e in es if q in e["name"].lower()]
    if not es:
        await update.message.reply_text("没有条目" if not q else "没找到"); return
    lines = [("✅ " if e["on"] else "⬜ ") + e["name"] for e in es]
    await send_plain(ctx.bot, update.effective_chat.id,
                     f"预设「{pname}」的条目（酒馆里现在的开关；/toggle 关键词 开关）\n\n" + "\n".join(lines))


async def cmd_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    q = cmd_arg(update)
    if not q:
        await update.message.reply_text("用法：/toggle 关键词（名字带这个词的条目一起开或关，直接改酒馆里的开关；/entries 看全部条目）"); return
    es = prompt_entries(st_cur_preset())
    hit = [e for e in es if q.lower() in e["name"].lower()]
    if not hit:
        await update.message.reply_text("没有名字带这个词的条目，/entries 可以看全部条目"); return
    target = not all(e["on"] for e in hit)        # 有关着的就全开，全开着就全关
    if LOCK.locked():
        await update.message.reply_text("还在生成中，稍等"); return
    try:
        async with LOCK:
            await SB.fresh()
            await SB.call("toggle", [e["id"] for e in hit], target)
    except Exception as e:
        await update.message.reply_text(f"改条目开关失败：{err_text(e)[:300]}"); return
    await update.message.reply_text(("已打开" if target else "已关闭") + f" {len(hit)} 条（酒馆里也跟着变了）：\n" +
                                    "\n".join(("✅ " if target else "⬜ ") + e["name"] for e in hit))


async def cmd_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    card = cur_card()
    if not card:
        await update.message.reply_text("先用 /chars 选角色"); return
    arg = cmd_arg(update)
    m1 = re.fullmatch(r"(?:深度|depth)\s*(\d+)", arg)
    m2 = re.fullmatch(r"(?:频率|freq)\s*(\d+)", arg)
    if not arg: args, msg = (None, None, None), None
    elif arg in ("清空", "clear"): args, msg = ("", None, None), "作者注释已清空"
    elif m1: args, msg = (None, int(m1.group(1)), None), f"深度改成 {m1.group(1)}"
    elif m2: args, msg = (None, None, int(m2.group(1))), f"频率改成每 {m2.group(1)} 条（0 = 不插）"
    else: args, msg = (arg, None, None), "作者注释已设置（只对酒馆里当前这个聊天生效）"
    if LOCK.locked():
        await update.message.reply_text("还在生成中，稍等"); return
    try:
        async with LOCK:
            await pull_chat(card)
            r = await SB.call("note", *args)
    except Exception as e:
        await update.message.reply_text(f"作者注释操作失败：{err_text(e)[:300]}"); return
    if msg:
        await update.message.reply_text(msg); return
    await update.message.reply_text(
        f"作者注释（酒馆里这个聊天的；插在倒数第 {r.get('depth') if r.get('depth') is not None else '默认'} 条前，"
        f"每 {r.get('freq') if r.get('freq') is not None else '默认'} 条插一次）：\n"
        f"{r.get('text') or '（空）'}\n\n/note 内容  设置\n/note 清空\n/note 深度 4\n/note 频率 1")










def model_labels(ms):
    cm = cur_model()
    return [("✅ " if m == cm else "") + m for m in ms]


async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    a = cur_api()
    try:
        ms = await list_models(a)
    except Exception as e:
        await update.message.reply_text(f"拉取模型列表失败：{e}"[:1000]); return
    raw = " ".join(ctx.args).strip()
    q, full = raw.lower(), list(ms)
    if q:
        ms = [m for m in ms if q in m.lower()]
    ms = recent_first(ms, [cur_model()], lambda m: m)
    if raw and raw not in full:
        ms = [raw] + ms                         # 列表里没有也能用：直接用你打的这个名字
    if not ms:
        await update.message.reply_text("用法：/model 关键词（搜索），或者 /model 完整模型名（列表里没有也能直接用）"); return
    ctx.user_data["models"] = ms
    labels = model_labels(ms)
    if raw and raw not in full:
        labels[0] = f"✍️ 直接用「{raw}」"
    await update.message.reply_text(f"API「{a['name']}」共 {len(ms)} 个模型（/model 关键词 搜索；/model 完整名字 列表里没有也能用）：",
                                    reply_markup=pager(labels, 0, "m", "mp"))


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    await do_stop(ctx.bot, update.effective_chat.id)


async def do_stop(bot, chat_id):
    try:
        s = await SB.call("status", timeout=20)
    except Exception:
        s = {"busy": LOCK.locked()}
    if not s.get("busy"):
        await bot.send_message(chat_id=chat_id, text="现在没有在生成"); return False
    await SB.call("stop")
    return True


async def cmd_stream(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    STATE["stream"] = not STATE.get("stream", CFG.get("stream", True)); save_state()
    await update.message.reply_text("流式输出：" + ("开" if STATE["stream"] else "关"))


API_TIP = ("选 API / 中转站：\n🔑 = 酒馆里存的 Custom 密钥（备注写的地址），在酒馆里加、删密钥，这里自动跟着变；"
           "🍺 = 酒馆现在选中的那条\n切过去时，原来的模型这家也有就接着用，没有就让你选\n"
           "手动添加：/addapi 名字 地址 key · 删除：/delapi 名字")


def api_menu(ctx):
    apis = get_apis(); cur = cur_api()["name"]
    ctx.user_data["apis"] = [a["name"] for a in apis]
    rows = [[Btn((("✅ " if a["name"] == cur else "") + ("🔑 " if a.get("st_key_id") is not None else "")
                  + ("🍺 " if a.get("st_active") else "") + a["name"])[:60], callback_data=f"a:{i}")]
            for i, a in enumerate(apis)]
    return InlineKeyboardMarkup(rows)


async def cmd_api(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    await update.message.reply_text(API_TIP, reply_markup=api_menu(ctx))


async def switch_api(bot, chat_id, ctx, name):
    """切酒馆的 API：原来的模型新的这家也有就沿用；没有就用这家上次选的；都没有就弹模型列表"""
    a = next((x for x in get_apis() if x["name"] == name), None)
    if not a:
        await bot.send_message(chat_id=chat_id, text="这个 API 不在了（酒馆里删了密钥？），再发 /api 看看"); return
    if a["type"] != "vertex" and a.get("st_key_id") is None:
        await bot.send_message(chat_id=chat_id, text=f"「{name}」是 bot 自己存的（apis.json / config.json），酒馆里没有它的密钥，"
                                                     "只能给杂活用（/dm 杂活）。要拿来聊天，请在酒馆里把它加成 Custom 密钥，备注写地址。"); return
    if LOCK.locked():
        await bot.send_message(chat_id=chat_id, text="还在生成中，稍等"); return
    old = cur_model()
    try:
        async with LOCK:
            await SB.fresh()
            if a["type"] == "vertex":
                await SB.call("setApi", "vertexai", "", "")
            else:
                await SB.call("setApi", "custom", a["base_url"], a["st_key_id"])
    except Exception as e:
        await bot.send_message(chat_id=chat_id, text=f"切换失败：{err_text(e)[:300]}"); return
    STATE["api"] = name; save_state()
    saved = (STATE.get("models") or {}).get(name)
    try:
        ms = await list_models(a)
    except Exception as e:
        await bot.send_message(chat_id=chat_id, text=f"酒馆的 API 已切换：{name}\n⚠️ 拉不到它的模型列表（{err_text(e)[:200]}），"
                                                     f"这家可能也连不上。现在的模型：{cur_model() or '没选'}"); return
    pick = old if old and old in ms else (saved if saved and saved in ms else None)
    if pick:
        try:
            async with LOCK:
                await SB.call("setModel", pick)
        except Exception as e:
            await bot.send_message(chat_id=chat_id, text=f"API 切好了，但设置模型失败：{err_text(e)[:300]}"); return
        STATE.setdefault("models", {})[name] = pick; save_state()
        await bot.send_message(chat_id=chat_id, text=f"酒馆的 API 已切换：{name}，模型用 {pick}"); return
    if not ms:
        await bot.send_message(chat_id=chat_id, text=f"酒馆的 API 已切换：{name}，但它没返回任何模型"); return
    ms = recent_first(ms, [m for m in (old, saved) if m], lambda m: m)
    ctx.user_data["models"] = ms
    await bot.send_message(chat_id=chat_id, text=f"酒馆的 API 已切换：{name}。" + (f"这家没有 {old}，" if old else "")
                           + f"选一个模型（共 {len(ms)} 个，/model 关键词 可搜索）：",
                           reply_markup=pager(model_labels(ms), 0, "m", "mp"))


API_KB = InlineKeyboardMarkup([[Btn("🔀 换 API / 中转站", callback_data="apis")]])


async def gen_error(bot, chat_id, e, tail=""):
    """生成出错：连不上中转站时说清楚是哪家，附一个换 API 的按钮"""
    if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)):
        await bot.send_message(chat_id=chat_id, text=f"❌ 连不上「{cur_api()['name']}」（{err_text(e)[:200]}），这家可能挂了，换一个试试。{tail}",
                               reply_markup=API_KB)
    else:
        await bot.send_message(chat_id=chat_id, text=f"出错了：{e}"[:3000] + tail)


async def cmd_addapi(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    args = ctx.args
    try:
        await update.message.delete()  # 消息里有 key，删掉
    except Exception:
        pass
    if len(args) < 3:
        await update.effective_chat.send_message("用法：/addapi 名字 地址 key\n例：/addapi 中转A https://xxx.com/v1 sk-xxxx")
        return
    name, url, key = args[0], args[1], args[2]
    if name == "vertex":
        await update.effective_chat.send_message("vertex 这个名字被占用了，换一个"); return
    saved = [a for a in (rjson(APIS_F, []) or []) if a["name"] != name]
    a = {"name": name, "type": "openai", "base_url": url}
    if key.startswith("st:"):
        a["key_from_st"] = key[3:]
    else:
        a["key"] = key
    saved.append(a)
    wjson(APIS_F, saved); os.chmod(APIS_F, 0o600)
    _model_cache.pop(name, None)
    await update.effective_chat.send_message(f"已添加「{name}」（含 key 的那条消息已删除）。\n"
                                             "注意：聊天现在用的是酒馆自己的 API 设置，bot 里加的这个只能当杂活模型（/dm 杂活）。"
                                             "要拿来聊天，请在酒馆里加成 Custom 密钥，备注写地址。")


async def cmd_delapi(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    name = " ".join(ctx.args).strip()
    saved = rjson(APIS_F, []) or []
    left = [a for a in saved if a["name"] != name]
    if len(left) == len(saved):
        await update.message.reply_text("没有这个名字（config.json 里写的要去文件里删）"); return
    wjson(APIS_F, left)
    if STATE.get("api") == name: STATE["api"] = "vertex"; save_state()
    await update.message.reply_text(f"已删除「{name}」")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    card = cur_card()
    chat = await chat_now(card) if card else None
    try:
        s = await SB.call("status", timeout=20)
        page = ("正在生成" if s["busy"] else "空闲") + (f"，停在聊天「{s['chat']}」" if s.get("chat") else "")
    except Exception as e:
        page = f"连不上（{err_text(e)[:120]}）"
    note = ((chat or {}).get("note") or {}).get("text")
    lines = [
        f"角色：{card['name'] if card else '未选'}",
        f"聊天：{chat['id']}（{len(chat['messages'])} 条）" if chat else "聊天：还没打开",
        f"预设：{st_cur_preset() or '无'}",
        f"API：{cur_api()['name']}\n模型：{cur_model() or '没选'}",
        f"生效世界书：{len(base_books(card))} 本（全局 + 卡绑定的）",
        f"插图：{'开' if img_on() else '关'} · 作者注释：{'有' if note else '无'}",
        f"主动消息：{'开' if pro()['on'] else '关'}（下一次约 {next_text()}，/dm 查看）",
        f"酒馆页面：{page}",
        "", NOTICE,
    ]
    await update.message.reply_text("\n".join(lines))


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    q = update.callback_query
    data = q.data or ""
    bot, chat_id = ctx.bot, update.effective_chat.id
    if q.message and q.message.message_id in EPHEMERAL and CFG.get("auto_clean", True):
        forget(bot, chat_id, q.message.message_id, msg_life("", True))   # 菜单：有人点就重新计时
    if data.startswith("dm"):
        await pro_callback(update, ctx, data); return
    if data.startswith("sw:"):
        await do_swipe(update, ctx, int(data[3:])); return
    if data.startswith("opt:"):
        await pick_option(update, ctx, int(data[4:])); return
    await q.answer()
    if data == "noop":
        return
    if data[:2] in ("c:", "p:", "m:", "a:", "h:") and CFG.get("auto_clean", True):
        forget(bot, chat_id, q.message.message_id, 0)          # 菜单点完就删
    if data.startswith("cp:"):
        files = ctx.user_data.get("list") or [c["file"] for c in list_cards()]
        await q.edit_message_reply_markup(pager(char_labels(files), int(data[3:]), "c", "cp"))
    elif data.startswith("pp:"):
        ps = ctx.user_data.get("presets") or list_presets()
        await q.edit_message_reply_markup(pager(preset_labels(ps), int(data[3:]), "p", "pp"))
    elif data.startswith("hp:"):
        names, labels = ctx.user_data.get("hist") or ([], [])
        await q.edit_message_reply_markup(pager(labels, int(data[3:]), "h", "hp", per=8))
    elif data.startswith("h:"):
        names, _ = ctx.user_data.get("hist") or ([], [])
        i = int(data[2:])
        if LOCK.locked():
            await bot.send_message(chat_id=chat_id, text="还在生成中，稍等"); return
        if i < len(names): await restore_chat(bot, chat_id, names[i])
    elif data.startswith("c:"):
        files = ctx.user_data.get("list") or [c["file"] for c in list_cards()]
        i = int(data[2:])
        if LOCK.locked():
            await bot.send_message(chat_id=chat_id, text="还在生成中，稍等"); return
        if i < len(files): await select_char(bot, chat_id, files[i])
    elif data.startswith("p:"):
        ps = ctx.user_data.get("presets") or list_presets()
        i = int(data[2:])
        if i < len(ps):
            if LOCK.locked():
                await bot.send_message(chat_id=chat_id, text="还在生成中，稍等"); return
            await q.message.reply_text(f"切换预设「{ps[i]}」中（大预设要几秒）…")
            try:
                async with LOCK:
                    await SB.fresh()
                    got = await SB.call("setPreset", ps[i])
            except Exception as e:
                await q.message.reply_text(f"切换预设失败：{err_text(e)[:300]}"); return
            push_recent("recent_presets", ps[i]); STATE["preset"] = got; save_state()
            await q.message.reply_text(f"酒馆的预设已切换：{got}（手机上也跟着变）")
    elif data.startswith("mp:"):
        ms = ctx.user_data.get("models") or []
        await q.edit_message_reply_markup(pager(model_labels(ms), int(data[3:]), "m", "mp"))
    elif data.startswith("m:"):
        ms = ctx.user_data.get("models") or []
        i = int(data[2:])
        if i < len(ms):
            if LOCK.locked():
                await bot.send_message(chat_id=chat_id, text="还在生成中，稍等"); return
            try:
                async with LOCK:
                    await SB.fresh()
                    await SB.call("setModel", ms[i])
            except Exception as e:
                await q.message.reply_text(f"切换模型失败：{err_text(e)[:300]}"); return
            STATE.setdefault("models", {})[cur_api()["name"]] = ms[i]; save_state()
            await q.message.reply_text(f"酒馆的模型已切换：{ms[i]}（API：{cur_api()['name']}）")
    elif data.startswith("a:"):
        names = ctx.user_data.get("apis") or [a["name"] for a in get_apis()]
        i = int(data[2:])
        if i < len(names):
            await switch_api(bot, chat_id, ctx, names[i])
    elif data == "apis":
        await bot.send_message(chat_id=chat_id, text=API_TIP, reply_markup=api_menu(ctx))
    elif data.startswith("w:") or data.startswith("wp:"):
        await wi_callback(q, ctx, data)
    elif data == "img":
        await redo_images(bot, chat_id)
    elif data == "regen":
        await regen(bot, chat_id)
    elif data == "cont":
        await cont(bot, chat_id)
    elif data == "stop":
        try:
            await SB.call("stop")
        except Exception as e:
            log.warning("停止失败：%s", e)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    await user_turn(ctx.bot, update.effective_chat.id, update.message.text, update.message.message_id)


async def user_turn(bot, chat_id, raw_text, tg_id=None, echo=False):
    """你说一句话 -> 发进酒馆 -> 酒馆生成回复。echo=True 时先把这句话发出来（选项按钮用）"""
    card = cur_card()
    if not card:
        await bot.send_message(chat_id=chat_id, text="先用 /chars 选角色"); return
    if LOCK.locked() and not PRO["busy"]:               # 主动消息在开聊天的话就等它一下（你的优先，它很快）
        await bot.send_message(chat_id=chat_id, text="上一条还在生成，稍等"); return
    async with LOCK:
        if echo:
            tok = KEEP.set(True)
            try:
                tg_id = (await bot.send_message(chat_id=chat_id, text="➡️ " + raw_text)).message_id
            finally:
                KEEP.reset(tok)
        try:
            await st_gen(bot, chat_id, card, "new", raw_text, tg_id)
        except Exception as e:
            log.exception("gen")
            await gen_error(bot, chat_id, e, "\n\n你的消息多半已经在酒馆里了，换好 API 后点 /regen 重试")


async def pick_option(update: Update, ctx, i):
    q = update.callback_query
    card = cur_card()
    chat = await chat_now(card) if card else None
    m = chat["messages"][-1] if chat and chat["messages"] else None
    if not m or m["role"] != "assistant" or q.message.message_id not in (m.get("tg") or []):
        await q.answer("只能选最后一条回复里的选项"); return
    opts = message_options(card, m, cur_preset())
    if i >= len(opts):
        await q.answer("这个选项不存在了"); return
    if LOCK.locked():
        await q.answer("还在生成中"); return
    await q.answer()
    await user_turn(ctx.bot, update.effective_chat.id, opts[i], echo=True)


async def on_edited(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """你在 TG 里编辑了自己发过的消息 -> 同步改酒馆里的那条"""
    if not allowed(update): return
    em = update.edited_message
    card = cur_card()
    chat = await chat_now(card) if card else None
    if not chat or not em or not em.text:
        return
    idx = find_by_tg(chat, em.message_id)
    if idx is None or chat["messages"][idx]["role"] != "user":
        return
    try:
        async with LOCK:
            chat = await pull_chat(card)
            idx = find_by_tg(chat, em.message_id)
            if idx is None:
                return
            await SB.call("edit", idx, em.text)
            chat = await pull_chat(card)
    except Exception as e:
        await ctx.bot.send_message(chat_id=update.effective_chat.id, text=f"同步修改失败：{err_text(e)[:300]}"); return
    recent = idx >= len(chat["messages"]) - 2
    kb = InlineKeyboardMarkup([[Btn("🔄 按新内容重新生成", callback_data="regen")]]) if recent else None
    await ctx.bot.send_message(chat_id=update.effective_chat.id, text="✏️ 已同步修改（酒馆里也改了）", reply_markup=kb,
                               reply_to_message_id=em.message_id)


async def post_init(app: Application):
    start_health()
    install_ephemeral()
    if STATS["restart_reason"]:
        log.warning("上次重启原因：%s", STATS["restart_reason"])
    asyncio.get_running_loop().create_task(stall_watch(app))
    asyncio.get_running_loop().create_task(pro_loop(app))
    asyncio.get_running_loop().create_task(scan_loop())
    threading.Thread(target=loop_guard, args=(asyncio.get_running_loop(),), daemon=True).start()
    await app.bot.set_my_commands([
        BotCommand("chars", "角色列表/搜索"), BotCommand("new", "新开聊天"), BotCommand("chats", "酒馆里的聊天"),
        BotCommand("regen", "重新生成"), BotCommand("continue", "继续写（被截断时）"), BotCommand("undo", "撤回上一轮"), BotCommand("stop", "停止生成"),
        BotCommand("edit", "改写（回复某条）"), BotCommand("raw", "拿原文（回复某条）"),
        BotCommand("del", "删除（回复某条）"), BotCommand("branch", "开分支（回复某条）"),
        BotCommand("preset", "切换预设"), BotCommand("entries", "预设条目开关"), BotCommand("toggle", "开关条目"),
        BotCommand("wi", "世界书开关"), BotCommand("img", "插图设置/测试"),
        BotCommand("note", "作者注释"), BotCommand("dm", "主动消息（谁会来找你）"),
        BotCommand("api", "切换 API/中转"), BotCommand("model", "切换模型"),
        BotCommand("stream", "流式显示开/关"), BotCommand("status", "当前状态"), BotCommand("help", "帮助")])


def main():
    app = (Application.builder().token(CFG["token"]).concurrent_updates(True).post_init(post_init)
           .connection_pool_size(64).connect_timeout(15).read_timeout(30).write_timeout(60).pool_timeout(15)
           .get_updates_connect_timeout(15).get_updates_read_timeout(40).get_updates_pool_timeout(15)
           .build())
    cmds = {"chars": cmd_chars, "new": cmd_new, "chats": cmd_chats, "regen": cmd_regen, "continue": cmd_continue, "undo": cmd_undo,
            "edit": cmd_edit, "raw": cmd_raw, "del": cmd_del, "branch": cmd_branch,
            "preset": cmd_preset, "entries": cmd_entries, "toggle": cmd_toggle, "note": cmd_note,
            "wi": cmd_wi, "img": cmd_img, "model": cmd_model, "api": cmd_api,
            "stream": cmd_stream, "stop": cmd_stop, "dm": cmd_dm, "addapi": cmd_addapi, "delapi": cmd_delapi, "status": cmd_status}
    app.add_handler(TypeHandler(Update, log_update), group=-1)
    app.add_handler(CommandHandler(["start", "help"], tracked("/help", cmd_start), filters=filters.UpdateType.MESSAGE))
    for name, fn in cmds.items():
        app.add_handler(CommandHandler(name, tracked("/" + name, fn), filters=filters.UpdateType.MESSAGE))
    app.add_handler(CallbackQueryHandler(tracked("按钮", on_callback)))
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND, tracked("聊天消息", on_text)))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.TEXT & ~filters.COMMAND,
                                   tracked("编辑消息", on_edited)))
    app.add_error_handler(on_error)
    for ln in NOTICE.split("\n"):
        log.warning(ln)
    log.info("start（转发器）: %d cards, preset=%s, api=%s, model=%s", len(list_cards()), st_cur_preset(),
             cur_api()["name"], cur_model())
    # 重启期间发来的消息不丢（卡死自动重启时也能接着处理）
    app.run_polling(drop_pending_updates=False, allowed_updates=["message", "edited_message", "callback_query"])




def card_mtime(card):
    try:
        return os.stat(ST / "characters" / card["file"]).st_mtime
    except OSError:
        return 0.0


def st_chat_dir(card):
    return ST / "chats" / Path(card["file"]).stem


def st_chat_file(card, name=None):
    name = (card.get("chat") or "") if name is None else name
    return st_chat_dir(card) / f"{name}.jsonl" if name else None


def st_chat_list(card):
    """这张卡在酒馆里的所有聊天 [(修改时间, 名字, 大小)]，最近的在前"""
    out = []
    try:
        with os.scandir(st_chat_dir(card)) as it:
            for e in it:
                if e.name.endswith(".jsonl"):
                    s = e.stat()
                    out.append((s.st_mtime, e.name[:-6], s.st_size))
    except OSError:
        pass
    return sorted(out, reverse=True)


def st_tail(path, k=4, nbytes=262144):
    """只读聊天文件的结尾几条（大聊天不整个读）"""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - nbytes))
            data = f.read()
    except OSError:
        return []
    lines = data.split(b"\n")
    if size > nbytes:
        lines = lines[1:]
    out = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            j = json.loads(ln)
        except Exception:
            continue
        if not isinstance(j, dict) or "mes" not in j or j.get("is_system"):
            continue
        out.append({"role": "user" if j.get("is_user") else "assistant", "content": j.get("mes") or ""})
    return out[-k:] if k else out


def tgmap():
    return STATE.setdefault("tgmap", {})


def st_to_chat(card, d):
    """页面给的聊天 -> bot 以前用的格式；TG 消息 id、插图这些从 tgmap 里按「谁发的|发送时间」对回去"""
    if d.get("avatar") and d["avatar"] != card["file"]:
        raise RuntimeError(f"酒馆页面停在了别的卡上（{d['avatar']}），没切过来")
    mp = tgmap().get(f"{card['file']}|{d.get('id') or ''}") or {}
    msgs, seen = [], {}
    for i, x in enumerate(d.get("msgs") or []):
        fp0 = f"{int(bool(x.get('u')))}|{x.get('d') or ''}"
        k = seen.get(fp0, 0); seen[fp0] = k + 1
        fp = fp0 if k == 0 else f"{fp0}#{k}"
        ent = mp.get(fp) or {}
        n = max(1, int(x.get("n") or 1)); sid = min(max(0, int(x.get("sid") or 0)), n - 1)
        sw = [""] * n; sw[sid] = x.get("mes") or ""
        m = {"role": "user" if x.get("u") else "assistant", "content": x.get("mes") or "",
             "reasoning": x.get("r") or "", "swipes": sw, "sid": sid, "fp": fp, "tg": list(ent.get("tg") or [])}
        if x.get("s"):
            m["hidden"] = True
        for kk in ("imgs", "img_tg", "img_sid"):
            if kk in ent:
                m[kk] = ent[kk]
        if i == 0 and not x.get("u"):
            m["greeting"] = True
        msgs.append(m)
    return {"id": d.get("id") or "", "char": card["file"], "messages": msgs, "vars": d.get("vars") or {},
            "note": d.get("note") or {}}


async def pull_chat(card, name=None):
    """页面切到这张卡（name=打开哪个聊天，默认酒馆里这张卡最近的那个），拉回整段聊天"""
    card = get_card(card["file"]) or card
    await SB.fresh()
    want = (card.get("chat") or "") if name is None else name
    if want and not st_chat_file(card, want).exists():
        want = ""
    await SB.ensure(card["file"], want, card_mtime(card))
    chat = st_to_chat(card, await SB.call("getChat"))
    CHAT_CACHE[card["file"]] = chat
    return chat


def pend_pop(card, chat):
    """你在搭讪开的那个聊天里回了话：它就是正经聊天了，以后不会被删"""
    pend = pro().get("pending") or {}
    x = pend.get(card["file"])
    if x and x.get("chat") == chat.get("id"):
        pend.pop(card["file"], None); save_state()


async def st_gen(bot, chat_id, card, mode, text="", tg_id=None, chat=None):
    """让酒馆生成。mode：new=发 text（空 = 让角色接着回最后一条）/ swipe=重新生成一个候选 / continue=继续写。
    边轮询边把正在写的内容显示到 TG"""
    bump_img()
    chat = chat or await pull_chat(card)
    ms = chat["messages"]
    n0 = len(ms)
    target = ms[-1] if mode in ("swipe", "continue") and ms else None
    if mode == "new" and ms:
        await drop_kb(bot, chat_id, ms[-1])
    old = (list(target.get("tg") or []) + list(target.get("img_tg") or [])) if target else []
    ids, dropped = [], not old            # 重新生成/继续：旧的那几条等新内容出字时删掉，新的发在最下面
    if target and target.get("tg"):
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=target["tg"][-1], reply_markup=STOP_KB)
        except Exception:
            pass
    s0 = await SB.call("status")
    if s0["busy"]:
        raise RuntimeError("酒馆页面正在生成别的东西（在网页上点了生成？），等它写完再试")
    if mode == "new":
        await SB.call("send", text or "")
        pend_pop(card, chat)
    elif mode == "swipe":
        await SB.call("regen")
    else:
        await SB.call("cont")
    log.info("酒馆生成 %s char=%s chat=%s", mode, card["name"], chat["id"])
    expect = n0 + (1 if (mode == "new" and text) else 0)       # 回复会出现在第几条（从 0 数）
    stream_on = STATE.get("stream", CFG.get("stream", True))
    interval = float(CFG.get("stream_interval", 1.5))
    preset = cur_preset()
    rules = tag_rules(card, preset)
    typing = asyncio.create_task(typing_loop(bot, chat_id))
    wait_msg = None
    if not stream_on and not target:
        wait_msg = await bot.send_message(chat_id=chat_id, text="生成中…", reply_markup=STOP_KB)
    cache = {"shown": []}
    started, prev, fails, s = False, None, 0, s0
    t0 = time.monotonic()
    try:
        while True:
            await asyncio.sleep(interval)
            el = time.monotonic() - t0
            try:
                s = await SB.call("status", timeout=60); fails = 0
            except Exception as e:
                fails += 1
                log.warning("读酒馆状态失败（%d）：%s", fails, e)
                if fails >= 20:
                    raise RuntimeError(f"一直读不到酒馆页面的状态：{err_text(e)[:200]}")
                continue
            beat()
            new_err = s.get("err") if s.get("errAt", 0) > s0.get("errAt", 0) else ""
            if s["boot"] != s0["boot"] or s["busy"] or s["gen"] > s0["gen"]:
                started = True                               # 页面中途刷新了也算开始过
            if not started:
                if new_err and el > 20:
                    raise RuntimeError(f"酒馆报错：{new_err}")
                if el > 240:
                    raise RuntimeError("酒馆 4 分钟都没开始生成" + (f"：{new_err}" if new_err else ""))
                continue
            L = s["last"]
            has = (not L["u"]) and (s["len"] > expect if mode == "new" else s["len"] >= n0)
            if stream_on and s["busy"] and has and (L["mes"] or L["r"]):
                typing.cancel()
                if not dropped:
                    await del_msgs(bot, chat_id, old); dropped = True
                tmp = {"role": "assistant", "content": L["mes"], "reasoning": L["r"]}
                try:
                    ids = await sync_msgs(bot, chat_id, ids, render_chunks(display_text(card, tmp, preset, streaming=True),
                                                                           rules), STOP_KB, cache)
                except Exception as e:
                    log.warning("流式显示失败：%s", e)
            if s["press"] or (s["busy"] and s["ended"] < s["gen"]):
                prev = None
                if el > int(CFG.get("gen_max_sec", 1200)):
                    raise RuntimeError(f"生成超过 {int(CFG.get('gen_max_sec', 1200)) // 60} 分钟还没结束")
                continue
            sig = (s["len"], L["mes"], L["sid"], L["u"])
            if sig == prev:
                break                                        # 停下来了，并且连着两次没变化
            prev = sig
    finally:
        typing.cancel()
        if wait_msg:
            await del_msgs(bot, chat_id, [wait_msg.message_id])
    chat = await pull_chat(card)
    ms = chat["messages"]
    if mode == "new" and tg_id and len(ms) > n0 and ms[n0]["role"] == "user":
        ms[n0]["tg"] = [tg_id]
    new_err = s.get("err") if s.get("errAt", 0) > s0.get("errAt", 0) else ""
    ok = bool(ms) and ms[-1]["role"] == "assistant" and (len(ms) > expect if mode == "new" else len(ms) >= n0) \
        and bool((ms[-1]["content"] or "").strip() or ms[-1].get("reasoning"))
    if not ok:
        await del_msgs(bot, chat_id, ids)
        if dropped and target and ms:
            ms[-1]["tg"], ms[-1]["img_tg"] = [], []
        save_chat(card, chat)
        if target and ms:
            await show_msg(bot, chat_id, card, chat, len(ms) - 1)
        raise RuntimeError("酒馆没写出回复" + (f"：{new_err}" if new_err else "（多半是 API 报错）"))
    if not dropped:
        await del_msgs(bot, chat_id, old)
    if target:
        ms[-1]["img_tg"] = []
    ms[-1]["tg"] = ids
    save_chat(card, chat)
    await show_msg(bot, chat_id, card, chat, len(ms) - 1)
    maybe_images(bot, chat_id, card, chat, len(ms) - 1)


def size_text(n):
    return f"{n / 1048576:.1f}MB" if n >= 1048576 else f"{max(1, n // 1024)}KB"


# ---------------------------------------------------------------- 用完就消失：菜单、提示、确认、你发的命令；聊天气泡和图片保留
KEEP = contextvars.ContextVar("keep_msg", default=False)
EPHEMERAL = {}


def msg_life(text, kb):
    if kb is not None:
        return int(CFG.get("menu_life_sec", 30))             # 菜单：30 秒没人点就删
    return min(int(CFG.get("note_life_max", 180)), 20 + len(text or "") // 10)


def forget(bot, chat_id, mid, sec):
    """sec 秒后删掉这条消息"""
    async def job():
        await asyncio.sleep(sec)
        EPHEMERAL.pop(mid, None)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass
    old = EPHEMERAL.pop(mid, None)
    if old:
        old.cancel()
    EPHEMERAL[mid] = asyncio.create_task(job())


def install_ephemeral():
    """bot 发的消息默认都会自己消失；聊天气泡（put_msg）和图片（send_photo）不受影响"""
    from telegram.ext import ExtBot
    orig = ExtBot.send_message
    if getattr(orig, "_eph", False):
        return

    async def send_message(self, chat_id, text, *a, **k):
        m = await orig(self, chat_id, text, *a, **k)
        try:
            if not KEEP.get() and CFG.get("auto_clean", True):
                forget(self, chat_id, m.message_id, msg_life(text, k.get("reply_markup")))
        except Exception:
            pass
        return m
    send_message._eph = True
    ExtBot.send_message = send_message


_put_msg_keep = put_msg


async def put_msg(bot, chat_id, mid, html_text, raw, kb):
    tok = KEEP.set(True)                                      # 聊天气泡：保留
    try:
        return await _put_msg_keep(bot, chat_id, mid, html_text, raw, kb)
    finally:
        KEEP.reset(tok)


_tracked_plain = tracked


def tracked(name, fn):
    inner = _tracked_plain(name, fn)
    if not name.startswith("/"):
        return inner

    async def w(update, ctx):
        try:
            return await inner(update, ctx)
        finally:                                              # 你发的命令处理完就删
            m = update.effective_message
            if m and CFG.get("auto_clean", True):
                forget(ctx.bot, m.chat_id, m.message_id, 0)
    return w


import stbridge  # noqa: E402


SB = stbridge.Bridge(ST, CFG.get("cdp_url", "http://127.0.0.1:9222"), CFG.get("st_page_match", ":8000"))


CHAT_CACHE = {}


if __name__ == "__main__":
    main()
