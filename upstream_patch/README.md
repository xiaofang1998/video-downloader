# 上游补丁：把视频订单交给 xianyu-video-bot

这是给 **闲鱼超级管家（xianyu-super-butler）** 用的补丁。上游仓库不在这台机器上，
所以这里给你三个文件 + 明确到行的改法，动手前不会摸黑。

```
upstream_patch/
├── xianyu_video_bridge.py     ← 拷到上游根目录（和 XianyuAutoAsync.py 同级）
├── xianyu_video_bridge.json   ← 拷到上游根目录，然后按需改
└── README.md                  ← 本文件
```

---

## 它做了什么

```
闲鱼买家：先发一条视频链接  →  拍下付款
                                    │
   ① 上游 WebSocket 收到「我已付款，等待你发货」
   ② 补丁从 ai_conversations 翻出这位买家刚发的链接
   ③ POST 本机 video-bot  /api/orders     ← 跳过卡券发货
                                    │
   ④ video-bot：识别链接 → 展开短链 → yt-dlp 下载 → 传百度网盘 → 拿分享链接
   ⑤ POST 上游  /api/xianyu/deliver
   ⑥ 上游把文案（含网盘链接+提取码）发给买家，并把订单标记为已发货
```

设计上有两条底线：

- **不改上游任何原有逻辑**。补丁只在「检测到付款」这个分支前面插了一句判断，
  失败/异常一律回退到原来的卡券发货。
- **删掉就能恢复原状**。删掉 `xianyu_video_bridge.py` 和那两处插入即可。

---

## 前置条件

| 在哪 | 要准备什么 |
| --- | --- |
| 本地 video-bot | 跑起来（`start.bat` 或 `run.py serve`），默认 `127.0.0.1:8765` |
| 本地 video-bot | 百度网盘授权过：`run.py baidu-login` |
| 本地 video-bot | `config.json` 里 `channels.xianyu_bridge.enabled = true` |
| 上游 | 正常登录了闲鱼账号（账号在线，有 WebSocket 连接） |

> ⚠️ 前提是上游本身能跑通。它走的是逆向闲鱼协议的路线，本身有封号风险 ——
> 这条风险由上游承担，与本功能的实现无关。

---

## 第 1 步：拷文件

把 `xianyu_video_bridge.py` 和 `xianyu_video_bridge.json` 拷到上游根目录
（也就是 `XianyuAutoAsync.py`、`reply_server.py`、`Start.py` 所在的那一层）。

---

## 第 2 步：改 `XianyuAutoAsync.py`（只加 9 行）

打开 `XianyuAutoAsync.py`，搜 `_is_auto_delivery_trigger`，找到这个分支
（大约在 7785 行附近）：

```python
            # 【重要】检查是否为自动发货触发消息 - 即使在人工接入暂停期间也要处理
            elif self._is_auto_delivery_trigger(send_message):
                logger.info(f'[{msg_time}] 【{self.cookie_id}】检测到自动发货触发消息，即使在暂停期间也继续处理: {send_message}')
                # 使用统一的自动发货处理方法
                await self._handle_auto_delivery(websocket, message, send_user_name, send_user_id,
                                               item_id, chat_id, msg_time)
                return
```

把它改成（**加粗的部分是新增的**）：

```python
            # 【重要】检查是否为自动发货触发消息 - 即使在人工接入暂停期间也要处理
            elif self._is_auto_delivery_trigger(send_message):
                logger.info(f'[{msg_time}] 【{self.cookie_id}】检测到自动发货触发消息，即使在暂停期间也继续处理: {send_message}')

                # ── 视频代下载桥接：交出去就不要再发卡券 ──────────────
                try:
                    from xianyu_video_bridge import handoff_to_video_bot
                    if await handoff_to_video_bot(
                        self, message, item_id, chat_id, send_user_id, send_user_name
                    ):
                        return
                except Exception as _bridge_err:
                    logger.error(f'[{msg_time}] 【{self.cookie_id}】视频桥接异常，回退到卡券发货: {_bridge_err}')
                # ──────────────────────────────────────────────────────

                # 使用统一的自动发货处理方法
                await self._handle_auto_delivery(websocket, message, send_user_name, send_user_id,
                                               item_id, chat_id, msg_time)
                return
```

关键点：`handoff_to_video_bot()` 返回 `True` 才 `return`。
**没找到链接、video-bot 没起来、配置关了 —— 全部返回 `False`，继续走卡券发货。**
所以这个补丁不会让原本能用的自动发货变哑。

---

## 第 3 步：改 `reply_server.py`（只加 2 行）

打开 `reply_server.py`，搜 `app = FastAPI(` （大约 388 行），在**这个语句块之后**插入：

```python
app = FastAPI(
    title="Xianyu Auto Reply API",
    version="1.0.0",
    description="闲鱼自动回复系统API",
    docs_url="/docs",
    redoc_url="/redoc"
)

# ── 视频代下载桥接：注册 POST /api/xianyu/deliver ──
from xianyu_video_bridge import install_deliver_endpoint
install_deliver_endpoint(app)
# ─────────────────────────────────────────────────
```

顺序无所谓，只要在 `app` 建好之后、模块加载完成之前即可。

---

## 第 4 步：两边配置对上

**上游** `xianyu_video_bridge.json`（就放在上游根目录）：

```jsonc
{
  "enabled": true,
  "local_url": "http://127.0.0.1:8765",   // video-bot 的地址
  "local_token": "",                       // 对应 video-bot 的 server.token
  "item_ids": [],                          // ★ 强烈建议填上视频商品的 item_id
  "link_lookback": 30,
  "buyer_roles": ["user"],
  "auth_token": "",                        // 回传接口的共享密钥，见下
  "mark_shipped": true
}
```

**本地** `config.json`（video-bot 侧）：

```jsonc
{
  "server": { "token": "" },               // 设了就要和上游 local_token 一致
  "channels": { "xianyu_bridge": { "enabled": true } },
  "delivery": {
    "uploader": "baidu",
    "mode": "share_link",
    "bridge": {
      "enabled": true,
      "upstream_url": "http://127.0.0.1:8080",
      "deliver_path": "/api/xianyu/deliver",
      "token": "",                          // 设了就要和上游 auth_token 一致
      "source_channels": ["xianyu"]
    }
  }
}
```

> ★ `item_ids` 建议**显式列出**要接的商品。留空（`[]`）表示所有商品都走桥接，
> 那意味着你店里别的商品点「我已付款」之后也不再发卡券了 —— 这是最容易踩的坑。

---

## 验证顺序（一步一步来，别跳）

```bash
# 1. video-bot 自带的环境自检
run.py doctor
#    「[交付（网盘链接）]」里应该看到：上传通道 百度网盘 / 授权状态 ✓ refresh_token 已配置

# 2. 单独测上传（最重要的一步，先确认能拿到链接）
run.py upload "D:\某个视频.mp4"

# 3. 探上游
run.py bridge-ping
#    期望：✓ http://127.0.0.1:8080/health → HTTP 200

# 4. 起两边服务
#    video-bot：start.bat
#    上游：      python Start.py

# 5. 用你自己的小号在闲鱼里走一遍：
#    发一条视频链接 → 拍下付款 → 看买家侧是不是收到带网盘链接的文案
```

不想动真账号的话，可以先用 console 单测本地链路（故意不填 `channel: "xianyu"`，
桥接渠道就不会推回闲鱼）：

```bash
curl -X POST http://127.0.0.1:8765/api/orders \
  -H "Content-Type: application/json" \
  -d '{"text":"https://v.douyin.com/xxxx/","conversation_id":"test1","sender":"me"}'
```

---

## 排错

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 上游日志 `没找到买家发的链接` | 买家付款前没发链接，或 `ai_conversations.role` 不是 `'user'` | 核对上游表里的 `role` 取值，改 `buyer_roles` |
| 上游日志 `提交给 video-bot 失败` | video-bot 没启动 / 端口不对 / token 不匹配 | 先 `run.py bridge-ping`，再查 `local_url` 与 `local_token` |
| 买家收到「（网盘链接稍后补发）」 | 网盘上传或分享失败 | 本地控制台看该订单的事件日志；先 `run.py upload` 单独验证 |
| 回传接口 409 `没有在线实例` | 该闲鱼账号的 WebSocket 断了 | 上游里确认账号在线状态 |
| 回传接口 400 `缺少 cookie_id` | 上游提交时没带 meta | 确认 `handoff_to_video_bot` 真的被调用了（上游日志有「✅ 已交给 video-bot」） |
| 链接发出去被闲鱼吞掉 | 闲鱼对外链有风控 | 换短链中转页，或把链接放进「自动发货内容」让买家自己去订单页看 |
| 分享链接创建失败（`errno=2` 或 `13998 invalid app`） | 百度「文件分享服务」是企业开发者付费能力，个人应用拿不到 | 把本地 `delivery.mode` 改成 `upload_only`，或换交付通道（见主 README） |

看本地订单和事件的完整过程：打开 video-bot 控制台 `http://127.0.0.1:8765/`，
每个订单点开都有 `[桥接]` 开头的日志。

---

## 回滚

1. 删掉上游根目录的 `xianyu_video_bridge.py` / `xianyu_video_bridge.json`；
2. 把第 2、3 步加的那两段删掉。

上游即恢复原样。或者更省事：把上游 `xianyu_video_bridge.json` 里的
`"enabled"` 改成 `false`，补丁立即失效，不用改代码。
