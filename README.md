# 视频下载工具

一个支持 30+ 平台的视频下载工具：粘贴链接，自动解析并下载无水印高清视频。
可以当桌面应用双击即用，也可以当命令行工具跑。

```text
粘贴链接  →  自动识别平台 / 展开短链 / 搜索落源  →  下载  →  点开文件
```

---

## 特性

| 能力 | 说明 |
| --- | --- |
| **链接识别** | 从一段中文 + emoji 里精确切出 URL，识别 30+ 平台，自动区分短链 / 完整页 / 直链 / 受保护平台 |
| **短链展开** | `v.douyin.com` / `b23.tv` / `xhslink.cn` / `v.kuaishou.com` / `t.cn` / `youtu.be` … 跟随重定向直到落地地址 |
| **标题落源** | 只发了一句标题、没发链接时，自动拿标题去 Google / DuckDuckGo 找原视频 |
| **无水印下载** | 抖音 / TikTok / 快手走 TikHub 第三方解析（拿无水印直链），其余平台走 yt-dlp |
| **多画质** | best / 1080 / 720 / 480 可选，B站 / YouTube 自动合流（内置 ffmpeg） |
| **直链下载** | 拿到 `...mp4` 直链也能直接下 |
| **受保护平台识别** | 腾讯视频 / 爱奇艺 / 优酷 / 芒果TV 会被识别为 DRM，不做无意义的尝试 |

**它不做什么**：不绕过任何 DRM，不破解付费内容，不逆向任何平台的私有接口，
不破解 Cloudflare / Turnstile 之类的人机验证。

---

## 支持平台

```bat
start.bat platforms
```

会打出每个平台的「支持 / cookie 策略 / 实测说明」。2026-09 实测：

| 平台 | 结果 | 备注 |
| --- | --- | --- |
| 抖音 | ✅ 走 TikHub 第三方解析 | yt-dlp 已过时（`a_bogus` 签名），要 TikHub key 或登录态 |
| TikTok | ✅ 走 TikHub 第三方解析 | 同上 |
| 快手 | ✅ 走 TikHub 第三方解析 | 同上 |
| 小红书 | ✅ | 带不带 cookie 都行 |
| 哔哩哔哩 | ✅（含 `b23.tv` 短链） | 带不带 cookie 都行 |
| YouTube | ✅ | 千万别给它带 cookie |
| 直链 `.mp4` | ✅ 直接下载 | — |
| 微博 | ⚠️ 需登录态 | 样本链接易失效 |
| 西瓜视频 | ⚠️ 解析器上游失效 | 等 yt-dlp 修复 |
| X / Instagram / Facebook / Vimeo / Twitch | ✅ | yt-dlp 有解析器 |
| 腾讯视频 / 爱奇艺 / 优酷 / 芒果TV | ⛔ 按设计拦截（DRM） | — |
| 好看视频 / 皮皮虾 / 微信视频号 | ❌ yt-dlp 无解析器 | 拿直链可下 |

---

## 快速开始

### 最省事：打包好的 exe

双击 `视频下载工具.exe` 就是一个原生窗口，粘贴链接 → 下载。见下文「打包成桌面应用」。

### 命令行

项目根目录双击 **`start.bat`**（或中文名 `启动.bat`），会自动找解释器、装依赖、
启动服务并打开浏览器。

```bat
start.bat serve                                     :: 启动服务（默认）
start.bat download https://www.bilibili.com/video/BVxxx   :: 只下一条
start.bat parse "7.92 复制打开抖音 https://v.douyin.com/xxx/"  :: 只看链接识别
start.bat resolve https://b23.tv/xxx                :: 只做短链展开
start.bat cdp-login douyin                          :: 登录抖音（登录态兜底）
start.bat doctor                                    :: 环境自检
start.bat selftest                                  :: 离线自测
```

> ⚠️ 本机 PATH 里可能没有 `python` / `uv`，命令统一用 `start.bat` 或
> `.venv\Scripts\python.exe run.py <子命令>`。

启动成功后打开 <http://127.0.0.1:8765/>。

> 💡 下载**抖音 / TikTok / 快手**需要 TikHub key 或登录态，见下一节；
> B站 / YouTube / 小红书等开箱即用。

---

## 抖音 / TikTok / 快手：走 TikHub 或登录态

这三个平台现在是 **`a_bogus` 请求签名**风控，yt-dlp 的解析器已经过时（带不带
cookie 都没用，实测 `403 Uifid Not Found`）。本地逆向签名不现实，走两条路：

### 主力：TikHub 第三方解析（推荐）

[TikHub.io](https://tikhub.io) 在服务端维护了签名算法，一次 API 调用直接返回无水印直链：

1. 注册 <https://user.tikhub.io>（邮箱即可）
2. Dashboard 顶部点 **Check-in** 签到领每日免费额度（不签到可能下不了）
3. **API Management → Pricing** 创建 API key，勾选**全部 Scopes** 保存
4. 填进 `config.json`：

```jsonc
{ "delivery": { "tikhub": { "api_key": "你的key" } } }
```

填完重启即可。免费档按签到额度走，量大需付费。

### 兜底：登录态（TikHub 额度不够时）

exe 界面顶部有 **🔑 登录抖音 / 🔑 登录TikTok** 按钮，点一下弹 Edge 扫码登录
（登录态存在工具自己的 profile，与日常浏览器隔离）。之后 TikHub 额度用完时
自动切到这个登录态本地解析。

命令行等价：`start.bat cdp-login douyin`（或 `tiktok`）。

---

## 小红书 / 西瓜：需要配 Cookie

小红书大多时候不带 cookie 也能下，被风控时带游客 cookie 即可；西瓜视频基本必须配。

```bat
start.bat cookies        :: 查看浏览器里有没有目标站点 cookie、用的什么加密
```

Chrome / Edge 127+ 启用了 **App-Bound 加密**（v20），yt-dlp 解不开，两条路：

- **cookies.txt（推荐）**：浏览器装「Get cookies.txt LOCALLY」扩展导出，填
  `config.json` 的 `download.cookies_file`。
- **直接读浏览器**：`start.bat login xiaohongshu`（会提示你关闭浏览器后自动读）。

> 本项目只用 yt-dlp 官方的 cookie 读取能力，不包含任何针对平台风控 / 人机验证的绕过。

---

## 配置说明（`config.json`）

首次运行自动生成，注释写在默认值里。几个关键项：

```jsonc
{
  "server": { "host": "127.0.0.1", "port": 8765, "token": "" },
  "net": { "proxy": "auto" },          // auto 跟随系统代理 | none 直连 | http://127.0.0.1:7890
  "download": {
    "quality": "best",                 // best / 1080 / 720 / 480
    "prefer_compatible": true,         // 优先 H.264+AAC，保证发哪都能播
    "cookies_from_browser": "",        // 填 chrome/edge 可下需登录的内容
    "ffmpeg_location": ""              // 留空自动查找
  },
  "search": {                          // 标题落源用的搜索引擎
    "serper_api_key": "",              // 推荐 https://serper.dev
    "google_cse_key": "", "google_cse_cx": ""
  }
}
```

**视频编码（重要）**：YouTube 现在默认给 AV1 + Opus，老手机 / 部分播放器 / 剪映
打不开。默认 `prefer_compatible: true` 会优先挑 H.264 + AAC（体积多约 70%，换来通用）。

**搜索落源**：不配 key 只有 DuckDuckGo 兜底（免 key，国内需走代理）。想要稳，
去 <https://serper.dev> 拿个 key 填进 `search.serper_api_key`。

---

## 打包成桌面应用

一个单文件 exe，双击就是一个原生窗口，不依赖闲鱼、不依赖上游：

```bat
.venv\Scripts\python.exe -m pip install pyinstaller
.venv\Scripts\python.exe build_desktop.py
```

产物是 **`dist\视频下载工具.exe`（约 118MB，单文件）**，双击即用。用户第一次运行
会在 exe 同级生成 `config.json` / `data` / `downloads` / `logs`。

> 分发时抖音/TikTok/快手两种玩法：① 你统一把 TikHub key 配进 exe 旁的 config.json
> （买家零操作）；② 让买家自己点「登录抖音/登录TikTok」走登录态兜底。其余平台开箱即用。

### 几个设计取舍

| 决定 | 原因 |
| --- | --- |
| 桌面外壳走浏览器应用模式（`msedge --app=`），不用 pywebview | pywebview 的 WebView2 初始化失败会递归重开自己（实测炸出 50+ 进程）；浏览器应用模式不挑环境、不会炸 |
| `user-data-dir` 放 `%LOCALAPPDATA%` | 不污染安装目录，也保证关窗能连带退出 |
| onefile（单文件） | 一个 exe 就能跑，拷贝分发省心；代价是冷启动慢几秒（要解压） |
| `console=False` | 不要黑框；致命错误弹 MessageBox，日志写 `logs/desktop.log` |
| 打包脚本连带 ffmpeg 依赖 DLL 一起收 | 只收 `ffmpeg.exe` 是陷阱（conda 版依赖同目录上百 DLL，缺了静默闪退）；脚本会顺 PE 导入表捞传递依赖，收进 exe 前真跑一次验证 |
| 窗口存活靠页面心跳而非 `proc.wait()` | Edge 启动器进程 ≠ 浏览器进程，`proc.wait()` 会立刻返回 |

排障：`logs/boot.log` 记每个启动阶段 + 进程号，`logs/desktop.log` 记程序输出。

---

## 目录结构

```
├── start.bat / 启动.bat           # 双击即用
├── run.py                        # CLI：serve / download / parse / resolve / cdp-login / doctor …
├── build_desktop.py              # 打包脚本
├── desktop.py / desktop.spec     # 桌面应用入口与 PyInstaller 配置
├── config.json                   # 首次运行自动生成
├── webui/index.html              # 前端（单文件，无外部依赖）
├── xydl/
│   ├── linkparse.py              # 链接识别：URL 抽取 / 平台判定 / 分享文案清洗
│   ├── resolver.py               # 短链展开 + 搜索落源
│   ├── downloader.py             # yt-dlp + 直链 + TikHub + 快手 + ffmpeg
│   ├── tikhub.py                 # TikHub 第三方解析（抖音/TikTok/快手无水印）
│   ├── cdp_fetch.py              # CDP 控制 Edge（登录态兜底）
│   ├── cdp_cookies.py            # CDP 拿明文 cookie
│   ├── pipeline.py               # 下载任务编排
│   ├── store.py                  # SQLite 持久化
│   ├── http.py                   # 纯标准库 HTTP（重定向、三态代理）
│   ├── channels/                 # 消息渠道层（控制台 / 收件箱 / 闲鱼桥接）
│   ├── uploaders/                # 交付通道（none / s3 / baidu）
│   └── config.py                 # 配置加载
└── tests/                        # 400+ 项测试，全部离线可跑
```

---

## 测试

```bat
.venv\Scripts\python.exe -m pytest          :: 全量，离线可跑
.venv\Scripts\python.exe -m pytest -k linkparse  :: 只跑链接识别
start.bat selftest                          :: 端到端冒烟
```

测试覆盖的不是「代码能跑」，而是几个真实踩过的坑：

* 短链判定必须按域名（`v.douyin.com` 要展开，`douyin.com/video/123` 不用）
* `\b` 在中文旁不成立
* 系统代理解析不能信环境变量（残留的死端口会盖掉真正的 Clash 端口）
* 下载失败不能误报成功（任务目录残留会污染「取最大文件」兜底）
* ffmpeg 候选必须实测能否运行（失效 shim 会静默不合流）

---

## 使用须知

这个工具只是一个下载调度器，**不绕过任何技术保护措施**。请确保只下载你有权下载
的内容（自己的作品、已获授权的内容、平台允许下载的公开内容）。下载后的内容请
遵守原平台的服务条款和当地法律。

---

## 附录：接闲鱼自动发货（可选，需要上游）

如果你还想做「闲鱼买家下单 → 自动下载 → 发链接交付」的自动化，需要一个能收发
闲鱼消息的上游（如 `xianyu-super-butler`），本项目负责下载和交付：

```text
买家付款 → 上游检测到付款 → POST 本机 /api/orders → 下载 → 传对象存储拿直链
        → POST 上游 /api/xianyu/deliver → 上游发链接给买家并标记发货
```

闲鱼聊天只支持文本和图片，发不了视频文件，所以交付只能是「传到某处 → 发链接」。

- 本机侧：授权交付通道、打开桥接渠道（`channels.xianyu_bridge.enabled`）、
  `start.bat bridge-ping` 探上游。
- 上游侧：改动在 `upstream_patch/`，含补丁模块 + 精确到行的插入说明。

> ⚠️ 上游走的是逆向闲鱼协议的路线，封号风险由上游承担 —— 本项目自身不含任何
> 逆向对接代码，只负责下载和把文件变成链接。

交付通道（`xydl/uploaders/`）可插拔：

| 通道 | 说明 |
| --- | --- |
| `s3` | **推荐**。对象存储预签名直链（COS / OSS / R2 / MinIO / 七牛），纯标准库 SigV4 |
| `baidu` | 百度网盘：上传可用，但分享链接需企业开发者认证 + 付费 |
| `none` | 默认。只下不发链接 |

> 百度网盘实测（2026-10）：上传 100% 可用，分享链接拿不到（`errno=2`，实为
> 未开通「文件分享服务」）。所以交付首选对象存储。
