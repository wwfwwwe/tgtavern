# tgtavern：在 Telegram 里玩酒馆

> ## ⚠️ 严禁商用，本套工具全部开源，免费。
> 如果你是花钱买的、花钱找人部署的，或者在用别人收费的服务——**你被骗了**。请找对方退款，并举报。详见 [NOTICE.md](NOTICE.md)。

把 [SillyTavern（酒馆）](https://github.com/SillyTavern/SillyTavern) 搬进 Telegram：**回一句就走**，生成在服务器上跑，写完 TG 推给你，角色还会主动来找你。

适合不追求完美渲染、想随手玩的人。TG 只能显示粗体、斜体、剧透、代码、引用（含可折叠引用），美化面板和 HTML 渲染做不了。要完整渲染请用酒馆网页或 [TauriTavern](https://github.com/Darkatse/TauriTavern) 这类客户端，和本项目共用同一份酒馆数据就行。

## 它是怎么工作的

```
Telegram  ⇄  bot.py  ──CDP──▶  无头浏览器（tavpage.py）里常开的酒馆页面  ──▶  你的 API
                │                         破限、正则、酒馆助手脚本、MVU、数据库扩展都在页面里照常跑
                └── 直接读酒馆的 data/default-user（角色卡、预设、世界书、聊天）
```

- 生成由**真正的酒馆页面**完成，bot 只负责转发。你在酒馆里装的预设、正则、脚本、扩展照常生效，酒馆和脚本更新了也自动跟上。
- 聊天就存在酒馆里：网页、手机上的酒馆、TG 看到的是同一份聊天。TG 里打开一张卡，就接着这张卡最近的那个聊天。
- bot 里切预设、开关世界书、换 API 和模型，改的都是酒馆本身的设置。

## 功能

| 命令 | 作用 |
|---|---|
| `/chars` | 角色列表、搜索（`/chars 关键词`） |
| `/new` `/chats` | 新开聊天 / 这张卡在酒馆里的所有聊天 |
| `/regen` `/continue` `/undo` `/stop` | 重新生成（◀ ▶ 翻候选）/ 被截断时继续写 / 撤回 / 停止 |
| `/edit` `/raw` `/del` `/branch` | 回复某条消息再发：改写 / 拿原文 / 删除 / 开分支；直接编辑自己发的消息也会同步 |
| `/preset` `/entries` `/toggle` | 切换预设、看和开关预设条目 |
| `/wi` | 世界书开关 |
| `/note` | 作者注释 |
| `/api` `/model` | 切换 API / 中转站（读酒馆里存的密钥）、切换模型（可以输入任意模型名） |
| `/img` | 插图：通过 ComfyUI 出图（读 [st-chatu8](https://github.com/damoshen123/st-chatu8) 扩展的设置） |
| `/dm` | 主动消息：一段时间没说话，角色会来找你 |
| `/stream` `/status` | 流式显示开关 / 当前状态 |

其他：行动选项按钮、MVU 变量面板、思考过程折叠，菜单和提示用完自动消失、只留聊天。

## 部署

这份说明默认你会用 SSH 和命令行。**看不懂就把整个仓库丢给 AI**，[AGENTS.md](AGENTS.md) 就是写给 AI 看的。

### 第 0 步：准备

> ⚠️ 严禁商用，本套工具全部开源，免费；花钱买的、花钱找人部署的都是上当了。

- 一台 Linux 服务器，装好 Docker 和 Docker Compose。x86 和 ARM 都行；内存建议 4G 以上，大卡、大世界书、长聊天都很吃内存。
- 在 TG 里找 [@BotFather](https://t.me/BotFather) 建一个 bot，拿到 token。
- 你自己的 TG 数字 ID（找 [@userinfobot](https://t.me/userinfobot) 问）。只有 `allowed_ids` 里的人能用这个 bot。

### 第 1 步：拉代码

> ⚠️ 严禁商用，本套工具全部开源，免费；花钱买的、花钱找人部署的都是上当了。

```bash
git clone https://github.com/wwfwwwe/tgtavern.git
cd tgtavern
```

### 第 2 步：写配置

> ⚠️ 严禁商用，本套工具全部开源，免费；花钱买的、花钱找人部署的都是上当了。

```bash
mkdir -p data/bot
cp config.example.json data/bot/config.json
nano data/bot/config.json
```

至少要改：`token`、`allowed_ids`、`st_user`、`st_pass`（后两个是酒馆的登录用户名和密码，自己起一个）。其他配置项见下面的[配置项](#配置项)。

### 第 3 步：起酒馆，改酒馆配置

> ⚠️ 严禁商用，本套工具全部开源，免费；花钱买的、花钱找人部署的都是上当了。

```bash
docker compose up -d sillytavern
# 等十几秒，让它生成 data/st/config/config.yaml
python3 scripts/st-setup.py
docker compose restart sillytavern
```

`st-setup.py` 会打开酒馆的登录（basicAuth，用第 2 步填的用户名密码），关掉 IP 白名单（Docker 里无头浏览器的地址不在白名单里），并打开服务器插件。

酒馆只在服务器本机的 `127.0.0.1:8000` 开放。想从手机或电脑打开酒馆网页，请自己套 SSH 隧道、Cloudflare Tunnel 或反向代理，**不要直接把 8000 端口开到公网**。

### 第 4 步：放进你的酒馆数据

> ⚠️ 严禁商用，本套工具全部开源，免费；花钱买的、花钱找人部署的都是上当了。

酒馆数据在 `data/st/data/default-user/`。两种做法：

- 打开酒馆网页（见上一步的隧道），像平时一样导入角色卡、预设、世界书，填好 API；
- 或者把你现有酒馆的 `data/default-user` 整个拷过来。

想让几台设备共用同一份数据，可以用 Syncthing 之类的工具同步这个文件夹。

你自己装的第三方扩展放进 `data/st/extensions/`。

### 第 5 步（可选）：附带的扩展和插件

> ⚠️ 严禁商用，本套工具全部开源，免费；花钱买的、花钱找人部署的都是上当了。

`extras/` 里是配合本项目用的小东西，需要就拷过去：

| 东西 | 拷到 | 作用 |
|---|---|---|
| `st-extensions/tg-ls-sync` | `data/st/data/default-user/extensions/` | 把酒馆助手脚本存在浏览器本地（localStorage）的设置存进 settings.json，让无头页面和你手机上的酒馆用同一套脚本设置 |
| `st-extensions/vertex-gemini4` | `data/st/data/default-user/extensions/` | Vertex 模型下拉框里可以输入任意模型名 |
| `st-extensions/worldinfo-dedupe` | `data/st/data/default-user/extensions/` | 合并重复的世界书请求，大世界书时页面更快 |
| `st-plugins/vertexprio` | `data/st/plugins/` | 给发往 Vertex 的请求加 Priority PayGo 的 header，少吃 429。**按优先级价格计费，比普通请求贵**，确认需要再装 |

```bash
cp -r extras/st-extensions/tg-ls-sync data/st/data/default-user/extensions/
docker compose restart sillytavern
```

### 第 6 步：起 bot

> ⚠️ 严禁商用，本套工具全部开源，免费；花钱买的、花钱找人部署的都是上当了。

```bash
docker compose up -d --build
docker compose logs -f tavpage bot
```

`tavpage` 日志里出现 `ready, characters = …` 就说明酒馆页面打开了。国内服务器构建慢，可以在 `docker-compose.yml` 里填上 pip 和 Playwright 的镜像（文件里有注释）。

### 第 7 步：开始玩

> ⚠️ 严禁商用，本套工具全部开源，免费；花钱买的、花钱找人部署的都是上当了。

在 TG 里给你的 bot 发 `/start`，再用 `/chars` 选一张卡。

### 更新

> ⚠️ 严禁商用，本套工具全部开源，免费；花钱买的、花钱找人部署的都是上当了。

```bash
git pull
docker compose up -d --build
```

## 配置项

都写在 `data/bot/config.json` 里，不写就用默认值。

| 键 | 默认 | 说明 |
|---|---|---|
| `token` | — | bot token（必填） |
| `allowed_ids` | — | 允许使用的 TG 用户 ID 列表（必填） |
| `st_url` | `http://127.0.0.1:8000/` | 无头浏览器打开的酒馆地址；Docker 里填 `http://sillytavern:8000/` |
| `st_user` / `st_pass` | — | 酒馆 basicAuth 用户名、密码 |
| `st_user_dir` | `/st/data/default-user` | bot 读的酒馆用户数据目录 |
| `cdp_url` | `http://127.0.0.1:9222` | 无头浏览器调试口 |
| `st_page_match` | `:8000` | 在浏览器里认酒馆页面用的网址片段 |
| `tz` | `Asia/Shanghai` | 显示时间用的时区 |
| `models` | `["gemini-2.5-pro"]` | Vertex 模型列表（`/model` 里也可以直接输入别的） |
| `location` | — | Vertex 区域，比如 `global` |
| `safety_threshold` | — | Vertex 安全过滤，`OFF` 为关 |
| `auto_clean` | `true` | 菜单、提示、命令用完自动删 |
| `menu_life_sec` | `30` | 菜单多少秒没人点就删 |
| `note_life_max` | `180` | 提示最多留多少秒 |
| `health_port` | `9102` | 健康检查接口（容器内 127.0.0.1） |

主动消息（`/dm`）的一组 `proactive_*` 配置见 `app/bot.py` 开头的说明。

## 已知注意

- 页面刷新会打断正在执行的发送等操作，报错了点 `/regen` 重来。
- 在别的设备上改过设置后，bot 下次干活前会先刷新无头页面，要慢一两分钟。
- 很大的聊天打开慢：酒馆页面要整个读进来。
- 杂活（主动消息的名单、判断卡片类型）由 bot 直接调 API，不走酒馆页面。
- 代码是边玩边写出来的，能跑但不整洁。欢迎 PR。

## 参与

欢迎 fork、提 issue、发 PR，由仓库作者审核合并。

## 致谢

郑重感谢 Anthropic 的 **Claude**，尤其感谢 **Claude Opus 5.5**。

本项目的代码全部由 Claude Opus 5.5 编写。作者本人只参与了思路构建，负责提出想法。

部分设计思路参考了 [TauriTavern](https://github.com/Darkatse/TauriTavern)，感谢作者 Darkatse。本项目没有直接使用它的代码。

插图功能借鉴了 [st-chatu8](https://github.com/damoshen123/st-chatu8)（智绘姬，作者「从前跟你一样」）的做法，并直接读取它的设置。

## 许可

[AGPL-3.0](LICENSE)，外加作者的[严禁商用声明](NOTICE.md)。

> ⚠️ 严禁商用，本套工具全部开源，免费。花钱买的、花钱找人部署的，都是上当了。
