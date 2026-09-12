# 安装与升级

[English](installation_en.md) · [返回首页](../README.md) · [部署流程与验收](../deploy/README.md)

需要当前功能时，推荐使用测试通过的源码快照；指定已发布版本时使用对应 tag 的包，
并先确认其功能和协议满足需求。最新 Release tag 不一定包含当前源码分支的更新。
本文提供安装路径，[deploy/README.md](../deploy/README.md) 负责 staging、激活、回滚与
验收约定。既有自定义服务保留原来的运行用户、私有配置和安装布局。

[Release 安装](#release-install) · [源码部署](#source-install) ·
[DSH 接入](../integrations/dsh/README.md)

<a id="release-install"></a>

## GitHub Release 一键安装（指定已发布版本）

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
`install.sh` 和 `SHA256SUMS`。下例使用 `3.0.0`；请替换为已选定的已发布 tag
（变量中不带开头的 `v`）。该路径不会自动安装尚未发布的开发分支：

```bash
export CC_REMOTE_VERSION=3.0.0
release_base="https://github.com/muggle-stack/cc-remote/releases/download/v${CC_REMOTE_VERSION}"
curl -fLO "$release_base/install.sh"
curl -fLO "$release_base/SHA256SUMS"

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

当前 Wrapper 安装器会检查服务用户的 `~/.local/bin/claude` 是否可执行，
即使主要使用其他引擎也会检查。使用安装器前先准备该日常 CLI；源码运行配置中的
任意 `CLAUDE_BIN` 覆盖不会绕过这项安装检查。完成实际要用的引擎登录，DSH 另外
按接入说明配置本机服务。然后执行：

```bash
./install.sh wrapper \
  --relay https://remote.example.com \
  --pair XXXXX-XXXXX-XXXXX-XXXXX \
  --name "我的电脑"
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

协议发生变化时，应在同一维护窗口完成 Relay、Web 和所有 Wrapper；已经打开的页面要
硬刷新。安装器保留上一 release，服务验活失败会把 `current` 和服务定义恢复到旧版。

### 下载源配置

`CC_REMOTE_RELEASE_BASE_URL` 可指向可信镜像或本地 `file://` 目录，其中需要包含
对应安装包和 `SHA256SUMS`。依赖和 Python 下载分别可配置 `UV_DEFAULT_INDEX`、
`UV_PYTHON_INSTALL_MIRROR`，仍需通过原有校验。安装尚未开始时下载失败可以重试；
激活期间连接中断，必须先核对原事务状态。

<a id="source-install"></a>

## 生产部署（公网 VPS 中继 + 你机器上的 wrapper）

以下源码 staging 路径推荐用于当前功能部署，也适合自定义部署和故障恢复。
把中继搬到公网后，wrapper 从你的机器**出站**
`wss://` 连它，手机浏览器连同一个域名。模型链路完全不动。

让 AI 协助部署时，使用仓库内的
[部署 Skill](../.agents/skills/cc-remote-deploy/SKILL.md)，并遵循
[部署验收](../deploy/README.md#codex-code-shared-control-plane-acceptance)：每个 Codex Code
账号的 CLI 与 Wrapper 必须接到同一个官方 daemon。部署后如果本机装有受支持的
Codex App，AI 会先询问是否接入；这是[可选步骤](../deploy/README.md#optional-codex-app-attachment)，
不同意或暂不回答都不会改动 App，也不影响 cc-remote 部署。已有明确接入授权时不重复
询问；按平台分别遵循 [macOS](codex-desktop-launcher.md) 或 [Linux](codex-desktop-linux.md)
流程，保留账号边界，并逐一验证 CLI、Wrapper 与 App 的真实连接。

```
你的机器 wrapper ──wss:443──▶ Caddy(VPS, 自动 HTTPS) ──▶ relay(127.0.0.1:8765) ◀──wss:443── 手机浏览器
                                                              └─ 同源托管 web/dist
```

### 前置

- **VPS**：Ubuntu 22.04+ / Debian 12+（其他受支持的 Debian 系发行版；源码／CI 验证使用 Python 3.13），放行 **80 + 443**（80 给 Let's Encrypt 验证，443 给 wss）。
- **域名**：A 记录指向 VPS 公网 IP（Caddy 自动签 + 续 Let's Encrypt 证书）。
- **你的机器**：macOS 或 glibc Linux，允许出站 443；升级时保留已有服务管理方式。

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

### 2）测试并冻结一份源码快照

使用 Node 24，执行 [AGENTS.md](../AGENTS.md#commit-and-pr-gate) 的完整门禁，再将
测试过的源码和构建好的网页一起冻结。快照不包含凭据、私有状态、`.git`、虚拟环境
或依赖目录。所有目标使用同一份字节，网页构建命令为：

以下构建已包含在完整门禁中，通过后无需重复执行。

```bash
npm --prefix web ci
npm --prefix web run build   # 产出 web/dist/
```

再按部署流程用 `deploy/validate_protocol_bundle.py` 校验源码与 Web 协议。
网页构建不需要任何登录密钥。

**所有目标先 staging，再改动线上服务。** 下文分别描述 Relay 和 Wrapper，
不能在 Wrapper staging 未验证时先激活 Relay。协议 v65 不允许混用旧客户端：
停止不兼容的旧 Wrapper，激活 Relay + Web，再激活 Wrapper 并硬刷新网页。
Wrapper 激活须通过 `deploy/work_registry_snapshot.py` 保存 Work SQLite 与私有账号
控制状态，不再按“是否来自某个旧协议”决定是否保护。回滚先恢复匹配状态，再启动
旧代码；不要在运行中只复制 SQLite 主文件而遗漏 WAL。

### 3）上传 staging，由原子 release 安装器发布

```bash
# dev 机器：普通账号只写自己的 staging，不直接写 root-owned /opt
rsync -av --delete --exclude='.git' --exclude='.venv' \
  --exclude='web/node_modules' --exclude='.env' \
  /absolute/path/to/tested-snapshot/ "<vps-user>@<vps>:~/cc-remote-upload/"

# VPS：不要把 staging 覆盖到正在运行的 /opt 正式目录
ssh "<vps-user>@<vps>"
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

# 全部 Wrapper staging 验证后才激活；先停止不兼容的旧 Wrapper。
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
恢复，并验证旧 release 的 `/healthz`。成功后再启动 v65 wrapper。

验证：

```bash
curl https://your-domain.com/healthz
# 检查 ok 和 protocol；Wrapper 激活后再核对对应设备连接。
```

明文模式改用 `curl http://your-public-ip/healthz`。

### 5）Wrapper 的 staging 与激活

新建托管安装时，从同一冻结快照生成 Wrapper 角色包。构建主机与核验过的 `uv`
需匹配目标系统／架构；版本分别读取 `deploy/uv-version.txt` 和
`deploy/python-version.txt`。完整平台矩阵见 [发布工作流](../.github/workflows/release.yml)。
以 macOS arm64 为例，替换所有路径与 Git SHA：

```bash
python3.13 deploy/build_release.py \
  --root /absolute/path/to/tested-snapshot \
  --output-dir /absolute/path/to/artifacts \
  --role wrapper --os darwin --arch arm64 \
  --uv-bin /absolute/path/to/verified/uv \
  --git-sha FULL_40_CHARACTER_COMMIT_SHA
```

在 Wrapper 主机校验并解开对应包后，用包内安装器完成私有配置、服务、不可变 release
和回滚事务。它需要上文说明的日常 Claude CLI：

```bash
bash /absolute/path/to/unpacked-wrapper/deploy/install-wrapper.sh \
  /absolute/path/to/unpacked-wrapper \
  --relay https://remote.example.com \
  --pair XXXXX-XXXXX-XXXXX-XXXXX --name "我的电脑"
```

上例以 macOS 当前桌面用户执行。Linux 直接调用包内脚本时需使用 `sudo bash`，
并传 `--user youruser` 指定运行 Wrapper 的普通用户；不要把 `youruser` 原样照抄。

已配对安装升级时省略 `--relay`／`--pair`／`--name`。既有自定义拓扑不能被首次安装
模板覆盖。手工维护的不可变 Wrapper 可先用 `deploy/prepare_wrapper_stage.py` 验证
并准备运行环境；它**不执行激活**。根据其 `--help` 和当前安装事务继续：保留服务用户
与外部配置，停服保存私有状态，原子切换 `current` 后验活，并保留旧 release 用于回滚。

Linux 凭据放在 root-only `/etc/cc-remote/wrapper.env` 或 `/etc/cc-remote/device.env`，
不放在源码 `.env`；macOS 使用桌面用户的安装器私有配置。自托管 Wrapper 的激活必须
由独立控制端或退出后仍能完成的一次性系统任务执行。SSH／控制连接中断只是结果未知，
先检查原事务，不能直接重复安装。

Linux Wrapper 可选安装 Office 转换环境：

```bash
sudo apt-get update
sudo apt-get install -y libreoffice bubblewrap
```

这用于 DOCX/PPTX 等需要转换的格式；**XLSX 预览不需要这两个包**，macOS 也可用。
Relay 不需要安装转换器。DSH 原生进程、patch 和配对文件与 cc-remote 发布独立管理，
见 [DSH 接入](../integrations/dsh/README.md)。

#### 用设备中心配对 Mac / Linux（推荐）

安装器已负责配对和启动的机器可跳过本节。以下用于手工管理的源码安装；
不要在同一设备上重复启动另一个 Wrapper。

登录网页，点顶部的设备图标，选择“允许添加设备”。页面会生成一个只使用一次、
默认 10 分钟过期的配对码和命令。在新机器的 cc-remote 仓库中执行：

```bash
.venv/bin/python -m cc_remote.device pair https://your-domain.com XXXXX-XXXXX-XXXXX-XXXXX \
  --name "我的电脑"
.venv/bin/python -m cc_remote.wrapper
```

交互运行时，凭据会以 `0600` 保存到 `~/.cc-remote/device.json`。Linux systemd
部署建议直接写入 root-only EnvironmentFile，再重启服务：

```bash
sudo .venv/bin/python -m cc_remote.device pair \
  https://your-domain.com XXXXX-XXXXX-XXXXX-XXXXX \
  --name "我的服务器" --env-file /etc/cc-remote/device.env
sudo systemctl restart cc-remote-wrapper
```

Relay 只保存设备凭据的哈希；配对成功后明文凭据不会再次显示。设备中心可查看
在线/离线状态、切换机器、重命名或单独撤销设备。旧的 `WRAPPER_TOKEN` /
`WRAPPER_TOKENS_JSON` 手工配置方式仍保持兼容。

### 6）上线验收

先完成 [部署验收](../deploy/README.md)：协议与构建身份、稳定的进程和重启次数、
公网健康、所有预期 Wrapper、近期错误日志，以及每个 Codex 账号的共享控制。
可选 App 接入单独记录结果。再在手机验证交互：

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
