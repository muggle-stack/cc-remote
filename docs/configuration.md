# 配置、账号与数据

[English](configuration_en.md) · [返回首页](../README.md) · [安装与升级](installation.md)

- [原生客户端](#原生客户端)
- [多个账号](#多个账号)
- [环境变量](#环境变量)
- [鉴权模型](#鉴权模型)
- [可靠性边界](#可靠性边界)
- [安全须知](#安全须知务必读)

## 原生客户端

### Claude

Wrapper 使用日常 Claude Code，默认 `~/.local/bin/claude`，最低版本 `2.1.258`。
`CLAUDE_BIN` 留空仍使用该路径；显式覆盖必须是绝对路径。Agent SDK 固定为
`0.2.151`，不会使用 SDK 自带 CLI 替代你的日常安装。

原生 CLI、Desktop 和 Agent View 拥有的会话先在 Remote 中只读镜像。用户主动接管时，
Wrapper 仅向核验过的同用户 Claude 进程发送 SIGTERM，确认释放后恢复同一会话；
不终止终端 Shell，也不使用 SIGKILL。官方 `claude` 命令不经过 alias、shim 或 PATH 替换。

### Codex

Code 默认连接官方共享 app-server daemon。先在日常 CLI 环境核对：

```bash
codex app-server daemon --help
codex app-server proxy --help
```

两项可用只是前置；上线还要按 [共享控制验收](../deploy/README.md#codex-code-shared-control-plane-acceptance)
确认每个账号的 CLI 和 Wrapper 连接同一 daemon。
单账号兼容路径在 daemon 不可用时可能降级到私有 stdio；`CC_REMOTE_CODEX_DAEMON=off`
会强制私有路径。它不具备共享控制能力；要求共享 daemon 的显式多账号配置会在无法
验证时拒绝接入，不会静默换号。Codex Work 始终使用私有进程和目录。

桌面 App 接入单独选择，见 [macOS](codex-desktop-launcher.md)／[Linux](codex-desktop-linux.md)。

## 多个账号

Claude 和 Codex 分别支持最多 32 个 Profile。每个注册表必须有且只有一个
`default: true`，目录必须是互不重复的绝对路径，并已用原生 CLI 完成登录／配置。
单账号保持原有会话 ID 和界面；多账号在同一侧栏显示账号标签，并按账号隔离控制状态。

### 配置来源

| 引擎 | JSON 文件路径变量 | 直接传 JSON | macOS 安装器默认读取的文件 |
|---|---|---|---|
| Claude | `CC_REMOTE_CLAUDE_PROFILES_FILE` | `CC_REMOTE_CLAUDE_PROFILES_JSON` | `~/.cc-remote/claude-profiles.json` |
| Codex | `CC_REMOTE_CODEX_PROFILES_FILE` | `CC_REMOTE_CODEX_PROFILES_JSON` | `~/.cc-remote/codex-profiles.json` |

直接 JSON 优先于文件；未配置或文件不存在时保持单账号。以下示例保存为对应的私有
JSON 文件，替换目录后，在 Linux 的外部 Wrapper 配置中设置文件路径变量。

Claude 示例：

```json
{
  "personal": {"label": "个人", "config_dir": "/home/youruser/.claude", "default": true},
  "company": {"label": "公司", "config_dir": "/home/youruser/.claude-company"}
}
```

Codex 示例：

```json
{
  "personal": {"label": "个人", "home": "/home/youruser/.codex", "default": true},
  "company": {"label": "公司", "home": "/home/youruser/.codex-company"}
}
```

修改后通过部署流程重启 Wrapper。每个 Profile 独立使用原生登录、配置、会话、模型
和扩展目录。显式 Claude 多账号仅加载所选 Profile 的 user settings，并清除继承的
账号级 provider 环境变量；供应商配置写在该 Profile 中，不能依赖项目／local settings
切换账号。项目说明文件仍由 Claude 原生发现。Codex 每个 `CODEX_HOME` 使用独立 daemon；
Remote 与终端应使用相同目录。

多账号内部路由为 `<profile>@<native-session-id>`；“复制 session ID”仍复制原生 UUID。
Code、Work 和 Work 定时任务都可选择账号；Work 会冻结账号归属，默认账号变化或重试
不会换号。删除 Profile 后，原 Work 明确失败，需要恢复该 Profile 或另建工作。

第一次启用显式注册表时，应包含当前生效的原生账号目录。之后 Profile id 调整按真实
目录迁移本地状态，不得把已有 id 改绑到另一目录。迁移中断时保持同一目标配置再启动，
迁移完成前受影响引擎拒绝接入，避免串号。原生登录凭据不属于迁移内容。

### Codex 账号切换 hook

使用 `codex-auth` 切换同一原生目录的账号时，可把仓库
[`scripts/codex-auth-daemon-restart`](../scripts/codex-auth-daemon-restart) 配为切换后 hook。
它记录 daemon 代际并交给独立 worker 执行官方 restart，让 Remote 恢复同一 thread 的任务；
排队消息等待该任务的原生结束边界。它不保存凭据或重放原 Prompt。

显式多账号的 hook 要传稳定的 Profile id，包括默认账号：

```bash
scripts/codex-auth-daemon-restart \
  --profile-id company --codex-home /home/youruser/.codex-company
```

hook 和 Wrapper 应使用相同的 `CC_REMOTE_STATE_DIR`；日志默认为
`~/.cc-remote/codex-daemon-restart.log`。旧单账号 hook 的兼容归属不会随默认账号排序变化，
新配置应显式传入 Profile id。

## 环境变量

以下为常用设置；完整配置约束见 [config.py](../cc_remote/config.py) 和部署环境模板。真实环境变量优先于开发用本地 `.env`。

**中继（relay）**

| 变量 | 默认 | 说明 |
|---|---|---|
| `RELAY_HOST` / `RELAY_PORT` | `127.0.0.1` / `8765` | 监听地址（仅 Caddy 公网入口时保持 `127.0.0.1`；同时开放 LAN/Tailscale IPv4 直连时必须配合 `ALLOW_PRIVATE_ORIGINS=1` 改为 `0.0.0.0` 并限制防火墙）。 |
| `LOGIN_PASSWORD` | 空 | 单用户网页登录口令。未设置 `LOGIN_USERS_JSON` 时**必须设**。 |
| `LOGIN_USERS_JSON` | 空 | 可选多用户策略：`{"alice":{"password":"…","machines":["laptop","server"]}}`；设置后替代 `LOGIN_PASSWORD`。 |
| `SESSION_SECRET` | 空 | 给会话 token 签名的 HMAC 密钥。**必须设**（`openssl rand -hex 32`）。 |
| `SESSION_TTL_SECONDS` | `604800` | 会话 token 有效期（默认 7 天）。 |
| `LOGIN_BODY_MAX_BYTES` / `LOGIN_READ_TIMEOUT` / `LOGIN_INFLIGHT_CAP` | `4096` / `10` / `32` | 登录请求体字节数、总读取秒数和并发读取数的硬上限。 |
| `SESSION_REGISTRY_CAP` | `1024` | 进程内可撤销浏览器会话注册表的硬上限。 |
| `PUSH_VAPID_PUBLIC_KEY` / `PUSH_VAPID_PRIVATE_KEY` / `PUSH_VAPID_SUBJECT` | 空 | 可选真实 Web Push；三项必须同时配置。私钥建议填写 relay 用户可读的 PEM 绝对路径。旧用户和默认模式只发送完成/失败状态；用户主动选择“显示会话名称”后，Push 才携带安全截断的名称和设备内精确路由，始终不含 prompt、回复、路径或工具输出。 |
| `PUSH_DB_PATH` | `~/.cc-remote/relay-push.sqlite3` | 持久化、按用户和机器隔离的浏览器 Push 订阅库。 |
| `DEVICE_DB_PATH` | `~/.cc-remote/relay-devices.sqlite3` | 持久设备注册、显示名、最近在线时间和凭据哈希；不保存会话或 Artifact。 |
| `DEVICE_PAIRING_TTL_SECONDS` | `600` | 一次性配对码有效秒数，允许 60–3600。 |
| `PUBLIC_ORIGIN` | 空 | 浏览器允许连接 WS 的精确来源，如 `https://remote.example.com`；**必须设**，非 loopback 必须 HTTPS（除非开了 `ALLOW_INSECURE_HTTP`）。 |
| `ALLOW_PRIVATE_ORIGINS` | `0` | 设为 `1` 后，在保留 `PUBLIC_ORIGIN` 的同时，允许浏览器通过 `RELAY_PORT` 上的私网/loopback 字面 IP 直连：`127/8`、`10/8`、`172.16/12`、`192.168/16`、Tailscale `100.64/10`、IPv6 loopback/ULA。Origin 的协议/主机/端口还必须与实际请求目标完全一致；主机名、公网 IP 和其他端口仍拒绝。内网 HTTP 不加密，且通常不能安装 PWA。 |
| `ALLOW_INSECURE_HTTP` | `0` | 逃生开关：设为 `1` 允许 `PUBLIC_ORIGIN` / `RELAY_URL` 在非 loopback 时仍用明文 `http://`/`ws://`（例如直接暴露一个没有 TLS 终端的公网 IP）。默认关闭；开启后登录口令、会话 cookie 和全部流量都走明文，链路上任何人都能窃取或劫持会话，务必优先使用 TLS。 |
| `WRAPPER_TOKEN` | 占位值 | 单机器/兼容模式下的 wrapper Bearer token；未设置 `WRAPPER_TOKENS_JSON` 时必须配置。 |
| `WRAPPER_TOKENS_JSON` | 空 | 可选机器绑定 token：`{"laptop":"…","server":"…"}`；设置后替代 relay 的通配 `WRAPPER_TOKEN`。 |
| `WEB_STATIC_DIR` | 空 | 指向 `web/dist` 则同源托管网页；留空则只做 API/WS。 |
| `CLIENT_QUEUE_CAP` / `CLIENT_QUEUE_BYTES` | `4096` / `16777216` | 单客户端待发帧数/字节硬上限；超限断开慢客户端，不静默丢帧。 |
| `MAX_CLIENTS` / `CLIENT_HELLO_TIMEOUT` | `8` / `10` | 已接受客户端总数和首个 Hello 帧等待秒数的硬上限。 |
| `WS_MAX_SIZE_BYTES` | `16777216` | relay 与 wrapper 接受的单个 WebSocket 帧上限。 |

**wrapper**

| 变量 | 默认 | 说明 |
|---|---|---|
| `CC_REMOTE_VIEWER_HOME_PREVIEW` | `1` | 仅发现核验过的明确引用页面或所属静态监听器；`0` 关闭自动发现。见 [Viewer](remote-viewer.md)。 |
| `RELAY_URL` | `ws://127.0.0.1:8765/ws` | 中继的 WebSocket 地址（公网用 `wss://域名/ws`，除非开了 `ALLOW_INSECURE_HTTP`）。 |
| `ALLOW_INSECURE_HTTP` | `0` | 同中继的逃生开关；wrapper 也读这个变量，开启后 `RELAY_URL` 可以在非 loopback 时仍用 `ws://`。 |
| `WRAPPER_TOKEN` | `change-me-wrapper` | 同中继。 |
| `CC_REMOTE_MACHINE_ID` | `default` | 多机器 relay 中的稳定路由 id；使用 `WRAPPER_TOKENS_JSON` 时必须匹配对应键。 |
| `CC_REMOTE_DEVICE_CONFIG` | `~/.cc-remote/device.json` | 交互配对凭据路径；文件必须仅当前用户可读。显式的 `RELAY_URL` / `WRAPPER_TOKEN` / `CC_REMOTE_MACHINE_ID` 优先。 |
| `CLAUDE_BIN` | `~/.local/bin/claude` | wrapper 实际启动的日常 Claude Code；空值仍使用该默认路径。只有 CLI 安装在别处时才设为另一个绝对路径。 |
| `CC_REMOTE_CLAUDE_PROFILES_JSON` | 空 | 可选 Claude 多账号注册表；格式为 `{profile_id:{"label":"…","config_dir":"/绝对/CLAUDE_CONFIG_DIR","default":true}}`。最多 32 项、目录必须唯一，且必须且只能有一个默认项。Code、Work 与定时任务均可选择账号；空值保持当前单账号行为。显式 JSON 优先于文件。 |
| `CC_REMOTE_CLAUDE_PROFILES_FILE` | 空（macOS LaunchAgent 为 `~/.cc-remote/claude-profiles.json`） | 可选注册表 JSON 文件；必须是有上限的普通文件。文件不存在等同单账号，便于先安装再配置。 |
| `CC_REMOTE_CODEX_PROXY` | 空 | 仅注入 wrapper 启动的 Codex 子进程的 HTTP(S)/SOCKS5 代理；不改 wrapper 到 relay 的连接，也不影响用户终端里的 `codex`。例如 `http://127.0.0.1:8080`。 |
| `CC_REMOTE_CODEX_DAEMON` | `auto` | Code 默认连接 Codex 官方共享 daemon；`off` 强制使用私有 stdio app-server，并失去与原生 Codex CLI/App 的实时双向协同。Work 始终私有，不受此项影响。 |
| `CC_REMOTE_CODEX_PROFILES_JSON` | 空 | 可选 Codex 多账号注册表；格式为 `{profile_id:{"label":"…","home":"/绝对/CODEX_HOME","default":true}}`。最多 32 项、home 必须唯一，且必须且只能有一个默认项。每项使用独立 daemon；Code 合并展示并按标签区分，Codex Work 新会话和定时任务可选择任一项。空值保持单账号兼容。显式 JSON 优先于文件。 |
| `CC_REMOTE_CODEX_PROFILES_FILE` | 空（macOS LaunchAgent 为 `~/.cc-remote/codex-profiles.json`） | 可选注册表 JSON 文件；必须是有上限的普通文件。文件不存在等同单账号，便于先安装再配置。 |
| `CC_REMOTE_STATE_DIR` | `~/.cc-remote` | 本机 wrapper 状态目录。账号切换 hook 与 wrapper 必须使用同一个值，daemon 代际屏障保存在其中；不包含 Codex 凭据。 |
| `CC_CWD` | 当前目录 | 新会话默认工作目录。Claude `--resume` 靠它定位 `~/.claude/projects/` 下的会话文件，**必须对**；Codex 恢复时会优先从 rollout 取原 cwd。 |
| `CC_RESUME_SESSION_ID` | 空 | 恢复指定会话 UUID；留空开新会话。首次启动后 id 会持久化到 `~/.cc-remote/`。 |
| `CLAUDE_WORK_ROOT` | `~/.claude/cc-remote/work` | Claude Work 的私有注册表、资料库、会话目录和策略文件根目录。 |
| `CODEX_WORK_ROOT` | `~/.codex/cc-remote/work` | Codex Work 的私有注册表、资料库、会话目录和策略文件根目录。 |
| `MAX_CONCURRENT_SESSIONS` | `20` | Wrapper 常驻会话上限（内存随引擎/版本变化）。超了就驱逐 idle 的；客户端缓存仍在，可再切回。 |
| `DRAIN_TIMEOUT` | `15` | interrupt 后等终止 ResultMessage 的秒数，超时强制重连（排空保险）。 |
| `RING_MAX_EVENTS` / `RING_MAX_BYTES` / `TOOL_RESULT_MAX` | 见 [`.env.example`](../.env.example) | 实时尾巴缓冲 / 工具输出截断上限调优。 |
| `HISTORY_SOURCE_MAX_BYTES` | `67108864` | 单个 Claude transcript 的安全读取上限；超限返回明确错误，避免 SDK transcript 全量解析耗尽内存。Codex rollout 不受此总文件上限限制。 |
| `CODEX_HISTORY_WINDOW_MAX_BYTES` | `33554432` | Codex 超长 rollout 每页最多解析的源窗口；历史按轮次从文件尾流式分页，单轮超限时保留最近窗口和可继续加载的稳定游标。 |
| `WRAPPER_INBOX_CAP` / `WRAPPER_SEND_QUEUE_CAP` | `1024` / `8192` | wrapper 入站/出站内存队列条数硬上限。 |
| `WRAPPER_INBOX_BYTES` / `WRAPPER_SEND_QUEUE_BYTES` | `33554432` / `33554432` | wrapper 入站/出站队列序列化字节硬上限。 |
| `TURN_READER_QUEUE_CAP` | `4` | 单回合引擎事件消费队列；Codex app-server stdout 另有独立、有字节上限的突发缓冲，避免 Relay 变慢时阻塞 RPC 和终态。 |

单次消息最多 8 个附件，单个最多 6 MiB，解码后合计最多 8 MiB；超限会在启动模型前拒绝。

## 鉴权模型

- **网页客户端**：向中继 `POST /api/login` 换一个短期 HMAC 会话，放在 **HttpOnly + SameSite=Strict** cookie 中；JavaScript 读不到，URL 中也没有 token。配置 `LOGIN_USERS_JSON` 后，签名会话还携带允许的机器集合，机器列表和 WebSocket 路由都会再次校验。WebSocket 同时必须通过精确 `Origin` 校验。
- **wrapper ⇄ 中继**：WS 握手时带机器凭据；手工配置可使用 `WRAPPER_TOKEN` / `WRAPPER_TOKENS_JSON`，设备中心则签发独立、机器绑定且可单独撤销的凭据。Relay 只保存哈希，任何凭据都不能声明另一台机器的 `machine_id`。
- token 只走 cookie/请求头，从不进 URL 或线协议消息体；日志会自动打码 token/password 字段。

## 可靠性边界

- Web 客户端会给可重试命令附加稳定的 `cmd_id`，断线重连或 wrapper 恢复后重发；wrapper 在同一进程生命周期内去重并返回 ACK。每个实时会话还用 wrapper generation 配对 cursor，避免 wrapper 重启后把旧序号误当成新序号。
- 排队及打断后的替换消息一经 wrapper 接收，就由 wrapper 的有界内存队列持有；即使所有浏览器/PWA 休眠、断线或硬刷新，也会在当前回合真正结束后继续执行，并在客户端重连时恢复队列摘要。点击摘要会私有按需读取完整指令，可在执行前原子编辑文字且保留附件；完整 payload 不进入可重放 ring。该队列不会跨 wrapper 进程崩溃或重启持久化。
- 未确认命令队列和通用命令去重表是**有界内存状态**：浏览器硬刷新、客户端退出或 wrapper 进程崩溃，不承诺跨进程的 exactly-once。cc-remote 是交互控制面，不是持久任务队列；这类故障后应先核对 transcript/rollout 和会话状态，再决定是否重发。
- 已落盘的 Claude transcript 和 Codex rollout 是历史事实来源；wrapper 的 SQLite 摘要索引和浏览器 IndexedDB 都是可重建投影，实时 ring 只负责有界的断线补流。工具/思考等大块详情按单轮展开，不阻塞会话首屏。
- Work 定时任务是例外：计划、运行记录、租约、心跳、重试次数和下次运行时间写入 SQLite；wrapper 重启后会恢复过期租约，但仍不会把不确定结果伪装成成功。

## 安全须知（务必读）

> **cc-remote 会让远端的人在你机器上跑任意命令。请当成「给别人一个你机器的 shell」来对待。**

- Code 会话仍是远程开发控制面：Claude 默认使用 `permissionMode: bypassPermissions`；Codex 默认审批策略是 `never`，并可在 app-server 对当前 cwd 允许的 named permission profile 中选择执行环境。审批策略不会扩大 profile 的边界，Full Access 则会显著扩大能力。**能登录且能进入 Code 的人，仍应等同于拿到了这台机器的远程 agent/shell 权限。** Work 会话使用固定的 `cc_remote_work` profile、独立私有根目录且不开放外部目录，但这只是缩小默认能力面，不替代操作系统级的独立用户、容器或虚拟机隔离。
- `LOGIN_PASSWORD` / `LOGIN_USERS_JSON`、`WRAPPER_TOKEN` / `WRAPPER_TOKENS_JSON` 和 `SESSION_SECRET` 是认证边界：用强随机值、别提交 git、别贴到聊天里、定期轮换。仓库 `.env` 只适合本机开发；Linux 生产 Wrapper 使用 root-only `/etc/cc-remote/wrapper.env`；macOS 使用安装器管理的 release 外私有配置。systemd 模板会禁止服务及模型子进程读取这个源文件和遗留仓库 `.env`；Linux wrapper 还会关闭 dumpability，避免子进程从 `/proc/<pid>/environ` 或进程内存取回已经捕获的 token。
- 公网必须上 TLS（`wss://`，本仓库用 Caddy 自动签证书）。只有明确需要临时使用公网 IPv4 + 明文 HTTP/WS 时才设 `ALLOW_INSECURE_HTTP=1`；开启后登录口令、cookie、wrapper token 和全部会话流量都不加密，应尽快切回 TLS。`ALLOW_PRIVATE_ORIGINS=1` 只为同端口私网字面 IP 增加与实际请求目标一致的直连入口，不会放宽公网域名校验；Cookie 的 `Secure` 属性按受信请求传输判断，不读取调用者提供的 Origin。但使用内网 HTTP 时，登录口令、cookie 和会话内容在该网络中仍是明文。
- 建议：给中继加 IP 白名单 / 只在需要时开、给登录加失败限速（已内置每 IP 每分钟 5 次）。

## 模型配置

先在原生引擎中配置并完成登录，再接入 Remote。单账号 Claude 使用生效的
`CLAUDE_CONFIG_DIR`（通常为 `~/.claude`），Codex 使用 `CODEX_HOME`（通常为 `~/.codex`）。
订阅登录或供应商认证留在各自目录；Profile 只选择原生配置边界，
cc-remote 不下发模型凭据，也不充当模型 API 网关。

Wrapper 到 Relay 的代理使用外部配置中的 `HTTPS_PROXY` / `ALL_PROXY`；
`CC_REMOTE_CODEX_PROXY` 只影响 Wrapper 启动的 Codex 子进程，两者对应不同连接。
