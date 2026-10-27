# zcode2api (zcode-hub)

**ZCode 账号运营 + 双协议网关一体机**：把池内 ZCode Coding Plan / Start Plan / API Key 账号额度，统一转换为标准的
**Anthropic Messages API**（`/v1/messages`）与 **OpenAI Chat Completions API**（`/v1/chat/completions`），向 Claude Code、Cline、Codex CLI、Chatbox、NextChat 等各类 Agent 与客户端提供高可用 API 服务。

自带一套完整的账号运营控制台：多账号池轮询、高可信成套桌面指纹仿真、官方首启安装序仿真、单账号并发控制、429/5xx/3012 阶梯容灾、请求全景监控、限时套餐自动与手动领取、OAuth 免密登录入池，以及免浏览器的阿里云无痕验证求解——开箱即用，自托管部署。

---

## 核心能力

- **双协议兼容网关**：原生支持 `POST /v1/messages`（Anthropic Messages 协议，支持流式 SSE）与 `POST /v1/chat/completions`（OpenAI 格式双向翻译，支持流式与非流式），并提供 `GET /v1/models` 模型列表。
- **高可信桌面指纹池（一号一台）**：入池自动生成加权桌面成套 SKU（Mac Sequoia/Tahoe、Win11 主流分辨率等），每账号独立全新 UUIDv4 `device_mid`，杜绝宿主机云 Linux 内核共享导致的账号集群关联风险。
- **官方首启安装序仿真**：入池与指纹轮换自动执行 `client/configs` 与激活事件上报（`app_launch` / `app_daily_active`），持久化 `install_id` 与 `installed_at`。
- **反代与 CDN 基础设施头深度清洗**：自动剔除 `x-forwarded-*`、`forwarded`、`cf-*`、`cdn-loop`、`x-real-ip`、`via`、`cookie`、`referer`、`origin`、`accept-language` 等网络中间件头部，杜绝部署网络拓扑泄露与客户端伪装冲突。
- **单账号精细化并发控制**：支持单账号并发上限（默认 1 = 一账号一路，可在后台运行时热改，0 不限）。满并发账号智能跳过，不计入重试轮数；客户端断开、异常与等待期间即时释放并发槽位。
- **阶梯式故障转移与容灾策略**：
  - **429 智能退避重试**：优先根据上游 `Retry-After` 原地等待重试（等待期间释放并发槽位，防止阻塞）；重试耗尽无损换号且**不冷却账号**；Plan 通道 429 耗尽且同账号有 API Key 时自动切 `api.z.ai` 回退通道。
  - **5xx 熔断冷却**：重试 3 次后进入 300s 冷却，自动切下一个可用账号。
  - **3012/405 真风控隔离**：识别 "unusual activity" 立即标记 `DISABLED` 禁用账号保护资产，停止自动化空转，支持后台人工确认后一键启用恢复。
  - **401/403 鉴权失效标记**：非验证码的凭证失效直接标 `INVALID`。
- **全景请求监控与安全防护**：
  - 请求监控面板（`/admin/monitoring`）：实时展示在飞请求、历史请求耗时、状态码、模型、账号及错误堆栈。
  - 后台暴力破解防护：客户端 IP 连续登录失败达上限自动锁定 5 分钟。
  - 设置密钥脱敏回显：GET 设置接口密钥自动打码，保障安全性。
- **限时套餐领取（双轨机制）**：
  - 自动领取：入池即自动领取全部可领套餐，并有可配置的周期自动领取轮（默认 600 秒，后台设置页可改，0 = 关闭），持续盯当期活动赠送。
  - 领取语义对齐官方 3.11.2：成功回执携带上游 `server_time` 与套餐窗口；1005「名额用完」附带名额恢复时间 `next_at`，领取轮按其退避（等待期不重试领取、不耗验证码，`next_at` 过后自动恢复）。
  - 浏览器过码：支持在前端调起阿里云滑块验证码进行手动领取，应对极严风控环境。
- **OAuth CLI 免密登录**：
  - 提供海外邮箱与 Google 登录直达引导卡片，解决上游 `chat.z.ai/auth` 隐藏第三方登录按钮问题。
  - 严格遵循官方 CLI 的干净请求头规范，JWT 授权入池秒级完成，API Key 兑换异步后台回填。
  - 跟进官方新协议：采用 init 响应服务端下发的 `poll_token` 轮询（未下发时回落自造，兼容旧协议）；poll 4xx 承载的 `code=3004` 明确映射为会话过期。

## 支持的账号类型

| Provider | 模式 | 说明 |
|----------|------|------|
| `zai` | `jwt` | Coding Plan 额度（Plan 通道，需无痕验证码），自动续领活动套餐 |
| `zai` | `apiKey` | Z.AI API Key（回退通道，免验证码） |
| `bigmodel` | `apiKey` | 智谱开放平台（Anthropic 兼容端点） |

> 一个 Z.AI 账号可同时持有 JWT 与 API Key：OAuth 登录后会一并入池，JWT 额度告罄或 429 耗尽时自动无缝走 API Key 回退通道。

## 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # 按需修改密钥、端口等
.venv/bin/python cli.py serve   # 启动网关 + 后台 UI（默认 http://0.0.0.0:3000）
```

- 后台管理：`http://127.0.0.1:3000/admin/login`（初始账号见 `.env` 的 `ZCODE_ADMIN_KEY`）
- 对话端点：
  - Anthropic 协议：`POST http://127.0.0.1:3000/v1/messages`（兼容 Claude Code、Cline）
  - OpenAI 协议：`POST http://127.0.0.1:3000/v1/chat/completions`（兼容 OpenAI 客户端）
  - 模型列表：`GET http://127.0.0.1:3000/v1/models`
  - 探活端点：`GET http://127.0.0.1:3000/meta`

> 使用 Z.AI **JWT 模式**需要 Node.js 求解验证码，首次先执行：`cd captcha_node && npm install`。

## 快速上手一个账号

```bash
# 方式一：OAuth 登录（免密，自动入池 + 自动兑换回退 Key）
.venv/bin/python cli.py login zai

# 方式二：手动加入现成凭证（JWT 三段点分 或 API Key）
.venv/bin/python cli.py add-account zai 名称 <jwt|key>
```

## 命令行

```bash
python cli.py serve [--port 3000]    启动网关 + 后台 UI
python cli.py login zai              通过 OAuth 登录 Z.AI 并自动入池
python cli.py add-account <zai|bigmodel> <name> <jwt|key>   添加账号
python cli.py accounts [zai|bigmodel]  查看账号列表
python cli.py remove-account <provider> <id|name>  删除账号
python cli.py quota                  查看各账号实时额度
python cli.py status                 查看配置概览
python cli.py set-admin-key <key>    设置后台密码
python cli.py export [file]          导出全部账号
python cli.py import <file>          导入账号
```

## 后台 UI

| 页面 | 说明 |
|------|------|
| `/admin/login` | 后台登录（Bearer 密钥鉴权，连续失败 IP 锁定防护，凭证加密存于浏览器 localStorage）|
| `/admin/accounts` | 账号池：新增/导入/导出、启用禁用、指纹换发、**实时额度与状态监控**、套餐领取（自动/手动）|
| `/admin/agents` | 客户端接入：把本网关**一键写进 magpie 等 AI 客户端**的配置（写前备份、一键还原）|
| `/admin/settings` | 后台密码、网关 API Key（GET 脱敏掩码）、额度刷新间隔、单账号并发上限 |
| `/admin/monitoring` | 请求监控：**每请求一行的持久台账**（Token / 缓存命中 / 首字 / 生成速度 / 结果）+ 实时视图 |

### 请求监控能看到什么

每个请求一行（SQLite `data/usage.db`，默认保留 30 天 / 5 万行，重启不清零）：

| 维度 | 字段 |
|---|---|
| 归属 | 时间、端点（`/v1/messages`、`/v1/chat/completions`）、账号 + 通道（JWT / API Key）、模型（含客户端点的原名） |
| 结果 | 终态 `outcome`（completed / failed / client_aborted / upstream_aborted）+ 独立记录的 HTTP 状态码 + 错误全文 |
| 速度 | 首字延迟（TTFT）、生成耗时、总耗时、**生成速度 tok/s**（按生成窗口算，不含排队与首字等待） |
| 消耗 | 输入 / 输出 tokens、**缓存命中 tokens 与命中率**、上游上报的 `credit`（有则记） |

- 上游没报 usage 的请求也照样落行并标记 `usage_missing`：请求数、成功率、时延样本不会因此失真。
- `/v1/messages` 是原样透传，token 靠旁路嗅探（`UsageSniffer`）统计，不再永远是空。
- 账号口径：多账号重试不分裂，记**最终服务**的那个账号；`outcome` 与 HTTP 码分开记——「200 但客户端取消」不会被算成成功。
- 统计卡与聚合（按账号 / 按模型 / 按小时）都在 SQL 侧按当前筛选条件算，翻页不改变口径。

账号池页实时展示每个账号的**状态（正常 / 额度用完 / 限流 / 异常 / 禁用）**、各模型剩余额度、
调用与失败次数；并提供「手动刷新额度」与「批量领取套餐」入口。

## 局域网访问（其它电脑打开后台）

网关默认监听 `0.0.0.0`（`ZCODE_HOST` 可改），端口 3000。但 **Windows 防火墙默认拦截入站**，
所以首次要让同网段的其它电脑打开后台，先放行：

```bat
allow-firewall.bat          :: 双击 → UAC 确认 → 自动放行「网关解释器」与 3000 端口
allow-firewall.bat remove   :: 撤销这两条规则
```

之后在其它电脑用 `http://<本机IP>:3000/admin` 打开（本机 IP 看「客户端接入」页的局域网地址，
或 `ipconfig`）。自检：`curl http://<本机IP>:3000/meta` 应返回 `{"version":"..."}`。

> ⚠️ 后台默认口令是 `zcode`，**放行前先改掉**（设置 → 后台密码）。`/v1/*` 另有网关 API Key
> 单独把关（设置 → 网关 API Key），两者不要用同一个值。

## 多账号轮询与故障转移

- 在账号池粘贴 Coding Plan JWT（3 段点分）或 API Key，每行一个即可加入轮询。
- 网关每次请求选择下一个「可用」账号，跳过用完 / 限流 / 异常 / 禁用的账号。
- **单账号并发限制**：所选账号在飞并发达上限时智能跳过，不排队、不计入 `MAX_ACCOUNT_ATTEMPTS` 计数。
- **模型并发已满（3009）**：上游按模型计的并发租约，上一个请求刚结束的 1–2 秒内再发会全部回 3009。
  整轮都只是这个原因时，网关短等 1 秒再来（最多 3 轮，`ZCODE_MODEL_BUSY_WAIT` / `ZCODE_MODEL_BUSY_ROUNDS`），
  接得上就接、接不上才 503，不会像过去那样原地等 60 秒把客户端吊住。
- **模型额度用完（1005）**：只换号、**不动账号状态**——那是「这个模型」的额度没了，同账号其他模型还能用。
- **额度用完**（余额为 0 / 上游 402 / 错误体含 quota、余额不足等关键词）→ 标记 `exhausted`，换下一个账号。
- **上游 429** → **不冷却账号**，按 Retry-After（封顶）原地等待重试；等待期间释放该账号并发槽。耗尽后换号，账号保持可用。Plan 通道 429 耗尽且同账号有 API Key 时切回退通道。
- **上游 5xx** → 重试 3 次后标记 `cooling` 冷却 300 秒，自动换下一个账号。
- **真风控（3012 / 405 unusual activity）** → 标记 `disabled` 并熔断停用，防止账号进一步受损，需在后台手动恢复。
- **鉴权失败 401/403（非验证码）** → 标记 `invalid`，需重新登录。
- **验证码过期（403 / code 3007）** → 原地刷新验证码对同一账号重试（最多 3 次）。
- 达到请求上限仍无可用账号 → 返回 `503 no_available_account`。

账号状态机：

```
            ┌─────────┐   额度用完(定期自动再探)    ┌──────────┐
  登录 ────▶│ ACTIVE  │◀─────────────────────────│ EXHAUSTED│
            │         │   5xx 重试耗尽(冷却 N 秒) └──────────┘
            │         │◀───────┐      ┌─────────┐
            │         │        ├──────│ COOLING │
            │         │        └─────▶└─────────┘
            │         │ 401/403(非验证码)
            │         │──────────────────────▶ INVALID（凭证失效，重新授权）
            │         │ 3012/405(真风控)
            │         │──────────────────────▶ DISABLED（风控保护，后台手动恢复）
            └─────────┘
```

> 成功响应可清除 `COOLING` / `EXHAUSTED`；额度恢复时后台监控也会自动回 `ACTIVE`。

### 会话粘性（同一段对话不换号）

轮询是「还没人回答过」时的规则。一旦某段对话有了回答，网关会把它**粘在**那个账号上
（有效期 `ZCODE_AFFINITY_TTL`，默认 30 分钟）：上游按「账号 + prompt 前缀」缓存整段
对话，换号等于让上游把整段对话重新读一遍——缓存命中归零、钱照付、首字节更慢。
粘住的账号不可用（冷却 / 额度用完 / 禁用 / 并发满）时照常轮询到下一个。

会话指纹 = `system` 提示词 + 第一条 user 消息的哈希，多轮增长不影响（对齐 magpie 的
affinity 设计）。

### 流式保活与「未响应」

- **思考内容透传**：GLM 这类模型的思考阶段上游只发 `thinking_delta`，网关把它转成
  OpenAI 的 `delta.reasoning_content`（Anthropic 端点原样透传）。丢掉这些事件，客户端
  在整段思考期间收不到任何字节，界面就只能显示「未响应」——这是 2026-10-27 实测到的
  根因（一次回复 1996 条 thinking_delta，客户端静默 39 秒）。
- **SSE 心跳**：客户端空闲满 `ZCODE_SSE_KEEPALIVE_IDLE`（默认 15s）补一条注释帧
  `: keep-alive`，客户端与中间代理都能判断连接还活着。
- **上游看门狗**：上游彻底静默满 `ZCODE_UPSTREAM_STALL`（默认 600s）判定连接已死并
  断开，避免半死 TCP 连接把请求永久挂住。
- **缓存命中可见**：`cache_read_input_tokens` 报成 `usage.prompt_tokens_details.cached_tokens`，
  客户端状态栏的「命中」才有数。

## 客户端一键接入（magpie）

后台「客户端接入」页可以把本网关**一键写进 AI 客户端的配置**，让客户端（以及它管理的
agent）直接用上这里的 ZCode 账号池。第一个支持的是
[magpie](https://github.com/yetone/magpie)：菜单栏里的「每个 agent 的模型都在一处」——
它自己是个本地网关（`127.0.0.1:3425`），把 provider 分给 Claude Code / Codex / Gemini CLI /
DSH / ZCode 等 agent。

一键接入做三件事：

- 往 magpie 的 `providers.json`（默认 `~/.config/magpie/providers.json`，Windows 也是这个
  路径）写一条 provider（id 固定为 `zcode2api`），同时给出 OpenAI（`…/v1`）与 Anthropic
  （根地址）两个入口，模型取本网关 `/v1/models` 的清单；
- 写前备份原件、`0600` 落盘、临时文件 + `os.replace` 原子替换（中途失败按原字节回滚）；
- 「一键还原」回到首次接入前的字节；期间文件被外部改过，则只摘掉本网关那一条 provider，
  不覆盖其它改动。

接入后：打开 magpie → **Agents** 页 → 给 Claude Code / Codex 选 `zcode2api/glm-5.3-flash`。

也可以命令行（客户端不在网关这台机器上时）：

```bash
python -m app.clients list
python -m app.clients apply --client magpie --base-url http://127.0.0.1:3000 --key <网关 Key>
python -m app.clients restore --client magpie
```

看板上还给出 magpie 官方的 `magpie://import?…` 导入链接（复制 / 直接打开）：那条路由
magpie 自己弹窗确认、自己落盘，适合不想让网关写文件、或 magpie 跑在别的机器上时用。
注意首次使用前需要启动过一次 magpie（`magpie://` 协议由它注册）。

细节与「新增一款客户端」的步骤见
[docs/development/08-client-integration.md](docs/development/08-client-integration.md)。

## 无痕验证（真浏览器求解）

JWT 账号调用上游（对话 / 领取套餐）时需携带阿里云无痕验证参数（请求头
`X-Aliyun-Captcha-Verify-Param`）。该参数由 **Node + puppeteer-core 驱动真实
Chromium** 运行阿里云官方无痕 SDK 求出（happy-dom 伪造环境 2026-09 起被上游风控
全拒，`solver.js` 仅留作 `ZCODE_CAPTCHA_SOLVER=legacy` 回滚）。

- 求解器：`captcha_node/solver_pw.js`，首次使用前执行 `cd captcha_node && npm install`。
- **浏览器自动探测**：优先读 `ZCODE_CHROMIUM_PATH`；未设置时按平台常见路径找——Windows
  先找 Edge（系统预装，也是 Chromium）、再找 Chrome；Linux/macOS 找 `chromium` /
  `google-chrome` / 两个 .app。装的 Edge 就能用，不用额外下载。
- 预解池：`app/captcha.py` 后台常驻 1~2 枚 token（TTL 95s），热路径直接取现成的
  （亚毫秒），取走后立刻补货；上游返回挑战时整池作废重解。
- 求解失败会把求解器的 stderr 原样带出来（例如「未找到可执行 Chromium」），
  不会再只显示一句「多次重试无结果」。
- 配置兜底：`client/configs` 拉取失败时回落内置默认 scene/region/prefix。

## 鉴权

- **后台鉴权**：所有 `/admin/api/*` 需 `Authorization: Bearer <后台密码>`（也支持 `?app_key=`），内置 IP 失败节流保护。
- **网关鉴权（可选）**：在「设置」配置「网关 API Key」后，`/v1/messages` 与 `/v1/chat/completions` 须携带
  `Authorization: Bearer <key>` 或 `x-api-key: <key>`；留空则不校验。

## 环境变量

`cp .env.example .env` 后按需修改（.env 永不入库）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ZCODE_PORT` | 3000 | 服务端口 |
| `ZCODE_HOST` | 0.0.0.0 | 监听地址 |
| `ZCODE_ADMIN_KEY` | change-me | 后台密码初始值（之后写库，以库为准）|
| `ZCODE_GATEWAY_KEY` | 空 | 网关 API Key；留空不校验（生产务必设置）|
| `ZCODE_DATA_DIR` | data | 数据目录（SQLite 存放处）|
| `ZCODE_QUOTA_REFRESH_INTERVAL` | 1800 | 后台刷新额度间隔（秒），0 关闭 |
| `ZCODE_COOLING_SECONDS` | 300 | 限流冷却时长（秒）|
| `ZCODE_ACCOUNT_CONCURRENCY` | 1 | 单账号并发上限：上游对「账号 × 模型」并发硬限很低（超了回 429 code 3009），默认一账号一路，0 不限（运行时可在后台设置改，以库为准）|
| `ZCODE_CLAIM_ROUND_INTERVAL` | 3600 | 套餐自动领取轮间隔（秒），0 关闭（运行时可在后台设置改，以库为准）|
| `ZCODE_NODE_PATH` | node | 验证码求解所用 Node 可执行文件 |
| `ZCODE_CHROMIUM_PATH` | 自动探测 | 求解器用的浏览器可执行文件；不设则按平台找 Edge / Chrome / chromium |
| `ZCODE_CAPTCHA_SOLVER` | pw | 求解器实现；`legacy` 回滚到 happy-dom 版 solver.js |
| `ZCODE_CAPTCHA_RETRIES` | 4 | 单次求解失败重试次数 |
| `ZCODE_CAPTCHA_TIMEOUT` | 240 | 单次求解超时（秒）|
| `ZAI_UPSTREAM_URL` / `ZAI_FALLBACK_URL` / `BIGMODEL_UPSTREAM_URL` | — | 上游端点覆盖 |

## 开发与测试

```bash
.venv/bin/python -m pytest            # 全量测试（Mock 上游，无真实网络）
.venv/bin/ruff check app tests
.venv/bin/mypy app/constants.py
```

测试全部走 **Mock 上游**：`tests/mock_upstream/` 模拟 Z.AI 被依赖的全部端点，
支持 `x-mock-scenario` 故障注入（具体见 `docs/testing`），不依赖真实网络，可离线回归。

## 文档

完整文档见 [`docs/README.md`](docs/README.md)：

- **开发**：架构 · 数据格式 · API 规范 · 上游协议 · 开发指南 · 路线图 · 客户端一键接入
- **测试**：测试策略 · 单元用例 · 互通向量 · 集成 E2E · 验收标准

## 技术栈

- Python 3.11+ · FastAPI · Uvicorn · httpx
- SQLite（账号 / 设置持久化，WAL 模式）
- Node.js + jsdom（免浏览器求解阿里云无痕验证）

## 许可证

本项目采用 [AGPL-3.0](LICENSE) 许可证。

## 免责声明

本仓库仅供学习、研究、个人实验与内部验证使用，不提供任何形式的商业授权、适用性保证或结果保证。

作者不因使用、修改、分发、部署或依赖本项目产生的任何直接或间接损失、账号封禁、数据丢失、
法律风险或第三方索赔负责。请勿将本项目用于违反服务条款、协议、法律或平台规则的场景；商业前请自行确认许可证与相关协议。