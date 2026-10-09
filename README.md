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

> 💡 想下载**抖音 / TikTok / 快手**？这三个平台需要 TikHub key 或登录态，
> 见下文「抖音 / TikTok / 快手：走 TikHub 或登录态」。B站 / YouTube / 小红书等
> 开箱即用，不用额外配置。

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

## 抖音 / TikTok / 快手：走 TikHub 或登录态

这三个平台现在是 **`a_bogus` 请求签名**风控，yt-dlp 的解析器已经过时——带不带
cookie 都没用（实测返回 `403 Uifid Not Found`）。本地逆向签名不现实，所以走两条路：

### 主力：TikHub 第三方解析（推荐）

[TikHub.io](https://tikhub.io) 在服务端维护了抖音/TikTok/快手的签名算法，一次
API 调用直接返回无水印直链，最省心：

1. 注册 <https://user.tikhub.io>（邮箱即可）
2. Dashboard 顶部点 **Check-in** 签到领每日免费额度（不签到可能下不了）
3. 左侧 **API Management → Pricing** 创建 API key，勾选**全部 Scopes** 保存
4. 把 key 填进 `config.json`：

```jsonc
{ "delivery": { "tikhub": { "api_key": "你的key" } } }
```

填完重启即可。免费档按签到额度走，量大需付费。

### 兜底：登录态（TikHub 额度不够时）

工具内置「登录态兜底」：双击 exe 后顶部有 **🔑 登录抖音 / 🔑 登录TikTok** 按钮，
点一下弹 Edge 扫码登录（登录态存在工具自己的 profile 里，与日常浏览器隔离）。
之后 TikHub 额度用完时，会自动切到这个登录态本地解析。

命令行等价操作：`start.bat cdp-login douyin`（或 `tiktok`）。

---

## 小红书 / 西瓜下不了？需要配 Cookie

小红书和西瓜有反爬风控，偶发情况下 yt-dlp 会报：

```
ERROR: [Douyin] xxxx: Fresh cookies (not necessarily logged in) are needed
```

小红书大多时候不带 cookie 也能下，被风控时带游客 cookie 即可；西瓜视频基本必须配。

### 配置步骤

先看当前状态：

```bat
start.bat cookies
```

它会列出每个浏览器 profile 里有没有目标站点的 cookie，**以及 cookie 用的是什么加密**。
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
{ "download": { "cookies_file": "C:/path/to/cookies.txt" } }
```

`run.py cookies` 会告诉你当前 cookie 是 `v10`（yt-dlp 能解）还是 `v20`（解不开）。

#### 路线二：直接读浏览器 cookie

**Chrome / Edge 对 cookie 数据库是独占锁 —— 浏览器开着就绝对读不到。**
必须先完全关闭浏览器（含托盘/后台进程）。

```bat
start.bat login xiaohongshu --test "https://www.xiaohongshu.com/explore/xxxx"
```

这条命令会自动：检测浏览器是否还开着并**等它关闭** → 读 cookie → 写好配置 →
用给定的链接**当场验证**能不能解析。看到 `✅ 解析成功` 就是通了。

指定浏览器用 `--browser chrome`，只检查不配置用 `--check`。

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
| 抖音 | ✅ 走 **TikHub 第三方解析**（yt-dlp 已过时，`a_bogus` 签名解不了） | 否（要 TikHub API key） |
| TikTok | ✅ 走 **TikHub 第三方解析**（同上） | 否（要 TikHub API key） |
| 小红书 | ✅ 2.4 MB（分享域名 `xhslink.cn` 也认） | 带不带都行 |
| 哔哩哔哩 | ✅ 20.5 MB（完整链接 + `b23.tv` 短链都行） | 带不带都行 |
| YouTube | ✅ 232.5 MB | **否（千万别给它带 cookie）** |
| 快手 | ⚠️ 自研 GraphQL 解析（快手反爬严，新鲜 `did` 会被滑块拦截） | 否 |
| 直链 `.mp4` | ✅ 直接下载 | 否 |
| 微博 | ⚠️ 解析器存在，但需登录态；实测样本链接已失效 | 是 |
| 西瓜视频 | ⚠️ 解析器存在，但上游失效（`Failed to get SSR_HYDRATED_DATA`） | 是 |
| X / Instagram / Facebook / Vimeo / Twitch | ✅ yt-dlp 有解析器（未逐一实测） | 否 |
| 腾讯视频 / 爱奇艺 / 优酷 / 芒果TV | ⛔ 按设计拦截（DRM） | — |
| **好看视频 / 皮皮虾 / 微信视频号** | ❌ **yt-dlp 没有解析器，无法自动化** | — |

> **抖音 / TikTok 特别说明**：这两个平台现在是 **`a_bogus` 请求签名**风控，
> yt-dlp 的解析器已过时（带 cookie 也没用，实测 403 `Uifid Not Found`）。
> 唯一现实的路径是 **TikHub.io 第三方解析 API**（服务端维护签名算法）：
> 注册 TikHub.io 拿 API key，填到 `config.json` 的 `delivery.tikhub.api_key`，
> 下载抖音/TikTok 就会自动走 TikHub 拿无水印直链。免费档每日签到领额度。

> **关于 cookie**：cookie 是**站点特定**的 —— 有的站点不带就下不了，
> 有的站点带了反而会被风控拒绝。所以本项目按域名决定发不发
> （`download.cookie_mode: "auto"` + `cookie_domains` 白名单）。
> 下拉有实测对照表。

### cookie 到底哪些平台需要（实测对照）

同一个链接，带 cookie / 不带 cookie 各跑一次的结果：

| 平台 | 不带 cookie | 带 cookie | 结论 |
| --- | --- | --- | --- |
| **抖音** | ❌ `a_bogus` 签名（yt-dlp 过时） | ❌ 一样被签名拦 | **走 TikHub API** |
| 西瓜视频 | ❌ `Cookies are needed` | ✅ 解析通过 | 必须配（但解析器现在上游坏了） |
| 小红书 | ✅ 成功 | ✅ 成功 | 带不带都行 |
| 哔哩哔哩 | ✅ 成功 | ✅ 成功 | 带不带都行（带上可能拿到更高画质） |
| **YouTube** | ✅ 成功 | ❌ `The page needs to be reloaded` | **千万不要带** |
| TikTok / X / Instagram / Facebook | — | — | 同样不要带（cookie 与会话不匹配会被风控） |
| 快手 | ⚠️ 自研 GraphQL（`did` 老化后可用） | — | 不走 cookie，走自研直链 |
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
start.bat cdp-login douyin                        :: 登录抖音（登录态兜底，TikHub 额度不够时用）
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

## 接闲鱼：下单 → 自动下载 → 发链接

想真正做到「买家下单后自动接单、自动下载、自动交付」，需要一个能收发闲鱼消息的
上游（比如 `xianyu-super-butler`），这个项目负责下载和交付。两者通过 HTTP 对接：

```text
买家付款  →  上游检测到「我已付款」            (xianyu-super-butler)
          →  翻出买家之前发的链接，POST 本机 /api/orders
          →  本机：识别 → 展开 → 下载                (本项目)
          →  传到网盘拿分享链接
          →  POST 上游 /api/xianyu/deliver
          →  上游把「网盘链接 + 提取码」发给买家，并标记已发货
```

**为什么是发链接而不是发文件**：闲鱼聊天只支持文本和图片，发不了视频文件。
所以「把视频交给买家」在现实中只能走「传到某处 → 发链接」。

### 本项目这边要做的

1. 授权网盘（目前实现了百度网盘）：

   ```bat
   start.bat baidu-login
   ```

   它会打印一个授权页地址。用你自己的百度账号登录并同意后，把页面上的 `code`
   粘回来即可。凭据（`refresh_token`，有效期约 10 年）写进 `config.json`。

   > 前提：先去 <https://pan.baidu.com/union> 建个应用拿到 `AppKey` / `SecretKey`，
   > 并申请开通「上传」能力。这一步是人工的，代码替代不了。

2. 单独验证交付通道（**强烈建议先跑这一步**）：

   ```bat
   start.bat upload "D:\某个视频.mp4"
   ```

3. 打开桥接渠道，`config.json` 里：

   ```jsonc
   {
     "channels": { "xianyu_bridge": { "enabled": true } },
     "delivery": {
       "uploader": "baidu",
       "mode": "share_link",
       "bridge": {
         "enabled": true,
         "upstream_url": "http://127.0.0.1:8080",
         "source_channels": ["xianyu"]
       }
     }
   }
   ```

4. 探一下上游在不在：

   ```bat
   start.bat bridge-ping
   ```

### 上游那边要做的

上游仓库不在这里，改动放在 `upstream_patch/`，含一个自包含的补丁模块和
**精确到行**的插入说明，照做即可：

```text
upstream_patch/
├── xianyu_video_bridge.py     ← 拷到上游根目录
├── xianyu_video_bridge.json   ← 拷到上游根目录
└── README.md                  ← 改哪两处、怎么验证、怎么回滚
```

两条设计底线：补丁只在「检测到付款」那个分支前面插一句判断，失败一律回退到
上游原来的卡券发货；把 `xianyu_video_bridge.json` 里的 `enabled` 改成 `false`
就完全失效，不用改代码。

> ⚠️ 上游走的是逆向闲鱼协议的路线，本身有封号风险。这个风险由上游承担 ——
> 本项目自身不含任何逆向对接代码，只负责下载和把文件变成链接。

### 交付通道可替换

`xydl/uploaders/` 是可插拔的。目前有：

| 通道 | 说明 |
| --- | --- |
| `none` | 默认。只发文案不发链接（没配网盘时的安全降级） |
| `s3` | **推荐**。对象存储预签名直链：腾讯云 COS / 阿里云 OSS / Cloudflare R2 / MinIO / 七牛 |
| `baidu` | 百度网盘：分片上传可用，但**分享链接需要企业开发者认证 + 付费**，见下 |

加新通道只要继承 `Uploader` 实现 `upload()`，
在 `uploaders/__init__.py` 登记一行，业务代码不用动。

#### 对象存储怎么配（推荐）

一套 S3 兼容实现覆盖所有主流服务商，换服务商只改 `endpoint` + `region`：

```jsonc
"delivery": {
  "uploader": "s3",
  "s3": {
    // 腾讯云 COS      https://cos.ap-guangzhou.myqcloud.com        ap-guangzhou
    // 阿里云 OSS      https://s3.oss-cn-hangzhou.aliyuncs.com       oss-cn-hangzhou
    // Cloudflare R2   https://<accountid>.r2.cloudflarestorage.com  auto
    // MinIO（自建）    http://192.168.1.10:9000                     us-east-1
    "endpoint": "",
    "region": "",
    "bucket": "",
    "access_key_id": "",
    "secret_access_key": "",
    "prefix": "xianyu-video",
    "path_style": false,        // MinIO / 私有实现常需 true
    "url_expires_sec": 604800   // 预签名链接有效期，默认 7 天
  }
}
```

交付走**预签名直链**：链接自带有效期，过期自动失效，不用把桶设成公开。
签名是纯标准库实现的 AWS SigV4（没有 boto3 依赖），并且用 AWS 官方文档公开的
测试向量做已知答案校验 —— 签名算错时 S3 只会回一个 `SignatureDoesNotMatch`，
不告诉你哪儿错了，所以这个校验不能省。

若桶本身公开或挂了 CDN，填 `public_base_url` 就直接用永久直链、不做签名。

> ⚠️ **桶请设成「私有读写」。** 公有读的桶有个很坑的地方：**它根本不校验签名**，
> 于是预签名写错了也照样能下载 —— 你会以为一切正常，直到某天换了私有的桶
> 才发现全军覆没。所以 `run.py upload` 会**主动探测**裸链接：返回 200 就警告你
> 桶是公有的，返回 403 才确认保护真的生效。

#### 百度网盘实测结论（2026-10）

**上传 100% 可用；分享链接拿不到。**

| 能力 | 实测结果 |
| --- | --- |
| OAuth 授权 | ✅ 通 |
| 分片上传 `precreate`/`superfile2`/`create` | ✅ 通（实测 6MB 文件成功） |
| 创建分享链接 `/rest/2.0/xpan/share?method=set` | ❌ 恒定 `errno=2` |
| 创建分享链接 `/apaas/1.0/share/set`（新版） | ❌ `errno=13998 invalid app` |

`errno=2` 在官方错误表里写作「参数错误」，但**参数完全正确时也会返回它**
（`fid_list` / `fsid_list` / `file_id_list`、带不带 `pwd`、`period=0` 全试过）。
新版接口直接说 `invalid app`。真实原因是：**这个应用没有开通「文件分享服务」** ——
该服务是企业开发者专属的付费能力。

所以百度网盘只能当「存储」用，交付分两步：

1. `delivery.mode = "share_link"`（默认）：上传后自动生成链接。**资质没批之前会失败。**
2. `delivery.mode = "upload_only"`：只上传，不碰分享接口。文件自动落到
   `/apps/xianyu-video/`，运营去「我的分享」手动点一次分享。

> 想让百度网盘全自动交付，得去开放平台申请企业开发者认证并购买文件分享服务。
> 否则建议换交付通道 —— 对象存储（腾讯云 COS / 阿里云 OSS / Cloudflare R2）
> 的预签名直链完全自己可控、成本极低，且不需要任何资质审批。

---

## 打包成桌面应用

它也可以作为一个独立的「视频下载工具」卖 —— 不依赖闲鱼、不依赖上游，
双击就是一个原生窗口。

```bat
.venv\Scripts\python.exe -m pip install pyinstaller
.venv\Scripts\python.exe build_desktop.py
```

产物是 **`dist\视频下载工具.exe`（单个文件，约 118MB）**，双击即用，不依赖旁边任何目录。

单文件的好处是拷贝/分发就是拷一个 exe，没有「哪个目录忘了带就起不来」的问题。
用户第一次运行会在 exe 同级生成 `config.json` / `data` / `downloads` / `logs`。

> 分发给买家时，抖音/TikTok/快手有两种玩法：① 你把 TikHub key 统一配进 exe 旁的
> `config.json`（买家零操作）；② 让买家自己点界面上的「登录抖音/登录TikTok」走登录态兜底。
> 其余平台开箱即用。

### 几个设计上的取舍

| 决定 | 原因 |
| --- | --- |
| 桌面外壳走**浏览器应用模式**（`msedge --app=`），不用 pywebview | pywebview 内嵌 WebView2 初始化失败时会**递归重开自己** —— 实测一次启动炸出 50+ 进程，`ppid` 全指向第一个进程。卖出去的软件不能有这种失败模式 |
| 给窗口一份独立 `user-data-dir`，放在 `%LOCALAPPDATA%` | 不与用户日常浏览器互相污染；也保证那个浏览器进程确实是我们自己的子进程，关窗能连带退出。放 exe 同级的话用户目录里会莫名多出一百多 MB 缓存，整个目录拷给别人时还连缓存一起带走 |
| onefile（单文件） | 用户要「单独 exe 就能跑」。代价：每次启动要把内部约 170MB（含 ffmpeg）解到临时目录，冷启动比 onedir 慢几秒 |
| `console=False` | 不要黑框。致命错误会弹 MessageBox，日志写到 `logs/desktop.log` |
| 打包脚本收集 ffmpeg，**连带它依赖的 DLL，收进 exe 前真跑一次** | B站/YouTube/小红书 只给 DASH 分离流，没 ffmpeg 合不了流。对买家来说「下载失败」远比「请自己装 ffmpeg」友好。而**只收 `ffmpeg.exe` 是个陷阱**：conda 装的 ffmpeg 依赖同目录上百个 DLL，只收 exe 的话用户双击直接闪退（退出码 127，一行报错都没有）。所以打包脚本会顺着 PE 导入表把传递依赖全捞出来，onefile 启动时随包解到 `_MEIPASS/ffmpeg/` |
| 窗口存活用**页面心跳**而不是 `proc.wait()` | Edge 的启动器进程和真正的浏览器进程不是同一个，`proc.wait()` 常常立刻返回 —— 照它走会出现「窗口刚开、服务就停了」 |
| 端口被占时先探测是不是自己 | 是 → 复用那个实例再开个窗口（别起第二份程序抢同一个 SQLite）；不是 → 换空闲端口 |
| 打包前把旧产物**改名挪走**而不是删除 | 上千个小文件的批量删除会被安全策略拦下导致打包失败；改名等价且不受影响 |

启动排障：`logs/boot.log` 会记下每个阶段、进程号和父进程号；
`logs/desktop.log` 是程序自身的输出。这两个文件出问题时比猜有用得多。

### 打包后的路径规则（改代码时注意）

`xydl/config.py` 把两个目录分开了，**不能混**：

- `bundle_dir()` —— **只读资源**（`webui/index.html`）。打包后是 `sys._MEIPASS`，
  onefile 模式下那是临时目录。
- `app_dir()` —— **可写数据**（data / downloads / logs / config.json）。
  打包后是 **exe 所在目录**。

如果把可写数据放到 `_MEIPASS`，进程一退整个被删 —— 用户下好的视频会凭空消失。
`tests/test_frozen_paths.py` 专门守这条。

---

## 测试

```bat
.venv\Scripts\python.exe -m pytest  :: 384 项，全部离线可跑（用本地 HTTP 服务器代替真实站点）
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
* 打进包里的 ffmpeg **必须连依赖 DLL 一起拷，并拷完真跑一次** —— 只拷 exe 的话
  在用户机器上是退出码 127 的静默闪退，而打包脚本会照样报「已带上 ffmpeg」
* 冻结后 `sys.executable` 是**应用自己**，`subprocess.run([sys.executable, "-m", "yt_dlp"])`
  等于「启动自己 -m yt_dlp」，会无限递归派生进程
* 测试里的 `ThreadingTCPServer` 必须设 `block_on_close = False`，否则一个测试能白等十几秒
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
