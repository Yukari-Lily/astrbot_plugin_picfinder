# astrbot_plugin_picfinder v1.0

以图搜图，先找**精确出处**，找不到就直说找不到。

优先级：**Google Lens 完全匹配** → **SauceNAO** → **Ascii2D** → **TraceMoe** → **Yandex 收录页**，
再用平台接口补全作者/日期，并下载候选原图做 **多指纹核对（含裁切/滤镜容错）**，把二传图和相似图剔掉。

---

## 它现在怎么工作

```
用户图片
  ├─ Google Lens（完全匹配标签页，Playwright 浏览器）
  ├─ SauceNAO（Pixiv / 二次元图库）
  ├─ Ascii2D（色调检索 + bovw 特征检索）
  ├─ TraceMoe（动漫截图：番剧、集数、时间点）
  └─ Yandex（收录了这张图的网页，放最后）
        ↓
  平台接口补全：X(fxtwitter) / Pixiv / Bilibili / Danbooru / YouTube / OG
        ↓
  下载候选原图 → 多指纹比对：整图 dHash/pHash + 翻转/反色 + 任意比例裁切滑窗
  命中裁切/滤镜会标成「原图一致（裁切/滤镜）」，不会把不同图硬说成原图
        ↓
  每个引擎最多取 2 条，按「是否核对 + 平台权重」排序
```

输出格式与触发方式遵循原版 `picfinder_take`（合并转发，每条结果一个节点）：

```
————>saucenao<————
[缩略图]
[pixiv][https://www.pixiv.net/artworks/100000000] 相似度:96.13%
「菜花黄名子ちゃん」/「ちのすけ」
————>google lens<————
[缩略图]
[Twitter][https://x.com/.../status/...] 相似度:100.00%
「2026-11-24」/「artist」
```

缩略图沿用原版 `ats_pic` 的四角随机像素处理；下载失败显示 `[预览图下载失败]`。
Yandex / Ascii2D 本身没有相似度，结果行不会强行写 `相似度:100%`。
引擎有候选但全部核对不一致时，会先说明「下面只是相似图」再列出来；连候选都没有才回
`呜呜呜，都检索失败喵，牡蛎~`。

## 触发与回复

只有三种触发方式（触发词：`识图` / `搜图` / `查图` / `找图` / `识图喵`）：

1. **一条消息里同时有触发词和图片**——触发词出现在消息任何位置都行（`识图`、`帮我识图一下`、`这图能搜图吗`）；
2. **回复一张图片，再发触发词**——包括引用别人的图；
3. **整条消息只有触发词**——进入连续搜图模式，回复 `发，快发喵`，然后发图：
   - `batch_timeout` 秒内没图：`我的图图呢喵？`
   - 连发多张后超时：`怎么才N张喵～`
   - 发 `我都不看这些的喵` 结束：`好喵，一共发了N张喵～`
   - 连续搜图期间别人发指令：`@对方 正在搜图，请耐心等待喵~`

不会触发的情况：唤醒词、单纯 @ 机器人、带图的普通聊天（`这张图好看`）、引用图片闲聊（`llr真的有这么大吗`）。
插件不认 `is_at_or_wake_command`——那个标记会被唤醒词或别的插件（比如把「llr」标成 @ 的补丁）置为 True。

其它行为：

- 合并转发回复（aiocqhttp）；其他平台自动逐条发送
- 同群挂两个 bot 时互不去重：两个 bot 各自回各自的（连续搜图会话按「适配器 + 群」分开）

## 通用浏览器工具（内置）

同一插件还给主 AI 提供：`browser_open`、`browser_snapshot`、`browser_click`、`browser_type`、
`browser_press`、`browser_scroll`、`browser_screenshot`、`browser_evaluate`、`browser_tabs`、`browser_close`。
和搜图共用同一个 Chromium 进程；本机/内网地址写死禁止访问，避免页面诱导模型访问本机服务。

---

## 安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/Yukari-Lily/astrbot_plugin_picfinder.git
pip install -r astrbot_plugin_picfinder/requirements.txt
```

Google Lens 只在浏览器里渲染结果；Ascii2D 的 Cloudflare 人机验证也需要浏览器。
浏览器工具（`browser_open` / `browser_snapshot` / `browser_click` 等）已经内置在本插件里，和搜图共用同一个浏览器进程，不需要再装第二个插件。
首次使用会自动执行 `playwright install chromium`（约 150MB），root 下还会自动补系统依赖。
启动时优先用完整版 Chromium（而不是 headless shell），并做反检测：去掉 `--enable-automation`、
UA 用真实版本号、补 `window.chrome`/插件/WebGL、注入 `SOCS` 同意 Cookie；容器里装了 `Xvfb`
时会自动改走有头模式（更难过人机验证）。

容器里补系统依赖（通常要 root）：

```bash
docker exec -it <AstrBot容器名> playwright install --with-deps chromium
# 或者进入容器后：
playwright install --with-deps chromium
```

没有浏览器时，插件会自动跳过 Lens/Ascii2D，用 SauceNAO / TraceMoe / Yandex 出结果，并在回复里显示
`Google Lens 检索失败喵（没装浏览器）` 这类原因。

---

## 代理

`proxy` 只填**一个**节点或一个 HTTP/SOCKS5 URL；填多个时只使用第一个。

```yaml
# Trojan
- {name: jp, server: example.com, port: 10111, type: trojan, password: xxx, sni: m.ctrip.com, skip-cert-verify: true}
# 或 Shadowsocks
- {name: jp, server: example.com, port: 495, type: ss, cipher: aes-128-gcm, password: xxx}
```

插件会在本机起一个 SOCKS5 + HTTP 混合口，Lens / Ascii2D / SauceNAO / Yandex 都走它。

填了代理不等于生效（端口没起、密码错、浏览器漏配代理都会静默退回直连），所以插件启动后
会用直连与代理各探测一次出口，把结果写进日志：

```
picfinder: 代理出口 126.8.250.40 · Japan Tokyo · SoftBank Mobile Corp.｜直连 14.137.230.132 · JP Tokyo
```

两边 IP 相同会额外标一句「与直连同 IP，代理可能没生效」；代理不通则打
`代理出口探测失败: <原因>`。

`browser_tools_proxy` 控制 `browser_*` 工具：

- `inherit`：跟随 `proxy`，共用一个 Chromium
- `direct`：浏览器工具直连，搜图仍走 `proxy`，浏览器工具单独一个 Chromium
- 填 URL：浏览器工具走另一个代理，同样单独一个 Chromium

## 配置

| 配置项 | 默认 | 说明 |
| :--- | :--- | :--- |
| `proxy` | 空 | 搜图出口：一个 HTTP/SOCKS5 URL 或一条 ss/trojan 节点 |
| `browser_tools_proxy` | `inherit` | 浏览器工具出口：`inherit` / `direct` / 代理 URL |
| `saucenao_api_key` | 空 | 可选；不填走网页版，填了走 JSON API |
| `browser_profile_dir` | 空 | 搜图浏览器的持久化目录，保存 Lens Cookie；留空用临时目录，工具浏览器不用 |
| `batch_timeout` | `60` | 发出「发，快发喵」后等第一张图的秒数；之后每张图也重新计时 |

其余全部写死：Lens auto/安全搜索关、引擎全开、SauceNAO 阈值 65、指纹核对开、合并转发开、每引擎 2 条、触发词固定 5 个、不限次数。

自己的 Cookie 放在 `data/cookies.txt`（Netscape 格式）或 `data/cookies.json`（Playwright 数组），
插件只在启动时读取，不进仓库、不需要配置项。

触发方式见上方「触发与回复」；`/识图` 也会被当作 `识图` 处理。

---

## 关于 NSFW

- 整条链路**不调用多模态大模型**，不存在「模型拒答」导致搜图失败的问题。
- Lens 默认关闭安全搜索；Yandex 默认关闭家庭过滤；SauceNAO 使用全库（含成人分区）。
- 如果聊天平台本身会拦截图片/链接，那是平台侧限制，插件无法绕过。

## 已知限制

- **Google Lens 对机房 IP 风控很严**。反检测 + 同意 Cookie 只能降低概率，机房 IP 该拦还是拦：
  换住宅/家宽节点（填到 `proxy`）、放一份已登录的 Google Cookie 到 `data/cookies.txt`、
  容器里装 `xvfb`（有头模式），三者叠加最有效；还不行就靠 Yandex / SauceNAO / Ascii2D 出结果。
- Yandex 的「收录页」可能包含二传站；所以默认开启多指纹核对，核对不到会标成「收录页（未核对）」而不是「精确匹配」。
- 平台接口会变：X 用 fxtwitter，Pixiv 用公开 ajax 接口，YouTube 用 oEmbed；失败时降级为页面 OG 元数据。
- 指纹核对覆盖缩放、压缩、裁切、镜像、反色和常见滤镜；**大角度旋转/透视变形**没有做，仍可能核对失败（此时会降级为「收录页（未核对）」而不会误标原图）。
- SauceNAO 匿名网页版有频率限制，重度使用请填 API Key。

## License

MIT
