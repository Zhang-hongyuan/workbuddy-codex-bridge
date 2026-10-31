# codex app-server 协议参考（v2，codex-cli 0.160.0 实测）

官方出处：OpenAI 博客《Unlocking the Codex harness》（"未来维护的首要整合方式"）、
`codex-rs/app-server/README.md`、https://learn.chatgpt.com/docs/app-server。
协议承诺**向后兼容**：旧客户端可安全连新服务端。schema 导出：`codex app-server generate-json-schema --out <dir>`。

## 桌面原生边界（codex-bridge v1.5.2）

默认 dispatch 不连接 app-server。`codex://new?mode=codex&path=<绝对目录>&prompt=<原文>` 请求选项目和预填；WorkBuddy 列出核对要点（项目/完整原文/模型/推理档），用户在 App 核对并亲手提交一次，WorkBuddy 再 `desktop-confirm` 只读核验；UI 验收由用户在 App 观察执行。公开入口没有自动提交或 writer 接管承诺。注：`mode=codex` 是未公开查询参数，本机实证可用（2026-10-02），不在官方 deep link 文档参数列表；若失效，移除该参数后官方链接仍成立（`path` / `prompt` 为官方参数）。

本机 2026-10-02：桌面 App 使用子进程 stdio；桥的 managed daemon 是另一进程。共用 state_5.sqlite 不是共用 writer。`codex://threads/<id>` 只导航，`open` 退出 0 只表示系统接受请求。

`desktop-confirm` 以准备前快照排除旧任务，完整匹配 cwd/原文，读取首轮 turn_context 校验模型。`created` 表示已确认启动过；界面出现、过程更新和用户可干预必须由用户在 App 另外实际观察。模型下拉选择、首次任务提交及干预均由桌面 App 持有（用户本人操作）。

desktop dispatch 返回 `notification_status`：`requested` 仅表示 osascript 返回 0、接受提醒请求；`failed` 表示异常或非零退出（同时 stderr WARN，不阻塞已准备的草稿）；`skipped` 表示 prepare-only 未请求。通知状态不证明横幅/声音已投递或任务已预填，失败不应触发重复派单。

## watch 只读契约（v1.5.2）

- 查找 `CODEX_BRIDGE_CODEX_HOME`（默认 `~/.codex`）下 sessions 与 archived_sessions，以 session 元数据核对会话 ID；规范 UUID 前缀至少 8 位，多候选报歧义。
- 回放只到当前 EOF，不因历史 completed 结束；EOF 仍有未换行的半条记录时输出 unknown / `partial_record`，退出 3，锚点停在上一条完整记录末尾。follow 显式 `--turn-id` 固定目标；未给时先用锚点目标，无锚点目标时首次完整历史扫描后固定最后 task_started；无历史 start 时等首次新 start，之后不跟随新 turn。
- `--timeout` / `--idle-timeout` / `--interval` / `--wait-file` 为正有限秒数，默认依次 1800 / 900 / 1.2 / 20；总期限从调用开始含等待文件，follow 停更达到 idle-timeout 返回 unknown。
- 只处理换行完成的 JSONL 记录；坏 JSON、未知 payload 与无法还原的命令保留标记摘要，不推断完整命令。完成判断使用目标 turn 的结构化终态，不能从摘要文案判断。
- 输出 OFFSET、ANCHOR、WATCH_STATUS、STOP_REASON。ANCHOR 的 schema 为 `codex-watch-anchor-v1`，含 thread_id / device / inode / offset / sha256 / turn_id。推荐 `--anchor '<上次锚点 JSON>'` 自动恢复；`--since` 为非负整数，大于 0 必须带匹配锚点。锚点核对会话、文件身份、完整记录前缀摘要和目标 turn；在受支持会话目录内同 inode rename 可继续。替换/截断或身份无法继续核验时输出 unknown / `source_changed`、`ANCHOR=null`，不可用失效锚点续播；无已核验文件时也可能无锚点。
- 退出 0 = 目标 completed 或回放完整读到 EOF；2 = CLI 语法/参数范围错误或目标 failed/aborted；3 = 总期限、停更/文件等待上限、源文件改变或回放末尾半条记录后的 unknown；1 = 身份/读取/锚点错误；130 = 人工中断并输出当前锚点。日志摘要不替代真实 UI 验收。

## 后台传输与握手

**两种 transport，协议载荷相同（JSON-RPC 2.0），帧格式不同（2026-10-02 实证）：**

| transport | 形态 | 帧格式 | 用途 |
|---|---|---|---|
| stdio | spawn `codex app-server` 子进程 | **JSONL**（换行分隔） | 回退档，零依赖 |
| daemon（managed） | 直连 control socket `~/.codex/app-server-control/app-server-control.sock` | **WebSocket-over-UDS**：HTTP Upgrade `GET /rpc` → 101 → ws text 帧载 JSON-RPC（客户端帧必须 mask，服务端不 mask；ping 要回 pong） | 显式后台档：异步/急停/进行中纠偏 |

- 握手（两种 transport 相同）：`initialize {clientInfo:{name,version}}` → 返回 `userAgent`+`codexHome`；随后发 `initialized` 通知。
- **proxy 弃用**：`codex app-server proxy` 是 stdio↔UDS 裸字节 relay，不做 JSONL→WS 转换（官方 issue 同症状 Closed as not planned）。
- 本机 UDS 无需 pairing/enrollment（`remote_control_enrollments` 属 remote-control 设备层）；`remoteControlEnabled=false` 不影响本地业务通道。
- daemon 管理：`codex app-server daemon start/stop/restart/version`（幂等 start；settings：`~/.codex/app-server-daemon/settings.json`）。
- websocket 注意：README 标的 "websocket experimental 不可用" 指裸 stdio 模式开 ws 监听；managed daemon 的 control socket 本身就是 WS-over-UDS，两码事。

## 后台模式用到的方法

| 方法 | 关键参数 | 用途 |
|---|---|---|
| `thread/start` | `ephemeral`, `model`, `cwd` | 建会话。ephemeral=true 内存态不落盘 |
| `turn/start` | `threadId`, `model`, `input[{type:text,text}]`, `effort?` | 派任务/同会话追加（follow-up turn 合法） |
| `thread/read` | `threadId`, `includeTurns` | 读会话。**读对话必须 includeTurns:true** |
| `thread/list` | `limit`, `cwd?`, `archived?`, `sortKey?` | 列最近会话（支持 cwd 过滤） |
| `thread/resume` | `threadId`, `model?` | 把历史会话加载进当前进程（turn/start 前置，QA 实测坑） |
| `model/list` | — | 账号白名单清单（最终裁决权在服务端） |

## 流式事件（turn 进行中推送）

- `item/agentMessage/delta` — agent 回复增量（`params.delta` 拼全量）
- `item/started` / `item/completed` — 过程项（`params.item.type`：userMessage/reasoning/commandExecution/...）
- `turn/completed` — 终态：`params.turn.status` = completed / failed / interrupted；`durationMs`；failed 时 `turn.error` 带详情

## 未用但重要的方法

- `turn/steer {threadId, expectedTurnId, input}` — 进行中 turn 纠偏（需同进程/daemon）
- `turn/interrupt` — 急停（同上）
- `review/start` — 代码审查专项
- `thread/loaded/list` — 当前进程已加载会话

## 存储（实证）

- 会话 jsonl：`~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<thread_id>.jsonl`（session_meta 含 originator/cli_version）
- 索引：`~/.codex/state_5.sqlite → threads` 表（id/title/cwd/recency_at/archived/...）
- cwd 是 App 项目锚点，默认恒为 `<协作项目目录（CODEX_BRIDGE_CWD 配置）>`（委派单统一货架）；仅用户当单明确指定其他项目时覆盖。审计/修改的目标目录以绝对路径写进任务书，与 cwd 独立；App 项目归属仍需在界面核验，数据库 project_id 可能为空。
- deep link：`codex://threads/<thread_id>` 请求导航；不能从返回码推断列表更新、过程可见或 writer 转移。

## 版本锚点

- 0.159.1：`Added GPT-6.1 Sol as the default model in the bundled catalog`（gpt-6.1-sol 最低版本）
- 0.160.0：本桥定型版本；含 catalog authoritative 修正（#49135/#49763）
- 账号白名单实测（0.160.0，ChatGPT 账号）：gpt-6.1-sol(default) / gpt-6-astra / gpt-6-sol / gpt-6-luna / gpt-5.6-sol / gpt-5.6-terra / gpt-5.6-luna / gpt-5.5
