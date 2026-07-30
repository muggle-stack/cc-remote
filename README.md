# cc-remote

**把你机器上的 Claude Code / Codex，带到手机和任意浏览器。**

自托管 · 双引擎 · 多会话 · 实时过程 · 响应式 Web

**当前版本：v3.0.0** · Wire protocol v27

[English](README_en.md) ·
[5 分钟上手](#本地快速开始一台机器5-分钟) ·
[生产部署](#生产部署公网-vps-中继--你机器上的-wrapper) ·
[安全须知](#安全须知务必读) ·
[更新记录](CHANGELOG_zh.md)

cc-remote 是一个开源的远程控制面：本机 `wrapper` 驱动已经安装并登录的
`claude` / `codex`，浏览器通过你自托管的 WebSocket 中继查看和控制会话。
模型、认证与工具执行仍由本地 CLI 决定；cc-remote 不代理模型 API，也不会把
API key 烤进网页。

v3.0.0 不是一次换皮升级。它在原有双引擎、多会话和远程控制之上新增相互隔离的
Code / Work 双空间，并重新设计历史投影、原生客户端协同、多设备路由和发布边界，
重点解决超长会话打开慢、App/CLI 状态不同步、移动端历史跳动和多机器串台等真实问题。

![cc-remote 的 Claude 会话与多会话工作台](assets/readme-claude-multisession.jpg)

---

## 目录

- [v3 架构升级](#v3-架构升级)
- [核心能力](#核心能力)
- [架构](#架构)
- [真实界面与实用功能](#真实界面与实用功能)
- [本地快速开始（一台机器，5 分钟）](#本地快速开始一台机器5-分钟)
- [GitHub Release 一键安装（生产推荐）](#github-release-一键安装生产推荐)
- [生产部署（公网 VPS 中继 + 你机器上的 wrapper）](#生产部署公网-vps-中继--你机器上的-wrapper)
- [环境变量](#环境变量)
- [鉴权模型](#鉴权模型)
- [可靠性边界](#可靠性边界)
- [安全须知（务必读）](#安全须知务必读)
- [模型后端（可选）](#模型后端可选)
- [开发](#开发)
- [FAQ](#faq)
- [许可](#许可)

---

## v3 架构升级

v3 把 cc-remote 从“能在网页控制 CLI”推进为一个本地优先、可恢复、可同时连接
多台机器的完整控制面。相比此前公开版本，主要变化是：

| 升级方向 | v3.0.0 |
|---|---|
| **Code / Work 双空间** | 在原有仓库开发会话之外新增独立 Cowork 工作台。Claude/Codex 各自拥有私有项目、文件/链接/笔记资料库、可复用模板、定时任务和 Artifacts；Work 与 Code 的目录、会话、提示词和权限边界彼此隔离。 |
| **历史打开与超长会话** | 浏览器先绘制 IndexedDB 中最近一次验证的本地投影；wrapper 使用与源文件指纹绑定的 SQLite 摘要页，只先返回最新回合，工具输出、reasoning、进程日志和超长正文按回合展开。短会话不再等待全量扫描，长会话可继续向前分页且保持当前阅读位置。 |
| **Codex 大 rollout** | Codex 历史按回合从文件尾部向前读取，保留 app-server 的原生 resume / compact 状态，不把整个 rollout 重新上传给模型。对特定超大 Codex Desktop + OpenAI 恢复场景，才启用严格限定的官方 HTTP 兼容路径。 |
| **原生 App / CLI 协同** | Claude CLI/Desktop/Agent View 与 Codex shared daemon/App/CLI 使用各自的所有权模型。v3 对齐 running、只读、打断、steer、compact、turn binding 和终止状态，避免兄弟会话误锁、历史回合串到尾部或留下“假思考中”。 |
| **多设备隔离** | Device Center 提供一次性配对、独立可撤销的机器凭据和在线状态；relay 按用户允许的 `machine_id` 路由。设备、Code / Work、引擎、连接 generation 和会话归属分别隔离，延迟帧不能污染当前视图。 |
| **移动端与文件体验** | 历史到顶继续拉取时保留滚动锚点；图片按需加载，支持灯箱、再次点击收起和双指缩放；Markdown、源码、HTML、PDF 与 Office 预览仍在本机安全边界内完成。PWA 图标、窄屏弹层、错误提示和过程时间线也统一收敛。 |
| **可回滚发布** | 产品版本统一为 v3.0.0，wire protocol 为 v27。构建和部署同时校验产品版本与协议版本；VPS 使用不可变 release、独立 venv、原子 `current` 切换和失败回滚，避免直接覆盖正在运行的目录。 |

> **信任边界没有改变：**模型账号、API key、会话源文件和工具执行仍留在
> wrapper 所在机器；VPS relay 不保存对话或 Artifact。浏览历史只读取本地
> transcript / rollout 和可重建投影，不会 resume 引擎，也不会创建模型回合。

完整发布记录和升级注意事项见 [中文 CHANGELOG](CHANGELOG_zh.md)
（[English](CHANGELOG.md)）。

## 核心能力

| 场景 | 可以做什么 |
|---|---|
| **双引擎** | 在同一个 Web UI 中使用 Claude Code 和 Codex；每个会话保持自己的模型、思考强度、权限与运行状态。 |
| **Code / Work 双空间** | Code 继续面向代码仓库；Work 是完全独立的 Cowork 工作区，用于文档、表格、演示、资料整理和临时协作，不混入代码会话列表。 |
| **Work 项目与资料库** | 为 Claude/Codex 分别建立私有项目、文件/链接/笔记资料库和可复用工作模板；创建工作时会把选定上下文物化到专属目录。 |
| **Work 定时任务与隔离** | 支持一次、每日、每周任务；执行记录、租约、失败重试和防重叠状态均持久化。每个工作默认只能访问自己的私有目录，需要的资料通过会话附件或项目资料库显式加入。 |
| **远程操作** | 手机、平板或桌面浏览器实时看流式回复并发送附件。Codex 忙碌时默认把新输入作为原生引导追加到当前任务，也可改为排队；Claude 保留打断并发送。停止始终是独立操作。 |
| **完整过程** | 折叠展示引擎公开提供的 reasoning 摘要、计划、命令输出、文件 diff、MCP、协作代理、Hook 和终端交互事件。 |
| **Artifacts 与文件预览** | Work 自动列出当前工作产生的文件；源码可定位行号，Markdown 可预览和冲突安全编辑，HTML 在隔离 iframe 中渲染，图片/PDF 可直接查看，DOCX/XLSX/PPTX 由 wrapper 本机沙箱临时转换后预览。 |
| **人工确认** | 回传 Claude `can_use_tool`，以及 Codex 命令、文件修改、用户输入、通用权限和 MCP elicitation；终端占用时可只读镜像，也可由用户主动接管。 |
| **会话管理** | 搜索、切换、重命名、归档、删除和消息级派生；Codex 支持主动 compact、原生 Review、派生到独立 worktree，以及把空闲对话迁移到另一工作目录。 |
| **运行控制** | 切换模型、思考强度、服务档位、权限和 Plan 模式；Codex Code 的 `/permissions` 在同一紧凑面板中分别控制审批策略、官方执行环境 profile 和 Cached/Live 网页搜索；`/goal` 管理长目标，`/status` 只读展示 app-server 状态、用量与限额。 |
| **真实扩展目录** | 通过 `/extensions`、`/skills`、`/plugins`、`/apps`、`/mcp`、`/hooks` 按需读取当前引擎目录。Code 中可按引擎能力管理 Skills、插件和 Claude Hooks；Codex Hooks 受官方接口限制为只读。Work 为避免改变私有工作环境，只读展示全部扩展。 |
| **连续性** | 后台会话继续运行，多端实时同步；浏览器本地投影先绘制，wrapper 从 Claude transcript / Codex rollout 的物化摘要索引分页校验，断线后只按游标补实时尾巴。 |
| **多机器与 PWA** | 一个 relay 可连接多个具名 wrapper；可选账号策略把用户限制到指定机器。网页可安装为 PWA；通知默认使用不含会话信息的通用模式，也可由用户主动开启安全截断的会话名称与精确跳转。 |
| **自托管** | wrapper 只出站连接；会话、Work 数据和预览转换都留在本机，VPS 只做无状态中继且可替换；网页认证使用 HttpOnly cookie，CLI 凭据与 API key 不进入前端。 |

> 不同引擎可用的模型、服务档位和运行控制以本机 CLI 及其 SDK/app-server 能力为准。

## 架构

两条**互相独立**的链路：

```
模型链路（cc-remote 不碰）:  claude / codex ──(各自本地配置)──▶ 模型服务

控制链路（本仓库）:          浏览器 ⇄ 中继(WebSocket) ⇄ wrapper ⇄ SDK / app-server ⇄ 本地 CLI
```

| 组件 | 跑在哪 | 干什么 |
|---|---|---|
| **wrapper** | `claude` / `codex` 所在的机器 | 持有会话池、把 SDK/app-server 事件翻成线协议、管打断/排空、按需从 transcript/rollout 读历史，并在本机临时转换 Office 预览。**只出站连中继，机器不需要开入站端口。** |
| **relay（中继）** | 公网 VPS（或本地） | 纯 WebSocket 转发器（FastAPI）。每个 `machine_id` 一个 wrapper 槽，浏览器使用 HttpOnly 会话 cookie，并只接收所选机器的事件。**不持久化会话或 Artifact，从不 import `claude-agent-sdk`、从不碰模型 API**。 |
| **web** | 浏览器 | React 客户端；中继同源托管它的静态文件（`web/dist`）。 |

### Code 与 Work

侧栏顶部的 **Code / Work** 开关复用同一套 Claude/Codex 引擎，但两类会话在
存储、列表和权限上彼此隔离：

- **Code** 保持原有行为，以用户选择的代码仓库为工作目录，适合开发、调试和部署。
- **Work** 适合文档、表格、PPT、调研、资料库和临时对话。Claude 数据默认在
  `~/.claude/cc-remote/work`，Codex 数据默认在 `~/.codex/cc-remote/work`；每项工作有
  独立的 `workspace/` 和上传文件。Artifact 是该工作目录中产生的普通文件，删除工作时
  只删除注册表确认属于该 Work 的目录。Work 会替换两家 CLI 面向代码开发的基础提示词；
  闲聊不会主动检查文件或提及代码项目，需要编程时仍可按用户的明确要求执行。
- Work 不开放用户主目录或任意外部目录。需要引用现有资料时，通过附件或项目资料库
  显式复制到私有工作目录，避免对话意外读取其他项目和历史。
- 工作模板是可复用的工作说明/流程模板，会写入该项目的 `WORK.md`，不会在后台执行
  未审核的第三方代码。定时任务由 wrapper 持久化和领取，使用同一 Work 隔离策略运行。

### 原生终端与 Remote 如何协同

Code 会话按两家 CLI 的真实控制面协同，不替换官方命令：

- **Claude：**`claude` 始终还是官方命令和官方 TUI，cc-remote 不创建 alias、shim
  或 PATH 劫持。直接用 `claude`、Claude Desktop 或 Agent View 打开的会话在 Remote
  中实时只读镜像，避免两个输入端同时写入。需要从 Remote 写入时，由用户主动点击接管；
  cc-remote 只向扫描到的精确同用户 Claude 进程发送 SIGTERM，确认释放后再由 SDK 恢复
  同一会话。它不终止终端 Shell、不使用 SIGKILL，也不会静默接管现有进程。
- **Codex Code：**默认通过 Codex 官方共享 app-server daemon 接入，让原生 Codex
  客户端与 Remote 共享 thread 和控制状态；如果本机版本不支持，会明确降级到私有
  app-server。`CC_REMOTE_CODEX_DAEMON=off` 可用于故障排查。
- **切换 Codex 账号：**把
  `scripts/codex-auth-daemon-restart` 配置为 `codex-auth` 的切换后 hook。
  脚本在官方 daemon restart 前后写入本地代际屏障。Remote 检测到切号后会立即
  中断旧 daemon 上正在运行的 Turn，保持会话为 running，在新 daemon 上恢复同一
  thread，并把同一个任务继续执行完；浏览器中 queued 的后续消息在此期间不会提前
  发送。Goal 使用 Codex 原生自动续跑，普通对话使用不会显示成用户消息、也不增加
  rollback 用户轮次的内部续跑输入。官方 graceful restart 仍可能等待其他原生客户端，
  因此 hook 将它交给独立后台 worker 后立即返回。执行日志位于
  `~/.cc-remote/codex-daemon-restart.log`。它不会重放 Prompt，也不读取或保存
  Codex 凭据。
- **Work：**Claude 与 Codex Work 都保持各自的私有进程和目录，不加入 Code 的共享
  控制面，避免工作资料与代码会话互相泄漏。

### Artifact 预览在哪里运行

- HTML 内容在浏览器端经 DOMPurify 清理后进入无脚本、无外部网络的 sandbox iframe。
- PNG/JPEG/GIF/WebP/AVIF 和 PDF 由 wrapper 做路径、类型和大小校验后，通过当前鉴权
  WebSocket 定向返回给请求它的浏览器。
- DOC/DOCX/ODT/RTF、XLS/XLSX/ODS、PPT/PPTX/ODP 由 **wrapper 所在机器**上的
  LibreOffice 转成 PDF；Linux 使用 bubblewrap 隔离网络、用户目录和文件系统，只挂载本次
  临时目录。转换完成后临时目录立即删除。
- VPS relay 只转发有上限的预览帧，不落原文件或转换结果。换 VPS 不需要迁移会话；换
  wrapper 设备时迁移本机 transcript/rollout、Work 根目录和 cc-remote 状态即可。

## 真实界面与实用功能

以下截图来自实际运行中的 cc-remote，不是设计稿。

### 多会话管理：后台继续跑，随时切回来

左侧会话池按工作目录分组，可以搜索、切换、重命名和归档会话；一个会话在后台处理时，仍可进入另一个会话继续工作，切回来即可看到完整实时进度。Claude Code 与 Codex 会话共用同一套工作台，但各自保留独立的上下文、模型、权限和运行状态。

![按项目分组并可搜索切换的多会话工作台](assets/readme-multi-session.jpg)

### Claude Code：思考、工具调用和 Hook 都能看见

Claude 会话不是一个只显示最终文字的简化聊天框。Remote 会接收 Claude Code SDK 暴露的思考、命令调用、工具结果和 Hook 生命周期，按发生顺序折叠展示；底部同时显示 Claude 当前模型、思考强度、权限模式和上下文占用。

![Claude Code 的思考、命令调用和 Hook 处理过程](assets/readme-claude-session.jpg)

### 新会话：先选引擎和工作目录

一个入口创建 Claude Code 或 Codex 会话；工作目录可浏览选择，第一条消息可直接带图片或文件。会话建立后再按需要调整模型、权限和 Plan 模式，不用先填写一排默认参数。

![选择引擎和工作目录并创建新会话](assets/readme-new-session.jpg)

### Codex：计划与处理过程完整保留

Codex 会话把 app-server 提供的 reasoning 摘要、计划、命令、diff、MCP、协作代理与 Hook 组织成可折叠时间线。运行中可以展开追踪细节，完成后收起为一行摘要；最终答复始终独立显示。

![可折叠的计划、Hook 和工具调用处理过程](assets/readme-process-timeline.jpg)

### Codex 会话级控制：模型、思考、权限、搜索与状态

模型、思考强度、服务档位和权限都绑定当前会话；可以在不改本机全局配置的情况下调整下一回合。Codex 的“审批策略”和“执行环境”是两件事：`never` / `on-request` / `untrusted` 决定何时询问，Read Only / Workspace / Full Access 或自定义 named profile 决定文件系统和网络边界。`/permissions` 把两者与 Cached/Live 网页搜索放在同一个弹层中，不占用手机端输入栏宽度。Codex 运行中按 Enter 默认引导当前任务，仍可切换为排队，空输入时的停止按钮不会隐式发送；输入区同时提供附件、上下文占用以及 `/goal`、`/status` 等命令入口。

![Codex 模型选择和会话控制](assets/readme-model-controls.jpg)

### 常用操作速查

- **会话**：新建、搜索、后台运行、重命名、归档、删除、派生、Codex compact、Review 和独立 worktree；每个未归档 Codex Code 会话的三点菜单还可在不改变 thread ID 的情况下，把空闲对话迁移到另一目录继续。
- **回合**：流式输出、Codex 原生引导、排队、停止/打断、复制、编辑重发、从指定消息派生。
- **工具**：命令输出、文件修改与 diff、MCP、协作代理、Hook、审批和用户输入回传。
- **终端协同**：Codex Code 共享官方 daemon 并支持双向控制；Claude 原生 CLI、Desktop
  和 Agent View 在 Remote 中实时只读镜像，需要写入时由用户明确接管。
- **状态**：模型、思考强度、权限、Plan、上下文、目标、用量、rate limit 和运行告警。
- **扩展**：通过斜杠命令实时查看 Skills、Plugins、Apps、MCP 和 Hooks；Code
  可按引擎能力安全增删本地 Skills、管理 Claude Hooks，并通过原生管理器安装/卸载插件。
  Codex Hooks 和 Work 中的全部扩展保持只读。
- **设备**：响应式手机界面、深浅主题、多浏览器/多机器同步、PWA、可选通用/会话级后台完成提醒与断线重连。登录后的通知、主题和退出登录统一收在 Header 三点菜单中。

## 本地快速开始（一台机器，5 分钟）

先在 **agent CLI 所在的那台机器**上把中继 + wrapper + 网页都跑起来，验证整条链路。生产部署见下一节。

### 前置

- 一台已完成 **Claude Code** 或支持 `app-server` 的 **Codex CLI** 登录、且 CLI **本身已经能正常对话**的机器。Claude wrapper 会显式启动日常使用的 `~/.local/bin/claude`，而不是 SDK 内置副本；Codex 每次新建 app-server 时会重新选择本机最新可用版本。两个都可用即可在网页中切换引擎。
- **Python 3.10+**、**Node 20.19+**（用来构建网页）。
- 可选：要预览 DOCX/XLSX/PPTX 等 Office 文件，Linux wrapper 主机需安装
  **LibreOffice + bubblewrap**（例如 `sudo apt install libreoffice bubblewrap`）；VPS 不需要。

### 1）装依赖 + 构建网页

```bash
git clone https://github.com/muggle-stack/cc-remote.git && cd cc-remote

python3 -m venv .venv && source .venv/bin/activate
pip install --require-hashes --only-binary=:all: -r requirements.lock

npm --prefix web ci
npm --prefix web run build          # 产出 web/dist/
```

### 2）配置

```bash
install -m 600 .env.example .env
```

编辑 `.env`，至少改这几项：

```ini
# 网页登录口令（自己定一个强口令）
LOGIN_PASSWORD=<一个强口令>
# 给会话 token 签名用的密钥
SESSION_SECRET=<openssl rand -hex 32>
# wrapper ⇄ relay 的共享 token
WRAPPER_TOKEN=<openssl rand -hex 32>
# 浏览器访问中继时的精确来源；本地 HTTP 只允许 loopback
PUBLIC_ORIGIN=http://127.0.0.1:8765
# 让中继同源托管网页
WEB_STATIC_DIR=web/dist
# agent 会话的默认工作目录（你要让它操作的项目目录）
CC_CWD=/path/to/your/project
# 与日常终端共用同一个 Claude Code 安装；仅在安装位置不同时覆盖
CLAUDE_BIN=~/.local/bin/claude
```

> 本地 loopback 快速体验时，中继和 wrapper 可读同一个 `.env`；它不适合生产。
> 公网 wrapper 必须按下文使用 root-only `/etc/cc-remote/wrapper.env`，避免
> `bypassPermissions` 模型/工具直接读取控制面密钥。
>
> Python SDK 仍固定为仓库验证过的版本，负责消息协议和 interrupt/drain；
> 真正执行会话的是 `CLAUDE_BIN` 指向的日常 Claude Code。这样 Remote 与终端
> 共用同一套 CLI 更新、Keychain/登录状态和 `~/.claude/settings.json`。
> `CLAUDE_BIN` 为空时也会回到 `~/.local/bin/claude`，不会静默改用 SDK bundle。

### 3）跑起来（两个终端）

```bash
# 终端 1：中继（同源提供 网页 + /ws + /api，监听 http://127.0.0.1:8765）
python -m cc_remote.relay

# 终端 2：wrapper（驱动本地 claude / codex）
python -m cc_remote.wrapper
```

### 4）打开网页

浏览器开 **http://127.0.0.1:8765** → 用 `LOGIN_PASSWORD` 登录 → 发条消息，应能看到流式回复、可打断、可多会话切换。

> 想改网页代码时用开发模式：`npm --prefix web run dev`（Vite）。生产/联调直接用上面的 `build` + 中继同源托管更简单。

## GitHub Release 一键安装（生产推荐）

正式版把 Relay 和 Wrapper 拆成按系统/架构构建的独立包。Relay 包只含后端和已构建
Web，Wrapper 包只含本机控制端；两者都自带 `uv`，安装时创建托管的 Python 3.13
环境。用户不需要 clone 仓库、安装 Node 或在服务文件里粘贴 token。

支持矩阵：

| 角色 | 系统 | 架构 | 常驻方式 |
|---|---|---|---|
| Relay | Ubuntu 22.04+ / Debian 12+ | x86_64、arm64 | systemd + Caddy |
| Wrapper | macOS | Intel、Apple Silicon | 当前用户 LaunchAgent |
| Wrapper | glibc Linux + systemd（推荐 Ubuntu 22.04+ / Debian 12+） | x86_64、arm64 | 指定普通用户的 systemd 服务 |

### 1）下载并校验引导脚本

在 GitHub Release 页面确认版本与 release attestation，再在待安装机器下载同一版本的
`install.sh` 和 `SHA256SUMS`：

```bash
release=https://github.com/muggle-stack/cc-remote/releases/download/v3.0.0
curl -fLO "$release/install.sh"
curl -fLO "$release/SHA256SUMS"

# Linux
grep ' install.sh$' SHA256SUMS | sha256sum -c -
# macOS 改用：
# grep ' install.sh$' SHA256SUMS | shasum -a 256 -c -
chmod +x install.sh
```

引导脚本检测 OS/CPU，只下载对应角色包，并在解压和执行前校验该包的 SHA-256。

### 2）VPS 安装 Relay

先把域名 A/AAAA 记录指向 VPS，并放行 80/443，然后运行：

```bash
./install.sh relay --domain remote.example.com
```

Linux 上脚本会自行请求 `sudo`。首次安装会交互要求一个至少 16 字符的网页登录口令，
自动生成 Relay 密钥，安装 Caddy/systemd，并在 `/opt/cc-remote/releases/` 中完成
不可变 staging、原子 `current` 切换和失败回滚。已有
`/opt/cc-remote/.env` 会原样保留。

如果还要通过 LAN/Tailscale IPv4 地址直连同一台 Relay，首次安装时显式开启：

```bash
./install.sh relay --domain remote.example.com --allow-private-origins
```

这会让 Relay 监听 `0.0.0.0:8765`，公网域名仍由 Caddy 提供 HTTPS。端口 8765
会出现在所有 IPv4 网卡上，必须用主机防火墙只允许可信 LAN/Tailscale 对端。
已有安装仍保留 `.env`；要开启该模式，先手动把其中的
`RELAY_HOST=0.0.0.0` 和 `ALLOW_PRIVATE_ORIGINS=1` 一起设置，再用相同参数升级。

打开 `https://remote.example.com/` 登录，在顶部设备中心选择“允许添加设备”，复制
一次性配对码。

### 3）在 Claude / Codex 所在机器安装 Wrapper

确保这台机器上的官方 `claude` 或 `codex` 已登录且本身能正常工作，然后执行：

```bash
./install.sh wrapper \
  --relay https://remote.example.com \
  --pair XXXXX-XXXXX-XXXXX-XXXXX \
  --name "MacBook Pro"
```

macOS 必须以当前桌面用户运行，安装器创建用户 LaunchAgent；Linux 会请求 `sudo`，
但 Wrapper 和所有模型/工具子进程仍以发起安装的普通用户运行。设备长凭据只写入
`0600` 私有配置：macOS 为 `~/.cc-remote/device.json`，Linux 为
`/etc/cc-remote/device.env`；不会进入 plist、systemd unit 或 release 目录。

升级同一台机器时下载新版本 `install.sh` 后重新执行即可。Relay 仍传 `--domain`；
Wrapper 已有设备凭据时只需：

```bash
./install.sh wrapper
```

协议大版本升级仍应在同一维护窗口完成 Relay、Web 和所有 Wrapper；已经打开的页面要
硬刷新。安装器保留上一 release，服务验活失败会把 `current` 和服务定义恢复到旧版。

## 生产部署（公网 VPS 中继 + 你机器上的 wrapper）

以下保留源码 staging / 手工配置路径，适合开发、自定义部署和故障恢复。普通正式安装
优先使用上面的 GitHub Release。把中继搬到公网后，wrapper 从你的机器**出站**
`wss://` 连它，手机浏览器连同一个域名。模型链路完全不动。

```
你的机器 wrapper ──wss:443──▶ Caddy(VPS, 自动 HTTPS) ──▶ relay(127.0.0.1:8765) ◀──wss:443── 手机浏览器
                                                              └─ 同源托管 web/dist
```

### 前置

- **VPS**：Ubuntu 22.04+ / Debian 12+（或其他自带 Python 3.10+ 的 Debian 系发行版），放行 **80 + 443**（80 给 Let's Encrypt 验证，443 给 wss）。
- **域名**：A 记录指向 VPS 公网 IP（Caddy 自动签 + 续 Let's Encrypt 证书）。
- **你的机器**：Linux（下面用 systemd 常驻 wrapper），出站 443 到公网放行。

没有域名时也支持公网 IPv4 + 明文 HTTP/WS 的临时逃生路径：VPS 只需放行
80，wrapper 需能出站访问 80。该模式仍由 Caddy 反代到 loopback relay，保留
请求限制和服务加固，但**没有任何传输加密**；登录口令、cookie、wrapper token
和全部会话内容都可能被链路上的人读取或篡改。

### 1）生成 token / 口令

```bash
openssl rand -hex 32   # WRAPPER_TOKEN（relay 与 wrapper 两边要一致）
openssl rand -hex 32   # SESSION_SECRET（relay 用）
# 再想一个 LOGIN_PASSWORD（网页登录口令）
```

### 2）在 dev 机器构建网页

```bash
npm --prefix web ci
npm --prefix web run build   # 产出 web/dist/
```

> 现在网页**不再把 token 烤进 JS**：登录改为向中继 POST 口令换取短期会话 token。所以构建不需要任何 `VITE_*` 变量。

> **升级到协议 v27**：线协议会严格拒绝版本不一致。请在同一次维护窗口部署
> `cc_remote/` 和新的 `web/dist/`，然后依次重启 relay、wrapper；不要新旧版本滚动混跑。
> 升级期间已有 WebSocket 会短暂重连，relay 重启也会要求浏览器重新登录。已打开的
> 旧版页面必须做一次**硬刷新**（重新加载新的带 hash 静态资源），仅重新登录不够。
> 手工发布时先停本机 wrapper，再停服更新 relay + web，最后启动 v27 relay 和
> v27 wrapper；这样旧 wrapper 不会占住同一 `machine_id` 的连接槽。

### 3）上传 staging，由原子 release 安装器发布

```bash
# dev 机器：普通账号只写自己的 staging，不直接写 root-owned /opt
rsync -av --delete --exclude='.git' --exclude='.venv' \
  --exclude='web/node_modules' --exclude='.env' \
  ./ <vps-user>@<vps>:~/cc-remote-upload/

# VPS：不要把 staging 覆盖到正在运行的 /opt 正式目录
ssh <vps-user>@<vps>
sudo mkdir -p /opt/cc-remote
```

安装器会把 staging 复制到新的
`/opt/cc-remote/releases/release-*`，在其中构建独立 venv，全部校验通过后再原子切换
`/opt/cc-remote/current`。旧 release 的代码、`web/dist` 和 venv 会完整保留用于失败回滚；
不会再对脏的正式目录执行 `rsync --delete`。

### 4）VPS：配 `.env` + 一键 setup

```bash
# 在 VPS 上：.env 是 releases 之外唯一共享的运行配置
sudo test -f /opt/cc-remote/.env || sudo install -m 600 \
  ~/cc-remote-upload/deploy/env.relay.example /opt/cc-remote/.env
sudoedit /opt/cc-remote/.env
# 填 LOGIN_PASSWORD / SESSION_SECRET / WRAPPER_TOKEN；保持：
# WEB_STATIC_DIR=/opt/cc-remote/current/web/dist

# 升级时先停本机 wrapper，随后让安装器一次切换 relay + web
sudo bash ~/cc-remote-upload/deploy/setup-vps.sh \
  your-domain.com ~/cc-remote-upload
```

若暂时只有公网 IPv4，则改成下面这一组严格匹配的配置和参数：

```ini
# /opt/cc-remote/.env
PUBLIC_ORIGIN=http://your-public-ip
ALLOW_INSECURE_HTTP=1
```

```bash
sudo bash ~/cc-remote-upload/deploy/setup-vps.sh \
  your-public-ip ~/cc-remote-upload
```

脚本只会在开关明确开启、参数是公网 IPv4 且 `PUBLIC_ORIGIN` 精确匹配时选择
明文 Caddy 配置；私网、loopback、保留地址和错误拼写都会拒绝启动。

脚本会：装 `python3-venv` + Caddy、建 `ccremote` 系统用户、创建不可变 release
和 release-local venv、合并 Caddy 配置、原子切换 `current`，再重启 relay。若新
relay 重启或健康检查失败，`current`、Caddyfile、systemd unit 会作为一个事务全部
恢复，并验证旧 release 的 `/healthz`。成功后再启动 v27 wrapper。

验证：

```bash
curl https://your-domain.com/healthz
# 期望：{"ok":true,"wrapper_connected":false,"clients":0}
```

明文模式改用 `curl http://your-public-ip/healthz`。

### 5）你的机器：配 root-only wrapper 环境 + systemd

如果需要 Office Artifact 预览，先在这台 wrapper 主机安装转换沙箱（不要装到 VPS）：

```bash
sudo apt-get update && sudo apt-get install -y libreoffice bubblewrap
```

```bash
cd /path/to/cc-remote
python3 -m venv .venv
.venv/bin/pip install --require-hashes --only-binary=:all: -r requirements.lock

# 密钥源由 root 持有；模型/工具使用你的普通用户运行，不能直接读取该文件。
sudo install -d -o root -g root -m 0755 /etc/cc-remote
sudo install -o root -g root -m 0600 deploy/env.wrapper.example \
  /etc/cc-remote/wrapper.env
sudoedit /etc/cc-remote/wrapper.env  # 填 RELAY_URL / WRAPPER_TOKEN / CC_CWD

# 装 systemd 服务（先编辑 User、仓库/venv/home 路径；不要改回仓库 .env）
sudo cp deploy/cc-remote-wrapper.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now cc-remote-wrapper
journalctl -u cc-remote-wrapper -f     # 期望：connected to relay / wrapper running
```

明文模式下 wrapper 侧还必须同时设置：

```ini
RELAY_URL=ws://your-public-ip/ws
ALLOW_INSECURE_HTTP=1
```

回 VPS 再看对应模式的 `/healthz` → 应 `wrapper_connected:true`。

#### 用设备中心配对 Mac / Linux（推荐）

登录网页，点顶部的设备图标，选择“允许添加设备”。页面会生成一个只使用一次、
默认 10 分钟过期的配对码和命令。在新机器的 cc-remote 仓库中执行：

```bash
python -m cc_remote.device pair https://your-domain.com XXXXX-XXXXX-XXXXX-XXXXX \
  --name "MacBook Pro"
python -m cc_remote.wrapper
```

交互运行时，凭据会以 `0600` 保存到 `~/.cc-remote/device.json`。Linux systemd
部署建议直接写入 root-only EnvironmentFile，再重启服务：

```bash
sudo .venv/bin/python -m cc_remote.device pair \
  https://your-domain.com XXXXX-XXXXX-XXXXX-XXXXX \
  --name nono --env-file /etc/cc-remote/device.env
sudo systemctl restart cc-remote-wrapper
```

Relay 只保存设备凭据的哈希；配对成功后明文凭据不会再次显示。设备中心可查看
在线/离线状态、切换机器、重命名或单独撤销设备。旧的 `WRAPPER_TOKEN` /
`WRAPPER_TOKENS_JSON` 手工配置方式仍保持兼容。

### 6）手机验证

手机浏览器（任意网络）打开对应的 `https://your-domain.com/` 或
`http://your-public-ip/` → 用 `LOGIN_PASSWORD` 登录 → 发消息，应看到流式回复 +
可打断 + 多端同步。

### 公司/内网走 HTTP 代理出网？

wrapper 用 `websockets` 出站，认 `HTTPS_PROXY` / `ALL_PROXY` 环境变量。在
`/etc/cc-remote/wrapper.env` 加：

```ini
HTTPS_PROXY=http://your-proxy:port      # SOCKS 用 ALL_PROXY=socks5://...
```

（若代理做 TLS 中间人，需把它的根证书加进系统信任。）

## 环境变量

**中继（relay）**

| 变量 | 默认 | 说明 |
|---|---|---|
| `RELAY_HOST` / `RELAY_PORT` | `127.0.0.1` / `8765` | 监听地址（仅 Caddy 公网入口时保持 `127.0.0.1`；同时开放 LAN/Tailscale IPv4 直连时必须配合 `ALLOW_PRIVATE_ORIGINS=1` 改为 `0.0.0.0` 并限制防火墙）。 |
| `LOGIN_PASSWORD` | 空 | 单用户网页登录口令。未设置 `LOGIN_USERS_JSON` 时**必须设**。 |
| `LOGIN_USERS_JSON` | 空 | 可选多用户策略：`{"alice":{"password":"…","machines":["mac","nono"]}}`；设置后替代 `LOGIN_PASSWORD`。 |
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
| `WRAPPER_TOKENS_JSON` | 空 | 可选机器绑定 token：`{"mac":"…","nono":"…"}`；设置后替代 relay 的通配 `WRAPPER_TOKEN`。 |
| `WEB_STATIC_DIR` | 空 | 指向 `web/dist` 则同源托管网页；留空则只做 API/WS。 |
| `CLIENT_QUEUE_CAP` / `CLIENT_QUEUE_BYTES` | `4096` / `16777216` | 单客户端待发帧数/字节硬上限；超限断开慢客户端，不静默丢帧。 |
| `MAX_CLIENTS` / `CLIENT_HELLO_TIMEOUT` | `8` / `10` | 已接受客户端总数和首个 Hello 帧等待秒数的硬上限。 |
| `WS_MAX_SIZE_BYTES` | `16777216` | relay 与 wrapper 接受的单个 WebSocket 帧上限。 |

**wrapper**

| 变量 | 默认 | 说明 |
|---|---|---|
| `RELAY_URL` | `ws://127.0.0.1:8765/ws` | 中继的 WebSocket 地址（公网用 `wss://域名/ws`，除非开了 `ALLOW_INSECURE_HTTP`）。 |
| `ALLOW_INSECURE_HTTP` | `0` | 同中继的逃生开关；wrapper 也读这个变量，开启后 `RELAY_URL` 可以在非 loopback 时仍用 `ws://`。 |
| `WRAPPER_TOKEN` | `change-me-wrapper` | 同中继。 |
| `CC_REMOTE_MACHINE_ID` | `default` | 多机器 relay 中的稳定路由 id；使用 `WRAPPER_TOKENS_JSON` 时必须匹配对应键。 |
| `CC_REMOTE_DEVICE_CONFIG` | `~/.cc-remote/device.json` | 交互配对凭据路径；文件必须仅当前用户可读。显式的 `RELAY_URL` / `WRAPPER_TOKEN` / `CC_REMOTE_MACHINE_ID` 优先。 |
| `CLAUDE_BIN` | `~/.local/bin/claude` | wrapper 实际启动的日常 Claude Code；空值仍使用该默认路径。只有 CLI 安装在别处时才设为另一个绝对路径。 |
| `CC_REMOTE_CODEX_PROXY` | 空 | 仅注入 wrapper 启动的 Codex 子进程的 HTTP(S)/SOCKS5 代理；不改 wrapper 到 relay 的连接，也不影响用户终端里的 `codex`。例如 nono 可填 `http://127.0.0.1:7897`。 |
| `CC_REMOTE_CODEX_DAEMON` | `auto` | Code 默认连接 Codex 官方共享 daemon；`off` 强制使用私有 stdio app-server。Work 始终私有，不受此项影响。 |
| `CC_REMOTE_STATE_DIR` | `~/.cc-remote` | 本机 wrapper 状态目录。账号切换 hook 与 wrapper 必须使用同一个值，daemon 代际屏障保存在其中；不包含 Codex 凭据。 |
| `CC_CWD` | 当前目录 | 新会话默认工作目录。Claude `--resume` 靠它定位 `~/.claude/projects/` 下的会话文件，**必须对**；Codex 恢复时会优先从 rollout 取原 cwd。 |
| `CC_RESUME_SESSION_ID` | 空 | 恢复指定会话 UUID；留空开新会话。首次启动后 id 会持久化到 `~/.cc-remote/`。 |
| `CLAUDE_WORK_ROOT` | `~/.claude/cc-remote/work` | Claude Work 的私有注册表、资料库、会话目录和策略文件根目录。 |
| `CODEX_WORK_ROOT` | `~/.codex/cc-remote/work` | Codex Work 的私有注册表、资料库、会话目录和策略文件根目录。 |
| `MAX_CONCURRENT_SESSIONS` | `20` | 常驻 agent 子进程上限（内存随引擎/版本变化）。超了就驱逐 idle 的；客户端缓存仍在，可再切回。 |
| `DRAIN_TIMEOUT` | `15` | interrupt 后等终止 ResultMessage 的秒数，超时强制重连（排空保险）。 |
| `CODEX_TURN_IDLE_WARN_SECONDS` | `90` | Codex app-server 连续无事件时显示“仍在等待”提示；`0` 禁用。只提示，不自动打断 ultra 推理或长工具。 |
| `RING_MAX_EVENTS` / `RING_MAX_BYTES` / `TOOL_RESULT_MAX` | 见 `.env.example` | 实时尾巴缓冲 / 工具输出截断上限调优。 |
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

- Web 与 TUI 会给可重试命令附加稳定的 `cmd_id`，断线重连或 wrapper 恢复后重发；wrapper 在同一进程生命周期内去重并返回 ACK。每个实时会话还用 wrapper generation 配对 cursor，避免 wrapper 重启后把旧序号误当成新序号。
- 排队及打断后的替换消息一经 wrapper 接收，就由 wrapper 的有界内存队列持有；即使所有浏览器/PWA 休眠、断线或硬刷新，也会在当前回合真正结束后继续执行，并在客户端重连时恢复队列摘要。点击摘要会私有按需读取完整指令，可在执行前原子编辑文字且保留附件；完整 payload 不进入可重放 ring。该队列不会跨 wrapper 进程崩溃或重启持久化。
- 未确认命令队列和通用命令去重表是**有界内存状态**：浏览器硬刷新、TUI 退出或 wrapper 进程崩溃，不承诺跨进程的 exactly-once。cc-remote 是交互控制面，不是持久任务队列；这类故障后应先核对 transcript/rollout 和会话状态，再决定是否重发。
- 已落盘的 Claude transcript / Codex rollout 是历史事实来源；wrapper 的 SQLite 摘要索引和浏览器 IndexedDB 都是可重建投影，实时 ring 只负责有界的断线补流。工具/思考等大块详情按单轮展开，不阻塞会话首屏。
- Work 定时任务是例外：计划、运行记录、租约、心跳、重试次数和下次运行时间写入 SQLite；wrapper 重启后会恢复过期租约，但仍不会把不确定结果伪装成成功。

## 安全须知（务必读）

> **cc-remote 会让远端的人在你机器上跑任意命令。请当成「给别人一个你机器的 shell」来对待。**

- Code 会话仍是远程开发控制面：Claude 默认使用 `permissionMode: bypassPermissions`；Codex 默认审批策略是 `never`，并可在 app-server 对当前 cwd 允许的 named permission profile 中选择执行环境。审批策略不会扩大 profile 的边界，Full Access 则会显著扩大能力。**能登录且能进入 Code 的人，仍应等同于拿到了这台机器的远程 agent/shell 权限。** Work 会话使用固定的 `cc_remote_work` profile、独立私有根目录且不开放外部目录，但这只是缩小默认能力面，不替代操作系统级的独立用户、容器或虚拟机隔离。
- `LOGIN_PASSWORD` / `LOGIN_USERS_JSON`、`WRAPPER_TOKEN` / `WRAPPER_TOKENS_JSON` 和 `SESSION_SECRET` 是认证边界：用强随机值、别提交 git、别贴到聊天里、定期轮换。仓库 `.env` 只适合本机开发；生产 wrapper 必须使用上述 root-only `/etc/cc-remote/wrapper.env`。systemd 模板会禁止服务及模型子进程读取这个源文件和遗留仓库 `.env`；Linux wrapper 还会关闭 dumpability，避免子进程从 `/proc/<pid>/environ` 或进程内存取回已经捕获的 token。
- 公网必须上 TLS（`wss://`，本仓库用 Caddy 自动签证书）。只有明确需要临时使用公网 IPv4 + 明文 HTTP/WS 时才设 `ALLOW_INSECURE_HTTP=1`；开启后登录口令、cookie、wrapper token 和全部会话流量都不加密，应尽快切回 TLS。`ALLOW_PRIVATE_ORIGINS=1` 只为同端口私网字面 IP 增加与实际请求目标一致的直连入口，不会放宽公网域名校验；Cookie 的 `Secure` 属性按受信请求传输判断，不读取调用者提供的 Origin。但使用内网 HTTP 时，登录口令、cookie 和会话内容在该网络中仍是明文。
- 建议：给中继加 IP 白名单 / 只在需要时开、给登录加失败限速（已内置每 IP 每分钟 5 次）。

## 模型后端（可选）

cc-remote **不碰模型 API**——它只驱动你机器上已经配置好的 CLI：Claude 使用 `~/.claude/settings.json`，Codex 使用自己的登录与 `~/.codex/config.toml`。所以：

- 用**官方 Anthropic API**：装好 `claude` 能对话即可，cc-remote 直接用。
- 用**兼容端点（如 GLM / z.AI）**：照常在 `settings.json` 里设 `ANTHROPIC_BASE_URL`（指向官方兼容端点或你自建的代理），cc-remote 一样只做控制链路。
- 用 **Codex**：先确保本机 `codex` 可正常对话且 `codex app-server` 可启动；cc-remote 不接触其 API key，也不会改写全局认证配置。

## 开发

```
cc_remote/
  protocol.py      # pydantic 线协议（客户端/中继/wrapper 都依赖）
  config.py        # 环境变量配置
  relay/           # FastAPI 中继：server / auth / pairing / forward
  wrapper/         # Claude SDK + Codex app-server / 会话池 / stream / ringbuffer / transport
web/               # React 客户端（Vite + TS）
tests/             # 零 token 单元测试 + 端到端脚本
deploy/            # Caddyfile / systemd / setup-vps.sh / env 示例
```

```bash
python -m pip install -r requirements-dev.txt
pytest                              # 单元测试（不触模型，零 token）
npm --prefix web run test:reliability # 前端可靠性纯测试

# 显式真实链路测试（需要已运行的 relay + wrapper，会调用模型）
CC_REMOTE_RUN_E2E=1 CC_REMOTE_E2E_SCENARIO=smoke \
  RELAY_URL=wss://remote.example/ws LOGIN_PASSWORD='...' \
  pytest -q tests/test_e2e_entry.py
npm --prefix web run lint           # 前端静态检查
npm --prefix web run dev            # 网页开发模式
npm --prefix web run build          # 网页生产构建
```

贡献指南与内部架构约定见 [CLAUDE.md](CLAUDE.md)。

## FAQ

- **wrapper 重启会丢历史吗？** 已落盘历史不会；它来自磁盘上的 Claude transcript / Codex rollout。重启会丢尚未确认的内存命令和实时 ring，详见上面的可靠性边界。
- **中继重启会断吗？** 会短暂断连并要求重新登录（进程内撤销注册表会重置）；对话不丢，因为会话在 wrapper 机器上。
- **可以更换 VPS 或迁移到新设备吗？** 可以。VPS 只提供 relay + Web 静态文件，不是会话权威；更换它只需部署同版本并让 wrapper 指向新的 `RELAY_URL`。迁移 wrapper 设备时，复制 Claude transcript、Codex rollout、`CLAUDE_WORK_ROOT` / `CODEX_WORK_ROOT` 和 `~/.cc-remote`，在新设备重新登录 CLI 后再启动 wrapper。
- **要开入站端口吗？** 不用。wrapper 只出站连中继。
- **多贵？** cc-remote 本身零模型开销；浏览/刷新/看历史都不花 token。真正的模型花费取决于本地 agent CLI 使用的后端。

## 许可

MIT，见 [LICENSE](LICENSE)。
