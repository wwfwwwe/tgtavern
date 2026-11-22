#!/usr/bin/env python3
"""第一次起酒馆后运行一次：改 data/st/config/config.yaml，让无头页面能登进去、服务器插件能加载。

⚠️ 严禁商用，本套工具全部开源，免费。
   如果你是花钱买的、花钱找人部署的、或者在用别人收费的服务——你被骗了。请找对方退款，并举报。

改的项：
  whitelistMode: false            Docker 里 tavpage 的地址不在白名单里；靠 basicAuth 保护
  basicAuthMode: true             用户名密码取 data/bot/config.json 的 st_user / st_pass
  enableServerPlugins: true       extras/st-plugins 里的插件要它
  enableServerPluginsAutoUpdate: false
改之前会备份成 config.yaml.bak。改完要重启酒馆：docker compose restart sillytavern
"""
import json, re, shutil, sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
cfg_f = root / "data/st/config/config.yaml"
bot = json.loads((root / "data/bot/config.json").read_text(encoding="utf-8"))
if not cfg_f.exists():
    sys.exit(f"没找到 {cfg_f}：先 docker compose up -d sillytavern，等它起来生成配置文件")
user, pw = bot.get("st_user") or "", bot.get("st_pass") or ""
if not user or not pw or "basicAuth" in user:
    sys.exit("先在 data/bot/config.json 里填好 st_user / st_pass（自己起一个用户名和密码）")

s = cfg_f.read_text(encoding="utf-8")
shutil.copy(cfg_f, cfg_f.with_suffix(".yaml.bak"))


def top(key, val):
    global s
    pat = re.compile(rf"^{key}:.*$", re.M)
    line = f"{key}: {val}"
    s = pat.sub(line, s) if pat.search(s) else s.rstrip("\n") + "\n" + line + "\n"


top("whitelistMode", "false")
top("basicAuthMode", "true")
top("enableServerPlugins", "true")
top("enableServerPluginsAutoUpdate", "false")
q = lambda v: json.dumps(v, ensure_ascii=False)
block = f"basicAuthUser:\n  username: {q(user)}\n  password: {q(pw)}"
pat = re.compile(r"^basicAuthUser:\n(?:[ \t]+.*\n?)*", re.M)
s = pat.sub(block + "\n", s) if pat.search(s) else s.rstrip("\n") + "\n" + block + "\n"
cfg_f.write_text(s, encoding="utf-8")
print("改好了。现在运行：docker compose restart sillytavern")
print("⚠️ 严禁商用，本套工具全部开源，免费。花钱买的、花钱找人部署的都是上当了。")
