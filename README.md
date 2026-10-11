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
| **短链展开** | `v.douyin.com` / `b23.tv` / `xhslink.cn` / `v.kuaishou.com` / `t.cn` / `youtu.be` / `weixin.qq.com/sph/…` … 跟随重定向直到落地地址 |
| **标题落源** | 只发了一句标题、没发链接时，自动拿标题去 Google / DuckDuckGo 找原视频 |
| **无水印下载** | 抖音 / TikTok 走自研登录态解析、快手走自研 GraphQL（都拿无水印直链），其余平台走 yt-dlp |
| **多剧集 / 合集** | B站 分P / 合集 / 番剧 / UP主投稿 一条链接自动展开成 N 集，按 `01 02 03` 编号下载并打包成 zip；视频号短剧把各集链接一起粘进来即可批量下 |
| **微信视频号** | `weixin.qq.com/sph/…` 分享链接可直接识别；标题/作者/封面匿名可取，mp4 用元宝解析出的带 token 链接直接下；内置 ISAAC64 解密（前 128KB） |
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
| 抖音 | ✅ 自研登录态 CDP | yt-dlp 已过时（`a_bogus` 签名），跑一次 `cdp-login douyin` 即可 |
| TikTok | ✅ 自研登录态 CDP | 同上（`cdp-login tiktok`） |
| 快手 | ✅ 自研 GraphQL 解析 | 反爬严，可能需浏览器访问一次 |
| 小红书 | ✅ | 带不带 cookie 都行 |
| 哔哩哔哩 | ✅（含 `b23.tv` 短链·多剧集） | 带不带 cookie 都行；分P/合集/番剧/UP主投稿自动展开 |
| 微信视频号 | ✅ 自研解析（元宝链路拿明文 mp4） | 见下文专章；短链用元宝解析一次即可 |
| YouTube | ✅ | 千万别给它带 cookie |
| 直链 `.mp4` | ✅ 直接下载 | — |
| 微博 | ⚠️ 需登录态 | 样本链接易失效 |
| 西瓜视频 | ⚠️ 解析器上游失效 | 等 yt-dlp 修复 |
| X / Instagram / Facebook / Vimeo / Twitch | ✅ | yt-dlp 有解析器 |
| 腾讯视频 / 爱奇艺 / 优酷 / 芒果TV | ⛔ 按设计拦截（DRM） | — |
| 好看视频 / 皮皮虾 | ❌ yt-dlp 无解析器 | 拿直链可下 |
| 微信公众号文章 | ❌ 不做 | 正文视频基本是腾讯源（DRM） |

---

## 清晰度 / 水印：按订单可选

两个选项**都按订单生效**（界面上选、或命令行传），不会互相串（并发下单各选各的）。

### 清晰度 `--quality`

```bat
start.bat download "<链接>" --quality 1080     :: best / 1080 / 720 / 480 / 360
```

- 支持的站点交给 yt-dlp 的格式选择器（`height<=N` + 兼容编码优先 H.264/AAC）；
- 视频号交给 `spec[]` / 编码档位挑选（抓包通道能拿到真实分辨率档位）；
- **站点没有该档位会自动降级**，不会为了"必须是 1080"直接失败。

### 水印 `--watermark`

| 取值 | 行为 |
| --- | --- |
| `auto`（默认） | **优先取无水印源**；只有带水印版本时照常交付，并在交付信息里标注「带水印源」 |
| `off` | 只要无水印源；拿不到就**报错不交付**（宁可不交也不交带水印的） |
| `delogo` | 优先无水印源；只有带水印版本时下完自动用 ffmpeg 擦掉（**有损**） |
| `any` | 不挑，拿到哪个算哪个（也不标注） |

**"优先取无水印源"是无损做法**：平台常同时给两份地址（抖音 `playwm` 与 `play`
就是同一份视频的两个地址），工具会改写并**按顺序尝试** —— 干净版不存在时自动退回
带水印那份，而不是直接失败。

### 像素级擦水印（有损，对成片用）

```bat
start.bat delogo "D:\downloads\a.mp4" --platform douyin        :: 按平台预设（右下角）
start.bat delogo "D:\downloads\a.mp4" --rect 760:1790:300:100  :: 手动指定，最准
start.bat delogo "D:\downloads\a.mp4" --platform douyin --dry-run  :: 只看会用哪个矩形
```

- 默认**另存** `xxx.nowm.mp4`，覆盖源文件要显式 `--in-place`（有损处理要能回退）；
- 原理是 ffmpeg `delogo`：用周边像素**插值覆盖**水印区域，会留淡淡痕迹、局部画质下降；
- 想无痕只能上 AI inpaint（模型 + GPU，单文件 exe 不划算），本项目不做。

---

## 快速开始

### 最省事：打包好的 exe

双击 `视频下载工具.exe` 就是一个原生窗口，粘贴链接 → 下载。见下文「打包成桌面应用」。

### 命令行

项目根目录双击 **`start.bat`**（或中文名 `启动.bat`），会自动找解释器、装依赖、
启动服务并打开浏览器。

```bat
start.bat serve                                     :: 启动服务（默认）
start.bat download https://www.bilibili.com/video/BVxxx   :: 只下一条（多剧集自动展开）
start.bat series https://space.bilibili.com/xxx/video     :: 先看会下几集（不下载）
start.bat wxinfo https://weixin.qq.com/sph/xxxxx     :: 看视频号能解析到什么
start.bat channels-login                            :: 登录视频号助手（诊断用；实测不能下视频号视频）
start.bat parse "7.92 复制打开抖音 https://v.douyin.com/xxx/"  :: 只看链接识别
start.bat resolve https://b23.tv/xxx                :: 只做短链展开
start.bat cdp-login douyin                          :: 登录抖音（登录态兜底）
start.bat doctor                                    :: 环境自检
start.bat selftest                                  :: 离线自测
```

> ⚠️ 本机 PATH 里可能没有 `python` / `uv`，命令统一用 `start.bat` 或
> `.venv\Scripts\python.exe run.py <子命令>`。

启动成功后打开 <http://127.0.0.1:8765/>。

> 💡 下载**抖音 / TikTok / 快手**需要先登录一次（自研解析），**微信视频号**用元宝解析；
> B站（含多剧集）/ YouTube / 小红书等开箱即用。

---

## 抖音 / TikTok / 快手：全自研解析

这三个平台现在是 **`a_bogus` 请求签名**风控，yt-dlp 的解析器已经过时（带不带
cookie 都没用，实测 `403 Uifid Not Found`）。本地逆向签名不现实，所以走**自研登录态**：在工具自己的 Edge profile 里打开一次
站点（扫码登录），之后解析时直接截页面**自己带签名发出**的请求，拿无水印直链 ——
不依赖任何第三方付费服务。

exe 界面顶部有 **🔑 登录抖音 / 🔑 登录TikTok** 按钮，点一下弹 Edge 扫码登录
（登录态存在工具自己的 profile，与日常浏览器隔离），登录一次之后长期可用。

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

## 多剧集下载（B站 / 视频号）

一条链接对应一整部剧时，工具会自动展开成 N 集，**逐集编号下载**，默认打包成一个
zip 交付（客户拿到一堆同名 mp4 是排不出顺序的）。

### B站：一条链接通吃所有形态

| 链接形态 | 例子 | 结果 |
| --- | --- | --- |
| 多分P | `bilibili.com/video/BV1xx?p=1` | 每个分P 一集 |
| 合集 / 系列 | `space.bilibili.com/<mid>/channel/collectiondetail?sid=…` | 合集内全部视频 |
| UP主投稿 | `space.bilibili.com/<mid>/video` | 该 UP 的投稿（受上限约束） |
| 番剧 | `bilibili.com/bangumi/play/ss…` | 按集展开 |
| 列表 / 收藏夹 | `bilibili.com/list/…` | 列表内全部 |

实现上只用 yt-dlp 的 `--flat-playlist -J`（**只取条目清单，不逐集解析详情**），
所以一个 50 集的合集探测只要一次请求；每集真正的标题在下载时由 yt-dlp 自己探测。

### 视频号：整部剧怎么批量下

视频号**没有公开的匿名作品列表接口**，所以"自动列出整部剧"离不开客户端上下文
（注入/抓包）。本项目不依赖第三方付费解析，因此走一条更实在的做法：

- 标题里的剧集规律会被识别（`出狱后，我举世无双-第1集` → 剧名 `出狱后，我举世无双`），
  用于命名与日志提示；
- **把各集在元宝里解析出的链接一起粘进来（一行一条）**，管线会逐条下好并按集号命名；
- 拿不到就如实说明原因，**绝不假装成功**（假装成功比报错更坑：买家以为在下一整部）。

### 控制范围

```jsonc
"download": {
  "series": {
    "enabled": true,        // 关掉就回到「一次一条链接」的老行为
    "max_episodes": 50,     // 误把 800 个投稿全下了是真实事故，所以有上限
    "episode_range": "",    // 只下指定集，如 "1-10,15"
    "mode": "zip"           // zip 打包交付 | separate 保留多个文件
  }
}
```

接单前先看一眼会下多少集，避免"以为一集、结果整部剧"：

```bat
start.bat series https://space.bilibili.com/946974/video -n 10   :: 看前 10 集会下什么（不下载）
start.bat download "https://www.bilibili.com/video/BVxxx" --range 1-5   :: 只下 1~5 集
start.bat download "https://www.bilibili.com/video/BVxxx" --single      :: 只下当前这集
```

### 全部下载 vs 指定剧集

**两种都支持**，靠 `episode_range` 切换：

| 想要的 | 怎么做 | 结果 |
| --- | --- | --- |
| **整部/整个合集** | `episode_range` 留空（默认） | 源里有多少集就下多少，最多 `max_episodes`（默认 50） |
| **指定几集** | `--range "1-5,8,12"` 或 config 里写 `episode_range` | 只下这几集 |
| **只要当前这一集** | `--single` | 完全不展开，按单集处理 |

区间写法：`1-10,15`、`3`、`20-30`；逗号/中文逗号/空格/顿号都能当分隔符；
越界的号（如只有 10 集却写 `99`）会被忽略；**整段都解析不出来时会直接报错停下**
（订单转「需人工」），不会静默改成"全下"——运营写 `1-5` 的本意是省流量，
悄悄变成下 50 集是**放大**损失。

两个已修的坑，正好都是这两条路交界的：

1. **指定超出上限的集数**：`max_episodes=2` + `--range 5` 以前会匹配不到、静默
   退化成去下载合集页。现在会**按你点名的最大集号自动扩大探测窗口**（点第 5 集就取够 5 条）。
2. **链接里带 `?p=N`**：这种链接 yt-dlp 只返回那一集。所以**写了区间时会去掉 `p`
   去探测整部**（你要的是"这部剧的第几集"），没写区间则保持"只下这一集"。
   实测：链接是 `?p=2`、`--range 1` → 日志明确写 `实际下载：…?p=1`，下到 39MB 的第 1 集，
   而不是 207MB 的第 2 集。
3. 指定区间后**不会再报**"已达单次上限"——你已经明确选了自己要的集。

产物结构（两种模式下逐集文件都会留着，方便单独重发某一集）：

```text
downloads/<订单号>/
├─ episodes/01 某某短剧-第1集.mp4
├─ episodes/02 某某短剧-第2集.mp4
└─ 某某短剧.zip                 ← zip 模式下交付给客户的是这个
```

> 部分集失败不影响其余集：订单仍算完成，失败集写进订单备注（控制台卡片上会显示
> 「N/M 集 · 失败 K 集」），不会静默丢集。

**交付话术**（都在 `config.json` 的 `replies` 段里，运营可改）：

| 键 | 什么时候用 | 可用占位符 |
| --- | --- | --- |
| `series_done` | 全部成功 | `{title} {total} {count} {filename} {size}` |
| `series_partial` | **有集失败时强制用** | 同上 + `{failed}` |
| `series_truncated_note` | 被 `max_episodes` 截断时追加 | `{total}` |

> `{count}` 是"成功几集"、`{total}` 是"总共几集"，**两者必须分开写**：
> 买家按整部剧付钱，不能让他以为 50 集里下到的 5 集就是全部。
> 就算把 `series_partial` 清空，工具也会在 `series_done` 后面追加一句不可移除的
> 失败提示 —— 这条信息不允许被配置藏掉。

---

## 微信视频号（`/sph/xxx` 短链 / `/feed?token=…` 分享链接）

视频号和别的平台有两点根本不同，值得单独说明。

### 1. 分享链接不是内容页

```text
https://weixin.qq.com/sph/AJizVCRhAt
  └─ 301 ─→ https://channels.weixin.qq.com/finder-preview/pages/sph?id=AJizVCRhAt
              ↑ 一个 React 空壳页：正文只有 <div id="app">，没有 <video>，没有内嵌 JSON
```

**yt-dlp 完全没有视频号解析器**（实测确认：`wei|channel|finder|weixin` 一个都不存在），
所以视频号走自研解析链：

```text
分享链接 → 短码/exportId
   ├─ ① 匿名预览接口  POST /finder-preview/api/feed/get_feed_info   ← 不需登录
   │      拿标题 / 作者 / 封面 / 点赞 / exportId（**拿不到 mp4**）
   ├─ ② 元宝链路     把短链发给腾讯元宝 → 拿回带 token 的 feed 链接
   │      → 匿名接口直接下发**明文 MP4**（不需要解密）
   ├─ ③ 本地抓包     `channels-capture` 截微信 PC 客户端的媒体请求
   │      顺带拿到 spec[] 全档位（分辨率可选）与 decode_key
   └─ ④ 登录态浏览器   CDP 打开 Edge 抓 finder.video.qq.com 的媒体请求
          ⚠️ 实测无效，默认关闭（证据见下）
```

① 是唯一不需要登录的接口，所以**即使 mp4 拿不到，也能拿到标题和封面**
（文件名、订单标题、回复话术都用得上）。

```bat
start.bat wxinfo https://weixin.qq.com/sph/AJizVCRhAt   :: 逐级打印解析结果，不下载
```

#### 为什么"扫码登录再用浏览器抓"走不通（2026-10 实测）

这一条我一开始是打算当主力方案做的，**实测后被证伪**，所以默认关掉并写在这里，
免得再有人走一遍冤枉路：

| 试过的事 | 结果 |
| --- | --- |
| 扫码登录视频号助手（`channels.weixin.qq.com/platform`，已确认登录成功） | ✅ 登录态有效，控制台能看到账号 |
| 用这个登录态打开**预览页** `finder-preview/pages/sph?id=…` | ❌ 整页 28 个请求里**没有任何视频请求**；页面仍只渲染**封面图 + 二维码**，写着"可扫码前往微信观看此内容" |
| 用这个登录态打开**网页版** `web/pages/feed?eid=…` | ❌ 匿名请求返回 `{"errCode":10012 非法请求}`；带着登录态打开也拿不到 feed/媒体请求 |
| 登录入口本身 | `/platform` 是**视频号助手（创作者后台）**，和消费者侧的网页播放不是同一套授权 |

唯一在浏览器里能拿到的 `stodownload` 地址是**封面图**（路径 `/20350/`），
视频在 `/20302/`。代码里用 `looks_like_video_url()` 把两者分开 —— 否则会把封面
JPEG 当视频交付出去（这是"看起来成功"的那种事故）。

> 顺带修掉的坑：一开始登录入口我写成了 `/web/pages/home`，实测那个地址匿名访问
> 返回的是 **JSON** `{"errCode":10012}`，浏览器打开一片空白、根本没有二维码。
> 正确的登录页是 `/platform`。

所以视频号现在的主路是**元宝链路**（带 token 的 feed 链接一次请求拿明文 MP4）。
另外项目自带一条**本地抓包**兜底（`channels-capture`）：装根证书 + 改系统代理 +
人工在客户端里点开一次视频，适合本机诊断与拿分辨率档位，默认关闭。

### 2. 下载到的文件前 128KB 是加密的

视频号 CDN（`finder.video.qq.com/.../stodownload`）下发的是**被 ISAAC64 伪随机流
XOR 过的 MP4**：只有前 131072 字节加密，其余是明文。直接交付 = 客户看到花屏 / 打不开。

工具内置了完整解密（`xydl/wxdecrypt.py`）：

- 种子 = `decode_key`（API 返回的十进制字符串），ISAAC64 初始化后按 `randrsl[255]→[0]`
  取数、大端展开，XOR 前 128KB。
- **解码正确性有黄金向量兜底**：公开样本 + `decode_key=2136343393` 解密后前 32 字节
  必须是 `00000020 66747970 69736f6d…`（`ftyp isom`）。这条断言在单元测试和
  `selftest` 里都有。
- **安全策略**：只在解密结果通过 MP4 头校验时才覆盖原文件；校验不过就保留原文件并
  报错 —— 宁可让人工介入，也绝不交付被二次 XOR 破坏的文件。

`decode_key` 与直链**必须来自同一次 API 响应**（每次请求都会变），所以代码里两者
是一次性配对返回、一起入库的。

### 怎么用：粘链接就行（不需要登录、不需要抓包）

在微信里对视频号视频点**"复制链接"**，得到的是这种形态：

```
https://channels.weixin.qq.com/finder-preview/pages/feed
  ?comment_scene=39&token=XXXX&eid=export%2FUzFfBgAAxMOj…
```

把这条链接粘进工具，**直接就能下**（实测 11MB 视频 0.2 秒下完，明文 MP4，标题自动命名）：

```bat
start.bat download "https://channels.weixin.qq.com/finder-preview/pages/feed?token=…&eid=…"
start.bat wxinfo  "https://channels.weixin.qq.com/finder-preview/pages/feed?token=…&eid=…"  :: 只看解析
```

**关键就在那个 `token`**：它会被放进请求体的 `baseReq.generalToken`。

| `generalToken` 传什么 | 服务端返回 |
| --- | --- |
| **空字符串**（`weixin.qq.com/sph/xxx` 短链只能这样） | 只有标题/作者/封面/点赞 —— **媒体字段被剥离** |
| **链接里的 token** | `feedInfo.videoUrl` + `h264VideoInfo` + `h265VideoInfo` —— **明文 MP4 直链** |

所以：
- **带 token 的分享链接** → 一条 HTTP 请求拿到直链，不需要登录、不需要抓包、**也不需要解密**；
- **只有 `/sph/xxx` 短链**（没有 token）→ 只能拿元数据；用元宝解析一次换成带 token 的链接即可。
- 清晰度选 **h264**（`X-snsvideoflag=xWT113`）：页面播放器偏向 h265，但我们要交付给客户，
  h265 在老手机/剪映上打不开，所以取兼容性更好的那条。

> 这条路是把视频号前端 JS（`merlin.js` / `virtual_svg-icons-register.publish*.js`）挖出来、
> 逐个接口实测才定位到的：`/web/api/*` 需要客户端内部上下文（`LoadCtx failed -2`）、
> `/cgi-bin/micromsg-bin/*_findergetcommentdetail` 需要原生桥签名（`Cannot POST`），
> 而 `finder-preview/api/feed/get_feed_info` 只要 token 就完整下发媒体。全是公开接口的正常调用，
> 不抓包、不注入、不破解客户端。

诊断用（不下载，逐级打印）：

```bat
start.bat wxinfo "https://channels.weixin.qq.com/finder-preview/pages/feed?token=…&eid=…"
```

### 2b. 兜底：本地抓包（自研）

如果手上只有 `/sph/xxx` 短链，先用元宝把它换成带 token 的 feed 链接；实在拿不到
token 时，还有一条本地抓包兜底（见下一节）：让微信 PC 客户端播一次视频，在本机把
它的媒体请求截下来（顺带能拿到 `spec[]` 分辨率档位与 `decode_key`）。

> 抓包拿到的是**加密**文件（前 128KB ISAAC64），本项目会自动解密；
> 元宝那条是**明文**，不需要解密 —— 两条路的产物都能直接交付。

### 3. 兜底：内置本地抓包（`channels-capture`）

只有在"拿不到 token"时才需要它：让微信 PC 客户端播这个视频，
在本机把它的流量截下来。这条路已经做进项目自己，不用装第三方工具：

```bat
start.bat channels-capture              :: 起抓包服务（自动装当前用户证书 + 设系统代理）
:: 然后去电脑端微信里把那个视频播一下
start.bat channels-capture --status     :: 看证书/代理/已抓到几条
start.bat channels-capture --uninstall  :: 卸载证书
```

抓到的结果落在 `data/wx_capture.json`，流水线会自动用它：

```bat
start.bat wxinfo https://weixin.qq.com/sph/xxxxx    :: ②b 会显示本地抓包通道的情况
start.bat download https://weixin.qq.com/sph/xxxxx  :: 直接下（解密也是自动的）
```

#### 三条自我约束（都为了让"静默"不等于"乱来"）

| 约束 | 具体做法 |
| --- | --- |
| **选择性中间人** | 只解密视频号相关域名（`channels.weixin.qq.com`、`finder.video.qq.com`、`wxapp.tc.qq.com`）；其余域名**字节级原样隧道转发**，不解密、不记录 |
| **不装机器级证书** | 证书装进**当前用户**根证书库（`certutil -user`），**不需要管理员权限**，也不影响别的用户 |
| **不留后遗症** | 改系统代理前先记录原值（你机器上是 `127.0.0.1:7897`，Clash），**原系统代理作为链式上游**保留，退出时还原；进程被杀也会在下次启动时自检残留 |

> 证书是我们**自己签的**（openssl 或 `cryptography` 现生成，私钥只在你机器上），
> 不像某些工具那样内置一份全世界共用的 CA 私钥 —— 那种 CA 私钥一旦公开，
> 任何人都能冒充任意 HTTPS 网站。

#### 如果微信不走系统代理

有些环境里微信的视频号流量不理会系统代理。那就用 Clash（你本来就在跑）：

```bat
start.bat channels-capture --no-system-proxy
```

然后在 Clash 的 `Global Extend Script` 里把 `qq.com` 指过来（端口默认 `2023`）：

```js
function main(config) {
  config["proxies"].unshift({ name: "xydl_capture", type: "http",
                              server: "127.0.0.1", port: 2023 });
  config["rules"].unshift("DOMAIN-SUFFIX,qq.com,xydl_capture");
  return config;
}
```

#### 还能直接用外部工具抓的结果

同类工具（社区主流是 [ltaoo/wx_channels_download](https://github.com/ltaoo/wx_channels_download)，
本机代理 `127.0.0.1:2023` + 机器级根证书 + 往页面注入下载按钮）抓到的**直链**同样能用：

```bat
:: 它已经把文件下下来了：就地解密（只解前 128KiB，解错会保留原文件）
start.bat decrypt "D:\下载\某个视频号视频.mp4" --decode-key 2136343393

:: 只有直链：直接丢给我们（没给 decode_key 会被明确拒收，不会交付花屏文件）
start.bat download "https://finder.video.qq.com/251/20302/stodownload?encfilekey=..."
```

```text
❌ 这是视频号加密文件（前 128KiB 被 ISAAC64 加密），直接交付会花屏/打不开。
   需要该直链对应的 decode_key，然后跑：run.py decrypt "<文件>" --decode-key <key>
```

> 为什么不走"给微信内置浏览器开调试端口"那条更省事的路：本机实测微信 3.9 的
> `WeChatAppEx.exe`（CEF 内核）被腾讯**裁掉了 `remote-debugging-port`**
> （同文件里 `user-data-dir`/`no-sandbox` 都在，说明搜索没问题），所以那条路在
> 这个版本上不存在。抓包是唯一可行的本地路线。

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
    "ffmpeg_location": "",             // 留空自动查找
    "series": {                        // 多剧集 / 合集（B站分P·合集·番剧·UP主投稿）
      "enabled": true, "max_episodes": 50,
      "episode_range": "", "mode": "zip"
    },
    "wechat_channels": {               // 微信视频号
      "enabled": true,
      "use_browser": false,            // 登录态浏览器抓取：**实测对视频号无效**，别开
      "browser_wait_sec": 25,
      "auto_decrypt": true             // 前 128KB ISAAC64 解密；关掉会交付打不开的文件
    }
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

> 分发时抖音/TikTok/快手：让买家点一次「登录抖音/登录TikTok」（登录态存在工具自己的
> profile 里，长期有效），之后自动解析。其余平台开箱即用。

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
│   ├── downloader.py             # 引擎降级：yt-dlp + 直链 + CDP/快手 + ffmpeg
│   ├── watermark.py              # 无水印源优先 + ffmpeg delogo（像素级去水印）
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
