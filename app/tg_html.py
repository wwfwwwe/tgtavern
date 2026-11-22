"""酒馆美化正则的 HTML 输出 -> TG 能显示的轻量标记（**粗体** *斜体* • 列表 等），给 bot.py 的显示用。

规则只有一张固定的「HTML 标签 -> TG 格式」对照表，不针对任何一张卡：
  粗体/标题/表头/summary -> **粗体**    i/em -> *斜体*    s/del -> ~~删除线~~
  div/p/section… -> 换行分段            li -> 「• 」     tr -> 「a：b」或「a | b | c」
  details -> 折叠块（summary 做标题）     hr -> 分隔线     button -> 「▸ 文字」
  script/style/head/隐藏元素/图片/svg/表单控件 -> 去掉，颜色字号等样式全部丢弃
靠脚本运行时才生成内容的面板（酒馆助手前端卡）静态翻不出来：translate() 返回 None，bot 退回原来的折叠显示。
"""
import re
from html.parser import HTMLParser

SKIP = {"script", "style", "head", "title", "meta", "link", "noscript", "template", "svg", "canvas", "input",
        "select", "textarea", "iframe", "audio", "video", "object", "embed", "img", "picture", "source", "dialog",
        "option", "datalist", "map", "math"}
VOID = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr",
        "param", "keygen"}
BLOCK = {"div", "p", "section", "article", "header", "footer", "main", "nav", "aside", "ul", "ol", "li", "table",
         "thead", "tbody", "tfoot", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "details", "summary", "blockquote",
         "pre", "center", "figure", "figcaption", "dl", "dt", "dd", "form", "fieldset", "legend", "html", "body",
         "address", "hgroup", "menu"}
BOLD = {"b", "strong", "h1", "h2", "h3", "h4", "h5", "h6", "th", "summary", "dt", "legend"}
ITAL = {"i", "em", "cite", "q"}
STRIKE = {"s", "del", "strike"}
AUTO_CLOSE = {"li": {"li"}, "p": {"p"}, "tr": {"tr"}, "td": {"td", "th"}, "th": {"td", "th"},
              "dt": {"dt", "dd"}, "dd": {"dt", "dd"}, "option": {"option"}}
HIDE_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)   # opacity:0 多是展开动画的初始状态，不算隐藏
HIDE_CLASS = {"hidden", "d-none", "sr-only", "visually-hidden", "hide"}
FENCE = re.compile(r"```[\w-]*")
WORD = re.compile(r"[0-9A-Za-zÀ-￿]+")


class _Node:
    __slots__ = ("tag", "attrs", "kids")

    def __init__(self, tag, attrs):
        self.tag, self.attrs, self.kids = tag, attrs, []


class _Parser(HTMLParser):
    """宽松建树：没闭合的标签按常见规则自动收尾，多余的结束标签忽略"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {})
        self.stack = [self.root]
        self.unmatched = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        closes = AUTO_CLOSE.get(tag)
        if closes and len(self.stack) > 1 and self.stack[-1].tag in closes:
            self.stack.pop()
        n = _Node(tag, {k.lower(): (v or "") for k, v in attrs})
        self.stack[-1].kids.append(n)
        if tag not in VOID:
            self.stack.append(n)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].kids.append(_Node(tag.lower(), {k.lower(): (v or "") for k, v in attrs}))

    def handle_endtag(self, tag):
        tag = tag.lower()
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return
        self.unmatched += 1

    def handle_data(self, data):
        self.stack[-1].kids.append(data)


def _hidden(n):
    a = n.attrs
    if "hidden" in a or a.get("aria-hidden", "").lower() == "true":
        return True
    if HIDE_STYLE.search(a.get("style", "")):
        return True
    return bool(HIDE_CLASS & set(a.get("class", "").lower().split()))


def _wrap(s, mark):
    """给一段文字加 ** / * / ~~，多行就每行分别加"""
    out = []
    for ln in s.split("\n"):
        core = ln.strip()
        if core and not (core.startswith(mark) and core.endswith(mark)):
            ln = ln.replace(core, f"{mark}{core}{mark}", 1)
        out.append(ln)
    return "\n".join(out)


IMG_SRC = re.compile(r"^(?:https?:)?//", re.I)


class _Render:
    def __init__(self, scripted=False, groups=()):
        self.ol_stack = []
        self.scripted = scripted
        self.groups = [g for g in groups if len(g) >= 3]

    def node(self, n, pre=False, depth=0):
        if isinstance(n, str):
            if pre:
                return n
            if not n.strip():
                return " "                  # 模板里标签之间的缩进
            # 酒馆显示时会把正文换行变成 <br>，所以有字的文本里换行要保留
            # 但文本两头的换行多半是捕获组带进来的（「姓名:林婉\n」），当空格处理
            n = n.replace("\r", "")
            core = n.strip()
            lead = " " if n[:1].isspace() else ""
            tail = " " if n[-1:].isspace() else ""
            return lead + re.sub(r"[ \t\f]+", " ", core) + tail
        tag = n.tag
        if tag == "img":
            src = n.attrs.get("src", "").strip()
            # 图片地址里带着捕获的内容（比如 CG 文件名）= 正文的一部分，留成链接；模板自带的装饰图不要
            if IMG_SRC.search(src) and any(g in src for g in self.groups):
                return f"\n🖼 {src}\n"
            return ""
        if tag in SKIP or _hidden(n):
            return ""
        if tag == "br":
            return "\n"
        if tag == "hr":
            return "\n———\n"
        if tag == "ol":
            self.ol_stack.append(0)
        inner = "".join(self.node(k, pre or tag == "pre", depth + 1) for k in n.kids)
        if tag == "ol":
            self.ol_stack.pop()
        if tag == "details":
            return "\n" + self.details(n, pre, depth) + "\n"
        if tag in BOLD and inner.strip():
            inner = _wrap(inner, "**")
        elif tag in ITAL and inner.strip():
            inner = _wrap(inner, "*")
        elif tag in STRIKE and inner.strip():
            inner = _wrap(inner, "~~")
        if tag == "li":
            if self.ol_stack:
                self.ol_stack[-1] += 1
                return f"\n{self.ol_stack[-1]}. {inner.strip()}\n"
            return f"\n• {inner.strip()}\n"
        if tag == "button":
            t = inner.strip()
            # 带脚本的模板里按钮都是界面控件（切换标签页、关闭…），没脚本时才可能是选项
            return f"\n▸ {t}\n" if len(t) > 1 and not self.scripted else ""
        if tag in ("td", "th"):
            return "\x01" + inner.strip()
        if tag == "tr":
            cells = [c.strip() for c in inner.split("\x01") if c.strip()]
            if not cells:
                return ""
            if len(cells) == 2:
                return f"\n{cells[0].rstrip('：:')}：{cells[1]}\n"
            return "\n" + " | ".join(cells) + "\n"
        if tag in BLOCK:
            return "\n" + inner + "\n"
        return inner

    def details(self, n, pre, depth):
        title, body = "", []
        for k in n.kids:
            if isinstance(k, _Node) and k.tag == "summary" and not title:
                title = clean("".join(self.node(x, pre, depth + 1) for x in k.kids))
            else:
                body.append(self.node(k, pre, depth + 1))
        b = clean("".join(body))
        title = title.replace("\n", " ").strip()
        return (f"**{title}**\n{b}" if title else b).strip()


def clean(s):
    s = s.replace("\x01", " ")
    lines = [re.sub(r" {2,}", " ", ln).strip() for ln in s.split("\n")]
    out = []
    for ln in lines:
        if not ln and (not out or not out[-1]):
            continue
        if re.fullmatch(r"(\*\*|\*|~~)?\s*(\*\*|\*|~~)?", ln):   # 只剩标记没有字
            continue
        if len(ln) <= 3 and not WORD.search(ln.replace("*", "").replace("~", "")):   # ┌ ┐ × ⌂ 这类装饰
            continue
        out.append(ln)
    return "\n".join(out).strip()


def _top_details(root):
    """整段输出基本就是一个 <details>：把 summary 拿出来当标题"""
    def visible(n):
        return [k for k in n.kids if (isinstance(k, str) and k.strip()) or
                (isinstance(k, _Node) and k.tag not in SKIP and not _hidden(k))]
    n = root
    for _ in range(12):
        ks = visible(n)
        if len(ks) != 1 or isinstance(ks[0], str):
            return None
        n = ks[0]
        if n.tag == "details":
            return n
    return None


def _words(s):
    return [w for w in WORD.findall(s) if len(w) >= 2]


def coverage(groups, text):
    """捕获组里的字有多少出现在翻译结果里（防止 JS 才能显示的面板翻出一堆空壳）"""
    flat = re.sub(r"\s+", "", text)
    ws = [w for g in groups for w in _words(re.sub(r"<[^<>]*>", " ", g or ""))]
    total = sum(len(w) for w in ws)
    if total < 4:
        return 1.0
    return sum(len(w) for w in ws if w in flat) / total


def translate(out, groups=()):
    """正则替换后的 HTML -> (标题或 None, 正文)；翻不了返回 None"""
    has_script = re.search(r"<\s*script\b", out, re.I) is not None
    groups = [g for g in groups if g and g.strip()]
    if has_script and sum(len(w) for g in groups for w in _words(g)) < 4:
        return None                     # 内容全靠脚本读聊天/变量生成（酒馆助手前端卡），捕获组只是个开关
    if not has_script and not groups and re.search(r"<\s*(img|iframe|video|audio)\b", out, re.I) is None \
            and not re.sub(r"<[^<>]*>|```[\w-]*|\s", "", out):
        return None, ""                 # 只有标签没有字（拆开的边框开头/结尾）
    p = _Parser()
    try:
        p.feed(FENCE.sub("", out)); p.close()
    except Exception:
        return None
    r = _Render(has_script, groups)
    top = _top_details(p.root)
    if top is not None:
        title, body = "", []
        for k in top.kids:
            if isinstance(k, _Node) and k.tag == "summary" and not title:
                title = clean("".join(r.node(x) for x in k.kids)).replace("\n", " ")
            else:
                body.append(r.node(k))
        body = clean("".join(body))
    else:
        title, body = "", clean(r.node(p.root))
    if groups and coverage(groups, title + "\n" + body) < 0.97:   # 捕获的内容少一点都不行：宁可退回原文，也不吞内容
        return None
    # 尖括号换成全角，免得 bot 后面把文字里的 <xx> 当成标签
    title, body = title.replace("<", "＜").replace(">", "＞"), body.replace("<", "＜").replace(">", "＞")
    return (title.strip() or None), body


def is_fragment(out):
    """拆成两半的美化（一条正则只出开头 <div ...>，另一条出结尾 </div>；或者 <img src=... 只写了一半）。
    这种单独翻不了，原样留着，等几条正则都跑完拼起来再处理。带脚本/样式表的不算（原样留会把代码露出来）"""
    s = FENCE.sub("", out)
    if re.search(r"<\s*(script|style)\b", s, re.I):
        return False
    if re.search(r"<[^<>]*$", s):
        return True
    p = _Parser()
    try:
        p.feed(s); p.close()
    except Exception:
        return False
    open_ = [n for n in p.stack[1:] if n.tag not in ("p", "li", "td", "th", "tr", "dt", "dd", "option", "html", "body")]
    return bool(open_) or p.unmatched > 0
