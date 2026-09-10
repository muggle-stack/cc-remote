# 更新记录

[English](CHANGELOG.md)

## 未发布

- 按 Codex Code 主会话设置最大可用上下文（protocol v60）：300k 表示窗口为
  300,000 tokens，接近 95% 时按原生限制自动压缩。保留旧配置中用户填写的数值，
  分别显示生效容量与压缩点；从当前账号模型目录读取上限，调低窗口也仅在空闲且
  没有其他客户端保留订阅时重新加载生效。
- 新增会话文件入口和 `/open [路径]`：按页浏览目录、查看隐藏文件、复用文件预览侧栏，并可返回原目录。

- 音频链接可在桌面和手机文件面板直接播放（protocol v58），支持原生播放与进度
  控制、调速和原文件下载；关闭或切换文件时停止播放。保留现有 8 MiB 上限、精确
  文件授权和定向传输，仅在应用 CSP 中允许媒体 Blob URL，不扩大脚本或网络权限。
- 仅优化 Codex 历史读取性能，不改变协议和界面行为：浏览空闲会话后最多预读
  下一页摘要，仅在 rollout 指纹完全一致时复用有界缓存，并合并旧轮详情的原生
  游标定位请求。正在运行的最新页仍实时读取；源记录变化或失效后重新读取，
  保留账号隔离以及未命中时原有的错误处理。
- 修复发送或引导消息后历史突然只剩最新一轮的问题：补齐消息 ID 映射不再清空
  已显示内容或重置阅读位置，过期分页请求不会关闭当前历史阅读窗口。后台摘要
  刷新与主动历史请求使用同一分页来源，避免“加载更早历史”游标失配；真正回滚
  或重启时仍保留原有失效保护。
- 修复空闲与历史会话的每轮改动列表缺失，补齐 Codex 官方分页、原生 rollout 回退
  和完整历史缓存读取链路。兼容旧版 patch 与新版 `FileChange` 记录，不读取当前
  工作区、不要求展开工具详情。文件列表增加低饱和类型标识，突出文件名并缩短重复
  目录，同时保留完整路径身份。
- 每轮文件清单改为每页 64 个，支持“加载更多”、失败重试及准确总数；在工具卡片
  截短前保存原生文件索引，差异按文件独立存储、按需读取。切换会话或版本时丢弃旧
  分页响应，后续修改不影响已完成回合的版本。文件索引 4096 项及原生差异体积的
  资源保护上限仍明确提示，不会显示无效的加载入口。
- 补齐 Codex 实时与历史 diff 的截断标记，不再将部分差异
  当作完整记录；仅工具输出文字被截短时仍保留完整原生 diff。重建受影响的派生缓存，
  重新核验旧版逐工具归档，不删除原始会话、图片或不可变归档记录。
- 新增按会话、回合保存的不可变文件差异版本（protocol v57），由 Wrapper 的私有
  数据库保存，不依赖历史缓存。每轮文件列表默认折叠，同文件多次修改按完整原生
  证据合并；缺失或截断的历史明确提示，不以当前工作区替代。发送消息时保留阅读
  位置，长代码块复制按钮跟随可见区域，移除处理中空白页脚。修复 Claude 任务终止
  归属，并在实时与历史界面显示原生模型回退提示。为系统消息为空的 400 错误补充
  说明，不改写原生历史、不自动重试。
- 修复主目录页面发现（protocol v55）：明确引用的 HTML，以及经核验由当前用户
  运行的 Python 静态服务器链接，无需逐项目登记即可成为私有的会话预览。保留手动
  发布、云端链接、不跟随符号链接的文件检查和独立二进制通道；不会扫描主目录，
  也不会把自动发现的页面加入全局目录。支持本地 Three.js 扩展所用的 import map
  精确匹配与最长前缀匹配。
- 新增会话级页面关联（protocol v54），由主会话所属的 Wrapper 持久保存。根据
  已登记的源资源发布核验明确引用或写入的 HTML，在过程详情之外提供紧凑的
  “查看页面”入口；全局目录作为需要主动打开的辅助关联选择器。云端链接保持原生
  行为，保留资源来源设备身份，不扩大可访问路径范围。
- 新增已登记静态资源的远程 Viewer 预览（protocol v53）：默认使用不透明来源的
  Bridge iframe，复用 HTTPS 或明确允许的 HTTP/IP 来源，无需额外 DNS/TLS；
  可选 Isolated 模式为每个预览保留独立来源。资源在所属设备上明确登记，通过独立、
  按需拉取的二进制通道传输，支持桌面侧栏和移动端全屏查看。目录穿越、符号链接和
  未登记资源均拒绝访问。不引入浏览器引擎、模型调用或任意局域网 HTTP 代理。
  详见 `docs/remote-viewer.md`。
- Wrapper、Relay 与 Web 的协同 gate 升级到 protocol v52。Codex 原生异步提问的
  有界结构化元数据会保留在实时事件和历史中，显示为不阻塞任务的内联表单。回答复用
  会话作用域内的 query/steer 发件箱，不伪装成审批，也不把运行中的回合标成完成。
  旧版 Codex 历史投影会重新构建。
- BTW 面板的打开状态按设备、工作空间、引擎和主会话隔离，切换会话时不再留下
  无关的空面板；后台侧聊继续保留。旧的无作用域显示开关不再恢复，但不会删除任何
  侧聊。支持安全的 Markdown details/summary 折叠内容，并修复 Mac 原生拖选滚动
  在延迟布局更新时被拉回的问题。
- Wrapper、Relay 与 Web 的协同 gate 升级到 protocol v51。`/btw` 现在是常驻
  侧边对话工作区：快捷键只收起面板而不销毁会话，每条主会话可同时保留多个可单独
  关闭的侧聊；经 Relay 认证的 owner 权威目录与有界 ring replay 会在刷新、
  重连以及同一账号的其他标签页或设备上恢复这些侧聊；页面级连接 ID 保证
  复制出的标签页互不顶替。Codex 侧聊也会走 Codex 展示路径，隐藏成功的
  hook 管线事件，仅保留需要处理的失败。新侧聊默认使用 `xhigh` 思考强度
  （按所选模型能力自动夹取）；移动端输入法打开时，紧凑 Goal/计划条会临时让出
  输入空间，并在输入法关闭后原样恢复。
- Wrapper、Relay 与 Web 的协同 gate 升级到 protocol v49，并将实验性的 Codex
  托管浏览器 / Computer Use 完整拆到独立功能分支。Claude runtime 主线不再携带
  Playwright、浏览器控制帧、动态浏览器工具和 `/browser` UI；图片、PDF、GIF、
  SVG、Markdown 与 HTML artifact 预览继续保留。
- Wrapper、Relay 与 Web 的协同 gate 升级到 protocol v48。Codex ChatGPT
  账号现在可在现有脱敏状态界面查看获赠的额度重置券，并通过官方 app-server
  接口使用。兑换只允许在空闲会话进行，必须二次确认，结果仅回给请求端；可靠命令
  ID 同时作为原生幂等键。付费 credits 余额和消费控制仍不会离开本机。
- Wrapper、Relay 与 Web 的协同 gate 升级到 protocol v47。Remote 托管的 Claude
  会话默认不再覆盖自动压缩窗口，由 Claude Code 根据所选模型、账号、网关和原生
  设置决定；v3 控制记录中由 cc-remote 短期强制写入的 500K 默认会迁回原生行为。
  显式降低窗口时仍会先执行原生压缩并确认 compact boundary，再用新阈值重连；
  desired/applied 状态会跨重启和 fork 持久化，不会提前套用更小窗口。延迟到达的 SDK
  标题元数据不再触发伪外部重载；真正需要重载 transcript 时也会保留已选长上下文
  模型。cc-remote 不再拦截或改写 Claude 原生的图片/PDF `Read` 工具；模型上下文
  限制、媒体处理和自动压缩全部交还 Claude Code。
- Codex Work 的新建与已有会话现在都可选择 Fast 服务档位，同时保持 Claude Work
  使用引擎中立的命令面板。
- 经过验证的 Claude Agent SDK 固定版本升级到 `0.2.151`（内置 Claude Code
  `2.1.258`）；内置 Fable 5 与 Mythos 5 模型卡替换为官方
  `claude-fable-5-1` 和 `claude-mythos-5-1`。托管 Code 会话通过 Claude Code 原生的
  `[1m]` 上下文标记选择它们（路由到 Provider 前会移除标记）；旧会话中未带后缀的
  内置模型别名会保留原系列/版本并规范为该标记，其他真实记录的模型身份保持不变。
  Wrapper 现在会拒绝低于 `2.1.258` 的日常 Claude Code，不再在缺失本集成依赖的
  原生运行控制时静默启动。
- Wrapper、Relay 与 Web 的协同 gate 升级到 protocol v44，并新增 Claude 多账号
  Profile。每个用户自定义 Profile 独占一个明确的 `CLAUDE_CONFIG_DIR`；Code、Work、
  定时任务、模型、Skills、扩展、历史、外部进程归属和 fork 都保持账号绑定。空配置
  继续沿用原来的单账号 ID 和界面。Profile 拓扑按配置目录真实路径迁移本地归属，
  遇到歧义会 fail-closed，并与 Codex Profile 一样纳入可回滚的 Work 发布事务。
  Claude 的后台任务全量状态现在会在每次客户端 Hello 时恢复原生风格的 Bash/Agent
  监视区，同时不会把空闲会话重新标成运行中；任务完成后的续答保留真实时间分界，
  transcript 中真实的 compact boundary 也会复用现有“压缩上下文”过程标签。
- Wrapper、Relay 与 Web 的协同 gate 升级到 protocol v42。Claude 与 Codex 的
  待回答问题现在是会话权威状态，每次客户端 Hello 都会独立于 replay ring 恢复；
  超长活动回合即使淘汰开头标记，也会保留压缩后的 live 后缀，并只自动安装一页
  有界 canonical detail，更早过程继续通过现有分页显式加载；中断边界之后也不会
  发布排队中的旧问题。Claude 冷恢复时晚到的内部任务通知不再推进已完成答案的
  终态时钟，受影响的服务端与浏览器投影会一次性重建。
- Wrapper、Relay 与 Web 的协同 gate 升级到 protocol v41。Claude 上下文的自动
  读取不再发起可能阻塞的原生控制请求；用户显式执行 `/context` 时会优先读取精确
  明细，若可选控制面超时则保留并明确标注最近一次缓存或最近一轮 token 总量。
- Wrapper、Relay 与 Web 的协同 gate 升级到 protocol v40。Codex 重型回合详情
  的每个分页 cursor 现在都绑定到不可变、源文件隔离的快照；即使数百 MiB 的
  rollout 仍在持续追加，下一页也不会失效或被静默替换成另一段内容。若这个有界
  快照之后被淘汰，Web 会保留已经展开的内容并只执行一次最新页重置，不会反复
  请求旧 cursor、重复合并行或改变用户的阅读位置。
- Codex 共享 daemon 被意外替换时现在会按“不完整控制边界”处理：只重连一次，
  不重放 prompt，也不伪造回合终态；替换后的 resume 返回空思考强度时，仅在原生
  thread、模型与工作目录均未变化的前提下保留此前明确选择的强度。
- Claude Agent SDK 升级到 `0.2.142`；wrapper 继续固定到这个经过验证的精确
  patch 版本，并仍然启动用户配置的 Claude Code 可执行文件。
- Wrapper、Relay 与 Web 的协同 gate 升级到 protocol v39。Claude 子代理详情、
  脱离父回合的后台过程归属、分页回合详情、脱敏后的 Claude 额度事件及会话级自动
  压缩控制现在具有明确的兼容边界。Code、Work、BTW、新会话与 fork 可选择跟随
  Claude、自动模式或 `100K–1M` token 阈值；忙碌时的修改会等待可确认的回合终态。
  Codex steer 归属冲突也不会再把空闲会话的旧火花重新点亮。
- Wrapper、Relay 与 Web 的协同 gate 升级到 protocol v36。会话摘要现在会区分
  “确定有可展示过程”“确定为直接回复”和“原生摘要尚不能确定”；截断正文使用独立
  的详情入口，Codex 过程计时也改从首个真实可展示事件开始，不再继承用户消息时间。
- 新增 Relay 的官方容器化部署：`deploy/Dockerfile` 分阶段构建——Node 阶段从源码
  编译 `web/dist`，Python 阶段以非 root 的 `ccremote` 用户按哈希锁安装依赖；附
  `docker-compose.yml` 与 Docker 版 `env.relay.docker.example`。compose 只把端口
  发布到宿主机 loopback，TLS/WebSocket 终止仍交给现有 nginx；
  `deploy/nginx-reverse-proxy.conf.example` 记录该反代前端。Docker 构建支持
  `PIP_INDEX_URL` / `PIP_EXTRA_INDEX_URL` 构建参数，并补充大陆镜像加速指引。
- Wrapper、Relay 与 Web 的协同 gate 升级到 protocol v35。Codex app-server 的
  精确终态与通过源文件校验的 rollout 终态现在独立于 History 正文投影下发；数百
  MiB 的 rollout 即使仍在补建内容索引，也不会让已经完成的回合继续转圈。终态事实
  始终绑定账号、revision 与源文件，不会猜测“最后一个未完成回合”，也不会重复生成
  完成回执。
- Wrapper、Relay 与 Web 的协同 gate 升级到 protocol v34。主会话完成回执和精确
  Goal generation 的隐藏回执改由 wrapper 有界持久化；任一浏览器已读或隐藏后会
  同步到所有已连接浏览器，重连后仍保持一致，同时不会误隐藏后来替换的新 Goal。
- Work 消息附带的文件和图片现在会保存到当前会话私有的
  `workspace/uploads` 目录，Work 沙箱可以直接读取；上传的输入资料不会被误列为
  生成的 Artifacts，同时旧版相邻目录中的附件路径仍可兼容历史展示。
- 移动端 Markdown 源码编辑器现在会填满可用文件面板；暗色主题下的当前会话卡片
  会保持清晰选中，代码块复制按钮恢复可读对比度，桌面端代码块也会与页面背景
  保持清晰层次。
- 重连补发实时尾部前会恢复当前回合的精确归属，并将与 rollout 源文件绑定的
  浏览器/原生消息身份持久化到已完成的 Codex History；升级时会淘汰身份尚未持久化
  的旧投影，避免刷新后把同一回复重复绘制为两层。旧版 CLI rollout 中相邻的
  user 双记录现在也会复用 app-server 原生 item id，历史刷新不会再把同一次终端输入
  与其实时镜像画成两个回合。
  Remote steer 即使被官方 daemon 延迟写入并归到并行 CLI turn，也只会提取精确
  `clientId`、不会放行外部 CLI 正文；后续原生 History 行会并回原来的乐观消息，
  不再重复绘制。同一条浏览器消息的 live rollout item id 与官方 History item id
  现在可作为两个精确别名并存，不再互相冲突。初始 `turn/start` 输入现在也携带
  同一精确身份；即使超大任务的
  生命周期标记已经落到有界尾部之外，重启恢复仍可通过官方活跃状态与原生
  item/rollout 绑定重新接管，避免误报“已打断”及重复显示同一条输入。
  若有界尾部连当前 `TurnBinding` 也已淘汰，wrapper 现在会用其原始序号严格证明
  并在 Web 消费正文前绑定余下的当前回合后缀；旧版反向顺序写入的错误缓存会一次性失效。
  无实时竞态的
  官方 Codex History 明确报告线程空闲后，其持久化成功或失败终态也会替换临时的
  live 终态，同时保留实时详情，且不会削弱 Claude SDK 的终态权威。
- Wrapper、Relay 与 Web 的协同 wire gate 升级到 protocol v33，并为 Codex Work
  新增多账号支持。新 Work 会话与定时任务都可选择任一已配置 Profile，账号归属会
  独立于当前默认账号持久化，并在重试和 wrapper 重启后保持不变。即使早期账号拓扑
  迁移已经完成，升级时仍会幂等地把旧 Work 数据绑定到当时的默认账号。Profile 被
  移除后既有 Work 仍保持原归属并 fail-closed，不会被改绑；临时目录读取失败会保留
  对应账号最后一次成功的会话投影，不会触发静默回退。wrapper 发布会先快照两个 Work SQLite 注册表，
  验证账号归属迁移，并在失败回滚时先恢复匹配数据再启动旧代码。
- Wrapper、Relay 与 Web 的协同 gate 升级到 protocol v31。wrapper 内部的目录
  变化不再广播无关联会话列表，而是发送不进入重放环的失效提示；当前可见页面会
  将并发提示合并为绑定自身连接 generation 与 surface 的列表读取。流式公式可识别
  跨 delta 拆开的分隔符，暂停 Goal 恢复时会保留一次有界目标锚点，compact 续接也
  不再同时显示运行转圈和真实“已打断”终态。
- Wrapper、Relay 与 Web 的协同 wire gate 升级到 protocol v32，并新增可同时使用的
  Codex 多账号 Profile。每个 `CODEX_HOME` 独立拥有官方 daemon、目录、控制状态和
  历史命名空间；Code 统一展示并提供账号标签/筛选，Work 仍只使用默认 Profile。
  单账号保持原生 id 与原 UI；多账号卡片使用稳定的彩色 `default`/天体 ribbon。
  本地 Profile key 迁移支持崩溃续接，并覆盖 alias、fork 恢复、turn lease、控制状态、
  置顶、Work 归属与 rollback checkpoint。无界面 Profile 现在会为各自账号
  bootstrap 官方 remote-control daemon，不再静默降级到私有 stdio；只有 OAuth
  与会话数据的次账号会安全复用已校验的 managed standalone CLI 入口，同时保持
  登录、rollout、socket 和 daemon 独立，已有自定义目录不会被覆盖。账号控制面
  不可用时会明确失败，单账号的既有 fallback 语义保持不变。已登录账号若本次额度
  读取暂时没有返回窗口，也会显示为可刷新重试的读取失败，而不会再误导为缺少账号。
- Claude 问题在刷新以及 Claude/Codex 页面切换后保持同一条消息。wrapper 不再把
  Claude Code 内部生成的 `promptId` 误当作浏览器消息 id，只持久化按 turn generation
  冻结的 Agent SDK transcript 新增边界（SDK replay 作为兜底），或 broker 精确新增
  边界观察到的原生 user UUID 映射；学习这项 Claude 元数据时也不再误入 Codex
  账号缓存。升级时仅淘汰旧身份模型生成的 Claude 派生页，不清除 Codex 历史页。
  Agent SDK 回合仍在运行时，transcript EOF 会保持为开放投影，首次 ownership 扫描
  也不再重复镜像半成品历史；只有真实 `ResultMessage` 才能完成该回合。满足严格
  证据的延迟 `request_retry` 分叉不再隐藏已经成功完成的兄弟尾段；进入 resident
  会话或重试切换命令时，也会使用新序号发布当前生命周期状态，不再重播过期的
  `running` 帧。
- 新增 protocol v28 Codex 账户活动。现有的一次性状态读取会携带经过校验、限制
  为最近 53 周的每日 Token 序列；Web 提供仅 Codex 可见、仿 Desktop 的活动
  日历，且这些账户数据不会进入实时重放缓存。五档颜色按当前日历中的单日峰值
  相对计算，大数值使用“万 / 亿 / 兆”显示。
- 新增 protocol v27 Codex Code 会话目录迁移。wrapper 会让空闲会话以同一个
  原生 thread ID 在所选的现有目录恢复，保留其排队消息；若新目录恢复失败则回退
  原工作目录。所选目录可跨 wrapper 重启恢复；迁移不会派生新会话，也不会抢走
  浏览器焦点。
- 在协同的 protocol v30 gate 下，为工作目录外预览新增仅面向请求客户端的确认。
  授权同时绑定引擎、空间、会话、规范路径、文件所属 UID、设备号和 inode；文件
  身份变化后会重新确认。用户确认的文件保持只读，本会话结构化写入成功的精确文件
  才保留编辑权限。外部 Markdown 的相对图片按文档所在目录解析，但本地路径始终
  不会成为浏览器 URL。
- 在 Wrapper、Relay 与 Web 协同的 protocol v30 wire gate 下新增隔离的 Artifact
  与文件活动渲染。HTML 产物预览
  会保留文档 CSS，并提供用户显式启动的隔离交互预览；独立 SVG、Markdown SVG
  和对话 SVG 共用同一套有界安全清洗。内置工具成功读取工作目录外图片后，只向
  Remote 提供读取当刻的精确内存快照，不开放其所在目录；文件活动也按读取、创建、
  修改、删除和移动展示，不再统一使用编辑笔图标。
- 将忙碌会话的后续消息队列从浏览器内存移交给常驻 wrapper。Protocol v25
  允许排队消息和打断后的替换消息在所有 Web/PWA 客户端休眠或断线时，仍于当前
  回合结束后立即继续执行；客户端重连时会恢复 wrapper 的权威队列状态。队列标签
  只保留有界摘要，点击后私有按需读取完整指令，并可在 wrapper 中原子编辑而不丢附件。
- Wrapper、Relay 与 Web 的协同 wire gate 升级到 protocol v26，并为 Codex
  的 `$` 补全增加轻量 Skills-only 目录。缓慢的 Apps 或 MCP 枚举不再隐藏已经
  返回的 Skills；完整 Extensions 面板仍保留原生全量目录。
- 新增 Codex 官方 named permission profile 控制，并与审批策略分开管理。紧凑的
  权限面板可选择 Read Only、Workspace、Full Access 及按 cwd 生效的自定义
  profile，不增加输入框底栏控件；protocol v24 在 Wrapper、Relay 与 Web
  之间传递这些新控制项。
- 新增 Codex 会话级网页搜索模式（`cached` / `live`）；切换后会无损重连，
  wrapper 重启后仍保留，同时不修改用户的全局 `config.toml`。

- Claude Agent SDK 升级到 `0.2.128`，同时让 wrapper 显式运行用户日常使用的
  `~/.local/bin/claude`，不再静默选择 SDK 内置副本，使 Remote 与终端的凭据和
  CLI 更新保持一致。
- 在隔离的 Work 策略中保留用户的 Claude 订阅 OAuth 设置，并将内置
  `AskUserQuestion` 按原始单选/多选问题展示，不再误显示为通用工具权限审批。
- Codex 忙碌时发送默认与官方客户端一致，使用原生 `turn/steer` 引导当前任务；
  排队仍可选，停止保持为独立操作。Claude 继续使用原有打断并发送语义。
- 对配置的安全源窗口内、单个超长回合的重型过程做回合内分页，不再仅因浏览器
  256 块展示上限而用“较早过程已省略”替换本可读取的真实过程。
- shared daemon 恢复绑定期间拒绝其他 thread 的生命周期帧，并在服务端明确确认
  自动回合已不存在时安全解除假运行状态。
- Wrapper、Relay 与 Web 的协同 wire gate 升级到 protocol v22，增加可安全重放的
  用户问题关闭事件与多选回答。
- Codex 切号后会在新 daemon 上继续同一个正在运行的任务，续跑完成前不会提前发送
  queued 消息；Goal 走原生目标循环，普通回合使用隐藏的上下文续跑。若 daemon
  重启时正在运行的正是 Goal 自动回合，也会按同一规则迁移；app-server 只恢复 Goal
  状态却没有启动下一回合时，cc-remote 会自动补发隐藏续跑请求。
- 协同 wire contract 升级到 protocol v23；Codex 状态响应携带源 `request_id`，
  避免切号后延迟返回的旧账号快照覆盖新额度。
- 在上下文用量旁显示当前 Codex 账号的 5 小时和每周剩余额度，并在切号后按 daemon
  代际安全刷新。

## v3.0.0 — 2026-07-24

cc-remote v3 在原有 Claude Code + Codex 远程控制面之上新增隔离的 Cowork 风格
Work 工作台，并重新设计历史恢复、原生客户端协同、多机器路由、移动端可靠性和
发布运维。

### Code 与 Work

- 为 Claude 和 Codex 新增彼此独立的 Code / Work 双空间；会话列表、焦点、目录、
  基础提示词、权限和恢复状态均分开管理。
- 新增按引擎隔离的 Work 项目、文件/链接/笔记资料库、可复用工作模板，以及创建
  工作时物化到私有目录的上下文。
- 新增一次性、每日和每周定时任务；运行记录、租约、心跳、失败重试与防重叠状态
  持久化保存。
- 每项 Work 只能访问注册表确认属于它的私有目录；外部资料必须通过附件或项目
  资料库显式加入。
- 自动列出 Work 产生的 Artifacts，并在本机预览源码、Markdown、安全清理后的
  HTML、图片、PDF 及沙箱临时转换的 Office 文档。

### 会话、控制与扩展

- 补齐可靠的删除、重命名、归档、消息级派生、临时侧聊、排队、打断和后台会话
  控制，避免后台响应抢走当前焦点。
- 接入 Codex 原生 compact、Review 和独立 Git worktree 派生。尚未完成的 Codex
  Rollback 与 Claude Rewind 不对用户开放。
- 模型、思考强度、服务档位、协作/Plan 模式、权限、上下文、目标、状态、用量和
  限额均绑定当前会话。
- 新增真实 Skills、Plugins、Apps、MCP 和 Hooks 目录。Code 可在引擎支持时管理
  Skills、插件和 Claude Hooks；Codex Hooks 与 Work 中的全部扩展保持只读。
- 将 Claude 工具审批，以及 Codex 命令、文件修改、用户输入、通用权限和 MCP
  elicitation 请求回传给当前控制浏览器。

### 本地优先历史

- 网络校验前先绘制浏览器 IndexedDB 中最近一次验证的本地投影。
- wrapper 使用可重建的 SQLite 索引物化与源文件指纹绑定的回合摘要。
- 优先加载最新回合；工具输出、reasoning 和超长正文只在展开对应回合时按需获取。
- 向上翻页加载更早历史时保持当前阅读锚点，并在后台收敛追加中的源文件。
- 历史图片按需读取，不再嵌入每一个历史分页。

### Codex 超长会话与原生命周期

- 按回合从 rollout 尾部向前读取 Codex 历史，不把历史重新上传给模型，也不替换
  app-server 原生 resume 与 compact 状态。
- 对 Codex Desktop + OpenAI 的特定超大恢复场景，增加严格限定的官方 HTTP 传输
  兜底，处理 WebSocket 在完成前关闭的问题。
- 区分 Codex shared-daemon CLI 活动与私有 Codex App 所有权。
- 将 prompt、steer、commentary、工具、compact、abort 和 completion 绑定到权威
  turn，避免历史内容漂移到会话末尾。
- 正确镜像被打断和外部正在运行的工作，不留下错误只读锁或永久“思考中”状态。

### 设备与所有权

- 新增 Device Center、会过期的一次性配对码、哈希保存的机器凭据、重命名/撤销和
  在线状态。
- 新增可选多用户策略，把每个账号限制到明确允许的 wrapper 机器。
- 在设备发现、命令、事件和 Push 订阅上执行账号到机器的授权检查。
- 按设备、Code/Work、引擎、WebSocket generation 和会话归属隔离工作目录及延迟
  focus/rekey 事件。
- 在 Darwin/Linux 上共享精确进程身份扫描；Claude 接管只处理同一用户且精确匹配
  的进程。
- 新增按用户和机器隔离的 Web Push；旧用户迁移到不含会话信息的通用提醒。只有用户
  主动选择会话模式后，通知才携带有界显示名和经过验证的设备/空间/会话精确路由，
  始终不包含 prompt、回复、路径或工具内容。

### 移动端与 Artifact 体验

- 稳定向上分页、本地优先会话切换和有界实时尾巴补流。
- 历史图片按需加载；触屏灯箱支持点击关闭和双指缩放。
- 支持多图附件、稳定的待发送图片预览，以及跨会话和引擎切换保留各自输入草稿。
- Markdown 相对链接/图片、源码、安全 HTML、PDF 和 Office 沙箱预览均留在 wrapper
  的本地安全边界内。
- 更新 PWA 和通知资源，修复窄屏弹层、过程时间线及无法关闭的错误提示。
- 登录后的通知、主题和退出登录统一收入三点菜单；桌面使用可访问 popover，手机
  使用适配安全区和虚拟键盘的底部 Sheet。
- 让运行标志保持在排队/打断控件上方，保留 Claude 回合耗时，并将重复工具活动紧凑
  展示且不隐藏最终答复。

### 发布与运维

- Python、Codex `clientInfo`、Web package metadata 和公开构建清单统一为产品版本
  `3.0.0`。
- 严格 wire gate 升级为 protocol v20。
- 为 Linux x86_64、Linux arm64、macOS Intel 和 macOS Apple Silicon 发布可复现、
  带校验和及 GitHub artifact attestation 的 Relay/Wrapper 安装包。
- 新增校验后的角色引导程序、托管 Python 3.13 环境、macOS LaunchAgent 安装器和
  Linux Wrapper systemd 安装器；设备凭据始终放在不可变 release 与服务定义之外。
- staging 或激活 release 前同时校验产品版本和协议版本。
- VPS 使用不可变 release、release 内独立虚拟环境、原子切换、就绪检查和失败回滚。

### 升级注意事项

- v3.0.0 使用 wire protocol v20。Wrapper、Relay 和 Web 必须一起升级；混用协议
  版本会被拒绝。
- 部署后对已打开的浏览器页面执行硬刷新，使其加载 v3 哈希资源，并按 protocol v20
  重建本地投影。
- 运行密钥和机器状态必须放在 release 目录之外；不要覆盖 `.env`、`~/.cc-remote`、
  Claude transcripts 或 Codex rollouts。
- Claude 集成继续固定为 `claude-agent-sdk==0.2.119`。
- 浏览历史仍然只是本地读取，不会 resume Claude/Codex，也不会创建模型回合。
