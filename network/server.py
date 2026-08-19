"""TCP 游戏服务器模块。

房主运行此服务器，承载权威游戏逻辑。所有玩家（含房主自己）均以 TCP 客户端
身份连接到本服务器，规则计算与状态分发统一在此完成。

线程模型：
- 主线程运行 accept 循环，接受新连接。
- 每个客户端连接由独立线程读取消息并派发处理。
- 所有对 Game 的访问与广播操作通过 self._lock 串行化，避免竞态。
- 发送操作自带 per-connection 锁，避免数据交错。

关键流程：
- 玩家 join → 分配 ID → 加入 Game → 广播 player_joined → 回 join_ok
- 房主 start → game.start_hand() → 私发底牌 → 广播 state → 通知行动玩家
- 玩家 action → game.player_action() → 广播 state → 通知下一位行动玩家
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Any, Dict, List, Optional

from core.event_log import (
    EVT_ACTION,
    EVT_CHAT,
    EVT_CONNECT,
    EVT_DISCONNECT,
    EVT_GENERAL,
    EVT_HAND_END,
    EVT_HAND_START,
    EVT_HOST_CHANGE,
    EVT_JOIN,
    EVT_JOIN_FAIL,
    EVT_LEAVE,
    EVT_PROTOCOL_ERROR,
    EVT_SEND_FAIL,
    EVT_SERVER_START,
    EVT_SERVER_STOP,
    log_event,
)
from core.game import (
    DEFAULT_BIG_BLIND,
    DEFAULT_SMALL_BLIND,
    DEFAULT_STARTING_CHIPS,
    MAX_PLAYERS,
    Action,
    GameError,
    GameState,
    TexasHoldemGame,
)
from core.player import Player
from .protocol import (
    MSG_ACTION,
    MSG_CHAT,
    MSG_JOIN,
    MSG_LEAVE,
    MSG_PING,
    MSG_READY,
    MSG_RESET_REQ,
    MSG_START,
    MSG_STATUS_REQ,
    Msg,
    ProtocolError,
    decode_message_from_buffer,
    encode_message,
)

# 模块日志器，便于调试网络问题
logger = logging.getLogger("poker.server")

# 客户端心跳发送间隔建议值（客户端实际间隔见 network/client.py），
# 服务器据此判断"多久没收到任何数据就认为连接已死亡"。
# 【重点注释】局域网内出现"半开连接"（网线拔出、客户端断电、路由缓存失效）时，
# recv 不会立即返回错误而是永久阻塞，仅靠读取线程无法发现，必须靠心跳超时回收。
# 超时阈值取心跳间隔的 3 倍，给足网络抖动余量，避免误杀正常玩家。
IDLE_TIMEOUT: float = 45.0
# 服务器空闲连接巡检线程的执行周期（秒）
REAPER_INTERVAL: float = 10.0


class ClientConnection:
    """单个客户端连接的封装。

    持有 socket、接收缓冲区、发送锁以及关联的玩家信息。
    发送锁保证多线程向同一连接写数据时不交错。
    """

    def __init__(self, sock: socket.socket, address: tuple) -> None:
        """初始化连接封装。

        Args:
            sock: 已连接的 TCP socket。
            address: 对端地址 (host, port)。
        """
        self.sock: socket.socket = sock
        self.address: tuple = address
        # 接收缓冲区：累积原始字节，由协议层按帧切分
        self._recv_buffer: bytearray = bytearray()
        # 发送锁：保证并发 send 不交错
        self._send_lock: threading.Lock = threading.Lock()
        # 关联的玩家 ID，join 成功后设置
        self.player_id: Optional[int] = None
        # 玩家昵称，join 时设置
        self.name: str = ""
        # 连接是否已关闭
        self.closed: bool = False
        # 【重点注释】最近一次"收到客户端数据"的时间戳（time.monotonic 单调时钟）。
        # 由 reaper 线程比对：超过 IDLE_TIMEOUT 未收到任何数据（含心跳）即视为
        # 半开连接并主动关闭，避免僵尸连接长期占用线程与文件描述符资源。
        self.last_active: float = time.monotonic()
        # 断开清理是否已执行过：
        # 【重点注释】同一连接可能被两条路径各触发一次 _handle_disconnect
        # （读取线程 finally 中的正常清理，与收到 leave 消息时的主动清理），
        # 用该标记保证只执行一次移除与广播，避免重复日志与重复广播
        self._disconnect_handled: bool = False

    def send_message(self, message: dict) -> bool:
        """向该连接发送一条消息。

        Args:
            message: 消息字典。

        Returns:
            True 表示发送成功，False 表示连接已关闭或发送失败。
        """
        if self.closed:
            return False
        try:
            data = encode_message(message)
            # 加锁保证多线程并发发送不会交错产生粘包
            with self._send_lock:
                self.sock.sendall(data)
            return True
        except (OSError, ProtocolError) as exc:
            logger.warning("发送消息失败 %s: %s", self.address, exc)
            self.closed = True
            # 统一事件日志：消息发送失败（记录目标玩家与错误详情，便于排查网络问题）
            log_event(
                EVT_SEND_FAIL,
                f"向 {self.address[0]}:{self.address[1]} 发送消息失败",
                player_id=self.player_id if self.player_id is not None else "-",
                name=self.name if self.name else "-",
                error=str(exc),
            )
            return False

    def feed(self, data: bytes) -> List[dict]:
        """将新收到的字节追加到缓冲区，并尝试解析出完整消息。

        Args:
            data: 新接收的字节。

        Returns:
            本次解析出的消息字典列表（可能为空）。

        Raises:
            ProtocolError: 帧格式错误时抛出，调用方应关闭连接。
        """
        # 【重点注释】只要收到任何字节（即使是不完整帧），都说明客户端仍存活，
        # 刷新心跳时间戳，供 reaper 线程判断连接是否空闲超时
        self.last_active = time.monotonic()
        self._recv_buffer.extend(data)
        messages: List[dict] = []
        # 循环解析：一次追加可能包含多条完整消息
        while True:
            msg = decode_message_from_buffer(self._recv_buffer)
            if msg is None:
                break
            messages.append(msg)
        return messages

    def close(self) -> None:
        """关闭连接并释放资源。"""
        if self.closed:
            return
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass


class GameServer:
    """德州扑克游戏服务器。

    负责监听端口、管理连接、派发消息、驱动游戏流程。

    Attributes:
        host: 监听地址。
        port: 监听端口。
        game: 关联的游戏控制器。
        starting_chips: 新玩家初始筹码。
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8888,
        small_blind: int = DEFAULT_SMALL_BLIND,
        big_blind: int = DEFAULT_BIG_BLIND,
        starting_chips: int = DEFAULT_STARTING_CHIPS,
        max_connections: int = 50,
        public_ip: Optional[str] = None,
    ) -> None:
        """初始化服务器。

        Args:
            host: 监听地址，'0.0.0.0' 表示所有网卡（局域网/公网可访问）。
            port: 监听端口。
            small_blind: 小盲注。
            big_blind: 大盲注。
            starting_chips: 新玩家初始筹码。
            max_connections: 最大同时连接数（含未加入的探活连接）。超过时拒绝
                新连接，防止资源耗尽，保障多玩家场景下的稳定性。
            public_ip: 对外公告的 IP 地址。云服务器 / NAT 环境中本机自动探测到
                的是内网地址，通过该参数指定公网 IP 以生成正确的加入提示。
        """
        self.host: str = host
        self.port: int = port
        self.starting_chips: int = starting_chips
        self.max_connections: int = max_connections
        # 对外公告 IP：优先用管理员指定的公网 IP，否则启动后自动探测
        self.public_ip: Optional[str] = public_ip
        self.game: TexasHoldemGame = TexasHoldemGame(
            small_blind=small_blind, big_blind=big_blind
        )

        # 所有活跃连接：player_id -> ClientConnection
        self._connections: Dict[int, ClientConnection] = {}
        # 【重点注释】所有已接受的 TCP 连接（含尚未 join 的"预连接"）。
        # 连接数上限按本集合统计：仅统计已加入的 _connections 无法覆盖"只连接
        # 不加入"的僵尸连接（如攻击性扫描、异常客户端），会绕过限流保护。
        self._all_conns: set = set()
        # 串行化所有游戏操作与广播，避免竞态
        self._lock: threading.Lock = threading.Lock()
        # 监听 socket
        self._server_sock: Optional[socket.socket] = None
        # 服务器运行标志
        self._running: bool = False
        # 下一可用玩家 ID
        self._next_player_id: int = 1
        # 房主玩家 ID（第一个成功加入者）
        self.host_player_id: Optional[int] = None

        # 【重点注释】服务器运行统计（供 status 监控查询）：
        # - total_connections：累计接受的连接数
        # - peak_connections：同时在线连接数的历史峰值
        # - rejected_connections：因连接数上限被拒绝的次数
        # 用独立 _stats_lock 保护，避免与游戏锁耦合
        self._stats_lock: threading.Lock = threading.Lock()
        self._stats: Dict[str, int] = {
            "total_connections": 0,
            "peak_connections": 0,
            "rejected_connections": 0,
        }
        # 服务器启动时刻（用于计算运行时长）
        self._start_time: Optional[float] = None
        # 空闲连接回收线程引用（reaper）
        self._reaper_thread: Optional[threading.Thread] = None

    # ---------- 生命周期 ----------

    def start(self) -> None:
        """启动服务器：绑定端口、开始监听、进入 accept 循环。

        该方法阻塞，应在独立线程中调用（host 模式）或作为主线程（server 模式）。
        """
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # SO_REUSEADDR 允许快速重启复用端口
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind((self.host, self.port))
        # 监听队列长度：留足余量，短时间大量并发连接（如玩家集中进入）不丢连接
        self._server_sock.listen(min(MAX_PLAYERS * 4, 128))
        self._running = True
        # 记录服务器启动时刻，用于 status 监控中的运行时长统计
        self._start_time = time.monotonic()
        logger.info("服务器已启动: %s:%d", self.host, self.port)
        print(f"[服务器] 已启动，监听 {self.host}:{self.port}")
        # 统一事件日志：服务器启动（挂机等待连接期间的第一个关键事件）
        log_event(EVT_SERVER_START, f"服务器启动，开始监听 {self.host}:{self.port}")

        # 对外公告地址：优先用管理员指定的公网 IP，否则自动探测本机地址
        advertised_ip = self.public_ip or self.get_local_ip()
        print(f"[服务器] 对外连接地址: {advertised_ip}:{self.port}")
        print(f"[服务器] 其他玩家加入命令: python main.py join --host {advertised_ip} --port {self.port} --name <昵称>")

        # 启动空闲连接回收线程：定期关闭心跳超时的半开连接
        self._reaper_thread = threading.Thread(
            target=self._reaper_loop, daemon=True, name="reaper"
        )
        self._reaper_thread.start()

        try:
            while self._running:
                try:
                    sock, addr = self._server_sock.accept()
                except OSError:
                    break
                # 【重点注释】显式将连接 socket 设为阻塞模式（无超时）：
                # 防止任何上游继承的超时设置导致 recv 空闲超时被误判为断开。
                # 游戏对局中等待玩家行动可能远超任意超时值。
                sock.settimeout(None)

                # 【重点注释】连接数上限保护：按"所有已接受的 TCP 连接"统计，
                # 防止异常客户端反复建立连接耗尽文件描述符与线程资源。
                # 达到上限时直接拒绝（发送错误后关闭），并计入拒绝统计供监控查看。
                conn = ClientConnection(sock, addr)
                with self._lock:
                    over_limit = len(self._all_conns) >= self.max_connections
                    if not over_limit:
                        self._all_conns.add(conn)
                with self._stats_lock:
                    self._stats["total_connections"] += 1
                    if over_limit:
                        self._stats["rejected_connections"] += 1
                    else:
                        self._stats["peak_connections"] = max(
                            self._stats["peak_connections"], len(self._all_conns)
                        )
                if over_limit:
                    logger.warning("拒绝连接（已达上限 %d）: %s", self.max_connections, addr)
                    log_event(
                        EVT_GENERAL,
                        f"拒绝连接（达到上限 {self.max_connections}）",
                        remote=addr[0],
                        port=addr[1],
                    )
                    try:
                        # 直接构造并发送错误帧，不进入常规连接管理流程
                        sock.sendall(
                            encode_message(Msg.error("服务器连接数已达上限，请稍后重试"))
                        )
                    except OSError:
                        pass
                    sock.close()
                    continue

                # 统一事件日志：新连接建立（挂机状态下最常见的活动）
                log_event(EVT_CONNECT, f"新连接建立 {addr[0]}:{addr[1]}", remote=addr[0], port=addr[1])
                # 每个连接开一个线程读取消息
                t = threading.Thread(
                    target=self._client_loop, args=(conn,), daemon=True
                )
                t.start()
        finally:
            self._shutdown()

    def stop(self) -> None:
        """停止服务器，关闭所有连接。"""
        self._running = False
        if self._server_sock:
            try:
                # 关闭监听 socket 以打断 accept 阻塞
                self._server_sock.close()
            except OSError:
                pass

    def _shutdown(self) -> None:
        """清理所有客户端连接。"""
        with self._lock:
            for conn in self._connections.values():
                conn.close()
            self._connections.clear()
            self._all_conns.clear()
        logger.info("服务器已关闭")
        # 统一事件日志：服务器停止
        log_event(EVT_SERVER_STOP, "服务器已关闭")

    # ---------- 心跳回收与状态监控 ----------

    def _reaper_loop(self) -> None:
        """空闲连接回收循环：周期巡检并关闭心跳超时的半开连接。

        【重点注释】为什么需要独立巡检线程：
        对端崩溃/断网/断电时，TCP 不会主动通知服务器（半开连接），recv 会
        永久阻塞，客户端读取线程永远等不到退出信号。若不回收，这些僵尸连接
        将持续占用线程、socket 与文件描述符资源，最终拖垮服务器。
        客户端每隔 HEARTBEAT_INTERVAL 发送 ping，正常连接必然在 IDLE_TIMEOUT
        内收到数据；超过 IDLE_TIMEOUT 未收到数据的连接即可安全判定为死亡。
        本线程运行于服务器主循环之外，互不影响。
        """
        while self._running:
            # 周期休眠，减少不必要的巡检开销
            time.sleep(REAPER_INTERVAL)
            if not self._running:
                break
            # 快照所有连接，避免持锁过久阻塞游戏主流程
            with self._lock:
                conns = list(self._all_conns)
            now = time.monotonic()
            for conn in conns:
                # 超过空闲阈值仍未收到任何数据 → 判定为半开连接，强制关闭
                if now - conn.last_active > IDLE_TIMEOUT:
                    logger.info("心跳超时，回收空闲连接 %s", conn.address)
                    # 直接关闭 socket：客户端读取线程会因 recv 报错退出，
                    # 进而走 _handle_disconnect 完成玩家移除与广播
                    conn.close()

    def get_status(self) -> Dict[str, Any]:
        """生成服务器状态快照，供 status 命令 / status 客户端查询使用。

        Returns:
            包含在线玩家、连接数、运行时长等信息的字典。
        """
        # 游戏状态与连接数需分别加锁读取，避免竞态
        with self._lock:
            players = self.game.seats.all()
            conn_count = len(self._all_conns)
            state_name = GameState(self.game.state).name if self.game.state is not None else "-"
            # 当前行动者 ID：仅下注阶段有意义，其余阶段返回 None（与状态快照一致）
            if (
                0 <= self.game.current_pos < len(players)
                and self.game.state is not None
                and self.game.state
                in (
                    GameState.PREFLOP,
                    GameState.FLOP,
                    GameState.TURN,
                    GameState.RIVER,
                )
            ):
                current_player_id = players[self.game.current_pos].player_id
            else:
                current_player_id = None
        with self._stats_lock:
            stats = dict(self._stats)
        # 运行时长（秒），未启动时为 0
        uptime = int(time.monotonic() - self._start_time) if self._start_time else 0
        return {
            "server": f"{self.host}:{self.port}",
            "advertised_ip": self.public_ip or self.get_local_ip(),
            "online_players": len(players),
            "max_players": MAX_PLAYERS,
            "connections": conn_count,
            "max_connections": self.max_connections,
            "total_connections": stats["total_connections"],
            "peak_connections": stats["peak_connections"],
            "rejected_connections": stats["rejected_connections"],
            "uptime_seconds": uptime,
            "hand_number": self.game.hand_number,
            "game_state": state_name,
            "current_player_id": current_player_id,
            "host_player_id": self.host_player_id,
            "players": [
                {"player_id": p.player_id, "name": p.name, "chips": p.chips}
                for p in players
            ],
        }

    def run_console(self) -> None:
        """交互式服务器控制台（专用 server 模式使用）。

        从标准输入读取命令并执行，供运维人员查看服务器运行状态：
        - status   ：显示在线玩家数、连接数、运行时长等监控信息
        - players  ：列出当前所有玩家
        - help     ：显示命令帮助
        - quit     ：停止服务器并退出
        读取到 EOF（如通过 nohup 后台运行、stdin 已关闭）时自动结束，
        不影响服务器主循环。
        """
        print("服务器控制台已启动，输入 help 查看可用命令")
        while self._running:
            try:
                line = input("server> ").strip()
            except EOFError:
                # stdin 已关闭（后台运行场景），退出控制台线程
                print("控制台输入已关闭，服务器继续运行（可用 kill/stop 停止）")
                break
            except (OSError, ValueError):
                # 输入流异常时短暂等待后重试，避免线程静默退出
                time.sleep(0.2)
                continue
            if not line:
                continue
            cmd = line.lower()
            if cmd in ("help", "h", "?"):
                print("可用命令: status | players | help | quit")
            elif cmd in ("status", "s"):
                self._console_print_status()
            elif cmd in ("players", "p"):
                self._console_print_players()
            elif cmd in ("quit", "q", "exit", "stop"):
                print("正在停止服务器...")
                self.stop()
            else:
                print(f"未知命令: {cmd}（输入 help 查看帮助）")

    def _console_print_status(self) -> None:
        """控制台：以可读格式输出服务器状态。"""
        st = self.get_status()
        print("-" * 46)
        print(f"服务器地址     : {st['server']}（对外 {st['advertised_ip']}）")
        print(f"在线玩家       : {st['online_players']}/{st['max_players']}")
        print(f"当前连接数     : {st['connections']}/{st['max_connections']}")
        print(f"累计连接数     : {st['total_connections']}（峰值 {st['peak_connections']}）")
        print(f"拒绝连接数     : {st['rejected_connections']}")
        print(f"运行时长       : {st['uptime_seconds']} 秒")
        print(f"当前局数       : 第 {st['hand_number']} 局（{st['game_state']}）")
        print("-" * 46)

    def _console_print_players(self) -> None:
        """控制台：列出当前所有在线玩家。"""
        with self._lock:
            players = self.game.seats.all()
        if not players:
            print("当前没有玩家在线")
            return
        print(f"当前在线玩家（{len(players)} 人）:")
        for p in players:
            host_mark = "（房主）" if p.player_id == self.host_player_id else ""
            print(f"  #{p.player_id} {p.name}  筹码={p.chips} {host_mark}")

    # ---------- 客户端读取循环 ----------

    def _client_loop(self, conn: ClientConnection) -> None:
        """单个客户端的读取循环，持续接收并派发消息。

        Args:
            conn: 该客户端的连接封装。
        """
        # 断开原因：记录本次读取循环退出时的具体原因，随清理流程一并写入日志，
        # 便于在控制台快速定位"连接为什么断开"
        reason = "读取循环正常结束"

        try:
            while self._running and not conn.closed:
                try:
                    data = conn.sock.recv(4096)
                except OSError as exc:
                    # 【重点注释】对端强制断开连接（最常见是 Windows 的
                    # WinError 10054"远程主机强迫关闭"）。这通常是对端先退出的
                    # 正常清理路径，并非服务器故障，仅记录日志后退出循环
                    reason = f"对端强制断开: {exc}"
                    break
                if not data:
                    # recv 返回空字节表示对端发送了 FIN，即对方正常关闭连接
                    reason = "对端正常关闭"
                    break
                try:
                    messages = conn.feed(data)
                except ProtocolError as exc:
                    # 【重点注释】协议错误：对端发送了无法解析的帧（长度头非法、
                    # JSON 损坏等），说明双方协议不同步，继续读下去没有意义，
                    # 向对端提示错误后关闭该连接
                    logger.warning("协议错误 %s: %s", conn.address, exc)
                    # 统一事件日志：协议错误（记录错误详情，便于定位协议不匹配）
                    log_event(
                        EVT_PROTOCOL_ERROR,
                        f"客户端 {conn.address[0]}:{conn.address[1]} 协议错误",
                        error=str(exc),
                    )
                    reason = f"协议错误: {exc}"
                    conn.send_message(Msg.error(f"协议错误: {exc}"))
                    break
                for msg in messages:
                    self._dispatch(conn, msg)
        finally:
            self._handle_disconnect(conn, reason)

    # ---------- 消息派发 ----------

    def _dispatch(self, conn: ClientConnection, msg: dict) -> None:
        """根据消息类型派发到对应处理函数。

        Args:
            conn: 来源连接。
            msg: 已解析的消息字典。
        """
        msg_type = msg.get("type", "")
        try:
            if msg_type == MSG_JOIN:
                self._handle_join(conn, msg)
            elif msg_type == MSG_ACTION:
                self._handle_action(conn, msg)
            elif msg_type == MSG_START:
                self._handle_start(conn)
            elif msg_type == MSG_READY:
                # 简化模型：收到 ready 即视为可开局，无需特殊处理
                pass
            elif msg_type == MSG_RESET_REQ:
                self._handle_reset_req(conn)
            elif msg_type == MSG_CHAT:
                self._handle_chat(conn, msg)
            elif msg_type == MSG_LEAVE:
                # 主动离开：与断线走同一清理流程，但日志按"主动离开"事件记录
                self._handle_disconnect(conn, "客户端主动离开")
            elif msg_type == MSG_PING:
                conn.send_message(Msg.pong())
            elif msg_type == MSG_STATUS_REQ:
                # 状态查询：无需加入房间即可响应，供外部监控服务器状态
                conn.send_message(Msg.status_resp(self.get_status()))
            else:
                # 统一事件日志：收到未知消息类型，视为双方协议不一致
                log_event(
                    EVT_PROTOCOL_ERROR,
                    f"收到未知消息类型 {msg_type}",
                    remote=conn.address[0],
                )
                conn.send_message(Msg.error(f"未知消息类型: {msg_type}"))
        except GameError as exc:
            # 游戏规则违规，回错误提示但不断开连接
            conn.send_message(Msg.error(str(exc)))
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("处理消息异常: %s", msg)
            # 统一事件日志：服务器内部异常（防御性兜底，正常流程不应触发）
            log_event(
                EVT_GENERAL,
                f"处理消息异常: {msg_type}",
                error=str(exc),
            )
            conn.send_message(Msg.error(f"服务器内部错误: {exc}"))

    # ---------- 具体消息处理 ----------

    def _handle_join(self, conn: ClientConnection, msg: dict) -> None:
        """处理玩家加入请求。

        Args:
            conn: 来源连接。
            msg: join 消息，含 name 字段。
        """
        name = str(msg.get("name", "")).strip()
        if not name:
            conn.send_message(Msg.join_fail("昵称不能为空"))
            log_event(EVT_JOIN_FAIL, "加入失败：昵称为空", remote=conn.address[0], port=conn.address[1])
            return
        if len(name) > 16:
            conn.send_message(Msg.join_fail("昵称长度不能超过 16"))
            log_event(EVT_JOIN_FAIL, f"加入失败：昵称过长 {name[:8]}...", remote=conn.address[0])
            return

        with self._lock:
            # 检查昵称重复，并支持"断线重连接管"：
            # 【重点注释】客户端断线重连时，其旧连接可能尚未被服务器回收
            # （读取线程还在等待），此时新连接带着 reconnect 标记加入，若旧连接
            # 已死亡（closed），则允许接管该昵称的席位，避免重连被"昵称已被使用"
            # 拒绝；若旧连接仍存活（真有人在用此昵称），照常拒绝。
            takeover_pid: Optional[int] = None
            existing = next(
                (p for p in self.game.seats.all() if p.name == name), None
            )
            if existing is not None:
                old_conn = self._connections.get(existing.player_id)
                if bool(msg.get("reconnect", False)) and (old_conn is None or old_conn.closed):
                    # 重连接管：标记旧玩家，在锁外完成移除与广播
                    takeover_pid = existing.player_id
                else:
                    conn.send_message(Msg.join_fail("昵称已被使用"))
                    log_event(EVT_JOIN_FAIL, f"加入失败：昵称已存在 {name}", remote=conn.address[0])
                    return

            # 分配玩家 ID
            player_id = self._next_player_id
            self._next_player_id += 1

            # 创建玩家并加入游戏
            player = Player(
                player_id=player_id, name=name, chips=self.starting_chips
            )
            try:
                self.game.add_player(player)
            except GameError as exc:
                conn.send_message(Msg.join_fail(str(exc)))
                log_event(EVT_JOIN_FAIL, f"加入失败：{exc}", name=name, remote=conn.address[0])
                return

            conn.player_id = player_id
            conn.name = name
            self._connections[player_id] = conn

            # 首位加入者为房主（重连接管场景下房主已存在，不会触发）
            is_host = self.host_player_id is None
            if is_host:
                self.host_player_id = player_id

            # 生成状态快照（新玩家视角）
            state = self.game.get_state_snapshot(viewer_id=player_id)

        # 锁外处理：重连接管需要先移除旧玩家席位（含房主交接、掉线弃牌推进、
        # player_left 广播），再广播新玩家加入，保证各端状态一致
        if takeover_pid is not None:
            self._handle_name_takeover(takeover_pid)

        # 发送加入成功响应（含自身 player_id、房主标记与当前桌状态）
        conn.send_message(Msg.join_ok(player_id, state, is_host))
        if is_host:
            conn.send_message(Msg.log("你是房主，输入 /start 开始游戏"))

        # 统一事件日志：玩家加入成功（含是否为房主、当前人数）
        log_event(
            EVT_JOIN,
            f"玩家 {name} 加入房间",
            player_id=player_id,
            is_host=is_host,
            count=len(self.game.seats),
            remote=conn.address[0],
        )

        # 广播玩家加入通知与最新状态给所有人
        self._broadcast_msg(
            Msg.player_joined(name, len(self.game.seats))
        )
        self._broadcast_state()

    def _handle_start(self, conn: ClientConnection) -> None:
        """处理房主开局请求。"""
        with self._lock:
            # 仅房主可开局
            if conn.player_id != self.host_player_id:
                conn.send_message(Msg.error("只有房主可以开始游戏"))
                return
            if not self.game.can_start_hand():
                conn.send_message(Msg.error("当前无法开局（人数不足或已开始）"))
                return
            # 记录开局前的日志条数，用于广播新增事件
            log_before = len(self.game.log)
            self.game.start_hand()

        # 开局后：广播事件流 → 私发底牌 → 广播状态 → 通知行动玩家
        self._broadcast_new_logs(log_before)
        self._send_hole_cards()
        self._broadcast_state()
        self._notify_turn()
        # 统一事件日志：一局开始
        log_event(
            EVT_HAND_START,
            f"第 {self.game.hand_number} 局开始",
            players=len(self.game.seats),
            dealer=self.game.seats.all()[self.game.dealer_pos].name,
        )

    def _handle_reset_req(self, conn: ClientConnection) -> None:
        """处理房主重置房间请求：清零对局数、恢复筹码、保留玩家列表与规则。

        【重点注释】权限验证：仅房间创建者（host_player_id）可执行重置，
        服务器侧做权威校验（UI 菜单的可用性提示只是辅助，不能作为安全依据）。
        重置在游戏锁内同步执行，随后广播重置通知与最新状态，所有客户端
        （含掉线重连者）都会收到最新 state 并刷新界面。

        Args:
            conn: 发起重置请求的客户端连接。
        """
        with self._lock:
            # 权限校验：非房主直接拒绝，并提示原因
            if conn.player_id != self.host_player_id:
                conn.send_message(Msg.reset_fail("仅房间创建者可重置房间"))
                return
            # 【重点注释】调用游戏核心重置：对局数清零、玩家筹码恢复初始值、
            # 清空本局数据；玩家列表、昵称、ID、盲注等规则配置保持不变。
            # reset_room 会清空 game.log 并写入一条"房间已重置"日志，
            # 因此广播新增日志时 from 索引取 0 即可。
            self.game.reset_room(self.starting_chips)

        # 广播重置成功通知 → 重置事件流 → 最新状态快照（所有人同步刷新）
        self._broadcast_msg(Msg.reset_ok())
        self._broadcast_new_logs(0)
        self._broadcast_state()
        # 统一事件日志：记录谁在何时重置了房间，便于运维审计
        log_event(
            EVT_GENERAL,
            f"房主 {conn.name} 重置了房间",
            player_id=conn.player_id,
            players=len(self.game.seats),
        )

    def _handle_action(self, conn: ClientConnection, msg: dict) -> None:
        """处理玩家游戏行动。"""
        if conn.player_id is None:
            conn.send_message(Msg.error("尚未加入房间"))
            return
        action_name = str(msg.get("action", "")).lower()
        amount = int(msg.get("amount", 0) or 0)

        # 将字符串映射为 Action 枚举
        action_map = {
            "fold": Action.FOLD,
            "check": Action.CHECK,
            "call": Action.CALL,
            "raise": Action.RAISE,
            "all_in": Action.ALL_IN,
            "all-in": Action.ALL_IN,
        }
        if action_name not in action_map:
            conn.send_message(Msg.error(f"未知行动: {action_name}"))
            return
        action = action_map[action_name]

        with self._lock:
            # 记录行动前的日志条数，用于广播新增事件
            log_before = len(self.game.log)
            # 委托给游戏核心校验与执行
            self.game.player_action(conn.player_id, action, amount)
            hand_over = self.game.is_hand_over

        # 统一事件日志：玩家行动（金额仅在加注/全下等场景非 0）
        log_event(
            EVT_ACTION,
            f"{conn.name} 执行 {action_name}",
            player_id=conn.player_id,
            amount=amount if amount else "-",
            hand_over=hand_over,
        )

        # 广播事件流与状态变化
        self._broadcast_new_logs(log_before)
        self._broadcast_state()
        if hand_over:
            # 本局结束，通知所有人并附带结果摘要
            self._broadcast_msg(Msg.hand_over(self.game.last_result))
            # 统一事件日志：一局结束（携带胜负结果摘要）
            log_event(
                EVT_HAND_END,
                f"第 {self.game.hand_number} 局结束",
                result=self.game.last_result,
            )
        else:
            # 通知下一位行动玩家
            self._notify_turn()

    def _handle_chat(self, conn: ClientConnection, msg: dict) -> None:
        """处理聊天消息并广播。"""
        if conn.player_id is None:
            return
        text = str(msg.get("text", "")).strip()
        if not text:
            return
        if len(text) > 200:
            conn.send_message(Msg.error("聊天内容过长"))
            return
        self._broadcast_msg(Msg.chat_broadcast(conn.name, text))
        # 统一事件日志：聊天广播（截断文本并去掉换行，保证单行日志可读）
        log_event(
            EVT_CHAT,
            f"{conn.name} 发送聊天",
            player_id=conn.player_id,
            text=text[:50].replace("\n", " "),
        )

    def _handle_disconnect(self, conn: ClientConnection, reason: str = "客户端主动离开") -> None:
        """处理客户端断开连接：从游戏中移除并广播。

        Args:
            conn: 断开的连接封装。
            reason: 断开原因描述（来自 _client_loop 的退出分支，或主动离开），
                    用于写日志定位问题。
        """
        # 【重点注释】幂等保护：同一连接只清理一次。
        # 例如收到 leave 消息时已清理，随后读取线程 finally 又会调用本方法，
        # 若不拦截会导致玩家被重复移除、日志与广播重复
        # 先无条件将连接移出"所有连接"集合（含未 join 的预连接），
        # 保证即使提前返回也不会遗留僵尸计数
        with self._lock:
            self._all_conns.discard(conn)
        if conn._disconnect_handled:
            return
        conn._disconnect_handled = True

        conn.close()
        if conn.player_id is None:
            # 尚未成功 join 就断开的"预连接"，仅记录日志后直接返回
            logger.info("未加入的连接断开 %s: %s", conn.address, reason)
            # 统一事件日志：预连接（未加入即断开）事件
            log_event(
                EVT_DISCONNECT,
                f"未加入的连接断开 {conn.address[0]}:{conn.address[1]}",
                reason=reason,
                joined=False,
            )
            return
        with self._lock:
            # 记录移除前的日志条数，用于广播掉线弃牌产生的事件
            log_before = len(self.game.log)
            # 【重点注释】记录断开前对局是否已结束：若移除前对局已处于结束状态
            # （例如对手先离开导致本局结束），本次断开只是玩家离开空桌，
            # 不应再记录一次 HAND_END，避免同一局被重复记录
            was_hand_over = self.game.is_hand_over
            removed = self.game.remove_player(conn.player_id)
            self._connections.pop(conn.player_id, None)
            # 若房主离开，则把房主转给最前的玩家
            host_changed = False
            old_host = self.host_player_id
            if conn.player_id == self.host_player_id:
                remaining = self.game.seats.all()
                self.host_player_id = remaining[0].player_id if remaining else None
                host_changed = old_host != self.host_player_id
            player_count = len(self.game.seats)
            name = removed.name if removed else conn.name
            # 若断线导致游戏状态变化（自动弃牌推进），需检查流程
            hand_over = self.game.is_hand_over
            # 仅当"本次断开让对局从进行中变为结束"时才视为由本断开引发
            hand_ended_by_this = hand_over and not was_hand_over

        # 记录"谁在何时以何种原因断开"，便于排查断连问题
        logger.info("玩家断开: %s（剩余 %d 人），原因: %s", name, player_count, reason)
        # 【重点注释】统一事件日志：区分"主动离开"与"异常断开"两类事件。
        # - /leave 或正常退出走 MSG_LEAVE，reason 固定为"客户端主动离开"，记 EVT_LEAVE
        # - 其余情况（网络错误、对端强制断开、协议错误等）记 EVT_DISCONNECT
        if reason == "客户端主动离开":
            log_event(
                EVT_LEAVE,
                f"玩家 {name} 离开房间",
                player_id=conn.player_id,
                remaining=player_count,
            )
        else:
            log_event(
                EVT_DISCONNECT,
                f"玩家 {name} 断开连接",
                player_id=conn.player_id,
                reason=reason,
                remaining=player_count,
                hand_over=hand_over,
            )
        if host_changed:
            # 统一事件日志：房主离开后的交接（新房主为剩余玩家首位）
            log_event(
                EVT_HOST_CHANGE,
                f"房主 {name} 离开，房主交接",
                new_host=self.host_player_id,
            )
        if hand_ended_by_this:
            # 统一事件日志：掉线导致对局结束（对手自动赢得底池）
            log_event(
                EVT_HAND_END,
                f"第 {self.game.hand_number} 局结束（玩家 {name} 离开）",
                result=self.game.last_result,
            )

        if removed:
            self._broadcast_msg(Msg.player_left(name, player_count))
        # 广播掉线弃牌等产生的事件流，再广播最新状态
        self._broadcast_new_logs(log_before)
        self._broadcast_state()
        if not hand_over and removed:
            # 掉线弃牌后若本局未结束，通知下一位行动玩家
            self._notify_turn()

    def _handle_name_takeover(self, player_id: int) -> None:
        """处理断线重连接管：移除旧玩家席位并广播（供 join 重连接管调用）。

        【重点注释】调用时机：新连接带着 reconnect 标记加入、且旧连接已死亡时，
        在 _handle_join 中锁定外调用。旧玩家的连接尚未走完断开清理流程
        （读取线程可能仍阻塞在 recv），这里需要：
        1. 关闭旧连接并打上 _disconnect_handled 标记，防止旧读取线程 finally
           中的 _handle_disconnect 重复移除玩家 / 重复广播；
        2. 复用 _handle_disconnect 的移除逻辑（掉线弃牌推进、房主交接、
           player_left 广播、状态广播）。

        Args:
            player_id: 被接管旧玩家的玩家 ID。
        """
        # 关闭旧连接并防重复清理
        old_conn = self._connections.get(player_id)
        if old_conn is not None:
            # 将旧连接移出"所有连接"集合（其读取线程会因 _disconnect_handled
            # 提前返回，不会自行清理，需在此显式移除）
            with self._lock:
                self._all_conns.discard(old_conn)
            old_conn._disconnect_handled = True
            old_conn.send_message(Msg.kick("你的连接已被同名玩家重连接管"))
            old_conn.close()

        with self._lock:
            log_before = len(self.game.log)
            was_hand_over = self.game.is_hand_over
            removed = self.game.remove_player(player_id)
            self._connections.pop(player_id, None)
            # 若被接管者是房主，则把房主转给最前的玩家
            host_changed = False
            old_host = self.host_player_id
            if player_id == self.host_player_id:
                remaining = self.game.seats.all()
                self.host_player_id = remaining[0].player_id if remaining else None
                host_changed = old_host != self.host_player_id
            player_count = len(self.game.seats)
            name = removed.name if removed else "未知玩家"
            hand_over = self.game.is_hand_over
            hand_ended_by_this = hand_over and not was_hand_over

        logger.info("玩家席位被接管: %s（剩余 %d 人）", name, player_count)
        log_event(
            EVT_DISCONNECT,
            f"玩家 {name} 被重连接管",
            player_id=player_id,
            reason="重连接管",
            remaining=player_count,
        )
        if host_changed:
            log_event(EVT_HOST_CHANGE, f"房主 {name} 被接管，房主交接", new_host=self.host_player_id)
        if hand_ended_by_this:
            log_event(
                EVT_HAND_END,
                f"第 {self.game.hand_number} 局结束（玩家 {name} 被接管）",
                result=self.game.last_result,
            )

        if removed:
            self._broadcast_msg(Msg.player_left(name, player_count))
        self._broadcast_new_logs(log_before)
        self._broadcast_state()
        if not hand_over and removed:
            self._notify_turn()

    # ---------- 广播辅助 ----------

    def _broadcast_msg(self, message: dict) -> None:
        """向所有连接广播同一消息。"""
        with self._lock:
            conns = list(self._connections.values())
        for conn in conns:
            conn.send_message(message)

    def _broadcast_new_logs(self, before: int) -> None:
        """把 game.log 中自 before 起的新增事件逐条广播给所有客户端。

        【重点注释】游戏事件（下盲注、弃牌、跟注、翻牌、摊牌、赢家等）记录在
        game.log 中，若不主动广播，客户端"最近消息"区只会显示聊天与系统提示，
        玩家无法了解对局进展，理解门槛高。此处将新增条目作为 log 消息广播，
        使所有玩家都能看到完整的事件流。

        Args:
            before: 事件发生前的 game.log 长度，用于计算新增区间。
        """
        new_logs = self.game.log[before:]
        for line in new_logs:
            self._broadcast_msg(Msg.log(line))

    def _broadcast_state(self) -> None:
        """向每位玩家发送其视角的状态快照。"""
        with self._lock:
            items = []
            for pid, conn in self._connections.items():
                snapshot = self.game.get_state_snapshot(viewer_id=pid)
                # 【重点注释】附带权威的房主 ID：房主离开后服务器会重 assign
                # 房主给下一位玩家，客户端 join 时缓存的 is_host 已过期，
                # UI 需以本字段为准渲染"是否由你开局"的引导提示。
                snapshot["host_player_id"] = self.host_player_id
                items.append((conn, Msg.state(snapshot)))
        for conn, msg in items:
            conn.send_message(msg)

    def _send_hole_cards(self) -> None:
        """将每位玩家的底牌私发给本人。"""
        with self._lock:
            items = []
            for player in self.game.seats.all():
                if not player.has_hole_cards:
                    continue
                conn = self._connections.get(player.player_id)
                if conn is None:
                    continue
                cards_data = [c.to_dict() for c in player.hole_cards]
                items.append((conn, Msg.deal_hole(cards_data)))
        for conn, msg in items:
            conn.send_message(msg)

    def _notify_turn(self) -> None:
        """通知当前行动玩家轮到其行动，附带可执行选项。"""
        with self._lock:
            state = self.game.state
            if state not in (GameState.PREFLOP, GameState.FLOP, GameState.TURN, GameState.RIVER):
                return
            pos = self.game.current_pos
            players = self.game.seats.all()
            if not (0 <= pos < len(players)):
                return
            current_player = players[pos]
            options = self.game.get_player_options(current_player.player_id)
            conn = self._connections.get(current_player.player_id)
        if conn is not None:
            conn.send_message(Msg.turn(options))

    # ---------- 工具 ----------

    def get_local_ip(self) -> str:
        """获取本机局域网 IP，便于告知其他玩家连接地址。

        通过临时连接公网地址的方式取得本机出口 IP（不会真正发包）。
        """
        try:
            temp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            temp.connect(("8.8.8.8", 80))
            local_ip = temp.getsockname()[0]
            temp.close()
            return local_ip
        except OSError:
            return "127.0.0.1"

    @property
    def player_count(self) -> int:
        """当前桌玩家数。"""
        return len(self.game.seats)
