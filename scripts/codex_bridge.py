#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
codex_bridge.py — WorkBuddy ↔ Codex 通信桥 CLI（codex通信 skill 执行层）

默认 desktop：请求 App 原生新任务草稿；WorkBuddy 列出核对要点，用户核对项目/原文/模型/推理档
并亲手提交一次，desktop-confirm 只读确认真实任务。prepare/open 返回成功不等于任务已提交或可见。
显式 daemon / stdio：旧 app-server 后台执行，不能保证与桌面 App 共用 writer。
默认模型来自 config.json；正式任务必须 --name，cwd 默认锚定协作货架，执行目标由任务书指定。
"""
import argparse
import base64
import collections
import json
import math
import os
from pathlib import Path
import queue
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time

from desktop_dispatch import (DesktopDispatchError, confirm_dispatch,
                              is_desktop_thread, prepare_dispatch, _codex_dir)
from rollout_watch import WatchError, watch_rollout

# codex 可执行文件：环境变量 CODEX_BRIDGE_CODEX_BIN > PATH 查找 > ~/.local/bin/codex
CODEX_BIN = (os.environ.get("CODEX_BRIDGE_CODEX_BIN")
             or shutil.which("codex")
             or str(Path.home() / ".local/bin" / "codex"))
FALLBACK_MODEL = "gpt-6.1-sol"  # 内置兜底；"值"以 config.json 为准（可配置，热加载）
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
# 派单默认锚定的项目目录：环境变量 CODEX_BRIDGE_CWD 覆盖，默认用户主目录
DEFAULT_CWD = os.environ.get("CODEX_BRIDGE_CWD", str(Path.home()))


def _daemon_sock():
    """daemon control socket（WS-over-UDS），跟随 CODEX_BRIDGE_CODEX_HOME（默认 ~/.codex）。"""
    return str(_codex_dir() / "app-server-control" / "app-server-control.sock")
CLIENT_NAME = "workbuddy-bridge"
CLIENT_VERSION = "1.7.4"
MIN_VERSION_FOR_SOL = "0.159.1"  # gpt-6.1-sol 进入 bundled catalog 的最低版本


def load_config():
    """每次调用直读（热加载：用户改完立即生效）；损坏回退兜底+警告，不静默。"""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            config = json.load(f)
        if not isinstance(config, dict):
            raise ValueError("配置必须是 JSON 对象")
        return config
    except FileNotFoundError:
        return {}
    except (ValueError, OSError, UnicodeError) as e:
        print(f"WARN: config.json 读取失败（{e}），回退内置兜底 {FALLBACK_MODEL}", file=sys.stderr)
        return {}


def effective_model(cli_model=None):
    """优先级：--model 命令行 > config.json default_model > FALLBACK_MODEL。"""
    if cli_model:
        return cli_model
    m = load_config().get("default_model")
    if isinstance(m, str) and m.strip():
        return m.strip()
    return FALLBACK_MODEL


class BridgeError(Exception):
    pass


def _safe_getcwd():
    """获取当前工作目录；cwd 被删/权限失效时给人话而非裸 traceback。

    macOS 实测：shell 所在目录被 rm 或 chmod 000 后，os.getcwd() 直接抛
    FileNotFoundError/PermissionError（2026-10-03 QA Round 1 复现）。
    原始 errno 与系统消息保留在括号内，不误伤其他 OSError 场景。
    """
    try:
        return os.getcwd()
    except OSError as e:
        raise BridgeError(
            f"当前工作目录已失效（{e}）\n"
            f"  → shell 所在目录被删除或权限已变更；请 cd 到有效目录后重试") from e


def _safe_abspath(path):
    """os.path.abspath 人话化包装（QA Known Issue 缓修项，风格同 _safe_getcwd）。

    相对路径解析内部调 os.getcwd()；shell 所在目录被删/权限失效时 abspath
    抛裸 FileNotFoundError/PermissionError traceback。翻译为 BridgeError，
    保留原始 errno 与系统消息，并给出两条出路。绝对路径不触 getcwd，不受影响。
    """
    try:
        return os.path.abspath(path)
    except OSError as e:
        raise BridgeError(
            f"无法解析 --cwd 的绝对路径：{path}（{e}）\n"
            f"  → 相对路径依赖当前工作目录，而 shell 所在目录已失效；"
            f"请 cd 到有效目录后重试，或给 --cwd 传绝对路径") from e


# ── WebSocket-over-UDS（2026-10-02 高手情报实证：daemon control socket 说 WS 不说 JSONL）──
def _ws_connect(path):
    """建连 + HTTP Upgrade 握手。返回 (sock, 握手残余字节)——残余可能是合并投递的首帧，严禁丢弃。"""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(15)  # 15s 仅限建连+握手期
    try:
        s.connect(path)
    except OSError as e:
        raise BridgeError(
            f"连不上 daemon socket：{path}\n  详情：{e}\n"
            f"  → daemon 未运行？跑：codex app-server daemon start\n"
            f"  → 或本次用 --transport stdio 回退（无异步/无中途 steer-interrupt）")
    key = base64.b64encode(os.urandom(16)).decode()
    hs = (f"GET /rpc HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
          f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
    try:
        s.sendall(hs.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                raise BridgeError("daemon 在 WebSocket 握手阶段关闭连接（预期外，请查 daemon 状态）")
            buf += chunk
    except (socket.timeout, OSError) as e:
        s.close()
        raise BridgeError(f"daemon WS 握手失败/超时：{e}\n"
                          f"  → 查 codex app-server daemon version 或 --transport stdio 回退")
    head, _, rest = buf.partition(b"\r\n\r\n")
    hlines = head.decode(errors="replace").splitlines()
    parts = hlines[0].split() if hlines else []
    if len(parts) < 2 or parts[1] != "101":
        s.close()
        raise BridgeError(f"WebSocket Upgrade 被拒：{hlines[0] if hlines else '(空响应)'}")
    # P0 修复：进入长连接后改纯阻塞（reader 是 daemon 线程，close() 关 sock 即解锁退出），
    # 否则 15s 全局超时会在长任务流式静默窗误杀 reader（假超时）。
    s.settimeout(None)
    return s, rest


def _ws_send_text(s, text):
    payload = text.encode()
    mask = os.urandom(4)
    header = bytearray([0x81])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126); header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127); header += struct.pack(">Q", n)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    s.sendall(bytes(header) + mask + masked)


def _recv_exact(s, n, prebuf=b""):
    """精确读 n 字节（先消费 prebuf 再 recv）。返回 (data, 剩余 prebuf)。"""
    buf = bytes(prebuf[:n])
    prebuf = prebuf[n:]
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("daemon 连接中断（EOF）")
        buf += chunk
    return buf, prebuf


def _ws_recv(s, prebuf=b""):
    """收一条完整 text 消息；自动回 pong；忽略 pong；拒绝 binary。返回 (text, 剩余 prebuf)。"""
    frags = b""
    while True:
        hdr2, prebuf = _recv_exact(s, 2, prebuf)
        b1, b2 = hdr2[0], hdr2[1]
        fin, op = b1 & 0x80, b1 & 0x0F
        masked, ln = b2 & 0x80, b2 & 0x7F
        if ln == 126:
            ext, prebuf = _recv_exact(s, 2, prebuf)
            ln = struct.unpack(">H", ext)[0]
        elif ln == 127:
            ext, prebuf = _recv_exact(s, 8, prebuf)
            ln = struct.unpack(">Q", ext)[0]
        if masked:
            mask, prebuf = _recv_exact(s, 4, prebuf)
        else:
            mask = None
        if ln:
            data, prebuf = _recv_exact(s, ln, prebuf)
        else:
            data = b""
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if op == 0x9:  # ping → pong
            hdr = bytearray([0x8A]); m2 = os.urandom(4)
            pn = len(data)
            # 协议规定 control frame ≤125 字节；对端违规发大 ping 时如实按扩展长度回，
            # 避免 0x80|len 在 len>125 时生成非法帧头（2026-10-03 审计修复）
            if pn < 126:
                hdr.append(0x80 | pn)
            elif pn < 65536:
                hdr.append(0x80 | 126); hdr += struct.pack(">H", pn)
            else:
                hdr.append(0x80 | 127); hdr += struct.pack(">Q", pn)
            s.sendall(bytes(hdr) + m2 + bytes(b ^ m2[i % 4] for i, b in enumerate(data)))
            continue
        if op == 0xA:  # pong：本端不发 ping，unsolicited pong 不当消息，直接忽略
            continue
        if op == 0x2:
            raise ConnectionError("daemon 发来 binary frame（协议应为纯文本 JSON-RPC，连接状态异常）")
        if op == 0x8:
            raise ConnectionError("daemon 发来 close frame")
        frags += data
        if fin:
            return frags.decode(errors="replace"), prebuf


def humanize_error(context, err):
    """把协议错误翻译成人话"""
    msg = err.get("message", "") if isinstance(err, dict) else str(err)
    if "not supported when using" in msg:
        return (f"{context} 失败：模型不在当前账号白名单/客户端 catalog。\n"
                f"  原文：{msg}\n"
                f"  → 若目标是 {effective_model()}：需 codex >= {MIN_VERSION_FOR_SOL}，"
                f"执行 npm install -g @openai/codex@latest 升级")
    low = msg.lower()
    if ("not found" in low or "not loaded" in low) and "thread" in low:
        return f"{context} 失败：会话不存在或未加载，检查 thread_id 是否完整（36 位 UUID）。原文：{msg}"
    if "no active turn to interrupt" in low:
        return f"{context}：该 turn 已结束，无需急停。原文：{msg}"
    if "no active turn to steer" in low:
        return f"{context}：该 turn 已结束，进行中纠偏不适用，请用不带 --live 的追加式 steer。原文：{msg}"
    if "no rollout found" in low:
        return f"{context} 失败：会话无落盘记录（ephemeral 或 id 有误）。原文：{msg}"
    return f"{context} 失败：{msg}"


class AppServer:
    """app-server JSON-RPC 客户端。transport=stdio 一次性子进程（JSONL）；daemon 直连 WS-over-UDS（无子进程）。"""

    def __init__(self, cwd, transport="stdio"):
        self.transport = transport
        self.q = queue.Queue()
        self._rid = 0
        self._notes = []                             # call() 期间 stash 的流式通知，wait_turn 开场消化
        self._ws_prebuf = b""                        # WS 握手残余字节（可能是合并投递的首帧）
        self._errbuf = collections.deque(maxlen=100)  # stdio stderr 环形缓冲（防管道写满假死）
        if transport == "daemon":
            self.sock, self._ws_prebuf = _ws_connect(_daemon_sock())
            self.proc = None
            threading.Thread(target=self._reader_ws, daemon=True).start()
        else:
            if not os.path.exists(CODEX_BIN):
                raise BridgeError(
                    f"找不到 codex 可执行文件：{CODEX_BIN}\n"
                    f"  → 安装/修复：curl -fsSL https://chatgpt.com/codex/install.sh | sh（官方安装器）\n"
                    f"  → 若你换了安装方式，请设置 CODEX_BRIDGE_CODEX_BIN 环境变量指向实际路径")
            try:
                self.proc = subprocess.Popen(
                    [CODEX_BIN, "app-server"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, cwd=cwd)
            except OSError as e:
                # cwd 被删/二进制无执行权限等：给人话而非裸 traceback（2026-10-03 审计修复）
                raise BridgeError(
                    f"无法启动 codex app-server 子进程：{e}\n"
                    f"  → 检查 cwd 是否仍存在：{cwd}\n"
                    f"  → 检查二进制可执行权限：{CODEX_BIN}") from e
            self.sock = None
            threading.Thread(target=self._reader, daemon=True).start()
            threading.Thread(target=self._reader_err, daemon=True).start()
        try:
            self._handshake()
        except Exception:
            self.close()  # 握手失败不留半开资源（sock/proc 一并收掉）
            raise

    def _reader(self):
        for line in self.proc.stdout:
            self.q.put(line)
        # stdout EOF = 对端进程已退出：以死亡哨兵通知主线程真实死因，而非干等超时
        self.q.put(json.dumps({"method": "__bridge/readerDied",
                               "params": {"error": "stdio 通道已关闭（对端进程退出）"}},
                              ensure_ascii=False))

    def _reader_err(self):
        for line in self.proc.stderr:
            self._errbuf.append(line.rstrip("\n"))

    def _reader_ws(self):
        prebuf = self._ws_prebuf
        try:
            while True:
                msg, prebuf = _ws_recv(self.sock, prebuf)
                self.q.put(msg)
        except Exception as e:
            # 死亡哨兵：_wait 识别后立即以真实死因报错，杜绝"reader 死=假超时"
            self.q.put(json.dumps({"method": "__bridge/readerDied",
                                   "params": {"error": str(e)}},
                                  ensure_ascii=False))

    def _send(self, obj):
        try:
            if self.sock is not None:
                _ws_send_text(self.sock, json.dumps(obj, ensure_ascii=False))
            else:
                self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
        except (OSError, BrokenPipeError) as e:
            raise BridgeError(f"app-server 通道已断（进程可能已崩溃）：{e}")

    def _wait(self, pred, timeout, stash=False):
        """消费队列直到 pred 命中/超时/reader 死亡。stash=True 时未命中的通知暂存 self._notes。"""
        end = time.time() + timeout
        while time.time() < end:
            try:
                line = self.q.get(timeout=max(0.1, end - time.time()))
            except queue.Empty:
                break
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("method") == "__bridge/readerDied":
                raise BridgeError(
                    f"app-server 连接已断：{obj.get('params', {}).get('error', '?')}（非任务超时）")
            r = pred(obj)
            if r is not None:
                return r
            if stash and "method" in obj:
                self._notes.append(obj)
        return None

    def _handshake(self):
        self._send({"id": 0, "method": "initialize",
                    "params": {"clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION}}})
        resp = self._wait(lambda o: o if o.get("id") == 0 else None, 15)
        if not resp or "result" not in resp:
            err = ""
            if self.proc is not None:
                try:
                    self.proc.wait(timeout=1)
                except Exception:
                    pass
                err = "\n".join(self._errbuf).strip()[-300:]
            hint = ""
            if self.transport == "daemon":
                hint = ("\n  → daemon 已连上但 initialize 无应答（异常）：查 codex app-server daemon version"
                        "\n  → 或本次用 --transport stdio 回退（无异步/无中途 steer-interrupt）")
            raise BridgeError("initialize 握手失败：app-server 无应答。"
                              + (f"\n  详情：{err}" if err else "") + hint)
        self._send({"method": "initialized", "params": {}})
        self.user_agent = resp["result"].get("userAgent", "")

    def call(self, method, params, timeout=30):
        self._rid += 1
        rid = self._rid
        self._send({"id": rid, "method": method, "params": params})
        resp = self._wait(lambda o: o if o.get("id") == rid else None, timeout, stash=True)
        if resp is None:
            raise BridgeError(f"{method} 超时（{timeout}s 无响应）")
        if "error" in resp:
            raise BridgeError(humanize_error(method, resp["error"]))
        return resp.get("result", {})

    def wait_turn(self, timeout, thread_id=None, on_first_event=None, turn_id=None):
        """等本次 turn/completed，回复与过程项同时绑定 thread_id / turn_id。
        on_first_event：后台模式收到首个事件时可请求导航；不证明 App 可见或拥有 writer。"""
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise BridgeError("等待任务终态需要明确 thread_id")
        if not isinstance(turn_id, str) or not turn_id.strip():
            raise BridgeError("turn/start 未返回有效 turn_id，无法确认本次任务终态")
        deltas, items = [], []
        fired = False
        def pred(o):
            nonlocal fired
            p = o.get("params", {})
            if not isinstance(p, dict):
                return None
            m = o.get("method", "")
            if p.get("threadId") != thread_id:
                return None
            if m == "turn/completed":
                final_turn = p.get("turn")
                event_turn = final_turn.get("id") if isinstance(final_turn, dict) else None
            else:
                event_turn = p.get("turnId")
            if event_turn != turn_id:
                return None  # 旧轮次、其他会话及未绑定事件均不收集、不终判
            if m and not fired and on_first_event:
                fired = True
                try:
                    on_first_event()
                except Exception:
                    pass
            if m == "item/agentMessage/delta":
                deltas.append(p.get("delta") or "")
            elif m == "item/completed":  # 只记 completed：started/completed 成对，天然去重
                t = p.get("item", {}).get("type", "")
                if t and t != "agentMessage":
                    items.append(t)
            elif m == "turn/completed":
                return p.get("turn", {})
            return None
        # 先消化 call() 期间 stash 的通知（turn/start 应答前的开场 delta 不丢），再等队列新消息
        stashed, self._notes = self._notes, []
        for o in stashed:
            r = pred(o)
            if r is not None:
                return r, "".join(deltas), items
        turn = self._wait(pred, timeout)
        if turn is None:
            raise BridgeError(
                f"等待任务完成超时（{timeout}s）——长任务请调大 --timeout，"
                f"或稍后用 read <THREAD_ID> 取结果")
        return turn, "".join(deltas), items

    def close(self):
        try:
            if self.sock is not None:
                self.sock.close()
            if self.proc is not None:
                self.proc.kill()
        except Exception:
            pass


def extract_text_from_turns(turns):
    """从 thread/read(includeTurns=true) 的 turns 里宽容提取对话文本。"""
    lines = []
    for turn in turns or []:
        for item in turn.get("items", []) or []:
            t = item.get("type", "")
            if t == "userMessage":
                txt = "".join(c.get("text") or "" for c in (item.get("content") or []) if isinstance(c, dict))
                if txt:
                    lines.append(("user", txt))
            elif t == "agentMessage":
                txt = item.get("text", "")
                if txt:
                    lines.append(("agent", txt))
            elif t == "commandExecution":
                cmd = item.get("command", "")
                if cmd:
                    lines.append(("cmd", cmd))
    return lines


def check_turn_ok(final, tid):
    """turn 终态检查：非 completed 一律显式失败（退出码 2）。stdout 结果区调用方已打印，此处仅 stderr 补 ERROR。"""
    status = final.get("status", "?")
    if status != "completed":
        err = final.get("error") or final.get("errors") or ""
        print(f"ERROR: 任务未成功完成（thread={tid} status={status}）。{err}", file=sys.stderr)
        sys.exit(2)


def cmd_dispatch(a):
    if not a.ephemeral and not a.name:
        raise BridgeError(
            "正式派单必须 --name <主题>（命名规范：【WB委派·yymmdd·主题】，"
            "便于用户在 App 列表一眼辨认委派单）；协议冒烟请加 --ephemeral 豁免")
    if a.transport == "desktop" and (a.ephemeral or a.open_bg or a.no_open):
        raise BridgeError("默认敲锣需要 App 原生持久任务；--ephemeral/--open-bg/--no-open "
                          "仅用于显式 --transport daemon 或 stdio 的后台派单")
    if a.prepare_only and a.transport != "desktop":
        raise BridgeError("--prepare-only 仅 desktop 可用")
    if a.async_fire and a.ephemeral:
        raise BridgeError("--async 与 --ephemeral 不可同用：ephemeral 会话 turn 完成即被 daemon 回收，"
                          "结果收不回（QA-E01 实证）。去掉 --ephemeral 或改同步派单")
    if a.async_fire and a.transport == "stdio":
        raise BridgeError("--async 仅 daemon transport 可用（stdio 进程死=turn 断，2026-10-02 路B实验已证伪）")
    if not a.task.strip():
        raise BridgeError("--task 不能为空")
    cwd = _safe_abspath(a.cwd)
    if not os.path.isdir(cwd):
        raise BridgeError(f"cwd 不存在：{cwd}（cwd 必须锚定真实项目目录，App 才认这个项目分组）")
    model = effective_model(a.model)
    task_text = a.task
    if a.name:
        # 命名规范：前缀+动态日期（%y%m%d），App 列表标题取自任务文本开头
        title = f"【WB委派·{time.strftime('%y%m%d')}·{a.name}】"
        # 标题查重（2026-10-03 实测发现）：任务文本首行已含同款标题时尊重原文，不重复拼接
        first_line = a.task.lstrip().splitlines()[0] if a.task.strip() else ""
        if not first_line.startswith(title):
            task_text = f"{title}\n{a.task}"
    if a.transport == "desktop":
        result = prepare_dispatch(cwd, task_text, model, a.effort,
                                  prepare_only=a.prepare_only)
        result["notification_status"] = "skipped"
        # 浅提醒（macOS 通知横幅，非模态不抢焦）
        # 语义：用户亲手点发送=发射闸（终审权的仪式化），提醒只负责"喊你上闸"
        if not a.prepare_only and result.get("status") == "open_request_sent":
            try:
                # argv 传参（2026-10-03 注入修复）：标题/正文走 AppleScript run 参数，
                # 不再拼接进脚本字面量，a.name 含引号/&/do shell script 均为纯文本
                title_str = f"codex通信·待你发射{('·' + a.name) if a.name else ''}"
                body_str = "已请求打开 Codex 任务草稿，请核对后亲手点【发送】"
                notify_script = ("on run argv\n"
                                 "  display notification (item 2 of argv) "
                                 "with title (item 1 of argv) sound name \"Tink\"\n"
                                 "end run")
                notice = subprocess.run(["osascript", "-e", notify_script, title_str, body_str],
                                        capture_output=True, timeout=10)
                result["notification_status"] = "requested" if notice.returncode == 0 else "failed"
                if notice.returncode != 0:
                    print(f"WARN: 浅提醒请求失败（rc={notice.returncode}），请在 App 核对草稿",
                          file=sys.stderr)
            except (OSError, subprocess.SubprocessError) as error:
                result["notification_status"] = "failed"
                print(f"WARN: 浅提醒请求失败（{type(error).__name__}），请在 App 核对草稿",
                      file=sys.stderr)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print("NEXT_ACTION=WorkBuddy 向用户列出核对要点（项目/原文/模型/推理档）；"
              "用户在 App 核对并亲手提交一次，WorkBuddy 再 desktop-confirm。当前仅准备，尚未确认派单。",
              file=sys.stderr)
        return
    srv = AppServer(cwd, transport=a.transport)
    try:
        th = srv.call("thread/start", {"ephemeral": a.ephemeral, "model": model, "cwd": cwd})["thread"]
        tid = th["id"]
        params = {"threadId": tid, "model": model,
                  "input": [{"type": "text", "text": task_text}]}
        if a.effort:
            params["effort"] = a.effort
        turn = srv.call("turn/start", params)["turn"]
        # P1-01 修复（Astra 审计）：身份先于等待——派单即出号。
        # 后台模式仍可请求导航，但不同后端 writer 不会因 deep link 转移到 App。
        print(f"THREAD_ID={tid}")
        print(f"TURN_ID={turn.get('id', '')}")
        sys.stdout.flush()

        def do_register():
            # 旧后台路线的可选导航；系统接受 URL 不代表前端已注册或可干预。
            if a.open_bg:
                r = subprocess.run(["open", "-g", f"codex://threads/{tid}"], capture_output=True)
                print(f"DEEP_LINK={'requested(background; UI unverified)' if r.returncode == 0 else 'failed(rc=%d)' % r.returncode}")
            else:
                r = subprocess.run(["open", f"codex://threads/{tid}"], capture_output=True)
                print(f"DEEP_LINK={'requested(front; UI unverified)' if r.returncode == 0 else 'failed(rc=%d)' % r.returncode}")
            sys.stdout.flush()

        if a.ephemeral:
            print("DEEP_LINK=skipped(ephemeral 会话不落盘，不可注册)")
        elif a.no_open:
            print("DEEP_LINK=skipped(--no-open 后台执行，未验证 App 可见性)")
        elif a.async_fire:
            # 兼容旧后台模式：至多等待 10s 再请求导航；不作为视觉验收。
            end = time.time() + 10
            while time.time() < end:
                try:
                    line = srv.q.get(timeout=max(0.1, end - time.time()))
                except queue.Empty:
                    break
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("method"):
                    break
            do_register()
        # 同步后台分支的导航请求由 on_first_event 回调触发。
        if a.async_fire:
            # 异步发射（daemon 驱动 turn）：起跑即返回，回头 read 收结果
            print("STATUS=running")
            print(f"MODEL={model}")
            print(f"CWD={cwd}")
            print("ASYNC=daemon（发射后不管，read 收结果）")
        else:
            final, reply, items = srv.wait_turn(
                a.timeout, tid, turn_id=turn.get("id"),
                on_first_event=do_register if not (a.ephemeral or a.no_open) else None)
            status = final.get("status", "?")
            dur = final.get("durationMs", 0)
            print(f"STATUS={status}")
            print(f"DURATION_MS={dur}")
            print(f"MODEL={model}")
            print(f"CWD={cwd}")
            if items:
                print(f"ITEMS={' > '.join(items[:20])}")
            print("--- REPLY ---")
            print(reply if reply else "(无文本回复)")
            check_turn_ok(final, tid)
    finally:
        srv.close()


def cmd_desktop_confirm(a):
    result = confirm_dispatch(a.receipt_path, thread_id=a.thread_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    status = result.get("status")
    if status != "created":
        sys.exit(3 if status in ("awaiting_desktop_submit", "pending") else 2)


def require_background_writer(thread_id):
    if is_desktop_thread(thread_id):
        raise BridgeError("该任务由桌面 App 创建；请在 App 输入、停止或接管。"
                          "桥拒绝用独立 app-server resume/steer/interrupt 抢占 writer。")


def cmd_watch(a):
    try:
        return watch_rollout(a, _codex_dir())
    except WatchError as error:
        raise BridgeError(str(error)) from error


def cmd_status(a):
    srv = AppServer(_safe_getcwd(), transport=a.transport)
    try:
        if a.thread_id:
            th = srv.call("thread/read", {"threadId": a.thread_id, "includeTurns": True})["thread"]
            turns = th.get("turns", []) or []
            print(f"THREAD_ID={th.get('id')}")
            print(f"CWD={th.get('cwd', '')}")
            print(f"CREATED={th.get('createdAt', th.get('created_at', ''))}")
            print(f"TURNS={len(turns)}")
            if turns:
                last = turns[-1]
                print(f"LAST_TURN_STATUS={last.get('status', '?')}")
        else:
            res = srv.call("thread/list", {"limit": a.limit})
            rows = res.get("data") or res.get("threads") or []
            print(f"COUNT={len(rows)}")
            for t in rows:
                title = (t.get("title") or t.get("preview") or "")[:36]
                print(f"{(t.get('id') or '?')[:13]} | {(t.get('cwd') or '')[:40]:40s} | {title}")
    finally:
        srv.close()


def cmd_read(a):
    srv = AppServer(_safe_getcwd(), transport=a.transport)
    try:
        try:
            th = srv.call("thread/read", {"threadId": a.thread_id, "includeTurns": True})["thread"]
        except BridgeError as e:
            if "includeTurns" not in str(e):
                raise
            print("（提示：服务端不接受 includeTurns，已降级 includeTurns=False 重试，turns 可能为空）",
                  file=sys.stderr)
            th = srv.call("thread/read", {"threadId": a.thread_id, "includeTurns": False})["thread"]
        if a.raw:
            print(json.dumps(th, ensure_ascii=False, indent=1))
            return
        convo = extract_text_from_turns(th.get("turns"))
        print(f"THREAD_ID={th.get('id')}")
        print(f"CWD={th.get('cwd', '')}")
        print(f"MESSAGES={len(convo)}")
        print("--- CONVERSATION ---")
        for role, txt in convo:
            tag = {"user": "[用户/桥]", "agent": "[Codex]", "cmd": "[命令]"}.get(role, f"[{role}]")
            print(f"{tag} {txt}")
    finally:
        srv.close()


def cmd_steer(a):
    if a.transport is None:
        raise BridgeError("后台写操作必须显式 --transport daemon 或 stdio，且先确认会话归属；App 原生任务请用界面输入")
    require_background_writer(a.thread_id)
    model = effective_model(a.model)
    if a.turn_id and not a.live:
        raise BridgeError("--turn-id 仅配合 --live 使用（对进行中 turn 纠偏）；completed 后追加请去掉 --turn-id")
    if a.live and a.transport != "daemon":
        raise BridgeError("--live 原生 steer 仅 daemon transport 可用（stdio 进程死=turn 断，够不着进行中的 turn）")
    if a.live and not a.turn_id:
        raise BridgeError("--live 需要 --turn-id <进行中的 turn>（dispatch --async 输出的 TURN_ID）")
    srv = AppServer(_safe_getcwd(), transport=a.transport)
    try:
        if a.live:
            # 原生 turn/steer：对进行中的 turn 中途纠偏（v1.3，daemon）
            srv.call("turn/steer", {"threadId": a.thread_id, "expectedTurnId": a.turn_id,
                                    "input": [{"type": "text", "text": a.text}]}, timeout=30)
            print(f"THREAD_ID={a.thread_id}")
            print(f"TURN_ID={a.turn_id}")
            print("STEER=sent（纠偏已注入，turn 继续跑；read 收终态）")
            return
        # 历史会话不在新进程内存里，先 resume 加载再追加 turn（2026-10-02 QA 实测坑）
        srv.call("thread/resume", {"threadId": a.thread_id, "model": model})
        params = {"threadId": a.thread_id, "model": model,
                  "input": [{"type": "text", "text": a.text}]}
        if a.effort:
            params["effort"] = a.effort
        turn = srv.call("turn/start", params)["turn"]
        final, reply, items = srv.wait_turn(a.timeout, a.thread_id, turn_id=turn.get("id"))
        check_turn_ok(final, a.thread_id)
        print(f"THREAD_ID={a.thread_id}")
        print(f"TURN_ID={turn.get('id', '')}")
        print(f"STATUS={final.get('status', '?')}")
        print(f"DURATION_MS={final.get('durationMs', 0)}")
        print("--- REPLY ---")
        print(reply if reply else "(无文本回复)")
    finally:
        srv.close()


def cmd_interrupt(a):
    if a.transport is None:
        raise BridgeError("后台急停必须显式 --transport daemon，且先确认会话归属；App 原生任务请用界面停止按钮")
    require_background_writer(a.thread_id)
    if a.transport != "daemon":
        raise BridgeError("interrupt 仅 daemon transport 可用（stdio 够不着进行中的 turn）")
    srv = AppServer(_safe_getcwd(), transport="daemon")
    try:
        srv.call("turn/interrupt", {"threadId": a.thread_id, "turnId": a.turn_id}, timeout=30)
        print(f"THREAD_ID={a.thread_id}")
        print(f"TURN_ID={a.turn_id}")
        print("INTERRUPT=sent（急停已下达，status/read 复核终态）")
    finally:
        srv.close()


def main():
    def positive_int(x):
        try:
            n = int(x)
        except ValueError:
            raise argparse.ArgumentTypeError(f"必须是正整数（收到 {x!r}）")
        if n < 1:
            raise argparse.ArgumentTypeError(f"必须是正整数（收到 {x}）")
        return n

    def positive_float(x):
        try:
            n = float(x)
        except ValueError:
            raise argparse.ArgumentTypeError(f"必须是正有限数（收到 {x!r}）")
        if not math.isfinite(n) or n <= 0:
            raise argparse.ArgumentTypeError(f"必须是正有限数（收到 {x!r}）")
        return n

    def nonnegative_int(x):
        try:
            n = int(x)
        except ValueError:
            raise argparse.ArgumentTypeError(f"必须是非负整数（收到 {x!r}）")
        if n < 0:
            raise argparse.ArgumentTypeError(f"必须是非负整数（收到 {x!r}）")
        return n

    ap = argparse.ArgumentParser(
        prog="codex_bridge",
        description="WorkBuddy ↔ Codex 通信桥（默认 App 原生派单，后台显式选择）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_transport(p, dispatch=False, write=False):
        p.add_argument("--transport", choices=["desktop", "daemon", "stdio"] if dispatch else ["daemon", "stdio"],
                       default="desktop" if dispatch else None if write else "daemon",
                       help="desktop=请求 App 草稿，WB 核对，用户亲手提交（派单默认）；"
                            "daemon/stdio=独立后台，不能保证 App 实时可用")

    d = sub.add_parser("dispatch", help="默认请求 App 原生草稿；用户亲手提交后 desktop-confirm")
    d.add_argument("--cwd", default=DEFAULT_CWD,
                   help="锚定项目目录（默认：CODEX_BRIDGE_CWD 环境变量或用户主目录）")
    d.add_argument("--name", default=None,
                   help="任务主题（正式单必填）——自动拼命名前缀【WB委派·yymmdd·主题】（任务文本首行已含同款标题时不重复拼接）")
    d.add_argument("--task", required=True, help="任务文本（大任务请改用任务包文件化，本参数只写指引句）")
    d.add_argument("--model", default=None, help="覆写模型（优先级：--model > config.json.default_model > gpt-6.1-sol）")
    d.add_argument("--effort", default=None, help="请求的准确推理档；desktop 由 GUI 选择后核验，省略则沿用 App 当前选择")
    d.add_argument("--async", dest="async_fire", action="store_true",
                   help="desktop 准备阶段本身立即返回；daemon 异步执行；stdio 不支持")
    d.add_argument("--prepare-only", action="store_true", help="只生成 desktop 回执和 URL，不打开 App（诊断用，未派单）")
    d.add_argument("--ephemeral", action="store_true", help="内存会话（冒烟专用，不落盘不可注册）")
    vis = d.add_mutually_exclusive_group()
    vis.add_argument("--open-bg", action="store_true",
                     help="仅后台 transport：请求 open -g 导航，不保证 App 可见或可输入")
    vis.add_argument("--no-open", action="store_true",
                     help="仅后台 transport：不导航 App")
    d.add_argument("--timeout", type=positive_int, default=600, help="等结果超时秒数（默认 600）")
    add_transport(d, dispatch=True)
    d.set_defaults(fn=cmd_dispatch)

    dc = sub.add_parser("desktop-confirm", help="只读确认 GUI 已提交的新任务；不能代替视觉验收")
    dc.add_argument("receipt_path", help="dispatch 返回的 receipt_path")
    dc.add_argument("--thread-id", default=None, help="可选：GUI 已明确看到的任务 ID；仍须核验身份")
    dc.set_defaults(fn=cmd_desktop_confirm)

    s = sub.add_parser("status", help="查会话状态（无参=最近列表，有参=该会话详情）")
    s.add_argument("thread_id", nargs="?", default=None)
    s.add_argument("--limit", type=positive_int, default=10)
    add_transport(s)
    s.set_defaults(fn=cmd_status)

    r = sub.add_parser("read", help="读回指定会话的完整对话")
    r.add_argument("thread_id")
    r.add_argument("--raw", action="store_true", help="打印原始 JSON")
    add_transport(r)
    r.set_defaults(fn=cmd_read)

    st = sub.add_parser("steer", help="追加指令/纠偏：默认=completed 后追加；--live=进行中 turn 原生纠偏（仅 daemon）")
    st.add_argument("thread_id")
    st.add_argument("--text", required=True)
    st.add_argument("--live", action="store_true", help="对进行中 turn 原生纠偏（仅 daemon，需 --turn-id）")
    st.add_argument("--turn-id", default=None, help="进行中 turn 的 id（dispatch --async 输出的 TURN_ID）")
    st.add_argument("--model", default=None)
    st.add_argument("--effort", default=None)
    st.add_argument("--timeout", type=positive_int, default=600)
    add_transport(st, write=True)
    st.set_defaults(fn=cmd_steer)

    it = sub.add_parser("interrupt", help="急停进行中 turn（仅 daemon）")
    it.add_argument("thread_id")
    it.add_argument("turn_id")
    add_transport(it, write=True)
    it.set_defaults(fn=cmd_interrupt)

    w = sub.add_parser("watch", help="尾随会话 rollout 直播（只读零干扰，daemon/desktop 通吃）")
    w.add_argument("thread_id")
    w.add_argument("--follow", action="store_true", help="跟随绑定的 turn，终态或期限停止")
    w.add_argument("--turn-id", default=None, help="明确绑定目标 turn；省略先用锚点目标，再取首次历史最后开跑轮次")
    w.add_argument("--since", type=nonnegative_int, default=0, help="字节偏移；大于 0 必须附上一轮 --anchor")
    w.add_argument("--anchor", default=None, help="上一轮 ANCHOR JSON，核验线程/文件身份和前缀后续播")
    w.add_argument("--timeout", type=positive_float, default=1800, help="包含文件等待的总期限秒数（默认 1800：匹配 25 分钟级典型任务）")
    w.add_argument("--idle-timeout", type=positive_float, default=900, help="follow 停更上限秒数（默认 900，覆盖高推理档长静默窗），到时状态未知")
    w.add_argument("--filter", choices=["agentMessage", "command", "file"], default=None,
                   help="只看某类消息（默认全量）")
    w.add_argument("--interval", type=positive_float, default=1.2, help="正有限轮询间隔秒（默认 1.2）")
    w.add_argument("--wait-file", type=positive_float, default=20, help="等 rollout 身份完整落盘秒数（默认 20）")
    w.set_defaults(fn=cmd_watch)

    args = ap.parse_args()
    try:
        code = args.fn(args)
        if code:
            sys.exit(code)
    except (BridgeError, DesktopDispatchError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("ERROR: 用户中断", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
