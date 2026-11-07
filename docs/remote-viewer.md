# 远程 Viewer 预览

首版以纯静态、多文件的 Three.js／STL 查看器为兼容基线：HTML、ES modules、
CSS、静态图片／字体和 fetch 二进制资源。页面脚本在用户浏览器里运行；cc-remote 不运行 Chrome，
不调用模型，也不代理任意内网地址。带后台 API、WebSocket、登录和服务端写操作的
动态应用不在此版本范围内。

与原有单文件 HTML 预览不同，Wrapper 从经核验的页面目录提供原始文件，保留模块
相对路径和上级资源路径。因此不需要临时 HTTP 服务器保持运行，但文件所在设备
及其 Wrapper 必须在线。手机的 WebGL/内存性能仍会影响复杂模型的流畅度。

“查看页面”支持触控板／触屏双指缩放。顶部百分比菜单提供放大、缩小、适应宽度、
恢复 100%，范围为 10%–400%。首次打开保持页面原始比例；选择适应宽度后，调整面板
尺寸会重新适配。缩放只作用于预览文档，保留页面状态、原生滚动和控件点击坐标，
不会刷新 iframe 或缩放聊天界面。页面自己已取消默认行为的手势（例如 Three.js
控制器）仍由页面处理，可用顶部菜单调整整个页面。

## 1. 默认 Bridge 模式：沿用主站地址

默认使用 `bridge`，不需要新增域名、证书或端口。先按 `deploy/README.md` 的自动化合同
准备、验证并协调发布同一份 protocol v55 的 Relay/Web/Wrapper，更新代理规则，
按需发现页面。家目录免逐项目登记不等于扫描或列出整个家目录。不要把下面的示例
域名或路径当成操作者的真实机器清单，也不要在仓库中记录实际清单或密钥。

- HTTPS、HTTP 域名及裸 IP 均复用主站原有的访问许可。不会自动开启
  `ALLOW_INSECURE_HTTP` / `ALLOW_PRIVATE_ORIGINS`；HTTP 仍是明文，不提供链路安全。
- `VIEWER_MODE=off|bridge|isolated`。不设置模式时，没有模板即选 Bridge；已有显式
  `VIEWER_ORIGIN_TEMPLATE` 时保留 Isolated，不会静默改变认证模式。
- 主站 Cookie 名保持不变。主页面用 Cookie 连接 `/ws/viewer-client`；设备用独立的
  Authorization 连接 `/ws/viewer`。原有全站 WebSocket 代理涵盖两者。
- 页面运行在 opaque-origin iframe，只有 `allow-scripts`，不能读取主站 DOM、
  Cookie 或存储。资源从实例专属 MessagePort 进入；不使用第三方 Cookie、
  Service Worker、base64，也不把用户 HTML／JS 直接托管到主站同源。
- `/__cc_viewer/bridge/*` 必须保留 Relay 提供的 **HTTP** CSP sandbox 与实例脚本
  allowlist，不能被主站 CSP 或 `X-Frame-Options: DENY` 覆盖。仓库的 HTTP／HTTPS
  Caddy 模板已包含这个精确例外；主站和原 HTML runner 的策略不放宽。
- `web/dist/cc-remote-viewer-runner.js` 是独立构建的可信启动器，必须随同一份 Web
  构建发布。运行时解析器不进入聊天首屏包。runner／资源不能进 PWA shell 缓存。
  固定文件名的启动器与主站 shell 使用文件内容摘要作为 ETag，必须向 Relay
  重新校验；不能仅按文件大小或修改时间判断版本，发布包会统一文件时间。
  启动器与主页面版本不一致时会立即提示刷新 cc-remote 页面。
- Isolated 使用同一次构建的 `cc-remote-viewer-zoom.js`，通过已授权预览来源下的
  `/__cc_viewer/zoom.js` 读取。辅助脚本在源页面 meta CSP 前安装，不移除页面策略；
  父页只接收当前 iframe、匹配 origin 的缩放状态，缩放消息不包含资源权限或凭据。
- 自定义 nginx／容器代理同样要保留原 Host、受信 transport 和两条 WebSocket
  路由；只代理 `/ws` 的精确 location 不够。不要给整个主站加 `unsafe-inline`。

### 可选 Isolated 模式

此模式保留原生 URL／模块加载，需管理员单独准备以下条件，不是 Bridge 的前提：

- 主站示例：`https://app.example.com`。
- 为预览准备独立的通配域名和 TLS：`https://*.preview.example.com`。
- 两者应在同一个 site（相同协议、相同可注册域名）下，以兼容 Safari 的 Cookie
  限制，但必须是不同 origin。不能用主站下的 `/preview/` 路径代替隔离来源。
- Relay 的外部配置增加：

  ```dotenv
  VIEWER_MODE=isolated
  VIEWER_ORIGIN_TEMPLATE=https://{id}.preview.example.com
  ```

- `{id}` 必须是最左侧的完整主机名标签。每个预览实例有自己的 origin；不要改成
  所有项目共用一个可执行页面来源。
- 在主站现有 CSP 的 `frame-src 'self'` 后，**只增加**
  `https://*.preview.example.com`。不放宽 `script-src` / `connect-src`，
  不修改原有 `/html-preview-runner.*` 的隔离策略。
- 通配预览站点只反向代理到 Relay，保留原始 Host，并由受信任的 loopback TLS
  代理提供正确的 HTTPS scheme。不得继承主站的 `X-Frame-Options: DENY` 或
  主站 CSP；Relay 会为预览响应设置专属 CSP、沙箱、缓存与权限策略。
- 预览主机上的任意路径都归预览路由处理，不能回退到主站页面、登录或控制 API。
- 主站还需要代理 `/ws/viewer` 的 WebSocket 升级；现有 Caddy 的全站
  `reverse_proxy` 已涵盖该路径。只代理 `/ws` 的自定义 nginx 配置需另行增加它。
- 普通 HTTP 公网部署不支持 **Isolated 模式**，应使用 Bridge。HTTP `*.localhost` 仅用于本地测试；
  跨 site 的预览域名也不保证能通过移动 Safari 的 Cookie 策略。

参考片段见 `deploy/Caddyfile.viewer.example`。DNS、证书及主站 CSP 的更新必须
纳入同一次可回滚激活事务；不要临时覆盖共享 Caddyfile。现有自动安装器不会
替操作者申请 DNS 权限或自动配置通配证书。后续升级重新生成主站 CSP 时也必须
保留这项配置；只有 Web 构建更新不代表预览已启用。

启用隔离预览时，HTTPS 主站登录 Cookie 改用 `__Host-cc_remote_session`，
防止预览脚本通过父域 Cookie 干扰主站登录。因此启用/停用该配置后需要重新登录。
直接通过允许的私网 HTTP IP 登录仍沿用原 Cookie；远程 Viewer 则从主站 HTTPS
地址打开。Bridge 安装不改变登录行为。

## 2. 家目录页面免逐项目登记

默认开启 `CC_REMOTE_VIEWER_HOME_PREVIEW=1`。助手在会话中明确引用 `~/.../index.html`、
绝对 HTML 路径或相对会话工作目录的 HTML，或通过结构化工具成功写入 HTML 后，
Wrapper 按需核验并关联这一个页面。不会扫描整个家目录、不会自动把目录里的所有页面
加到列表，也不需要每个项目点击允许。管理员可在 Wrapper 环境设为 `0` 后重启，
恢复仅手动登记模式；原有登记不受影响。

思考内容不生成页面入口。正文行内代码中只有 `index.html` 这样的文件名时，不猜测它的位置，也不自动生成
入口；带目录的路径、明确的 Markdown 链接和成功的结构化文件写入仍可触发发现。

- 文件必须位于资源设备的 OS 用户家目录下。相对路径和 `~/` 属于会话设备；跨设备
  产物应引用资源设备的绝对路径或其静态服务 URL，不能猜测 SSH 命令的当前目录。
- HTTP 私网／localhost URL 只是发现线索：资源设备验证地址确实属于自身、精确
  端口的监听 PID 属于当前 OS 用户，再读取真实 argv/cwd。当前适配 Linux/macOS
  上的 `python -m http.server`，不支持 CGI/TLS/未知启动参数。Linux 需要系统
  `ip`、`ss`；macOS 使用系统 `ifconfig`、`lsof` 和原生进程参数接口。
  不解析任意域名，不请求 URL，不把未知服务转为 HTTP 代理。
- 静态服务以其 `--directory`／cwd 为资源根；HTML 路径以最近的 `.git`、
  `package.json` 或 `pyproject.toml` 祖先项目目录为根，没有这些标记时使用 HTML
  所在目录。只做有界祖先检查，不递归搜索；服务根／项目根不能越过家目录。
- 预览脚本可读取该根目录内的受支持普通静态文件，包括 JSON、模型资源等。
  因此应保持页面资源目录干净；不要把秘密放在非隐藏的静态资源目录中。隐藏文件、
  隐藏目录、路径穿越、符号链接、硬链接、其他用户文件仍拒绝，父目录也按 FD 核验。
  自动页面可使用现有固定 CDN allowlist（esm.sh、jsDelivr、unpkg），不放行任意网络。
- 私有自动登记保存在 Wrapper 状态目录 `viewer-home-pages.json`，最多 128 个
  资源根／1 MiB，不出现在全局登记清单。会话关联刷新后仍在；已发现页面不要求原
  HTTP 服务持续运行，但 Wrapper 和文件必须可用。未知服务、非本机地址或多个设备
  同时匹配时保留普通链接，不凭 IP/端口猜测。

### 可选：在资源设备手动登记其他范围

家目录外、未知静态启动器或需要更窄资源范围时，可保留以下明确登记方式。

以运行 Wrapper 的 OS 用户执行，使用该安装的 Python。路径仅为占位示例：

```bash
python -m cc_remote.viewer register robot \
  --label '机器人结构' \
  --root /absolute/path/to/project \
  --entry /viewer/index.html \
  --path /viewer/ \
  --path /description/robot/meshes/visual/ \
  --script-origin https://esm.sh
```

此命令只登记读取范围，不启动服务、不改模型配置、不复制资源、不删除文件。
默认注册表为 `~/.cc-remote/viewers.json`，必须属于当前用户且权限为 `0600`。
可用 `CC_REMOTE_VIEWERS_FILE` 指定另一个私有注册表；不要放进可被页面下载的目录。
Wrapper 在后台热加载，无需为每个 Viewer 重启主对话进程。

- `--root` 是 URL `/` 对应的项目根目录，不能是系统根目录或用户家目录。
- `--entry` 是包含文件名的绝对 URL 路径。
- `--path` 是允许读取的具体文件，或以 `/` 结尾的目录前缀；不能填写 `/`。
  例如脚本引用 `../description/.../part.stl`，需单独登记该资源目录，不能只登记
  `viewer/`。未登记文件和不支持的文件类型不会自动放行。
- 只登记需要展示的页面与资源目录，不登记配置、凭据、会话记录或其他私有资料。
  Viewer 内的脚本能够读取其已登记资源；主站隔离并不会把这些资源对页面隐藏。
- 所有资源必须是同一 OS 用户拥有的普通文件。目录/文件符号链接和硬链接被拒绝；
  整个项目根目录被替换后需要重新登记。手动登记仍不允许直接发布整个家目录。
- 按实际页面需求增加 `--script-origin`，目前支持 `https://esm.sh`、
  `https://cdn.jsdelivr.net`、`https://unpkg.com`；不填写时脚本只能来自本地目录。
  建议使用固定版本依赖。CDN 不可达时，将依赖归档到已登记目录，修改页面引用；
  系统不会盲目改写任意 JavaScript，也不会代理未知 CDN。
- 可附加 `--url http://localhost:9000/viewer/index.html` 作为旧链接别名。
  它只用于界面匹配，**不会请求该 URL 的主机或端口**。同一地址对应多个设备时
  由用户选择，不猜测当前聊天的机器就是 Viewer 的机器。

```bash
python -m cc_remote.viewer list
python -m cc_remote.viewer remove robot
```

移除只撤销登记，不删除原始文件。已打开的预览后续请求也会被拒绝。

## 3. 使用与生命周期

会话右上角的“更多设置 → 远程预览”默认只显示**本会话**关联的页面。要使用别的
已登记 Viewer，点击“关联已有预览”选择资源设备与页面；关闭面板不移除关联，
列表中的移除按钮只移除关联，不删除文件。旧版已经选中的面板会迁移为一次显式关联。

助手明确引用本地 HTML，或成功通过结构化文件工具写入 HTML 后，系统按照家目录
策略或已有手工登记核验这个文件，不扫描目录、不运行服务。核验成功后，
正文旁显示无底框的轻量“查看页面”入口，位于“已处理”折叠区域之外。
流式正文出现明确路径后即核验；未找到文件或临时失败时最多追加三次有界重试，
回复结束会为未确认的路径再核验，包含结束时旧请求仍在进行的情况。不依赖切换会话，
但入口仍需等待设备核验，不能保证与正文同一帧出现；未确认的路径不显示假入口。
不同入口 HTML 可以
共用一个已登记资源范围，无需逐页登记。通过 shell/SSH 生成的文件若没有结构化写入
事件，需要回复里明确引用文件路径，或使用“关联已有预览”。旧历史中的明确引用会
在查看时补建关联，不会为了发现页面而加载整份历史或启动模型。

相对路径只按服务端已知的会话工作目录解析；绝对路径由资源设备验证。聊天所在设备
不等于资源所在设备。同一路径在多个登记范围或设备中存在时不猜测，可手动关联。
`https://github.com/...`、GitHub Pages、普通网站，以及未能核验归属的 localhost/LAN URL
保持普通链接；`.html` 后缀不是本地资源证明，不会因此生成“查看页面”。消息中的本地 Web 链接
在当前会话已有核验关联、或当前手工登记列表匹配其别名时才进入预览；唯一匹配直接打开，
多个手工匹配需选择设备。未确认归属、功能关闭、列表读取中／失败时均保留普通链接行为，
不会转发到任意内网。预览面板始终保留“打开原链接”，供资源失效或加载失败时使用。
按住 Cmd/Ctrl 点击、中键打开仍保留原链接的浏览器行为。

桌面使用已有右侧面板，支持拖宽、放大、刷新、关闭；手机全屏显示并提供返回对话。
不要求先切换到“交互模式”。原页面的旋转、缩放、零件选择能力由原页面提供。

居中放大时，Tab 焦点保留在预览面板内；页面未处理的 Esc 会收回右侧。
Bridge 启动器负责通知父页面；Isolated 模式仅在浏览器导航到 iframe 的 HTML
响应开头的 BOM、注释和 doctype 之后补入键盘通知脚本，确保在页面自己的 meta CSP
生效前安装监听。保留页面的 CSP 限制和文档模式；原始资源读取、Range 和缓存校验仍沿用原字节内容。
导航后的 HTML 单独计算长度，不复用原文件的 ETag；源文件不会被修改。
父页面只接受当前放大面板内、正在获得焦点的 iframe 的通知，并核对来源，
不授予预览读取主站 DOM 或调用会话控制接口的权限。

关联记录保存在**会话所属设备**的私有状态目录 `viewer-pages.json`，按设备、Work/Code、
引擎和完整账号会话标识隔离。换浏览器或手机仍可读取同一会话列表；资源设备离线时
保留不可用条目。相同资源设备、登记 ID 和 HTML 路径复用同一条记录。新文件内容不会
自动重建已经打开的 iframe，检测到入口更新后提示手动刷新；资源版本仍在读取时验证。
临时会话捕获正式 ID 时迁移关联；Claude／Codex 账号 ID 变更或单／多账号切换时，
按账号身份原子迁移会话关联与撤销标记，迁移版本与记录一同保存，重启不会重复交换
账号归属。迁移失败时保留恢复标记，仅暂停受影响引擎的 Viewer 关联读写，不停用聊天。
未驻留的 Claude 历史会话从所属账号目录读取工作目录，不启动模型。
删除会话时清理其关联；fork 不复制整份列表，但其
历史中仍明确引用的页面可以重新核验关联。手动移除保留有界的撤销标记，避免刷新历史
立刻将同一页面加回来；再次显式关联可恢复。每会话最多 32 条含撤销标记的记录，
单会话元数据不超过 48 KiB，整个状态文件最多 4 MiB / 512 个会话。

面板打开状态另按设备、Work/Code、引擎、父会话隔离。刷新时只恢复所选页面的描述符，
重新认证和创建预览实例；不持久化 Cookie 或访问凭据。同一设备内切换会话、Work/Code
或 Claude/Codex 时隐藏原 frame，最多保留最近访问的三个实例；返回时保留页面内部状态，
不移动 iframe 的 DOM 节点或重新下载资源。隐藏面板不接收焦点或快捷键，授权仍按原会话续租。
超过三个时释放最久未访问的实例。关闭、切换设备或退出登录释放实例；刷新预览重新认证。
关闭不会停止设备、
终止后台模型或删除文件。

## 4. 安全与资源边界

Bridge 使用 HTML／CSS／JS 解析器构建本地模块依赖图，支持循环 ESM、export-from、
字面量动态 import、精确及最长前缀 import map、`import.meta.url`、相对 URL／Request、资源查询串、
CSS url／import 和 srcset。只在沙箱内生成 Blob，不改项目文件、不运行构建脚本。
非字面量动态 import、scoped import map、XHR／Worker、动态纹理、复杂 WASM
加载器、存储、多页导航、页面内下载和后台 API 不在兼容基线中。解析或策略错误会显示具体原因，
不会自动降低沙箱强度；glTF／WASM 文件可传输不等于任意对应 loader 已支持。

- 登录、设备权限和会话绑定在 Relay 校验。主页面定时续租，未续租的实例在
  90 秒后失效；一个实例最长一小时且不超过登录有效期。退出登录、移除设备、
  撤销登记、Wrapper 连接代际变化都会拒绝后续读取。
- 主站通过 frame 专属握手批准一次预览。地址和握手 ID 单独不构成访问凭据；
  认证材料只在 HttpOnly Cookie / Authorization 头中传输，不放进 URL 或 JSON。
- 每个设备最多 8 个并发读取，额外最多 64 个资源请求有界排队（最长 60 秒），
  避免页面并发加载几十个零件时直接丢文件；每个读取最多预取 8 个 64 KiB 块。资源数据不进入
  聊天消息队列、历史、回放环或 Relay 数据库。
- 单文件上限 256 MiB，支持单区间 Range、HEAD、ETag 校验和取消。缓存为私有且
  每次使用前重新验证，不能跨登录用缓存绕过鉴权。
- Bridge 每实例最多 4 个并发读取、64 个排队、4096 次读取、2 GiB 总传输；
  每块 64 KiB，最多 8 个消费信用。模块／样式各最多 256 个。HTML 上限 16 MiB，
  可包含非执行的 JSON／模型数据；JS／CSS 及内联脚本／样式单段仍为 2 MiB、
  解析代码文本预算 16 MiB。全部已加载与并发读取的文本共享 48 MiB 预算
  （解码文本按 UTF-16 大小计量，不代表浏览器进程总内存上限），Blob 缓存 64 MiB。
  超限提示包含资源路径、实际大小和上限；读取失败则显示 HTTP 状态，重连不会提高上限。
  缓存只属于当前实例；启动前重验代码及
  静态资源 ETag，传输中检查文件变化。不能保证正在修改的目录是原子快照。
- 新聊天消息不会重建当前 iframe。短暂断线保留画面并提示重连；权限撤销清理
  iframe。重连创建新实例，不承诺保留原页面内部状态。
- Isolated 代理应为长资源响应设置**有界且经过评估**的写入超时。现有部署的全局 30 秒写
  超时可能截断慢网络的大模型传输；不要关闭所有超时。保持 header/body 读取
  超时，必要时使用隔离的预览监听配置或缩小模型后，再做外网验收。
  Bridge 二进制走 WebSocket，不受这个 HTTP 响应总时长影响，但仍有单块空闲、
  连接、资源总时长和实例有效期限制。

## 5. 验收

执行 `AGENTS.md` 的完整门禁，再执行：

```bash
.venv/bin/python -m pytest tests/test_viewer.py tests/test_viewer_home.py
npm --prefix web run test:viewer
```

浏览器测试使用真实本地 Relay/Wrapper 二进制通道，验证模块、模型数据、WebGL、
Cookie 握手、主站隔离、刷新和手机布局，不启动 Claude/Codex、不消耗模型 Token。

验证 HTTP 裸 IP 时，可使用本机实际私网 IP（以下值均为占位符），而非 localhost
的安全上下文特例：

```bash
VIEWER_TEST_HOST=0.0.0.0 \
VIEWER_TEST_ORIGIN=http://YOUR_PRIVATE_IP:4174 \
npm --prefix web run test:viewer
```

真实 Three.js/STL 验收使用脱离原目录的只读源副本，通过 `VIEWER_TEST_SOURCE`
指定，`VIEWER_TEST_PATHS` 指定 JSON 路径列表，再运行 `test:viewer -- --grep 'real Three'`。
该 opt-in 用例针对本次 43 零件 Viewer 的 DOM／交互契约，不是任意页面的自动验收。
默认 CI 不携带私人源文件。测试目录和固定测试口令仅供受控测试网络，不能部署为服务。
已验证与未验证的边界见 [设计验收记录](remote-viewer-design.md#7-实现验收记录与尚未证明的部分)。

上线前另行验证：真实 Viewer 的全部资源、移动网络访问、iPhone Safari/WebGL、
大文件与聊天并行、断网重连、源文件更新、撤销后访问失败，以及不同设备的同名
`localhost` 地址不串号。模拟器测试不代替物理手机和真实 DNS/TLS 验收。


## Native video file previews (protocol v86)

Explicitly opening an MP4/M4V file uses the existing file authorization boundary
and returns only a short-lived opaque media ID in `FilePreview`. It is not a
publication, does not appear in the page catalog, and cannot open a private URL.
The main application renders a native `<video controls playsinline>` player;
it reads `/api/media/{machine_id}/{media_id}` with the existing HttpOnly login
cookie. IDs are locators, not credentials: every request requires an active
login, device access and the descriptor's authenticated owner. No secret or
filesystem path appears in the URL. Cross-origin browser requests are rejected.

GET/HEAD and single HTTP byte ranges (including suffix ranges and 416) travel
through the existing bounded, pull-driven `/ws/viewer` Wrapper channel. The
Relay streams chunks without persisting or buffering the whole video. Seeking
and closing cancel obsolete reads. Login/device revocation or Wrapper replacement
ends active streams. Wrapper descriptors expire after one hour, are bounded in
number, hold no idle FDs, and are removed on session deletion or identity change. Each open repeats
the file permission checks; each chunk verifies descriptor validity and file
identity. Modified files must be reopened. The initial limit is 256 MiB, matching
the Viewer transport limit. Browser codec support still applies (H.264/AAC MP4 is
recommended); unsupported media offers download rather than server transcoding.

Static HTML previews also accept MP3/M4A/AAC/WAV/OGG/Opus/FLAC and
MP4/M4V/WebM/OGV resources within their existing publication boundaries.
Bridge renders the page and runs scripts without waiting for media downloads.
For progressive MP4/M4V/M4A with browser-supported MSE codecs, MP4Box remuxes
authorized 256 KiB ranges into MediaSource fragments inside the opaque frame.
Metadata reads skip media payloads when the index is at the end of the file;
each range validates the original ETag and length. Seeking cancels obsolete
reads, and buffering stops roughly 15 seconds ahead of playback. Consumed native
buffers are evicted; JavaScript data pending segmentation is limited to 16 MiB
per player and metadata reads to 8 MiB. A frame can register up to 256 declarative
players, with at most eight resident players. Galleries using `preload="none"`
load visible controls instead of fetching every sample; explicit `play()` also
activates off-screen media. Paused players outside the viewport are retired as
needed, preserving their playback position. Explicit playback can retire a
paused visible player, but never interrupts an already playing/pending player.
WebKit attaches MediaSource directly via `srcObject`, avoiding its rejection of
opaque-origin MediaSource blob URLs. Other containers/codecs and browsers use
the existing bounded blob fallback asynchronously; the shared 64 MiB blob budget
still applies. Retiring a player cancels its reads and releases its media blobs
and cache budget; shared image/script assets are unaffected. Video posters remain
ordinary bounded image assets.
Isolated mode uses same-preview-origin HTTP resources and byte ranges.
Playback codecs depend on the browser; no transcoding is performed.

This native-media endpoint is separate from sandboxed HTML previews: Bridge
pages retain opaque origins and their existing resource/credential restrictions.
Main-page CSP adds only `'self'` to `media-src`; Bridge runner CSP is unchanged.
Isolated previews allow media from their own preview origin, data and blob URLs.
