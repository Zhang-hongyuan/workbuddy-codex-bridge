# codex-bridge（codex通信）

WorkBuddy ↔ Codex 桌面 App 的通信桥：让 WorkBuddy 把任务派给本机 Codex，读回结果，完成验收与合入——同时把"最终确认权"死死留在人手里。

## 它解决什么问题

直接把任务文本塞给另一个 AI agent 是很容易的，难的是：

- **不伪造确认**：`open` 返回 0、数据库有记录，都不等于任务真的出现在了 App 里
- **不抢占 writer**：桌面 App 与后台 daemon 是两个后端，乱接管会把会话搞坏
- **人不离场**：核对、提交、验收、合入，每一步都是用户本人的动作，桥只做准备和只读核验

## 工作流（四步敲锣）

1. **准备并唤醒**：脚本保存回执（0600 权限），请求打开 `codex://new?path=...&prompt=...` 深链预填草稿，并发一条 macOS 浅提醒
2. **用户亲手提交**：WorkBuddy 列出核对要点（项目/原文/模型/推理档），用户在 App 核对后亲手点发送——桥不代点，也无 GUI 自动化能力
3. **desktop-confirm**：只读 SQLite + 首轮 rollout 记录，核验新任务身份、项目、原文、实际模型与推理档
4. **UI 验收**：用户在 App 确认任务出现、过程更新、输入可干预

## 命令速查

```bash
B=scripts/codex_bridge.py
python3 "$B" dispatch --name "<主题>" --task "<任务文本>"   # 默认 desktop 敲锣派单
python3 "$B" desktop-confirm <receipt_path>                # 只读确认身份与首轮配置
python3 "$B" watch <THREAD_ID> --follow --turn-id <TURN_ID> # 只读尾随 rollout 直播（可断点续播）
python3 "$B" read <THREAD_ID>                              # 只读历史
python3 "$B" status [THREAD_ID]                            # 只读状态
```

显式后台模式（`--transport daemon|stdio`）支持异步派单、进行中 steer/interrupt，仅用于归属明确的后台会话。

## 配置

| 配置项 | 优先级/默认 |
|---|---|
| 模型 | `--model` > `config.json.default_model` > 内置兜底 |
| `CODEX_BRIDGE_CODEX_BIN` | codex 可执行文件路径（默认：PATH 查找 → `~/.local/bin/codex`） |
| `CODEX_BRIDGE_CWD` | 派单锚定的项目目录（默认：用户主目录） |
| `CODEX_BRIDGE_CODEX_HOME` | codex 数据目录（默认：`~/.codex`） |
| `CODEX_BRIDGE_STATE_DIR` | 派单回执目录（默认：`~/.workbuddy/codex-bridge/dispatch`） |

## 安全设计

- 零网络外发：仅本机 Unix socket / stdio 子进程 / `codex://` 深链
- 回执 0600 权限、符号链接拒绝、rollout 路径围栏、SQLite 只读（`mode=ro` + `query_only`）
- watch 断点锚点绑定 thread_id + device/inode + sha256 前缀，源文件被替换即报 `source_changed`，不套旧锚点
- macOS 通知走 osascript argv 传参，不做脚本字面量拼接

## 测试

```bash
python3 -m pytest tests/ -q
```

## 红线

- 密钥不进桥：桥只传任务文本，凭据留在本机原位置
- 终审权恒在 WorkBuddy 与用户：Codex 不自签 ACCEPT、不自行合入主线

## License

MIT，见 [LICENSE](LICENSE)。
