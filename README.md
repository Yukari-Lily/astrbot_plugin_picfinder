# astrbot_plugin_picfinder (AI 增强多引擎全网识图)

[![AstrBot](https://img.shields.io/badge/AstrBot-Plugin-blue)](https://github.com/Soulter/AstrBot)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 专为 **AstrBot** 打造的高精度全网图像溯源与以图搜图插件。  
> 突破传统二次元搜图引擎的局限，优先以 **Google Lens** 视觉完全匹配检索全网，深度识别 **X (Twitter)** 原推、**哔哩哔哩 (Bilibili)** 视频/动态、**YouTube** 视频以及 **Pixiv** 出处；并融合 **SauceNAO**、**TraceMoe**、**Ascii2D**（自带反 403 客户端，见下文）以及 **AstrBot 原生多模态 AI** 画面理解与交叉智能提炼，大幅提升整体识图成功率！

---

## ✨ 核心特性

- 🔍 **Google Lens 顶级优先级**：
  - 充分利用 Google 覆盖全球互联网的超大索引，优先匹配原图与视觉完全一致项（Visual Matches）。
  - 不仅局限于动漫插画，各类现实照片、梗图、视频截图均可精准溯源。
- 🌐 **深度跨平台来源解析与规范化**：
  - **X (Twitter)**：提取作者 Handle、原推链接与作者主页（如 `https://x.com/author/status/...`）。
  - **哔哩哔哩 (Bilibili)**：识别视频 BV 号、动态/专栏 Opus ID，一键直达对应投稿。
  - **YouTube**：识别视频 ID、Shorts 短视频出处。
  - **Pixiv / Fanbox / Skeb / ArtStation**：提取画师、插画 ID 与原画页面。
- 🤖 **AstrBot 多模态 AI 深度赋能 & 自动全网联网主动检索**：
  - **画面预解析**：通过 AstrBot 当前 Provider（GPT-4o、Gemini 1.5/2.0、Claude 3.5 等）分析画面角色名（中/日/英）、所属作品/IP、画面内可见水印（如抖音号/微博名/推特ID/Cos作者名）与台词。
  - **AI 自动全网联网检索（告别推诿）**：针对 Cosplay 摄影、真人照、冷门同人等以图搜图库未收录的原图，系统**自动提取角色+水印+形式等关键词全网实时检索**（B站、X、微博、米游社、抖音等），直接找出匹配的动态、合集与发布者原帖，直接输出真实链接，绝不输出空泛的“建议你去搜”废话！
  - **交叉比对与去噪提炼**：AI 自动过滤掉无作者信息的低质二传网站（如 Pinterest 二传、壁纸爬虫站），甄别并置顶首发原作者。
- 🎯 **多引擎互补融合**：
  - **SauceNAO**：动漫二次元顶级图库，Pixiv / Danbooru 极高准确度，支持配置相似度阈值。
  - **TraceMoe**：专用于动漫截图，精确指出所属番剧、季数、第几集与精确到秒的时间戳。
  - **Ascii2D**：提供色调 + Bovw 双路特征检索；内置 **反 403 客户端**（CSRF token、会话 cookie、Chrome TLS 指纹、Cloudflare 过盾升级链），不再像 `PicImageSearch` 那样裸 POST 直接被墙掉。
- 🛡️ **Ascii2D 403 真正绕过（不只是熔断跳过）**：详见下方 [Ascii2D 403 绕过说明](#-ascii2d-403-绕过说明)。
- 🧦 **内置 Shadowsocks 隧道**：直接填一条 `ss://` 链接即可，插件自己在本机起 SOCKS5 + HTTP CONNECT 混合代理口，把 Ascii2D 流量整条送进隧道——**不用再装 clash / v2ray**，也不引入任何额外 pip 依赖（自带纯 Python 的 AES-GCM / ChaCha20-Poly1305 实现）。

---

## 📦 安装与配置

### 1. 安装插件
将本仓库克隆或复制到 AstrBot 的 `data/plugins/` 目录：
```bash
cd data/plugins
git clone https://github.com/Yukari-Lily/astrbot_plugin_picfinder.git
```

### 2. 安装依赖
在 AstrBot 的 Python 环境中安装依赖：
```bash
pip install -r data/plugins/astrbot_plugin_picfinder/requirements.txt
```

### 3. 配置参数
在 AstrBot WebUI 面板的插件配置中，可按需调整：

| 配置项 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `prioritize_google_lens` | bool | `true` | 是否优先展示 Google Lens 完全匹配结果 |
| `enable_ai_enhancement` | bool | `true` | 是否启用多模态 AI 画面分析与智能总结 |
| `ai_fallback_search` | bool | `true` | 搜图无果时是否由 AI 画面特征进行兜底推测 |
| `saucenao_api_key` | string | `""` | SauceNAO API Key（填入可提升并发与每日额度） |
| `saucenao_threshold` | number | `70` | SauceNAO 相似度阈值 (0 - 100) |
| `enable_ascii2d` | bool | `true` | 是否启用 Ascii2D 兜底检索（自带反 403 客户端） |
| `ascii2d_proxy` | string | `""` | Ascii2D 专用代理，留空跟随 `proxy`；建议日本/住宅线路 |
| `ascii2d_ss_uri` | string | `""` | 内置 Shadowsocks 节点来源：`ss://` 链接 / Clash JSON、YAML / sing-box JSON / base64 订阅；插件自己起本地代理口，优先级高于 `ascii2d_proxy` |
| `ascii2d_ss_node` | string | `""` | 多节点配置时按名字子串挑选（如 `jp-res`）；留空取第一个 ss 节点 |
| `ascii2d_cookie` | string | `""` | Ascii2D Cookie 串（`cf_clearance=...; _session_id=...`），过 403 最直接 |
| `ascii2d_browser_fallback` | bool | `true` | 遇 Cloudflare 人机校验时用 Playwright 真浏览器过盾（需自装 playwright） |
| `ascii2d_bovw` | bool | `true` | 色调结果不足时补一次 Bovw 特征检索 |
| `enable_tracemoe` | bool | `true` | 是否启用 TraceMoe 动漫画面识别 |
| `proxy` | string | `""` | HTTP/SOCKS 代理（访问 Google / SauceNAO 建议配置，如 `http://127.0.0.1:7890`） |
| `max_results` | number | `3` | 每个平台/引擎展示的最大候选条数 |
| `show_thumbnails` | bool | `true` | 是否在回复中附带结果缩略图 |
| `triggers` | list | `["识图", "搜图", "查图", "找图", "识图喵"]` | 触发命令列表 |

---

## 🛡️ Ascii2D 403 绕过说明

### 为什么会 403
`PicImageSearch` 的 `Ascii2D` 引擎本质上只是裸 `httpx` 发一个 `POST /search/uri`：
没有 Rails 的 `authenticity_token`（CSRF）、没有 `_session_id` 会话 cookie、
UA 停留在 Chrome/99、TLS 指纹是 Python 的。Ascii2D 前面挂着 Cloudflare，
这套组合会被直接判成脚本 —— 于是 `403 Attention Required`。

### 现在怎么绕过
`engines/ascii2d.py` 自己走完整条浏览器链路，逐级升级：

| 层级 | 手段 | 作用 |
| :--- | :--- | :--- |
| 1 | `GET /` 采集 `authenticity_token` + `_session_id` | 过 Rails CSRF 与无会话校验 |
| 2 | curl_cffi Chrome 指纹 + HTTP/2 + `Origin`/`Referer`/`Sec-Fetch-*` | 过 Cloudflare 的 TLS / 头部指纹规则 |
| 3 | 优先 `POST /search/file` 上传本地图，其次 `/search/uri` | 上传比远程抓取更容易被放行 |
| 4 | 指纹画像轮换（chrome → chrome124 → safari → edge）、冷却抖动 | 绕过单点风控特征 |
| 5 | 复用 `ascii2d_cookie` 里的 `cf_clearance` / `__cf_bm` | 直接用你浏览器已过盾的身份 |
| 6 | Playwright 真浏览器过 `Just a moment...` 人机校验 | 只有真 JS 引擎能过 Managed Challenge |
| 7 | 结果不足时补一次 Bovw 特征检索 | 裁剪 / 改色 / 翻转图更容易命中 |

全部失败才会降级跳过（并给出可执行的原因，例如 Ray ID、该配代理还是该配 cookie），
主搜图流程绝不会被卡住。

### 四种加速方案（按推荐顺序）

0. **用内置 Shadowsocks 隧道（最省事，不装任何外部软件）**：把节点链接填进
   `ascii2d_ss_uri`，插件会自动在本机 `127.0.0.1` 起一个混合代理口并把 Ascii2D
   的请求全部送进去：

   **支持四种写法**，直接把你手头的配置粘进去就行：

   ```ini
   # ① SIP002 / 明文 ss:// / 老式四段（单节点最省事）
   ascii2d_ss_uri = ss://YWVzLTEyOC1nY206cGFzc3dvcmQ@1.2.3.4:8388#jp-residential
   ascii2d_ss_uri = ss://aes-256-gcm:p%40ss@1.2.3.4:8388
   ascii2d_ss_uri = 1.2.3.4:8388:chacha20-ietf-poly1305:password
   ```

   ```json
   // ② Clash JSON（整份配置直接粘，只挑 type: ss 的节点；vmess/trojan/vless 会被跳过）
   {
     "proxies": [
       {"name": "JP-RES-01", "type": "ss", "server": "2.2.2.2", "port": 8388,
        "cipher": "aes-128-gcm", "password": "pw1"},
       {"name": "JP-RES-02", "type": "ss", "server": "3.3.3.3", "port": 8389,
        "cipher": "chacha20-ietf-poly1305", "password": "pw2"}
     ],
     "proxy-groups": [{"name": "PROXY", "type": "select", "proxies": ["JP-RES-01"]}],
     "rules": ["MATCH,PROXY"]
   }
   ```

   ```yaml
   # ③ Clash YAML —— 同样整份粘（装了 PyYAML 用 PyYAML，没装走内置兜底解析器，
   #    支持块状列表；流式 {..} 只在装了 PyYAML 时可靠）
   proxies:
     - name: "JP-RES-01"
       type: ss
       server: 2.2.2.2
       port: 8388
       cipher: aes-128-gcm
       password: "pw1"
   ```

   ```json
   // ④ sing-box outbounds / base64 订阅（订阅是 base64 包一层 ss:// 列表）
   {"outbounds": [{"type": "shadowsocks", "tag": "jp-box", "server": "5.5.5.5",
                   "server_port": 8391, "method": "aes-128-gcm", "password": "pw4"}]}
   ```

   多节点配置默认取**第一个 ss 节点**；要指定就加一条：

   ```ini
   ascii2d_ss_node = res-02      # 名字包含 res-02 的节点，大小写不敏感
   ```

   日志会打印最终选中的节点，挑不到时会列出可选节点名。

   - 支持 `aes-128-gcm` / `aes-256-gcm` / `chacha20-ietf-poly1305`（TCP over AEAD，即 SIP004），**不支持** UDP / obfs / 2022 系列；
   - 本地代理口是 SOCKS5 + HTTP CONNECT 混合模式：curl_cffi 走 SOCKS5 保住浏览器 TLS 指纹，httpx 走 HTTP CONNECT（免装 `socksio`）；
   - 一个依赖都不用额外装：没有 `cryptography` / `pycryptodome` 时自动切到内置纯 Python 实现（RFC 5116 / RFC 8439 标准向量验证）。纯 Python 版实测吞吐 AES-GCM ≈130 KB/s、ChaCha20 ≈570 KB/s，几十 KB 的结果页够用；嫌慢就 `pip install cryptography`，会自动切到 C 后端（快 50 倍以上）；
   - 想单独排查隧道是否通，可以手动起：

     ```bash
     python -m proxy.shadowsocks --uri "ss://..." --port 1080
     curl -x socks5h://127.0.0.1:1080 https://api.ipify.org
     ```

   > 注意：要的是**住宅/家宽**节点。机房节点即使在日本也照样被 Cloudflare 按 ASN 拦（本项目实测 Tokyo 的 Oracle Cloud 与 Zouter 托管 IP 均被硬拦）。

1. **配 HTTP/SOCKS 代理**：`ascii2d_proxy` 填 `http://user:pass@host:port`（或 `socks5h://`）。
2. **直接贴浏览器 Cookie**：浏览器打开 `https://ascii2d.net`，F12 → Network →
   任意请求 → 复制 `Cookie:` 整串填进 `ascii2d_cookie`（含 `cf_clearance`）。
3. **启用真浏览器过盾**：
   ```bash
   pip install playwright
   playwright install chromium
   ```
   装完把 `ascii2d_browser_fallback` 保持 `true` 即可。Docker / ARM 环境已自带
   Chromium 的，可用环境变量 `ASCII2D_CHROME_PATH=/path/to/chrome` 指定。

> 提示：内置识别器只认结果页 DOM，不依赖 `pyquery`/`bs4`，站点模板小改也不会崩。

---

## 🎮 使用方法

插件指令极简纯粹，**仅保留「识图」单一指令**，所有复杂的画师画像、平台出处深挖与更多作品检索均由聊天模型通过 Function Calling 自主处理：

1. **直接搜图**：
   - 发送：`识图 [附带图片]`
2. **回复搜图**：
   - 看到群友发的图片，右键引用/回复该消息并发送：`识图`
3. **后缀搜图**：
   - 发送：`[附带图片] 识图`

---

## 🛠️ LLM 函数工具 (Function Calling / 供聊天模型自主调用)

除了基础的 `识图` 指令外，插件将最强大的深度分析能力注册为了标准的 AstrBot LLM Tools，聊天模型可在日常自然语言对话中自主调用：

1. **`get_author_and_artworks_from_image(image_url: string)`** ⭐（画师与作品深度提取）：
   - **功能**：从指定图片中深度识别创作者/画师（X/Twitter、Pixiv、B站、Danbooru 等），并检索获取该画师的平台主页、媒体相册直达以及该作者的其他插画作品列表与直链。
   - **使用场景**：用户在聊天中发图并询问「这张图是谁画的？还有他画的其他作品吗？」时，大模型自主调用此工具，获取作者资料及其他作品向用户介绍。

2. **`get_author_artworks_by_name(author_name: string)`**：
   - **功能**：已知作者名或推特 Handle 时，直接检索该画师的主页、相册与代表插画。
   - **使用场景**：用户询问「看看米山舞的其他画作」或「推荐几张 lack 的插画」时调用。

3. **`extract_platform_source(url: string)`**：
   - **功能**：深度解析并提取文本或链接中的平台出处、作者/UP主、BV号/推文ID/视频ID与规范化原址。
   - **支持平台**：X (Twitter)、哔哩哔哩 (视频/动态/专栏)、YouTube (长视频/Shorts)、Pixiv (插画/画师)、ArtStation、Skeb、NicoNico 等。

4. **`search_web_by_keywords(keywords: string)`**：
   - **功能**：使用画面关键词、作品、角色名、水印文字或画师圈名，在全网（B站、X、微博、米游社、抖音等）进行实时联网检索并返回出处链接与动态。
   - **使用场景**：聊天模型在以图搜图未直接匹配出原图时，提取关键词主动全网深搜。

5. **`search_image_by_url(image_url: string)`**：
   - **功能**：对任意公开网络图片直链执行全网反向识图（Google Lens + SauceNAO + TraceMoe）。

---

## 💡 输出效果示例

```text
🤖 【AI 溯源与识别分析】
🎯 **识别结论**：角色为《原神》中的「芙宁娜 (Furina)」，画面风格为同人厚涂插画。
👑 **推荐首发**：[X / Twitter] 原推作者 @illustrator_furina 于 2024年发布的同人画作。
🌐 **全网出处**：同时收录于 Pixiv (ID: 112233445) 与 B站动态专栏。
────────────────
🔍 【Google Lens 全网视觉匹配】
🐦 [X / Twitter] @illustrator_furina 「Furina Fontaine Artwork」
🔗 https://x.com/illustrator_furina/status/1789012345678901234

📺 [哔哩哔哩 动态/专栏] 「芙宁娜同人插画分享」
🔗 https://www.bilibili.com/opus/9123456789

🎨 【SauceNAO 精准二次元】(最高相似度: 94.2%)
🎨 [Pixiv 插画] [94.2%] 画师: illustrator_furina 「水神」
🔗 https://www.pixiv.net/artworks/112233445
```

---

## 📄 开源协议
本项目采用 [MIT 协议](LICENSE) 开源。
