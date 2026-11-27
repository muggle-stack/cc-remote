# Gemini 接入

Gemini 是 CC Remote 的可选 Code 引擎。Wrapper 在会话所在设备运行
`agy acp`。这版用 Antigravity CLI 1.5.18 和 ACP v1 验证。

## 在设备上启用

1. 在运行 Wrapper 的设备上安装 Antigravity CLI。Wrapper 在 `PATH` 和
   `~/.local/bin/agy` 查找可执行文件。自定义路径可通过
   `CC_REMOTE_ANTIGRAVITY_BIN` 配置。
2. 用本机 `agy` 完成 Google 登录，再给 Wrapper 的运行环境设置
   `CC_REMOTE_ANTIGRAVITY=1`。也可以使用环境中的 `GEMINI_API_KEY`，
   或在本机配置自定义网关。
3. 更新并启动相同协议版本的 Wrapper、Relay 和 Web。在设备中心选择
   配好的设备，从右上角切换到 Gemini。

自定义网关可以在 Wrapper 主机设置 `AGY_LLM_GATEWAY_URL`、
`AGY_LLM_GATEWAY_API_KEY` 和可选的 `AGY_LLM_GATEWAY_WIRE_PROTOCOL`。
如果这些环境变量均未设置，Wrapper 会读
`~/.antigravity/settings.json` 中已启用的 `gateway.url`、
`gateway.apiKey`、`gateway.wireProtocol`，以及
`models.defaultModelId`。凭据只传给本机 agy 子进程。
在 Wrapper 上设置 `CC_REMOTE_ANTIGRAVITY=0` 会关闭该设备的 Gemini 新会话和新消息入口。
独立 Gemini 服务在 Wrapper 重新连接时接收该设置，已接收的回合和排队消息继续处理。

### 授权工作目录

在 Wrapper 的运行环境设置 `CC_REMOTE_ANTIGRAVITY_TRUSTED_WORKSPACES`，值为
JSON 数组，例如 `["/Users/me/projects/app","/Users/me/worktrees/app-fix"]`。
每项填写该会话工作目录的物理绝对路径（可在目录中运行 `pwd -P` 查看）。
新版 Wrapper 启动时读取该设置。每个目录单独授权，默认列表为空。
授权前请确认项目以及 agy 会发现的仓库级 Hook 和其他扩展可信，因为它们在
agy 自己的生命周期中运行。切换或恢复会话时使用 CC Remote 的会话入口。

登录、会话列表和新建页的模型目录使用 Wrapper 创建的私有控制目录。
打开、恢复或继续 Gemini 会话前，Wrapper 校验实际工作目录是否已授权。
移除目录授权后，Wrapper 会拒绝在该目录新建、恢复或发送消息。已发给 agy 的任务可通过「停止」中止。

## 会话中的行为

模型、权限模式、思考强度和命令由 ACP 返回。模型名称可能已包含
思考档位。只有模型上报独立强度选项时，界面才显示独立的选择器。
上下文表盘显示 ACP 上报的使用量和窗口容量。CC Remote 的 `ask_user`
直接显示问卷，通过「提交回答」确认。其他工具审批沿用 agy 的权限规则，
编辑审批显示目标路径和 agy 随请求发送的差异。
权限规则继续在本机 Antigravity 配置中管理。模型目录复用一个无提示会话，
它不会出现在侧栏，选择目录或模型时也无需新建会话库。

回复下方显示该轮 ACP 返回的输入、输出 token，桌面悬停可查看缓存和
思考用量。原生历史没有补报每轮用量。
侧栏菜单可重命名 CC Remote 中的会话；`/clear` 打开新的 Gemini 会话页。
`/extensions` 查看本机和当前项目配置的 Skills、Plugins、MCP 与 Hooks。
仍在执行的原生命令和定时任务会显示在后台任务列表。收到原生终态后，
结果记到发起它的轮次。agy 意外断开时，尚无终态的任务标记为结果未知。
后台命令结束后，agy 发来的主动回复、工具和待答请求会在同一会话中显示。
CC Remote 通过 agy 的 Stop Hook 完成自主续写、发送完成通知并继续排队。
同会话其他后台命令继续运行。Wrapper 从私有目录加载自己的 Hook。已授权工作目录
的 Hook 仍按 agy 的规则执行。如果原有 Hook 要求继续，Wrapper 等下一次
停止回调。机制已在 agy 1.5.18 验证。缺少回调时，输出静默只会显示等待提示，
需要中止未确认结束的续写时可以点「停止」。
子代理调用显示角色、参数和派发结果。当前 ACP 未提供子代理的独立生命周期。
应用设置里的「Gemini 登录」可发起 agy 提供的个人账号或企业账号验证，
验证页在运行 Wrapper 的设备打开。无图形界面的设备可运行 `agy acp --login`。
终端客户端可运行 `cc-remote-tui --engine antigravity`。

浏览器关闭后，Wrapper 中的会话仍可运行。重新连接会恢复当前状态、
队列和正在等待的交互。Wrapper 重启后可以从列表打开 agy 已保存的会话，
并恢复 CC Remote 已保存的模型、思考强度和权限模式。原模型或权限模式
无法恢复时，底部设置可重新选择，再继续发送消息。
支持 `session/resume` 的 agy 会先恢复会话和控制项，再按需读取历史。
agy 声明支持追加提示词时，CC Remote 会在新建和重开可交互会话时提供
问卷和文件链接说明。
原生进程断开时，已发出且结果不明的请求会保留待确认状态。
停止超时后，可在输入区再次点「强制停止 Gemini」。Wrapper 先尝试关闭
当前原生会话。如果共享 agy 进程也无响应，Wrapper 会在其他活跃会话都
已结束或都请求强制停止时重启它，再恢复会话。

这版适配 Code 会话、原生历史与工具流、图片和内嵌文件、补充、
排队、停止、权限及常用表单。会话列表和保存由 agy 管理。
已授权的工作目录移除后，历史仍可打开，恢复原目录并重新打开后可继续。
侧栏「隐藏会话」可在「已隐藏」分组恢复显示，agy 的原生记录仍保留。
原生历史支持近端分页。Gemini 目前支持 Code，会话历史受 agy 的 ACP 接口约束。
Work 仍等待可单独验证的权限策略。当前 agy 的终端沙箱不约束项目
生命周期 Hook；隔离目录中的 Hook 可以在 ACP 审批之外写入工作目录以外的位置。

## 独立 Gemini 服务

需要在 Wrapper 重启和升级期间保留回合时，可以显式安装独立 Gemini 服务。
以下命令以运行 Wrapper 的系统用户执行。路径和设备 ID 取自现有安装。

```sh
"$WRAPPER_RELEASE/.venv/bin/python" "$WRAPPER_RELEASE/deploy/install_gemini_service.py" \
  --state-dir "$GEMINI_SERVICE_STATE" \
  --wrapper-state-dir "$WRAPPER_STATE" \
  --machine-id "$WRAPPER_MACHINE_ID" \
  --cwd "$WRAPPER_WORKING_DIRECTORY"
```

安装器在 macOS 注册 LaunchAgent，在 Linux 注册 systemd 用户服务，验证后
写入 Wrapper 私有状态目录的 `gemini-service.json`。待现有 Gemini 回合和队列
结束后，下一次正常启动 Wrapper 时会接入独立服务。显式设置
`CC_REMOTE_ANTIGRAVITY_SERVICE_SOCKET` 时，以环境变量为准。
使用自定义二进制时，安装器接受 `--agy`。服务在接入时从运行中的 Wrapper
取得 PATH、代理、证书路径、Gemini 网关环境配置和跨会话消息开关。
PATH 等配置应用于下一次启动的 agy 进程。服务安装文件仅保存启动所需的信息，
模型认证也可以继续使用该系统用户的 agy 配置。跨会话消息仍由 Wrapper 的
`CC_REMOTE_SESSION_MESSAGES` 控制。

本机服务协议独立版本化，双方允许已知消息新增可选字段。需要用新字段改变
既有行为时，应更新本机服务协议版本。常规 Wrapper 升级会重连兼容的服务，置顶、隐藏和名称
首次从 Wrapper 的原记录导入，模型、模式和回执继续使用同一份 Wrapper 状态。
agy 原生会话仍使用原来的保存位置。需要更新独立服务本身时，用新发布目录的
Python 再运行同一命令并加 `--upgrade`。忙碌时安装器保留旧服务，空闲时才切换，
新服务自检期间暂不接收命令，启动验证失败会恢复上一个服务定义。
工作目录授权由 Wrapper 随连接转交给独立服务。已安装过实验版的设备需要先用
`--upgrade` 更新到私有协议 v3 的独立服务，才能接入新版 Wrapper。

Wrapper 重连后会恢复原回合的状态、输出与待答卡片。独立服务需要持续运行；
重启它会结束它持有的原生回合。切换 Wrapper 时会给上一条连接留出释放时间，
待连接建立后再发送命令。

[Google Antigravity ACP 文档](https://antigravity.google/docs/ide/extensions/acp/)
