---
name: codex-bridge
description: codex通信——WorkBuddy 给本机 Codex 派单、读取结果、收单验收与合入。默认敲锣模式必须打开 Codex App，在指定项目创建正常原生任务；WorkBuddy 向用户列出核对要点（项目/原文/模型/推理档），用户在 App 核对并亲手提交一次，WorkBuddy 再只读确认；UI 验收由用户在 App 观察执行。触发词：用codex、派给codex、codex派单、发给codex、让codex干/写/查/审、codex干活、codex通信、codex bridge、codex会话、codex任务、问codex、codex做完了、codex收单、验收codex、合入codex、codex直播、watch一下。后台 daemon/stdio 仅显式选择，不能代替前端验收。默认异步协作——用户在 App 观察过程后告知完毕，WB 按收单 workflow 对账与终审，用户最终确认后由 WorkBuddy 合入；直播仅在用户明示（codex直播/watch一下）或用户明示离场时挂。
---

# codex通信（codex-bridge v1.7.4）

## 默认敲锣的完成标准

WorkBuddy 派单默认走 **desktop 原生任务**。动手前先做第 0 步 Git 化探针（见"默认派单"段），完整派单由本 Skill 的脚本、用户在 App 的核对与亲手提交共同执行，四步缺一不可：

1. **准备并唤醒**：脚本保存回执，请求打开 `codex://new?mode=codex&path=...&prompt=...`；成功后请求一条 **macOS 浅提醒**（通知中心横幅+Tink 声，非模态不抢焦）喊用户上发射闸。返回 `notification_status`：`requested` 仅表示 osascript 接受请求，`failed` 表示请求失败，`skipped` 表示 prepare-only 未请求；均不证明通知已投递或原文已预填。
2. **用户亲手提交（发射闸）**：WorkBuddy 向用户列出核对要点（指定项目、完整任务文本、模型与推理档），用户在 App 里逐项核对后亲手点击发送一次。WorkBuddy 不代点，也无 GUI 自动化能力——核对与点击恒为用户本人动作（2026-10-03 定型）。提醒失败时仍须核验草稿，不因通知状态重派任务。
3. **身份确认**：`desktop-confirm` 只读数据库和首轮执行记录，核对新 ID、目录、原文、实际模型及推理档。成功时得到 `thread_id` 和首轮状态。
4. **UI 验收（用户在 App 执行）**：用户确认新任务及时出现在指定项目；至少两次观察显示过程确实更新；输入框可输入或有明确的原生接管入口。完成后能继续正常对话。WorkBuddy 如实记录用户报告的观察时间及证据。

**`open` 返回 0、准备命令退出 0、数据库有记录，都不等于完整派单成功。** 回执只确认启动过，不能证明前端可见、持续更新或可干预；第 4 步必须由用户实际看界面。任务过快完成而未观察到过程时，应如实记为“完成可见，持续更新未验证”。

若用户无法在 App 核对（界面异常、模型不可选）或界面状态无法确认，明确报告具体卡点和“尚未派出/尚未确认”，保留回执。禁止自动退回 daemon、代点发送、重复派单，或伪造 App 内部工具调用身份。

**能力边界（2026-10-03 定型）**：desktop 模式任务执行权恒在 Codex App。桥保证观测诚实（不假报完成、不静默、异常如实报 unknown），不保证任务本身不死：App 崩溃、Mac 休眠、模型限流均无兜底。长任务默认由用户在 App 观察；用户明示直播或明示离场时，WB 用「长任务值守」范式保持观测链（见下文）。

## 为什么更换默认路线

2026-10-02 本机源码与进程证据：桥连接 managed daemon 的 Unix socket；桌面 App 使用自己启动的 stdio app-server。两者共用磁盘记录，**不等于共用运行后端或 writer**。外部 daemon 创建任务后再打开链接，App 可能报“已在另一个应用中打开”。等待首事件只能改变导航时机，不能移交 writer。

原生新任务链接只负责选项目和预填，没有自动提交或模型参数。由 App 发起首次提交，才能沿用正常任务的 backend、writer、项目和界面更新流程。脚本不重启 App、不删锁、不改全局 backend 配置。

## 默认派单：照此完整执行

### 第 0 步：Git 化探针（2026-10-03 定型，先于此段一切命令）

**任何要交给 Codex 动手的目录，先 Git 化建基线，再起草派单。** 靠人脑记的流程等于没有流程——探针由 WorkBuddy 每次派单前自动执行，用户零负担：

```bash
git -C "<目标目录>" rev-parse --is-inside-work-tree 2>/dev/null && git -C "<目标目录>" status --short --untracked-files=all
```

| 探针结果 | 动作 |
|---|---|
| 未 Git 化 | WorkBuddy 先梳理本体/数据、完成敏感信息检查与 `.gitignore`；无风险后自动 `git init` + `git add -A --dry-run` 核验待追踪清单，再 `git add -A` + baseline commit（信息：`baseline: <目录名> 委托前基线 <YYYYMMDD>`，日期用 `date` 命令取真值），基线落稳才起草任务书 |
| 已 Git 化但工作区脏 | **HARD STOP 问用户**：先 commit 收口，还是授权以脏状态为基线（验收 diff 会混既有改动，任务包须写明） |
| 已 Git 化且干净 | 记录当前 HEAD 为基线，直接进派单 |
| 纯只读单（审查/咨询，目录不被写） | 可豁免；默认仍建议建基线，一行命令买一份对账保险 |

敏感检查或待追踪清单发现凭据、硬编码密钥或不应入库的数据时，**HARD STOP 报告位置与类型，不输出凭据值**；风险未处理前禁止 `git add` / commit，不得先提交再清理。

探针对象 = Codex 会写入的目标目录（代码任务=被改 repo 的 worktree，只读审计单=被审目录），**与 dispatch 的 `--cwd` 项目锚点相互独立**——cwd 恒为协作货架（见派单段），目标目录以绝对路径写进任务包。基线完整 commit 号写进任务包（模板见 `references/task_packet.md`），收单 `git diff <基线>..<结果>` 即天然对账清单。

### 派单命令

```bash
B=~/.workbuddy/skills/codex-bridge/scripts/codex_bridge.py
python3 "$B" dispatch --cwd "<协作项目目录（CODEX_BRIDGE_CWD 配置）>" --name "<主题>" --task "<任务文本>"
# JSON: status=open_request_sent, receipt_path, request_id, url, notification_status 等。
# 此时是新任务草稿，尚未确认提交；不要回复“已经派出”。
```

接着 WorkBuddy 向用户列出核对要点，用户在 App 逐项核对（WB 无 GUI 自动化能力，核对恒为用户本人动作）：

- 项目与回执 cwd 一致。**项目归属规则（2026-10-03 定型）**：`--cwd` 恒为 `<协作项目目录（CODEX_BRIDGE_CWD 配置）>`——委派单统一货架。Codex 项目繁多，不集中归拢则用户无法检索；仅用户当单明确指定其他项目时才允许覆盖。审计/委托的目标目录不充当 cwd，以绝对路径写进任务书。
- 预填原文逐字一致，不增删、不重复粘贴。正式任务前缀仍是 `【WB委派·yymmdd·主题】`；末尾自动附唯一 `[WB_DISPATCH_ID=...]`，用来区分相同文本的不同派单，不要删除。
- 模型选择器指向回执请求的准确模型；有 `--effort` 时选准确推理档。URL 不设置模型，不能沿用上次选择后声称已按参数执行。
- 确认没有相同回执对应的已发送任务后，用户亲手点击发送一次；WorkBuddy 随后 `desktop-confirm` 只读确认。

```bash
python3 "$B" desktop-confirm "<dispatch 返回的 receipt_path>"
# 若界面确实展示任务 ID，可加 --thread-id <ID>，仍会严格核对身份。
```

状态解释：`awaiting_desktop_submit` / `pending` 表示尚未确认，允许短暂等待后重复**确认**；不要重复提交。`mismatch` 表示实际任务和要求不符，报告候选 ID，在原任务处理；不能另起重复任务掩盖错误。多个候选时停止自动绑定，不猜最新一条。`created` 证明新任务的首轮身份已确认，仍须完成视觉验收。

确认依据：新 ID 不在准备前快照里；规范化 cwd 与完整 `first_user_message` 相等；rollout 首次 `task_started`、同 turn 的 `turn_context` 和任务原文相符。首轮模型/推理档不符不能由后续轮次补过。数据库 `project_id` 不是成功门槛。文件可能延迟数秒落盘。确认所需的首轮记录末尾未换行且 JSON 解析失败时，视为尚未完整落盘，应等待后重试确认，不能重派。解析成功的记录进入身份与配置核验，末尾换行不作为单独成功门槛；已换行记录解析失败则报告读取错误。

长任务由 App 自己持续执行，脚本准备后立即返回；无需 `--async` 才能继续。`--async` 在 desktop 下兼容接受但仍只准备。诊断用 `--prepare-only` 仅写回执和 URL，不打开 App。

## 进行中进度（默认异步：GUI 第一观测位；watch 可选直播）

**默认异步惯例（2026-10-03 定型）**：desktop 派单确认后 WB **不主动挂直播**——用户在 App GUI 自由观察过程（全量真相：reasoning/工具输出/diff），完毕后以收单触发词告知 WB。WB 会话内不转播实况。

**watch 直播仅三场景**：

1. **用户明示直播**（"codex直播 / watch一下"）：`desktop-confirm` 状态 `created` 后拿到 `thread_id` 与 `first_turn_id`，WB 挂 `python3 "$B" watch THREAD_ID --follow --turn-id <first_turn_id>` 拉取式直播；watch 是日志摘要，不能替代 UI 验收。
2. **用户明示离场的长任务**：WB 按「长任务值守」范式值守，锚点落盘任务目录（如 `<项目目录>/WATCH_ANCHOR_<主题>.json`，续播时 `cat` 读出传给 `--anchor`），跨会话可续；恢复在场后用户可随时接管回看 GUI。
3. **daemon 路线（不变）**：标准形态 = `--async` 发射 + `watch --follow` 拉取式直播 + `read` 对账——daemon 无 GUI 可看，watch 恒为必需。

```bash
python3 "$B" watch THREAD_ID --follow --turn-id TURN_ID             # 绑定派单返回的 turn，跟到该 turn 终态或等待上限
python3 "$B" watch THREAD_ID --follow --turn-id TURN_ID --timeout 20 # 持续观察最多 20 秒，终态可能提前结束
python3 "$B" watch THREAD_ID --follow --turn-id TURN_ID --anchor '<上次 ANCHOR 的 JSON>' # 验证身份后自动恢复 OFFSET
python3 "$B" watch THREAD_ID                                       # 回放到当前 EOF，不因历史 completed 提前结束
```

THREAD_ID / TURN_ID 来源：desktop 原生任务取 `desktop-confirm` 返回的 `thread_id` / `first_turn_id`；后台派单取 dispatch 输出的 `THREAD_ID` / `TURN_ID`。

原理：两后端 writer 都把会话追加进 rollout jsonl；watch 只读尾随并分类摘要（[Codex]/[命令]/[工具]/[文件]），不调用 app-server、不接管 writer。它使用 `CODEX_BRIDGE_CODEX_HOME`（默认 `~/.codex`）并查找 `sessions` 和 `archived_sessions`；核对 session 元数据里的会话 ID，支持至少 8 位的规范 UUID 前缀，多个候选时报歧义，不猜最新文件。延迟取决于落盘和轮询，默认轮询 1.2 秒。watch 是日志摘要，不能替代真实 UI 验收，也不保证包含所有工具输出或零延迟。

`--follow` 推荐显式给 `--turn-id`，尤其使用后台 dispatch 返回的 `TURN_ID`。省略时先使用已验证锚点里的目标 turn；无目标锚点时在首次完整历史扫描后绑定最后一个 `task_started`，若当时没有则绑定首次新出现的 `task_started`。绑定后不改跟新 turn，旧 turn 的 completed 不能结束当前目标。回放不要求 turn，只读到当前 EOF；末尾仍有未换行的半条记录时，输出 unknown / `partial_record` 并退出 3，锚点停在上一条完整记录末尾。

所有秒数参数必须是正有限数：`--timeout` 默认 1800 秒（从 watch 调用起计，包含等待文件；匹配 25 分钟级典型任务）；`--idle-timeout` 默认 900 秒（follow 停更上限，覆盖高推理档长静默窗）；`--interval` 默认 1.2 秒；`--wait-file` 默认 20 秒。未读到目标终态而到达总期限或停更上限时，结果为 unknown，不能声称任务已完成。

停止时输出 `OFFSET=<数字>`、`ANCHOR=<紧凑 JSON>`、`WATCH_STATUS` 和 `STOP_REASON`。锚点 schema 为 `codex-watch-anchor-v1`，含 `thread_id` / `device` / `inode` / `offset` / `sha256` / `turn_id`，绑定会话、文件系统身份、完整记录前缀摘要及目标 turn；推荐单独传 `--anchor` 自动恢复偏移。`--since` 必须是非负整数（默认 0），大于 0 时同时提供匹配的 `--anchor`，不能拿裸偏移套到另一份文件。在受支持的 sessions / archived_sessions 范围内仅 rename 且 inode 不变时可继续；文件被替换、截断或身份无法继续核验时，输出 unknown / `source_changed` 和 `ANCHOR=null`，该锚点失效、不可续播。文件尚未找到或身份未完整落盘时也可能没有可用锚点。退出码见下表。

## 长任务值守（用户明示直播/离场时的 watch 节拍循环范式）

本段仅在「进行中进度」段的三场景之①②生效；默认异步下 WB 不执行值守。

- **退出码 3 ≠ 任务失败**：先看 `STOP_REASON` 与 ANCHOR。timeout / idle 且 ANCHOR 为有效对象时，带锚点续播：`python3 "$B" watch THREAD_ID --follow --turn-id TURN_ID --anchor '<上轮 ANCHOR 的 JSON>'`；file wait / timeout 且 `ANCHOR=null`、文件身份尚未核验时，用同一 THREAD_ID / TURN_ID 不带 `--anchor` 重新等待。`source_changed` 必须停止续播并重新核对来源，不套旧锚点、不重派任务。禁止把 JSON `null` 传给 `--anchor`。
- **超长任务显式调参**：默认 1800/900 已覆盖 25 分钟级典型任务；小时级任务再调大，如 `--timeout 10800 --idle-timeout 1800`。
- **Mac 休眠提醒**：值守期间建议 `caffeinate` 防系统休眠打断观测；休眠恢复后用 ANCHOR 续播（前提 rollout 文件未被截断替换，否则按 `source_changed` 处置）。
- **值守纪律**：值守期间 WB 主动播报，不向用户假报完成；turn 未达终态，结果恒为 unknown。

## 结果与干预

```bash
python3 "$B" read THREAD_ID       # 只读历史（默认文本视图有裁剪：不含 reasoning 与命令 stdout，全保真用 --raw 或直读 rollout jsonl）
python3 "$B" status THREAD_ID     # 只读状态，不能据此断言前端正常
```

> **transport 依赖补注（事实）**：status/read 经 app-server 协议读取，默认 daemon transport 需 daemon 在线；daemon 不在线时，desktop 任务可用 `--transport stdio`（一次性子进程）读取，或用 watch 回放 / 直读 rollout jsonl 纯文件对账。

后台派单不带触达参数时默认**前台跳转**（`open codex://threads/<id>`）——静默需求请显式 `--no-open`（纯协议层）或 `--open-bg`（后台注册不抢焦）。

原生任务的补充指令、纠偏、停止、接管均在 **Codex App 正常输入框/停止按钮**完成。不要调用独立 daemon 的 `thread/resume` 来“测试可写”。桥的 `steer` / `interrupt` 会拒绝已通过原生回执识别的任务；回执不是所有 App 任务的完整清单，未知 ID 也必须先核实归属，不能据“没有回执”推定 daemon 所有。

回执默认存于 `~/.workbuddy/codex-bridge/dispatch`（仅本机、权限 0600，含任务文本，勿放凭据）。测试可用 `CODEX_BRIDGE_STATE_DIR` 与 `CODEX_BRIDGE_CODEX_HOME` 指向隔离目录。

## 显式后台模式

仅用户明确选择后台执行时使用，不能冒充敲锣模式的验收：

```bash
python3 "$B" dispatch --transport daemon --async --no-open --name "<主题>" --task "<任务>"
python3 "$B" dispatch --transport stdio --no-open --name "<主题>" --task "<任务>"
# 以下控制仅用于归属该后台的任务：
python3 "$B" steer THREAD_ID --transport daemon --text "<追加指令>"
python3 "$B" steer THREAD_ID --transport daemon --live --turn-id TURN_ID --text "<纠偏指令>"
python3 "$B" interrupt THREAD_ID TURN_ID --transport daemon
```

- daemon：managed control socket 的 WebSocket-over-UDS；支持异步和进行中控制。此 socket 不保证是桌面 App 所用后端。
- stdio：一次性子进程，关闭进程可能中断任务；不支持异步或进行中控制。
- `--ephemeral`：后台内存冒烟，不是正式 App 任务。
- `--open-bg` / `--no-open`：仅后台 transport 接受。前者仅请求后台导航，不保证注册、更新或接管；后者不导航。默认 desktop 拒绝这两个参数。
- writer 冲突应检查拥有者；不能盲删锁、杀进程或宣称“等租约就好”。
- `steer` / `interrupt` 必须显式选择后台 transport；省略就拒绝执行。显式参数不替代会话归属核验。

## 配置、退出码与任务包

默认模型优先级：`--model` > `config.json.default_model` > `gpt-6.1-sol`；配置热加载，损坏会警告。模型最终以首轮实际执行记录核验。`--effort` 可显式要求推理档。正式任务必须 `--name`；后台 `--ephemeral` 冒烟豁免。后台 CLI 路径仍固定为 `~/.local/bin/codex（可用 CODEX_BRIDGE_CODEX_BIN 覆盖）`。

退出码按命令区分：

| 命令 | 0 | 1 | 2 | 3 |
|---|---|---|---|---|
| desktop dispatch | 准备成功，未确认派出 | 业务参数/打开/存储错误 | CLI 语法或参数范围错误 | — |
| desktop-confirm | 身份与首轮配置确认，UI 仍须验收 | 读取/回执错误 | 不匹配或歧义，或 CLI 语法错误 | 待提交/待落盘 |
| 后台同步 dispatch/追加式 steer | 本次 turn completed | 协议/业务参数错误 | CLI 语法或参数范围错误，或本次非成功终态 | — |
| 后台 async dispatch | turn 已启动，不是完成 | 协议/业务参数错误 | CLI 语法或参数范围错误 | — |
| 后台 live steer/interrupt | 请求已接受，终态仍须对账 | 协议/业务参数错误 | CLI 语法错误 | — |
| status/read | 读取成功 | 协议/读取错误 | CLI 语法或参数范围错误 | — |
| watch | 目标 completed，或回放完整读到 EOF | 身份/读取/坏锚点 | CLI 语法或参数范围错误，或目标 failed/aborted | 总期限/停更或文件等待上限/源文件改变/回放末尾半条记录，结果 unknown |

用户中断统一 130；watch 人工中断也输出当前锚点。`WATCH_STATUS` 区分 completed / aborted / failed / unknown / replay，`STOP_REASON` 说明具体停止原因。`status/read` 成功只证明读取成功。

desktop-confirm / watch 报 schema 校验错误（回执、数据库或 rollout 结构与桥的预期不符）= 本机 codex 内部存储版本已升级、桥尚未适配：立即停止派单并向用户报告，不得硬跑。

任务上下文较长时写 `<项目目录>/REQUEST_<主题>.md`（模板见 `references/task_packet.md`），派单文本只给路径与执行要求，避免超长 URL。任务包约束明确 Git 隔离、凭据禁读、用户记忆文件只读；交付与验收权按下述既有约定执行。

## 产物回收三通道

1. **只读结果**：原生任务在 App 查看过程与最终回复，也可 `read THREAD_ID` 只读历史（可能有落盘延迟）。后台同步派单仍输出 REPLY。不能为读结果调用 `steer` 或 `thread/resume`。
2. **本机文件系统**：Codex 写的文件落盘在任务书指定的目标目录——就是本机磁盘，Read/Glob 直接拿，无需"传输"。2026-10-02 实测：它写 deliverable.md，我方 Read 一字不差。
3. **git 分支**（代码任务）：Codex commit 到 `codex/<任务>` 分支，我方用 git diff/log 终审；取得用户最终确认后，由 WorkBuddy 合入。git 差异是验收证据，不自动等于 ACCEPT。

**交付双模路由（以本单授权及适用项目约束为准）**：

派单前置判断（我方每次派单执行，先做第 0 步 Git 化探针，再选定模式并在任务包写明）：
1. **代码修改默认模式A**：同一仓库的 `codex/<任务>` 分支和原目录之外的独立 worktree；原目录不切分支、不编辑、不暂存。目标尚未 Git 化时由第 0 步探针补齐基线，不能因此自动直改。
2. **代码模式B仅为明确例外**：用户明确授权原目录直改，且不与适用项目约束冲突时才可用；普通"修改/升级"授权不构成取消隔离。
3. **非代码文件任务**：按本单范围可用模式B；用户指定分支或项目约束要求隔离时仍用模式A。只读审查无需 commit。

- **模式A git 分支**：Codex 在已核验的独立 worktree 修改并 commit 到 `codex/<任务>` 分支，交回基线/结果提交号及 diff。我方终审、用户最终确认后由 WorkBuddy 合入；Codex 不合入主线。
- **模式B in-place 直改**：Codex 在明确授权的目录改文件，我方 Read 评估，不通过则还原或在原任务打回（原生任务用 App 输入）。**必备安全网（写进任务包约束段）**：Codex 改动任何既有文件前必须先 `cp` 备份为 `<文件名>.bak-<时间戳>`，我方验收及用户确认前不得删除备份。该模式不同时要求分支 commit。
- 模式B 已知软肋：大改动面（>10 文件）时 Read 审查效率低且依赖自报不漏——遇此规模即使非 repo，也建议用户考虑模式A 或 git init 临时装甲。用户记忆文件任何模式下永属只读。

**对账纪律**：派单时在任务文本里要求 Codex 最终回复自报产物清单（路径/行数/执行过的工具），拿回后用 Read/git 逐条核验——它自报 vs 我方实证，两清再入工作流。

## 收单 workflow（2026-10-03 定型）

触发词：codex做完了 / codex收单 / 验收codex / 合入codex。用户告知完毕后按以下序列执行：

1. **终态对账**：`read` / `status THREAD_ID` 拿最终状态与回复（desktop 任务也可在 App 内查看；transport 依赖见"结果与干预"段补注——daemon 不在线时用 `--transport stdio` 或 watch 回放 / 直读 rollout jsonl）。**过程细节补看**：用户在 GUI 已全程观察时可跳过；需复盘时 `watch THREAD_ID` 回放（分类摘要）或直读 rollout jsonl（全保真）。
2. **产物实证**：Codex 自报产物清单（路径/行数/执行过的工具）→ 我方 Read/git 逐条核验，自报 vs 实证两清再入流。
3. **终审**：模式A `git diff <基线commit>..<结果commit>` 逐条审；模式B Read 评估改动文件（.bak 备份在场）。
4. **终审不过 → 打回或还原**：原生任务走 App 输入框纠偏，不另起重复任务；或还原（模式A 弃分支 / 模式B .bak 还原）。
5. **终审过 → 交用户最终确认**。
6. **用户确认后由 WorkBuddy 合入（仅模式A）**：默认 `git merge --no-ff codex/<任务>` 保留审计轨迹；冲突即 HARD STOP 问用户，禁止自动解冲突；合入后分支默认保留作审计，用户明示才删。
7. **模式B**：验收及用户确认后才可删 `.bak`。
8. **回滚路径**：未合入 → 弃分支 / 还原 .bak；已合入 → `git revert` 并向用户报告。

合入主体恒为 WorkBuddy：Codex 不自签 ACCEPT、不合主线（与红线同律）。

## 红线（遵守适用项目约束）

- 密钥不进桥：桥只传任务文本，凭据留在本机原位置，脚本无任何网络外发。
- **终审权恒在 WorkBuddy（双模各自落地）**：AI 产出未经我方验收不得成为最终态——模式A：Codex 只在独立 worktree 的 `codex/<任务>` 分支 commit，我方终审及用户最终确认后才由 WorkBuddy 合入；模式B：验收及用户确认前 `.bak` 备份不得删除（不通过则还原）。Codex 不自签 ACCEPT、不自行合入或放行。
- 用户记忆文件对 Codex 只读；桥的会话产物可由 WorkBuddy 写日志（append-only 自由）。

## 深入参考

- 协议方法/参数/事件/版本史：references/api_reference.md
- 官方文档：https://learn.chatgpt.com/docs/app-server ｜ 协议 schema 导出：`codex app-server generate-json-schema --out <dir>`
- 官方冒烟客户端：`codex debug app-server send-message-v2 "<msg>"`
