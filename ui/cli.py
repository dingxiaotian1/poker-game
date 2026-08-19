"""命令行交互界面模块（v2：分区布局 + 快捷菜单 + 智能聊天）。

负责将服务器下发的状态渲染为可读的桌台视图，并将用户输入解析为行动命令。
采用"输入线程 + 主循环消息轮询"模型：输入线程持续读取 stdin 并入队，
主循环轮询客户端消息与命令队列，刷新界面。

v2 交互升级（本版本新增）：
1. 快捷操作菜单：按功能分类展示常用命令（行动区 [1]~[4]、房间区 [5]~[9]），
   输入数字编号即可执行，无需记忆命令词；加注支持 "3 250" 直接带金额。
2. 智能聊天输入：保留"直接输入文字即聊天"，并新增自然语言意图识别——
   轮到行动时输入"跟注/加注 100/全下/弃牌"等可直接触发行动；
   任何时刻输入"开始游戏/下一局/玩家/帮助"等可触发房间操作。
3. 分区界面布局：游戏进度区（状态/公共牌/玩家/底牌/行动引导）与
   消息聊天区（日志与聊天）用分隔线明确分离，避免信息混杂。
4. 进度可视化增强：状态栏集中展示底池/盲注/我的筹码/最高注等关键指标，
   当前行动者与轮次引导高亮，重要提示一目了然。
5. 房间重置功能：[9]重置房间（仅房主可用），执行前弹出确认提示防止误操作；
   确认后服务器清零对局数、恢复所有玩家筹码，同时保留玩家列表与房间规则。

传统 / 命令（/fold /call /raise 等）全部保留，兼容旧习惯。
"""
from __future__ import annotations

import queue
import re
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from core.card import RANK_DISPLAY, SUIT_SYMBOL, Card
from network.client import ClientError, GameClient

# 两次全屏重绘的最小时间间隔（秒）：
# 【重点注释】没有节流时，服务器每来一条消息（玩家行动、聊天、进出房）都会
# 触发一次清屏重绘，密集消息会让屏幕"持续闪烁刷新"，并反复清掉用户正在输入
# 的行。节流后，间隔内的多次消息会合并为一次渲染，交互更稳定。
MIN_RENDER_INTERVAL: float = 0.4

# 快捷菜单固定编号区间：
# 【重点注释】编号固定便于玩家记忆与肌肉记忆：1~4 永远是行动类，
# 5~9 永远是房间类。菜单项随游戏状态显示/隐藏，但编号不漂移。
MENU_ACTION_KEYS: Tuple[int, ...] = (1, 2, 3, 4)  # 行动区：弃牌/跟注(让牌)/加注/全下
MENU_ROOM_KEYS: Tuple[int, ...] = (5, 6, 7, 8, 9)  # 房间区：开始/玩家/帮助/退出/重置

# 加注金额提取正则（自然语言/菜单共用）：
# 匹配 "加注 100" / "加注到100" / "加到 100" / "raise 100" / "加100" 等写法，
# 仅提取第一个数字作为加注目标金额。
_RAISE_RE = re.compile(r"(?:加注到|加注|加到|加|raise)\s*(\d+)", re.IGNORECASE)


# ---------- 终端样式（信息层级） ----------

class Style:
    """ANSI 终端样式常量，用于提示信息的层级区分。

    层级约定：
    - 红色：错误/警告，最需注意
    - 黄色加粗：轮到你的行动，最核心引导
    - 青色：与"你"相关的信息（你的底牌、你的身份）
    - 绿色：系统流程提示（加入/离开/开局等）
    - 默认无色：普通游戏事件流（翻牌、跟注等）
    """

    RESET = "\x1b[0m"
    BOLD = "\x1b[1m"
    RED = "\x1b[31m"
    GREEN = "\x1b[32m"
    YELLOW = "\x1b[33m"
    CYAN = "\x1b[36m"
    WHITE = "\x1b[37m"
    # RESET, BOLD, RED, GREEN, YELLOW, CYAN = WHITE, WHITE, WHITE, WHITE, WHITE, WHITE
    


def _style(text: str, codes: str) -> str:
    """为文本包裹 ANSI 样式码，实现重点提示突出显示。

    Args:
        text: 原始文本。
        codes: 样式码组合（如 Style.BOLD + Style.YELLOW）。

    Returns:
        包裹了样式码的文本；在支持 ANSI 的终端中按样式显示，
        不支持时原样输出（转义码不影响文本内容）。
    """
    return f"{codes}{text}{Style.RESET}"


# ---------- 渲染辅助 ----------

def _card_display(data: dict) -> str:
    """将牌字典渲染为显示字符串。

    Args:
        data: 形如 {'rank': 14, 'suit': 'S'} 的字典。

    Returns:
        形如 'A♠' 的字符串。
    """
    rank = int(data.get("rank", 0))
    suit = str(data.get("suit", ""))
    return f"{RANK_DISPLAY.get(rank, '?')}{SUIT_SYMBOL.get(suit, '?')}"


def _cards_display(cards: List[dict]) -> str:
    """渲染多张牌为空格分隔的字符串。空列表显示为 '（无）'。"""
    if not cards:
        return "（无）"
    return " ".join(_card_display(c) for c in cards)


def _state_name_cn(state_name: str) -> str:
    """将英文状态名转为中文显示。"""
    mapping = {
        "WAITING": "等待中",
        "PREFLOP": "翻牌前",
        "FLOP": "翻牌",
        "TURN": "转牌",
        "RIVER": "河牌",
        "SHOWDOWN": "摊牌",
        "HAND_OVER": "本局结束",
    }
    return mapping.get(state_name, state_name)


def clear_screen() -> None:
    """清空终端屏幕。

    【重点注释】为什么不用 os.system("cls")：
    1. os.system 每次调用都会派生一个 cmd.exe 子进程，渲染频繁时会不断创建
       子进程，与后台输入线程争抢控制台句柄；
    2. 在 Windows 控制台中，cmd 子进程创建/销毁期间可能让阻塞中的
       sys.stdin.readline() 返回空或抛异常，导致输入线程静默退出，用户表现
       为"无法输入、输入内容被刷新清除、操作频繁中断"。
    这里改用 ANSI 转义序列（\\x1b[2J 清除全部内容 + \\x1b[H 光标归位），
    现代终端（Windows 10+ cmd、Windows Terminal、IDE 集成终端）均支持，
    且不派生任何子进程，输入线程不受干扰。
    """
    # 写入 ANSI 清屏指令并刷新缓冲区
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.flush()


# ---------- 快捷菜单数据结构 ----------

class MenuItem:
    """快捷操作菜单项。

    Attributes:
        key: 固定编号（1~8，见 MENU_ACTION_KEYS / MENU_ROOM_KEYS）。
        label: 菜单项显示文本（如 "跟注(20)"）。
        cmd: 触发后执行的命令名（fold/call/check/raise/all_in/start/players/help/quit）。
        arg: 固定参数（如 raise 时可为空，等待用户输入金额）。
        enabled: 当前是否可用（根据游戏状态判定，如非房主时"开始游戏"不可用）。
    """

    def __init__(
        self,
        key: int,
        label: str,
        cmd: str,
        arg: str = "",
        enabled: bool = True,
    ) -> None:
        """初始化菜单项。

        Args:
            key: 固定编号。
            label: 显示文本。
            cmd: 命令名。
            arg: 固定参数。
            enabled: 是否可用。
        """
        self.key: int = key
        self.label: str = label
        self.cmd: str = cmd
        self.arg: str = arg
        self.enabled: bool = enabled


# ---------- 主 CLI 类 ----------

class PokerCLI:
    """命令行交互主控。

    持有一个 GameClient，负责渲染状态、读取输入、派发命令。

    Attributes:
        client: 关联的游戏客户端。
        game_log: 最近的牌局事件日志，用于回显（与聊天分离）。
        chat_log: 最近的聊天消息，用于回显（与游戏事件分离）。
        running: 主循环是否继续运行。
    """

    def __init__(self, client: GameClient) -> None:
        """初始化 CLI。

        Args:
            client: 已连接（或即将连接）的游戏客户端。
        """
        self.client: GameClient = client
        # 牌局事件回显缓冲（盲注/下注/摊牌/系统提示等），保留最近 40 条
        self.game_log: List[str] = []
        # 聊天回显缓冲（玩家发言），保留最近 30 条；与 game_log 分离
        self.chat_log: List[str] = []
        # 命令队列：输入线程写入，主循环消费
        self._command_queue: "queue.Queue[str]" = queue.Queue()
        self.running: bool = True
        # 输入线程引用
        self._input_thread: Optional[threading.Thread] = None
        # 是否需要重绘
        self._dirty: bool = True
        # 上次全屏渲染的时间戳（time.monotonic 单调时钟），用于渲染节流
        self._last_render: float = 0.0
        # 【重点注释】重置房间的待确认标记：
        # 房主触发 [9]重置房间（或 /reset）后置 True，进入"确认等待"状态，
        # 此时所有输入都只接受 y/确认 或 n/取消，防止误操作清空整桌数据；
        # 确认后发送 reset_req，取消或超时则复位。
        self._pending_reset_confirm: bool = False

    # ---------- 主循环 ----------

    def run(self) -> None:
        """启动主交互循环。

        流程：启动输入线程 → 循环轮询消息与命令 → 刷新界面。
        收到断连或 /quit 命令时退出。
        """
        # 启动后台输入线程读取用户输入
        self._input_thread = threading.Thread(target=self._input_loop, daemon=True)
        self._input_thread.start()

        # 首次渲染欢迎信息
        print("=" * 54)
        print("  欢迎来到局域网德州扑克！输入 /help 查看帮助，或直接输入数字选择菜单")
        print("=" * 54)

        # 主循环：以 0.2s 超时轮询消息，期间也能及时处理命令
        while self.running:
            # 处理所有待处理的服务器消息
            messages = self.client.drain_messages()
            if messages:
                handled = False
                try:
                    for msg in messages:
                        if self._handle_message(msg):
                            handled = True
                except Exception as exc:  # pylint: disable=broad-except
                    # 【重点注释】兜底：单条消息处理异常不应让整个进程崩溃退出，
                    # 记录错误提示后继续运行，避免"莫名中断"
                    self._add_log(f"[消息处理错误] {exc}")
                # 收到状态/底牌/回合消息时重绘
                if handled:
                    self._dirty = True

            # 处理用户命令
            while True:
                try:
                    line = self._command_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle_command(line)
                except Exception as exc:  # pylint: disable=broad-except
                    # 与上面同理：命令处理异常只提示，不中断主循环
                    self._add_log(f"[命令处理错误] {exc}")
                self._dirty = True

            # 有变化时刷新界面（带节流，避免高频清屏重绘）
            if self._dirty:
                # 单调时钟取当前时间，与上次渲染时间比较是否达到最小间隔
                now = time.monotonic()
                if now - self._last_render >= MIN_RENDER_INTERVAL:
                    try:
                        self._render()
                    except Exception as exc:  # pylint: disable=broad-except
                        # 【重点注释】渲染异常兜底：界面渲染不应导致进程退出，
                        # 打印错误后继续下一轮，保证程序可用
                        print(f"[渲染错误] {exc}")
                    # 无论成功与否都更新节流时间与脏标记，防止持续重试
                    self._last_render = now
                    self._dirty = False
                # 未到节流间隔时保留 _dirty，等待下一轮循环再渲染
                else:
                    # 顺带让出 CPU，避免极端情况下忙转
                    time.sleep(0.05)

            # 短暂休眠避免 CPU 空转
            try:
                # 用一个小超时阻塞等待新消息，降低 CPU 占用
                msg = self.client.poll_message(timeout=0.2)
                if msg is not None:
                    if self._handle_message(msg):
                        self._dirty = True
            except Exception:  # pylint: disable=broad-except
                pass

        # 退出前断开连接
        self.client.disconnect()
        print("已退出游戏，再见！")

    def _input_loop(self) -> None:
        """输入线程：循环读取 stdin 并入队。

        使用 readline 而非 input()，因为 input 在子线程中行为不稳定。
        线程与主循环共享 self.running 标志：收到 /quit 命令或断开时主循环
        退出，本线程随之结束。

        EOF 处理策略：
        - Windows 控制台在清屏/重绘等操作后，readline 可能短暂返回空（瞬时
          EOF），若立即判定为输入流关闭，会把用户正常对局误判为"莫名退出"。
        - 因此采用"连续多次确认"机制：连续读到空输入达到 EOF_CONFIRM_LIMIT
          次，才视为输入流真正关闭；期间任一时刻读到有效内容即重置计数，
          恢复正常读取。退出前会打印明确提示，避免用户困惑。
        """
        # 连续读到空输入（EOF）的次数计数
        eof_streak: int = 0
        # 【重点注释】连续 EOF 次数上限：Windows 控制台清屏重绘后可能出现
        # 1-2 次瞬时空读，设为 3 次可在"防误退出"与"能正常退出"间取得平衡
        EOF_CONFIRM_LIMIT = 3
        # 每次 EOF 确认之间的等待时间（秒）：给输入流留出恢复时间
        EOF_RETRY_WAIT = 0.5

        while self.running:
            try:
                line = sys.stdin.readline()
            except (OSError, ValueError) as exc:
                # 【重点注释】Windows 控制台在清屏/渲染等操作后，stdin 读取
                # 可能短暂抛异常（如句柄无效）。记录原因并短暂等待后重试，
                # 而不是静默退出——否则用户会突然失去输入能力，表现为
                # "无法输入任何内容"。
                print(f"[输入异常] {exc}，正在恢复输入...", file=sys.stderr)
                time.sleep(0.2)
                continue
            if not line:
                # 读到空：疑似 EOF，先累加计数并等待重试，不立即退出
                eof_streak += 1
                if eof_streak == 1:
                    # 第一次空读：提示正在等待恢复，让用户知道程序没死
                    print("[提示] 输入流出现空读，等待输入恢复...", file=sys.stderr)
                elif eof_streak >= EOF_CONFIRM_LIMIT:
                    # 【重点注释】连续多次确认 EOF，判定输入流真正关闭。
                    # 退出前给出明确提示，避免用户困惑"为何莫名退出"
                    if self.running:
                        print("[提示] 输入流已关闭，即将退出游戏...", file=sys.stderr)
                        self._command_queue.put("/quit")
                    break
                # 本次空读可能是瞬时干扰，等待片刻后重试
                time.sleep(EOF_RETRY_WAIT)
                continue
            # 读到有效内容，说明输入流已恢复，重置 EOF 计数
            eof_streak = 0
            line = line.rstrip("\r\n")
            if line:
                self._command_queue.put(line)

    # ---------- 消息处理 ----------

    def _handle_message(self, msg: dict) -> bool:
        """处理一条入站消息，返回是否需要重绘。

        Args:
            msg: 消息字典。

        Returns:
            True 表示该消息触发了界面变化需重绘。
        """
        msg_type = msg.get("type", "")

        if msg_type == "state":
            # 状态更新：客户端已缓存，这里只需触发重绘
            return True
        if msg_type == "deal_hole":
            # 底牌与"你"直接相关，用青色加粗突出显示
            self._add_log(
                _style("你的底牌: " + _cards_display(msg.get("cards", [])), Style.CYAN + Style.BOLD)
            )
            return True
        if msg_type == "turn":
            opts = msg.get("options", {})
            if opts.get("can_act"):
                # 轮到自己是最高优先级引导，用黄色加粗
                self._add_log(_style(">>> 轮到你行动 <<<（输入数字或自然语言）", Style.YELLOW + Style.BOLD))
            return True
        if msg_type == "log":
            # 游戏事件流（盲注、弃牌、翻牌、摊牌等），默认样式展示
            self._add_log(str(msg.get("message", "")))
            return True
        if msg_type == "error":
            # 错误提示用红色，最醒目的警示
            self._add_log(_style("[错误] " + str(msg.get("message", "")), Style.RED))
            return True
        if msg_type == "chat_bc":
            sender = msg.get("sender", "?")
            text = msg.get("text", "")
            # 聊天以昵称作前缀，便于区分消息来源；发送者名字用青色突出，
            # 房主消息附带专属标记（房主身份对所有玩家一致可见）。
            # 聊天写入独立缓冲（_add_chat），与牌局事件分区显示
            host_mark = " (房主)" if sender == self._host_name(self.client.state) else ""
            if host_mark:
                host_mark = _style(host_mark, Style.YELLOW + Style.BOLD)
            sender_cn = _style(f"[{sender}]", Style.CYAN)
            self._add_chat(f"{sender_cn}{host_mark} {text}")
            return True
        if msg_type == "player_joined":
            name = msg.get("name", "?")
            count = msg.get("player_count", 0)
            # 系统流程提示用绿色
            self._add_log(_style(f"[系统] {name} 加入了房间（当前 {count} 人）", Style.GREEN))
            return True
        if msg_type == "player_left":
            name = msg.get("name", "?")
            count = msg.get("player_count", 0)
            self._add_log(_style(f"[系统] {name} 离开了房间（剩余 {count} 人）", Style.GREEN))
            return True
        if msg_type == "hand_over":
            # 展示本局结果摘要并引导进入下一局
            summary = str(msg.get("summary", ""))
            self._add_log(_style("──── 本局结束 ────", Style.BOLD))
            if summary:
                self._add_log(_style(f"[结果] {summary}", Style.YELLOW + Style.BOLD))
            self._add_log("房主输入 5 或 /start 开始下一局")
            return True
        if msg_type == "reset_ok":
            # 重置成功：服务器随后会广播最新 state（局数/筹码已复位），
            # 此处仅提示用户重置完成，状态刷新交给 state 消息
            self._add_log(
                _style("[系统] 房间已重置：对局数清零，所有玩家筹码恢复初始值", Style.GREEN)
            )
            return True
        if msg_type == "reset_fail":
            # 重置失败（如房主已交接、权限不足）：红色醒目提示
            self._add_log(_style("[错误] " + str(msg.get("reason", "重置失败")), Style.RED))
            return True
        if msg_type == "_disconnected":
            # 【重点注释】显示断开原因（来自 reader 线程记录），
            # 避免任何断开都表现为"莫名退出"，便于用户判断是网络问题、
            # 服务器关闭还是协议错误
            reason = str(msg.get("reason", "未知原因"))
            self._add_log(_style(f"[系统] 与服务器断开连接（{reason}）", Style.RED))
            self.running = False
            return True
        if msg_type == "_reconnecting":
            # 【重点注释】网络异常后客户端进入自动重连（指数退避），
            # 提示用户无需操作，等待自动恢复
            self._add_log(_style("[系统] 连接断开，正在自动重连...", Style.YELLOW))
            return True
        if msg_type == "_reconnected":
            # 重连成功：重新回到游戏中，提示用户当前状态已恢复
            self._add_log(_style("[系统] 重连成功，已重新加入房间！", Style.GREEN))
            return True
        if msg_type == "kick":
            self._add_log(_style("[系统] 你被踢出: " + str(msg.get("reason", "")), Style.RED))
            self.running = False
            return True
        return False

    def _add_log(self, text: str) -> None:
        """添加一条牌局事件/系统日志，最多保留 40 条。"""
        self.game_log.append(text)
        if len(self.game_log) > 40:
            # 保留最近 40 条，避免无限增长
            self.game_log = self.game_log[-40:]

    def _add_chat(self, text: str) -> None:
        """添加一条聊天消息到聊天缓冲，最多保留 30 条。

        聊天与牌局事件分区存储（self.chat_log / self.game_log），
        渲染时各自独立展示，互不混入。
        """
        self.chat_log.append(text)
        if len(self.chat_log) > 30:
            # 保留最近 30 条，避免无限增长
            self.chat_log = self.chat_log[-30:]

    # ---------- 快捷菜单 ----------

    def _build_menu(self) -> List[MenuItem]:
        """根据当前游戏状态构建快捷操作菜单。

        菜单按功能分为两组，编号固定：
        - 行动区 [1]~[4]：弃牌 / 跟注(或让牌) / 加注 / 全下，仅轮到自己时可用；
        - 房间区 [5]~[8]：开始游戏 / 玩家列表 / 帮助 / 退出，随时可用
          （"开始游戏"仅房主可用）。

        Returns:
            菜单项列表（固定顺序，编号与位置一一对应）。
        """
        items: List[MenuItem] = []
        # 读取轮到自己时的可行动作列表（服务器下发的权威数据）
        opts = self.client.turn_options
        options: List[str] = opts.get("options", []) if opts else []
        can_act: bool = bool(opts and opts.get("can_act"))

        # ---- 行动区：轮到自己时才可用 ----
        # [1] 弃牌：任何时候轮到都可以弃牌
        items.append(MenuItem(1, "弃牌", "fold", enabled=can_act and "fold" in options))
        # [2] 跟注/让牌：无人加注时可让牌，否则需跟注
        if can_act and "call" in options:
            call_amount = int(opts.get("call_amount", 0) or 0)
            items.append(MenuItem(2, f"跟注({call_amount})", "call", enabled=True))
        elif can_act and "check" in options:
            items.append(MenuItem(2, "让牌", "check", enabled=True))
        else:
            items.append(MenuItem(2, "跟注/让牌", "call", enabled=False))
        # [3] 加注：显示合法金额范围，输入 "3 <金额>" 执行
        if can_act and "raise" in options:
            min_raise = int(opts.get("min_raise_to", 0) or 0)
            max_raise = int(opts.get("max_raise_to", 0) or 0)
            items.append(MenuItem(3, f"加注({min_raise}~{max_raise})", "raise", enabled=True))
        else:
            items.append(MenuItem(3, "加注", "raise", enabled=False))
        # [4] 全下
        items.append(MenuItem(4, "全下", "all_in", enabled=can_act and "all_in" in options))

        # ---- 房间区：随时可用，其中"开始游戏"与"重置房间"仅房主 ----
        # [5] 开始游戏：房主开局/开下一局
        items.append(MenuItem(5, "开始游戏", "start", enabled=self.client.is_host))
        # [6] 玩家列表
        items.append(MenuItem(6, "玩家列表", "players", enabled=True))
        # [7] 帮助
        items.append(MenuItem(7, "帮助", "help", enabled=True))
        # [8] 退出
        items.append(MenuItem(8, "退出", "quit", enabled=True))
        # [9] 重置房间：清零对局数与筹码，恢复初始状态（仅房主可用）
        items.append(MenuItem(9, "重置房间", "reset", enabled=self.client.is_host))
        return items

    def _handle_menu_input(self, text: str) -> bool:
        """处理快捷菜单选择（数字编号输入）。

        支持两种格式：
        - "3"     ：执行编号 3 的菜单项（加注项会提示补充金额）；
        - "3 250" ：执行编号 3 的菜单项并携带金额参数 250。

        Args:
            text: 用户输入的原始文本（已确认以数字开头）。

        Returns:
            True 表示已作为菜单选择处理；False 表示不匹配任何编号。
        """
        # 拆分为 "编号 参数..." 两段
        tokens = text.split()
        if not tokens or not tokens[0].isdigit():
            return False
        num = int(tokens[0])
        # 在当前菜单中查找对应编号的项
        item = next((it for it in self._build_menu() if it.key == num), None)
        if item is None:
            self._add_log(f"[菜单] 无效选项 {num}，请输入菜单中显示的编号")
            return True
        if not item.enabled:
            self._add_log(f"[菜单] 「{item.label}」当前不可用")
            return True
        # 加注必须带金额：若用户只输入编号（如 "3"），提示补充金额写法
        arg = item.arg
        if item.cmd == "raise" and len(tokens) < 2:
            self._add_log(f"[菜单] 请补充加注金额，例如: {num} 250 或 /raise 250")
            return True
        # 输入了额外参数（如 "3 250"），用该参数覆盖菜单项固定参数
        if len(tokens) > 1:
            arg = tokens[1]
        # 统一走命令执行入口（与手动/自然语言共用）
        self._execute_command(item.cmd, arg)
        return True

    # ---------- 智能聊天输入（自然语言意图识别） ----------

    def _parse_intent(self, text: str) -> Optional[Tuple[str, str]]:
        """自然语言意图识别：尝试把一句日常用语转换为游戏命令。

        识别优先级与触发条件：
        1. 行动类（"跟注/加注 100/全下/弃牌"等）——仅在轮到自己行动时识别，
           且优先于房间类（避免"不玩了"在自己回合被误判为退出游戏）；
           【重点注释】这是关键防误判设计：若不在自己回合也识别"跟注"等词，
           玩家随口一句聊天（如"这把我跟注了"）就会被误发成行动指令；
        2. 房间管理类（"开始/下一局/玩家/帮助/退出"等）——任何时刻都识别，
           因为这类词作为聊天内容发送没有意义；
        3. 加注金额优先于普通行动词匹配（"加注 100"必须解析出金额）。

        Args:
            text: 用户输入的原始文本。

        Returns:
            (命令名, 参数) 元组；无法识别时返回 None（由调用方当作聊天发送）。
        """
        # 统一小写便于英文关键词匹配
        t = text.strip().lower()
        if not t:
            return None

        # ---- 行动类（优先，仅轮到自己时识别）----
        # 【重点注释】行动类必须先于房间类匹配：例如"不玩了"在轮到自己时
        # 应理解为"弃牌(fold)"，若先匹配房间类会被误判为"退出游戏(quit)"。
        # 同时，"退出/离开"等词不在行动类关键词中，仍会正常回落为退出指令。
        if self.client.is_my_turn:
            # 加注类优先：用正则提取金额（如"加注 100""加到100""raise 50"）
            raise_match = _RAISE_RE.search(t)
            if raise_match:
                return ("raise", raise_match.group(1))
            action_rules: Tuple[Tuple[Tuple[str, ...], str, str], ...] = (
                (("弃牌", "fold", "不跟", "不要了", "认输", "不玩了"), "fold", ""),
                (("让牌", "过牌", "check", "过", "不叫"), "check", ""),
                (("跟注", "call", "跟", "跟上", "跟了"), "call", ""),
                (("全下", "allin", "all-in", "梭哈", "推了", "推all"), "all_in", ""),
            )
            for keywords, cmd, arg in action_rules:
                if any(kw in t for kw in keywords):
                    return (cmd, arg)

        # ---- 房间管理类（任何时刻可识别）----
        # 注意：这些词作为聊天内容发送没有意义，因此不受"是否轮到自己"限制。
        room_rules: Tuple[Tuple[Tuple[str, ...], str, str], ...] = (
            (("开始", "开局", "start", "下一局", "再来一局", "重新开始", "新一局"), "start", ""),
            (("玩家", "players", "看看有谁", "谁在"), "players", ""),
            (("帮助", "help", "怎么玩", "命令"), "help", ""),
            (("退出", "quit", "离开", "再见", "拜拜"), "quit", ""),
        )
        for keywords, cmd, arg in room_rules:
            if any(kw in t for kw in keywords):
                return (cmd, arg)
        return None

    # ---------- 命令处理 ----------

    def _handle_command(self, line: str) -> None:
        """解析并执行一条用户输入。

        输入类型判定（按优先级）：
        1. 数字开头 → 快捷菜单选择（如 "3 250" = 加注 250）；
        2. "/" 开头 → 传统命令（兼容旧习惯，如 /call）；
        3. 自然语言 → 意图识别（行动词/房间管理词 → 对应命令）；
        4. 其余内容 → 当作聊天发送。

        Args:
            line: 用户原始输入行。
        """
        stripped = line.strip()
        if not stripped:
            return

        # 0. 重置确认状态：处于"等待确认重置"时，只接受 y/确认 或 n/取消，
        #    其余输入一律提示，避免在确认弹窗期间误发其他命令
        if self._pending_reset_confirm:
            self._handle_reset_confirm(stripped)
            return

        # 1. 快捷菜单选择：数字开头的输入（含 "3 250" 形式）
        if stripped.split(maxsplit=1)[0].isdigit():
            if self._handle_menu_input(stripped):
                return

        # 2. 传统 / 命令
        if stripped.startswith("/"):
            # 切分命令与参数
            parts = stripped[1:].split(maxsplit=1)
            cmd = parts[0].lower() if parts else ""
            arg = parts[1] if len(parts) > 1 else ""
            self._execute_command(cmd, arg)
            return

        # 3. 自然语言意图识别：成功转换则直接执行命令
        intent = self._parse_intent(stripped)
        if intent is not None:
            cmd, arg = intent
            self._execute_command(cmd, arg)
            return

        # 4. 未识别为命令 → 当作聊天发送
        try:
            self.client.send_chat(stripped)
        except ClientError as exc:
            self._add_log(f"[发送失败] {exc}")

    def _execute_command(self, cmd: str, arg: str = "") -> None:
        """统一执行一条命令（供菜单选择/自然语言/手动输入共用）。

        Args:
            cmd: 命令名。
            arg: 命令参数（如加注金额）。
        """
        try:
            if cmd in ("help", "h", "?"):
                self._print_help()
            elif cmd == "start":
                self.client.send_start()
            elif cmd in ("fold", "f"):
                self.client.send_action("fold")
            elif cmd in ("check", "c"):
                self.client.send_action("check")
            elif cmd in ("call", "cl"):
                self.client.send_action("call")
            elif cmd in ("raise", "r"):
                self._do_raise(arg)
            elif cmd in ("allin", "a", "all-in", "all_in"):
                self.client.send_action("all_in")
            elif cmd == "players":
                self._print_players()
            elif cmd in ("reset",):
                # 重置房间：先进入确认状态，用户确认后才真正发送请求
                self._request_reset()
            elif cmd == "chat":
                if arg:
                    self.client.send_chat(arg)
            elif cmd in ("quit", "exit", "q"):
                self.running = False
            else:
                self._add_log(f"未知命令: /{cmd}，输入 /help 查看帮助")
        except ClientError as exc:
            self._add_log(f"[发送失败] {exc}")

    def _request_reset(self) -> None:
        """请求重置房间：进入确认状态，防止误操作。

        【重点注释】重置会清空对局数与所有玩家筹码，属于高风险操作，
        因此不直接发送请求，而是先置 _pending_reset_confirm 标记并在界面
        显示确认提示；只有用户输入 y/确认 后才会真正调用 send_reset()。
        同时输入提示区会同步显示确认框（见 _render_input_hint）。
        """
        # 仅房主在服务器端才会真正放行，此处 UI 也做一次提示（菜单已禁用）
        if not self.client.is_host:
            self._add_log("[错误] 仅房间创建者可重置房间")
            return
        self._pending_reset_confirm = True
        self._add_log(
            _style(
                "⚠ 确认重置房间？对局数将清零，所有玩家筹码恢复初始值 "
                "（输入 y 确认 / n 取消）",
                Style.YELLOW + Style.BOLD,
            )
        )

    def _handle_reset_confirm(self, text: str) -> None:
        """处理重置确认输入：y/确认 发送请求，n/取消 放弃。

        处于确认状态时本方法接管所有输入（见 _handle_command），
        保证确认弹窗期间不会误发其他命令。

        Args:
            text: 用户输入的原始文本。
        """
        t = text.strip().lower()
        if t in ("y", "yes", "是", "确定", "确认", "重置"):
            # 用户确认：复位确认标记并发送重置请求（服务器还会校验房主权限）
            self._pending_reset_confirm = False
            try:
                self.client.send_reset()
            except ClientError as exc:
                self._add_log(f"[发送失败] {exc}")
                return
            self._add_log(_style("[系统] 已发送重置请求，等待服务器确认...", Style.GREEN))
        elif t in ("n", "no", "否", "取消", "不要"):
            # 用户取消：复位标记，恢复正常输入
            self._pending_reset_confirm = False
            self._add_log("已取消重置房间")
        else:
            # 其他输入：仍处于确认状态，提示用户输入 y 或 n
            self._add_log("请输入 y 确认重置 / n 取消")

    def _do_raise(self, arg: str) -> None:
        """处理加注命令，解析目标金额。

        Args:
            arg: 用户输入的金额参数字符串。
        """
        if not arg:
            self._add_log("用法: /raise <金额>，例如 /raise 100")
            return
        try:
            amount = int(arg)
        except ValueError:
            self._add_log(f"无效的金额: {arg}")
            return
        if amount <= 0:
            self._add_log("加注金额必须为正")
            return
        self.client.send_action("raise", amount)

    def _print_help(self) -> None:
        """输出帮助文本到日志区：菜单 + 命令 + 游戏流程 + 牌型大小。"""
        help_text = [
            "──── 快捷菜单 ────",
            "直接输入数字选择操作（加注示例: 3 250）",
            "行动: [1]弃牌 [2]跟注/让牌 [3]加注 [4]全下",
            "房间: [5]开始游戏 [6]玩家列表 [7]帮助 [8]退出 [9]重置房间(仅房主)",
            "",
            "──── 手动命令 ────",
            "/start        房主开始新一局",
            "/fold (f)     弃牌",
            "/check (c)    让牌",
            "/call (cl)    跟注",
            "/raise N (r N) 加注到 N",
            "/allin (a)    全下",
            "/players      列出玩家",
            "/reset        重置房间（仅房主，需二次确认）",
            "/chat <text>  发送聊天（或直接输入文字）",
            "/quit         离开并退出",
            "",
            "──── 智能聊天 ────",
            "轮到行动时可直接输入: 跟注 / 加注 100 / 全下 / 弃牌 / 让牌",
            "任意时刻可输入: 开始游戏 / 下一局 / 玩家 / 帮助 / 退出",
            "其他输入将作为聊天消息发送给房间内玩家",
            "",
            "──── 游戏流程 ────",
            "1. 房主输入 5 或 /start 开始，每位玩家发 2 张底牌（仅自己可见）",
            "2. 翻牌前 → 翻牌(3张) → 转牌(1张) → 河牌(1张) → 摊牌",
            "3. 每轮从庄家左手边顺时针行动，可选 弃牌/让牌/跟注/加注/全下",
            "4. 摊牌后牌型最大者赢得底池",
            "",
            "──── 牌型大小（从高到低）────",
            "皇家同花顺 > 同花顺 > 四条 > 葫芦 > 同花 > 顺子",
            "> 三条 > 两对 > 一对 > 高牌",
        ]
        for line in help_text:
            self._add_log(line)

    def _print_players(self) -> None:
        """列出当前桌所有玩家到日志区（含房主专属标识）。"""
        state = self.client.state
        if not state:
            self._add_log("尚未获取到桌状态")
            return
        players = state.get("players", [])
        host_name = self._host_name(state)
        self._add_log(f"---- 玩家列表（共 {len(players)} 人）----")
        for p in players:
            name = p.get("name", "?")
            # 房主标识：房主身份对所有玩家可见（不依赖本人视角）
            host_mark = " (房主)" if name == host_name else ""
            self._add_log(
                f"  #{p.get('player_id')} {name}{host_mark} "
                f"筹码={p.get('chips')} 状态="
                f"{'弃牌' if p.get('folded') else '全下' if p.get('all_in') else '在场'}"
            )

    def _host_name(self, state: Optional[dict]) -> str:
        """从状态快照解析当前房主昵称，无房主时返回空串。

        房主标识统一以服务器下发的 host_player_id 为准，
        保证 CLI 与 Web 端对房主身份的判断一致。

        Args:
            state: 服务器状态快照。

        Returns:
            房主昵称；快照为空或无房主时返回空字符串。
        """
        if not state:
            return ""
        host_id = state.get("host_player_id")
        # 遍历玩家列表找到对应 ID 的昵称（昵称唯一，服务器拒绝重名加入）
        for p in state.get("players", []):
            if p.get("player_id") == host_id:
                return p.get("name", "")
        return ""

    # ---------- 渲染（分区布局） ----------

    def _render(self) -> None:
        """渲染整个桌台界面到终端。

        布局采用"分区设计"：
        - 游戏进度区（上部）：标题栏、关键指标栏、公共牌、玩家表、
          我的底牌与行动引导；
        - 牌局事件区（中部）：独立展示盲注/下注/摊牌等游戏事件；
        - 聊天区（下部）：独立展示玩家发言（与牌局事件分离）；
        - 快捷操作区：固定编号菜单 + 输入提示行。
        """
        clear_screen()
        state = self.client.state
        if state is None:
            # 尚未收到首帧状态：只渲染提示与事件/聊天区，保持界面可用
            print(_style("正在连接服务器，等待首帧状态...", Style.GREEN))
            self._render_log_area()
            self._render_chat_area()
            self._render_menu_area()
            self._render_input_hint()
            return

        # ════ 游戏进度区 ════
        self._render_title_bar(state)     # 标题 + 阶段
        self._render_status_bar(state)    # 关键指标栏（底池/盲注/我的筹码/最高注）
        self._render_community(state)     # 公共牌
        self._render_players(state)       # 玩家表
        self._render_my_info(state)       # 我的底牌 + 行动引导
        self._render_context_hint()       # 当前状态的重要提示

        # ════ 牌局事件区（与聊天区分区隔离） ════
        self._render_log_area()
        # ════ 聊天区 ════
        self._render_chat_area()
        # ════ 快捷操作区 ════
        self._render_menu_area()
        self._render_input_hint()

    def _render_title_bar(self, state: dict) -> None:
        """渲染顶部标题栏：局数 + 当前阶段（加粗醒目）。"""
        print("╔" + "═" * 52 + "╗")
        title = f"德州扑克 · 第 {state.get('hand_number', 0)} 局"
        state_name = _state_name_cn(state.get("state_name", ""))
        # 标题与阶段组合，阶段用绿色加粗突出当前进度
        line = f"{title}    [阶段: {state_name}]"
        print(_style(f"║{line:^52}║", Style.BOLD))
        print("╚" + "═" * 52 + "╝")

    def _render_status_bar(self, state: dict) -> None:
        """渲染关键指标栏：底池/盲注/我的筹码/当前最高注。

        Args:
            state: 桌状态快照。
        """
        # 从状态中读取关键指标
        pot_total = state.get("pot", {}).get("total", 0)
        small_blind = state.get("small_blind", 0)
        big_blind = state.get("big_blind", 0)
        current_bet = state.get("current_bet", 0)
        # 我的筹码：从玩家列表中按 player_id 定位自己
        my_chips = 0
        for p in state.get("players", []):
            if p.get("player_id") == self.client.player_id:
                my_chips = p.get("chips", 0)
                break
        # 关键指标用青色标注"我的筹码"，其余为默认色
        parts = [
            f"底池: {pot_total}",
            f"盲注: {small_blind}/{big_blind}",
            _style(f"我的筹码: {my_chips}", Style.CYAN + Style.BOLD),
            f"最高注: {current_bet}",
        ]
        print("  " + "    ".join(parts))
        print("-" * 54)

    def _render_community(self, state: dict) -> None:
        """渲染公共牌区域。"""
        community = state.get("community_cards", [])
        print(f"公共牌: {_cards_display(community)}")
        print("-" * 54)

    def _render_players(self, state: dict) -> None:
        """渲染玩家列表区域（含庄家/当前行动者/自己高亮）。"""
        players = state.get("players", [])
        dealer_pos = state.get("dealer_pos", -1)
        current_player_id = state.get("current_player_id")

        print(f"{'玩家':<12} {'筹码':>6} {'本轮':>6} {'状态':<8} {'上次行动'}")
        for i, p in enumerate(players):
            name = p.get("name", "?")
            # 标记庄家(D)与当前行动者(*)
            prefix = ""
            if i == dealer_pos:
                prefix = "D"
            if p.get("player_id") == current_player_id:
                prefix += "*"
            display_name = f"{prefix}{name}"[:12]

            chips = p.get("chips", 0)
            current_bet = p.get("current_bet", 0)

            # 状态标记
            if p.get("folded"):
                status = "弃牌"
            elif p.get("all_in"):
                status = "全下"
            else:
                status = "在场"

            last_action = p.get("last_action", "")
            # 高亮自己：整行青色加粗 + <你> 标记，便于快速定位
            is_me = p.get("player_id") == self.client.player_id
            marker = " <你>" if is_me else ""
            # 房主专属标识：金色加粗，所有玩家界面一致显示（以服务器 host_player_id 为准）
            if p.get("player_id") == state.get("host_player_id"):
                marker += _style(" 房主", Style.YELLOW + Style.BOLD)
            row = (
                f"{display_name:<12} {chips:>6} {current_bet:>6} "
                f"{status:<8} {last_action}{marker}"
            )
            if is_me:
                row = _style(row, Style.CYAN + Style.BOLD)
            # 当前行动者若不是自己，用黄色标注，一眼看出"轮到谁"
            elif p.get("player_id") == current_player_id:
                row = _style(row + " ◀", Style.YELLOW)
            print(row)
        print("-" * 54)

    def _render_my_info(self, state: dict) -> None:
        """渲染我的底牌区域（青色加粗）。"""
        if self.client.hole_cards:
            cards_str = " ".join(c.display() for c in self.client.hole_cards)
            print(_style(f"你的底牌: {cards_str}", Style.CYAN + Style.BOLD))
        else:
            print("你的底牌: （尚未发牌）")

    def _render_turn_hint(self) -> None:
        """渲染行动提示：若轮到自己则显示可执行行动与金额范围。"""
        opts = self.client.turn_options
        if not opts or not opts.get("can_act"):
            return

        options = opts.get("options", [])
        call_amount = opts.get("call_amount", 0)
        min_raise = opts.get("min_raise_to", 0)
        max_raise = opts.get("max_raise_to", 0)

        # 组装简短行动指引（与快捷菜单编号对应，方便对照操作）
        hint_parts: List[str] = []
        if "fold" in options:
            hint_parts.append("1弃牌")
        if "check" in options:
            hint_parts.append("2让牌")
        if "call" in options:
            hint_parts.append(f"2跟注({call_amount})")
        if "raise" in options:
            hint_parts.append(f"3加注({min_raise}~{max_raise})")
        if "all_in" in options:
            hint_parts.append("4全下")

        # 轮到自己是最核心引导：黄色加粗突出显示
        prompt = "▶ 轮到你行动: " + "  ".join(hint_parts) + "  （输入数字或直接说话）"
        print(_style(prompt, Style.YELLOW + Style.BOLD))

    def _render_log_area(self) -> None:
        """渲染牌局事件区（盲注/下注/摊牌等游戏事件，与聊天区分隔）。"""
        # 分区分隔线：明确提示下方属于牌局事件区域
        print("─" * 54)
        print(_style("◈ 牌局事件 ◈", Style.GREEN))
        # 显示最近 10 条游戏事件日志（game_log 与 chat_log 分离存储）
        for line in self.game_log[-10:]:
            print(line)

    def _render_chat_area(self) -> None:
        """渲染聊天区（仅玩家发言，与牌局事件区分隔）。"""
        print("─" * 54)
        print(_style("◈ 聊天 ◈", Style.CYAN))
        # 显示最近 10 条聊天；空列表给出占位提示，避免误以为聊天区失效
        if not self.chat_log:
            print(_style("（暂无聊天消息，直接输入文字即可发言）", ""))
        for line in self.chat_log[-10:]:
            print(line)

    def _render_menu_area(self) -> None:
        """渲染快捷操作菜单区（按功能分类、固定编号）。"""
        print("─" * 54)
        items = self._build_menu()

        # 将菜单项按"行动/房间"两个分区组织渲染
        action_items = [it for it in items if it.key in MENU_ACTION_KEYS]
        room_items = [it for it in items if it.key in MENU_ROOM_KEYS]

        print("快捷操作（输入数字选择）:")
        # 行动区：只有启用项用青色加粗，禁用项用灰色（普通色）提示不可用
        action_parts: List[str] = []
        for it in action_items:
            if it.enabled:
                action_parts.append(_style(f"[{it.key}]{it.label}", Style.CYAN + Style.BOLD))
            else:
                action_parts.append(f"[{it.key}]{it.label}(不可用)")
        print("  行动: " + "  ".join(action_parts))

        room_parts: List[str] = []
        for it in room_items:
            if it.enabled:
                room_parts.append(_style(f"[{it.key}]{it.label}", Style.CYAN + Style.BOLD))
            else:
                room_parts.append(f"[{it.key}]{it.label}(仅房主)")
        print("  房间: " + "  ".join(room_parts))

    def _render_input_hint(self) -> None:
        """渲染输入提示区：说明当前支持的三种输入方式。

        若处于"重置确认"状态，则优先显示确认提示框（y/n），
        告知用户当前需要做的是确认还是取消重置操作。
        """
        print("─" * 54)
        self._render_context_hint()
        if self._pending_reset_confirm:
            # 【重点注释】确认提示框：重置属高风险操作，进入确认状态后
            # 输入提示行替换为明确的确认引导，防止误操作
            print(
                _style(
                    "⚠ 确认重置房间？输入 y 确认 / n 取消",
                    Style.YELLOW + Style.BOLD,
                )
            )
        else:
            print("输入: 数字=快捷操作，/命令，或直接说话聊天（/help 帮助）")

    def _render_context_hint(self) -> None:
        """根据当前游戏状态输出一行上下文引导，降低玩家理解门槛。

        不同阶段给出不同提示：
        - 等待中：告知还需几人或提示房主开局
        - 下注阶段：提示谁在行动（轮到自己时 _render_turn_hint 已引导）
        - 本局结束：提示如何开始下一局
        """
        state = self.client.state
        if state is None:
            print(_style("正在连接服务器...", Style.GREEN))
            return

        state_name = state.get("state_name", "")
        players = state.get("players", [])
        current_player_id = state.get("current_player_id")
        min_players = 2  # 与游戏核心 MIN_PLAYERS 保持一致

        if state_name in ("PREFLOP", "FLOP", "TURN", "RIVER"):
            # 下注阶段：轮到自己时 _render_turn_hint 已给足引导，这里只提示等待
            if current_player_id != self.client.player_id:
                actor = next(
                    (p.get("name", "?") for p in players
                     if p.get("player_id") == current_player_id),
                    "对方",
                )
                print(_style(f"⏳ 等待 {actor} 行动，请稍候...", Style.GREEN))
            else:
                # 轮到自己：在提示区同步输出行动引导（黄色加粗）
                self._render_turn_hint()
        elif state_name == "HAND_OVER":
            # 本局结束：明确告知如何开始下一局
            print(_style("本局已结束，房主输入 5 或 /start 开始下一局", Style.GREEN))
        elif state_name == "WAITING":
            # 等待中：按人数与房主身份给出不同引导
            count = len(players)
            if count < min_players:
                print(_style(f"当前 {count} 人，还需 {min_players - count} 人才能开局", Style.GREEN))
            # 【重点注释】以服务器广播的权威房主 ID 判断（而非 join 时缓存），
            # 保证房主离开交接后提示依然准确
            elif state.get("host_player_id") == self.client.player_id:
                print(_style("房间已就绪，你是房主，输入 5 或 /start 开始游戏", Style.GREEN))
            else:
                print(_style("房间已就绪，等待房主输入 5 或 /start 开始游戏", Style.GREEN))
        elif state_name == "SHOWDOWN":
            print("正在结算本局，请稍候...")
