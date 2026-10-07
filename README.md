# 视频代下载 · 半自动接单流水线

给「视频代下载」这类闲鱼小生意做的一套接单自动化：**买家发来的链接 → 自动识别 →
自动展开短链 / 搜索落源 → 自动下载 → 自动生成回话文案**，运营只需要复制粘贴两次。

```text
买家发链接  →  运营粘贴到控制台  →  系统全自动跑完  →  点「复制回话」粘回闲鱼
                                                    →  点「取文件」发给买家
```

---

## ⚠️ 先说清楚一件事：为什么不是「自动对接闲鱼客服」

**闲鱼没有任何开放的客服 / IM 接口。** 阿里开放平台不提供闲鱼买卖家聊天消息的读写
API。市面上所有「闲鱼自动回复机器人」「闲鱼自动发货」，实现方式无非三种：

| 手段 | 后果 |
| --- | --- |
| 逆向闲鱼 App 通信协议 | 违反《闲鱼用户协议》，账号封禁风险极高 |
| Xposed / Frida 注入 App | 同上，且可能触犯《网络安全法》 |
| Android 无障碍服务模拟点击 | 同上，且极不稳定 |

所以本项目**不包含、也不会提供**这类对接代码。取而代之的是把渠道层做成
**可插拔接口**，默认给你两个完全合规的适配器：

* **本地网页控制台**（默认启用）—— 人机协同，把「复制粘贴」压缩到两次点击。
* **目录收件箱**（`channels.inbox.enabled`）—— 任何外部程序往目录里丢个文件就是
  一条消息，系统把回话写成同级文件。用来和你已有的工具解耦对接。

将来你若拿到**官方渠道**（千牛客服、企业微信、飞书、微信客服），只要继承
`xydl/channels/base.py` 里的 `Channel`，实现 `deliver()` 并在 `start()` 里把外部消息
喂给 `pipeline.submit_text()` —— **业务逻辑一行都不用改**。

---

## 快速开始

### 最省事：双击 `start.bat`

直接双击项目根目录的 **`start.bat`**（或中文名的 `启动.bat`，等价）。它会自动：

1. 优先用项目自带的虚拟环境 `.venv`
2. 没有就调 `uv sync` 现场创建并安装依赖
3. 启动服务并自动打开浏览器

关掉窗口即停止。

### 手动启动

> ⚠️ **本机 PATH 里既没有 `python` 也没有 `uv`。**
> `python` 会报「无法将"python"项识别为 cmdlet、函数、脚本文件…」（退出码 9009）；
> `uv` 只存在于 Cherry Studio 的托管目录 `C:\Users\libofang\.cherrystudio\bin`，
> 你自己开的 cmd 里找不到它。
> 所以命令只有两种写法：**`start.bat`** 或 **`.venv\Scripts\python.exe`**。

```bat
cd /d C:\Users\libofang\Documents\xianyu-video-bot

start.bat doctor          :: 环境自检，出问题先跑这个
start.bat selftest        :: 离线自测，确认「装好了」（不需要外网）
start.bat serve           :: 启动接单服务（无参数时默认就是它）

:: 等价的另一种写法
.venv\Scripts\python.exe run.py serve
```

启动成功后打开 <http://127.0.0.1:8765/>。

### 起不来怎么办

```bat
start.bat doctor
```

按输出逐项排查。最常见的三种情况：

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `无法将"python"项识别为...` | 本机没有 `python` 命令 | 用 `start.bat`，或 `.venv\Scripts\python.exe` |
| `'uv' 不是内部或外部命令` | `uv` 只在 Cherry 托管目录里 | 同上 —— 别用 `uv`，用 `start.bat` |
| `端口 8765 起不来（WinError 10048）` | 已经有一个实例在跑 | 关掉旧窗口；或改 `config.json` 的 `server.port` |
| 提示 `没找到 yt-dlp` | 用错了 Python 解释器 | 用 `start.bat`，它会用项目自带的 `.venv` |

### ffmpeg 是**硬需求**，不是可选项

B站 / YouTube / 小红书 / TikTok 等现代平台**只提供 DASH 分离流**（视频轨和音频轨
分开）。没有 ffmpeg 就无法合流成完整文件。

更坑的是它的失败方式：yt-dlp 在这种情况下**退出码仍是 0**，只是静默地留下
`标题.f100026.mp4`（视频轨）和 `标题.f30280.m4a`（音频轨）两个分片。如果不做检测，
发给客户的就是一个只有声音的文件。

本项目对此做了三重防护：

1. `find_ffmpeg()` **实测**每个候选能否运行（不能只看文件存在——失效的 mise shim
   就是存在的，但执行返回 `0xC0000135` DLL 缺失）。
2. 分片文件（`.f<format_id>.<ext>`）**永远不会被当成下载产物**。
3. 检测到只有分片时，直接判定失败并给出明确原因，绝不交付半成品。

装 ffmpeg 后确认：

```bat
start.bat doctor   :: 应该看到「ffmpeg 已启用  <路径>」
```

如果自动查找没命中，把它填进 `config.json`：

```json
{ "download": { "ffmpeg_location": "C:/ffmpeg/bin/ffmpeg.exe" } }
```

---

## 抖音 / 小红书下不了？需要配 Cookie

抖音和小红书有**反爬风控**，yt-dlp 会报：

```
ERROR: [Douyin] xxxx: Fresh cookies (not necessarily logged in) are needed
```

注意这不是「要你登录」，而是要你**带上浏览器 cookie**。B站、YouTube、微博、
西瓜视频等不需要，抖音、小红书基本必须配。

### 配置步骤

先看当前状态：

```bat
start.bat cookies
```

它会列出每个浏览器 profile 里有没有抖音/小红书的 cookie，**以及 cookie 用的是什么加密**。
然后两条路：

#### 路线一：cookies.txt（最可靠，推荐）

Chrome / Edge 从 **127 版**起对 cookie 启用了 **App-Bound 加密**，yt-dlp **解不开**
（源码里完全没有相关支持）。如果你的浏览器是这个版本之后的新版，走这条路：

1. 浏览器里装一个能导出 Netscape 格式 cookie 的扩展
   （比如 Chrome 应用商店的 **Get cookies.txt LOCALLY**）
2. 打开并登录 <https://www.douyin.com>
3. 用扩展导出 `cookies.txt`（放到项目目录即可）
4. 改 `config.json`：

```jsonc
{ "download": { "cookies_file": "C:/Users/libofang/Documents/xianyu-video-bot/cookies.txt" } }
```

`run.py cookies` 会告诉你当前 cookie 是 `v10`（yt-dlp 能解）还是 `v20`（解不开）。

#### 路线二：直接读浏览器 cookie

**Chrome / Edge 对 cookie 数据库是独占锁 —— 浏览器开着就绝对读不到。**
必须先完全关闭浏览器（含托盘/后台进程）。

```bat
start.bat login douyin --test "https://v.douyin.com/xxxx/"
```

这条命令会自动：检测浏览器是否还开着并**等它关闭** → 读 cookie → 写好配置 →
用给定的链接**当场验证**能不能解析。看到 `✅ 解析成功` 就是通了。

指定浏览器用 `--browser chrome`，只检查不配置用 `--check`。

### 备选：用 cookies.txt

本机浏览器不方便时（比如在另一台机器上登录），可以用浏览器扩展导出
Netscape 格式的 `cookies.txt`，然后：

```jsonc
{ "download": { "cookies_file": "C:/path/to/cookies.txt" } }
```

`cookies_file` 优先于 `cookies_from_browser`。

> **说明**：这条路用的是 yt-dlp 官方支持的 cookie 读取能力 —— 也就是你**自己的
> 浏览器会话**。本项目不包含任何针对特定平台风控、或针对 Cloudflare / Turnstile
> 之类人机验证的绕过实现。

---

## 它能做什么

| 能力 | 说明 |
| --- | --- |
| **链接识别** | 从一堆中文 + emoji 里精确切出 URL，识别 30+ 个平台，区分短链/完整页/直链/受保护平台 |
| **短链展开** | `v.douyin.com` / `b23.tv` / `xhslink.cn` / `v.kuaishou.com` / `t.cn` / `youtu.be` … 跟随重定向链直到落地地址，并自动跳过登录墙 |
| **搜索落源** | 买家只发了一句标题、没发链接时，拿标题去 Google / DuckDuckGo 找原视频页面 |
| **下载** | yt-dlp（视频页 / HLS / DASH 合流）+ 直链 HTTP 双引擎，自动降级 |
| **自动回话** | 接单、进度、完成、失败、需人工，五套话术全部可在配置文件里改 |
| **订单管理** | SQLite 落库、异步 worker、进度追踪、取消、重试、完整事件日志 |
| **受保护平台识别** | 腾讯视频/爱奇艺/优酷/芒果TV 会被识别为 DRM 平台，直接转人工，不做无意义的尝试 |

**它不做什么**：不绕过任何 DRM，不破解付费内容，不逆向任何平台的私有接口，
不破解 Cloudflare / Turnstile 之类的人机验证。

---

## 平台支持情况（实测）

**接单前先跑一下这个**，别接了下不了的单：

```bat
start.bat platforms
```

下面是 2026-09 在本机的实测结果：

| 平台 | 结果 | 需 cookie |
| --- | --- | --- |
| 抖音 | ✅ 6.0 MB | 是 |
| 小红书 | ✅ 2.4 MB（分享域名 `xhslink.cn` 也认） | 是 |
| 哔哩哔哩 | ✅ 20.5 MB（完整链接 + `b23.tv` 短链都行） | 是 |
| YouTube | ✅ 232.5 MB | **否（千万别给它带 cookie）** |
| 直链 `.mp4` | ✅ 直接下载 | 否 |
| 微博 | ⚠️ 解析器存在，但需登录态；实测样本链接已失效 | 是 |
| 西瓜视频 | ⚠️ 解析器存在，但上游失效（`Failed to get SSR_HYDRATED_DATA`） | 是 |
| TikTok / X / Instagram / Facebook / Vimeo / Twitch | ✅ yt-dlp 有解析器（未逐一实测） | 否 |
| 腾讯视频 / 爱奇艺 / 优酷 / 芒果TV | ⛔ 按设计拦截（DRM） | — |
| **快手 / 好看视频 / 皮皮虾 / 微信视频号** | ❌ **yt-dlp 没有解析器，无法自动化** | — |

> **关于 cookie**：cookie 是**站点特定**的 —— 有的站点不带就下不了，
> 有的站点带了反而会被风控拒绝。所以本项目按域名决定发不发
> （`download.cookie_mode: "auto"` + `cookie_domains` 白名单）。
> 下拉有实测对照表。

### cookie 到底哪些平台需要（实测对照）

同一个链接，带 cookie / 不带 cookie 各跑一次的结果：

| 平台 | 不带 cookie | 带 cookie | 结论 |
| --- | --- | --- | --- |
| **抖音** | ❌ `Fresh cookies are needed` | ✅ 成功 | **必须配 cookie** |
| 西瓜视频 | ❌ `Cookies are needed` | ✅ 解析通过 | 必须配（但解析器现在上游坏了） |
| 小红书 | ✅ 成功 | ✅ 成功 | 带不带都行 |
| 哔哩哔哩 | ✅ 成功 | ✅ 成功 | 带不带都行（带上可能拿到更高画质） |
| **YouTube** | ✅ 成功 | ❌ `The page needs to be reloaded` | **千万不要带** |
| TikTok / X / Instagram / Facebook | — | — | 同样不要带（cookie 与会话不匹配会被风控） |
| 微博 | 未实测 | 未实测 | 没有有效样本链接 |

所以：

* **抖音的 cookie 不能删** —— 删了抖音就下不了。
* YouTube 之类**必须不带** —— 这就是为什么需要按域名下发，而不能全局无脑带。
* 随时可以自查：

```bat
start.bat platforms
```

会打出每个平台的「支持 / cookie 策略 / 实测说明」。

> **关于解析器失效**：平台改版会让 yt-dlp 的解析器临时失效（西瓜就是）。
> 这种情况只能等上游修，升级即可：
> ```bat
> .venv\Scripts\python.exe -m pip install -U yt-dlp
> ```

### 不支持自动化时怎么办

快手、好看视频、皮皮虾、微信视频号这类 yt-dlp 解析不了的站点，仍然可以人工兜底：

1. 用第三方在线工具或别的方式拿到**视频直链**（`...mp4`）
2. 把直链粘进控制台 —— **直链下载一直是支持的**，走 HTTP 引擎

---

## 目录结构

```
xianyu-video-bot/
├── start.bat                   # ★ 双击即用：找解释器 → 必要时 uv sync → 启动 → 开浏览器
├── 启动.bat                     #   上面那个的中文名副本
├── run.py                      # CLI 入口：serve / once / parse / resolve / download / doctor / selftest
├── config.json                 # 首次运行自动生成，所有可调项都在这里
├── webui/index.html            # 控制台前端（单文件，无外部依赖）
├── xydl/
│   ├── linkparse.py            # ★ 链接识别：URL 抽取 / 平台判定 / 分享文案清洗
│   ├── resolver.py             # ★ 短链展开 + 搜索落源
│   ├── search/                 # 搜索 provider：Serper / Google CSE / DuckDuckGo
│   ├── downloader.py           # ★ yt-dlp 封装 + 直链下载 + ffmpeg 发现与校验
│   ├── pipeline.py             # ★ 订单流水线编排
│   ├── replies.py              # 回话模板引擎
│   ├── store.py                # SQLite 持久化（WAL，多线程安全）
│   ├── http.py                 # 纯标准库 HTTP（手动跟随重定向、三态代理）
│   ├── channels/               # ★ 可插拔渠道层
│   │   ├── base.py             #   Channel 抽象 —— 接新渠道只需要改这里
│   │   ├── console.py          #   本地 Web 控制台
│   │   └── inbox.py            #   目录收件箱
│   ├── config.py               # 配置加载（深合并 + 点号路径 + 环境变量覆盖）
│   ├── models.py               # 领域模型与状态机
│   └── utils.py                # 文件名清洗 / 标题抽取等
└── tests/                      # 176 项测试，全部离线可跑
```

---

## 使用方法

### 下载好的文件在哪

**每一单一个独立子目录**，避免并发下载时文件名互相覆盖：

```
xianyu-video-bot\
└── downloads\
    ├── 1\   ← 订单 #1
    │   └── 吹笛子建议收藏这首拿去商演真的夯爆了 《韩湘子秘谱》@钟世祺.mp4
    ├── 2\   ← 订单 #2
    └── 3\
```

三种拿到文件的方式：

1. **控制台点「📂 打开文件夹」** —— 直接在资源管理器里定位到文件（推荐）
2. 控制台点「⬇ 取文件」 —— 浏览器直接下载
3. 顶部「📂 下载目录 点我打开」 —— 打开整个下载根目录

想换存放位置，改 `config.json` 里的 `paths.download_dir`（支持绝对路径）。

> 订单号对应控制台里显示的 `#1` `#2`，`run.py once` 的输出里也会打印完整路径。

### 命令行

```bat
start.bat serve                                  :: 启动服务（无参数时默认）
start.bat once "买家消息原文" -c 买家ID             :: 同步跑一条，打印全过程
start.bat parse "买家消息原文"                     :: 只看链接识别（不联网，调正则用）
start.bat resolve https://b23.tv/xxx             :: 只做短链展开
start.bat resolve -q "猫咪打呼噜合集"              :: 只用标题落源
start.bat download https://www.bilibili.com/video/BVxxx
start.bat doctor                                 :: 环境自检
start.bat selftest                               :: 离线端到端自测
```

（把 `start.bat` 换成 `.venv\Scripts\python.exe run.py` 完全等价。）

### 控制台 HTTP 接口

```bash
# 提交订单
curl -X POST http://127.0.0.1:8765/api/orders \
  -H "Content-Type: application/json" \
  -d '{"text":"https://v.douyin.com/xxx/","conversation_id":"buyer1","sender":"张三"}'

GET  /api/state                       # 总览：统计、引擎状态、最近订单
GET  /api/orders?status=active&q=关键词
GET  /api/orders/<id>                 # 详情 + 自动生成的回话 + 事件日志
POST /api/orders/<id>/retry           # 重试
POST /api/orders/<id>/cancel          # 取消
POST /api/orders/<id>/replied         # 标记已回话
GET  /api/orders/<id>/file            # 取成品文件（限定在下下载目录内）
```

### 目录收件箱

在 `config.json` 里打开后，往 `inbox/` 丢文件即是一条消息（处理完移入
`inbox/processed/`，回话写在同名 `.reply.txt` 里）：

```text
# inbox/order1.txt
conversation: buyer789
sender: 王五
---
7.92 复制打开抖音，看看【@小明 的作品】 https://v.douyin.com/xxxx/
```

或者 JSON：

```json
{"conversation_id": "buyer789", "sender": "王五", "text": "https://..."}
```

---

## 配置说明（`config.json`）

首次运行自动生成，注释都写在默认值里。几个关键项：

```jsonc
{
  "server": {
    "host": "127.0.0.1",       // 默认只监听本机，别随便改成 0.0.0.0
    "port": 8765,
    "token": ""                // 非空则所有 /api/* 需要 ?token=xxx
  },
  "net": {
    // 代理三态：""/"auto" 跟随系统代理 | "none"/"direct" 强制直连 | "http://127.0.0.1:7890"
    "proxy": "auto"
  },
  "download": {
    "workers": 2,
    "quality": "best",         // best / 1080 / 720 / 480
    "max_filesize_mb": 2048,
    "prefer_ytdlp": true,
    "ffmpeg_location": "",     // 留空自动查找
    "cookies_from_browser": "" // 填 "chrome"/"edge" 可下载需要登录的内容
  },
  "search": {
    // 有 key 的 provider 才会启用，按顺序降级
    "providers": ["serper", "google_cse", "duckduckgo"],
    "serper_api_key": "",      // 推荐：https://serper.dev 返回真实 Google 结果
    "google_cse_key": "",      // 或 Google 官方 CSE（每天 100 次免费）
    "google_cse_cx": ""
  },
  "channels": {
    "console": { "enabled": true },
    "inbox":   { "enabled": false, "poll_sec": 2.0 }
  }
}
```

**搜索落源配置建议**：不配 key 时只有 DuckDuckGo 兜底（免 key，但国内需要走代理）。
想要稳定落源，去 <https://serper.dev> 注册拿个 key 填进 `search.serper_api_key`,
一个 key 即用。

### 交付给客户的文件编码（很重要）

```jsonc
"download": {
  "prefer_compatible": true,   // 默认开
  "quality": "best"
}
```

**YouTube 现在默认给 AV1 + Opus**，塞在 `.mp4` 里。但老手机、部分播放器、
剪映之类**打不开**。代下载是交付给客户的，兼容性比体积重要，所以默认优先挑
**H.264 + AAC**：

| | 视频 | 音频 | 同一条 1080p 视频的体积 |
| --- | --- | --- | --- |
| 默认（关掉偏好） | `av1` | `opus` | 29.8 MB |
| `prefer_compatible: true` | **`h264`** | **`aac`** | 51.6 MB |

体积大约多 70%，换来「发到哪都能播」。站点没有 H.264 时会自动退回它给的最佳编码
（降级链有四档），不会因为偏好把下载搞失败。

想省流量/要最高画质就把它关掉。

**改话术**：直接改 `config.json` 里的 `replies` 段，支持
`{title} {filename} {size} {progress} {platform} {error} {url} {eta}` 占位符。
写错占位符不会崩，只会原样显示。

```jsonc
"replies": {
  "received":  "亲，链接收到啦～正在帮你下载，一般 1-3 分钟出文件，稍等我发你哈 😊",
  "done":      "下载好啦！\n文件：{filename}\n大小：{size}\n请查收～",
  "progress_step": 25,        // 每涨 25% 才考虑推一次进度
  "progress_min_seconds": 20, // 下载前 20 秒不推（小文件直接跳过，不打扰买家）
  "progress_max_count": 3     // 单笔订单最多推 3 条进度
}
```

---

## 接自己的渠道

```python
# xydl/channels/my_channel.py
from .base import Channel
from ..models import IncomingMessage


class MyChannel(Channel):
    name = "my_channel"
    label = "我的官方渠道"

    def start(self) -> None:
        super().start()                  # 注册 deliver 回调
        # 在这里订阅你的渠道消息，收到就投给流水线：
        #   self.pipeline.submit(IncomingMessage(
        #       channel=self.name, conversation_id=会话ID,
        #       text=买家原文, sender=昵称, msg_id=渠道消息ID))
        #
        # msg_id 很重要：它是幂等键。同一个 msg_id 重复投递只会成一次单
        # （收件箱渠道就是用文件名当 msg_id 的）。

    def deliver(self, order, text: str, file_path: str) -> None:
        """把回话送出去。不要抛异常，抛了会被流水线记日志并忽略。"""
        # 你的渠道 SDK:  send_text(order.conversation_id, text)
        # 有 file_path 时再发文件
```

然后在 `xydl/channels/__init__.py` 的 `CHANNEL_CLASSES` 里登记一行即可。

---

## 测试

```bat
.venv\Scripts\python.exe -m pytest  :: 220 项，全部离线可跑（用本地 HTTP 服务器代替真实站点）
.venv\Scripts\python.exe -m pytest -k linkparse  :: 只跑链接识别
start.bat selftest                    :: 端到端冒烟
```

测试覆盖的不是「代码能跑」，而是几个**真实踩过的坑**：

* 短链判定必须**按域名**而不是按平台（`v.douyin.com` 要展开，`douyin.com/video/123` 不用）
* `\b` 在中文旁不成立（`"App查看"` 里 `App` 后面不是词边界）
* `re.compile` 拼接时 `_URL_BODY` 自带 `+`，再拼 `*` 会变成非法的 `+*`
* 控制台自动生成的 `msg_id` 只用毫秒时间戳会撞号，导致同毫秒的第二单被静默丢弃
* 「配置了代理」不能实现成「显式禁用系统代理」（那样会绕过用户的 Clash）
* `--no-part` 会让失败的半截文件不带 `.part` 后缀，从而被误认为成品
* 分片文件绝不能当成下载产物（否则给客户发一个只有声音的文件）
* ffmpeg 候选路径**必须实测能否运行**，失效的 shim 会让 yt-dlp 静默不合流
* Windows 的 `SO_REUSEADDR` 允许绑定**已在监听**的端口，`allow_reuse_address=True`
  会让第二次启动「静默成功」——必须关掉，让端口冲突明确报错

---

## 使用须知

这个工具本身只是一个下载调度器，**它不绕过任何技术保护措施**。请自行确保使用方式
合法合规：

* 只处理你或你的客户**有权下载**的内容（自己的作品、已获授权的内容、平台允许
  下载的公开内容）。
* 不要用它分发他人享有著作权的作品。以营利为目的的批量转载在多数司法辖区
  构成侵权。
* 受 DRM 保护的正版长视频（腾讯视频/爱奇艺/优酷/芒果TV 等）已被识别并拒接，
  这是有意的设计。
* 控制台默认只监听 `127.0.0.1`。如果你要暴露到局域网，**务必设置
  `server.token`**。
