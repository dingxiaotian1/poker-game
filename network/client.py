"""TCP 游戏客户端模块。

负责与房主服务器建立连接、收发消息，并将收到的消息放入队列供 UI 层消费。
客户端不执行任何游戏规则计算，仅做：
1. 连接服务器并发送 join
2. 在后台线程读取服务器消息并入队
3. 提供 send_action/send_start/send_chat 等发送接口
4. 维护本地缓存的最新状态、底牌、可行动选项，供 UI 直接读取

UI 层通过 poll_message(timeout) 非阻塞获取消息并刷新界面。
"""
from __future__ import annotations

import logging
import queue
import socket
import threading
import time
from typing import Any, Dict, List, Optional

from core.card import Card
from core.event_log import (
    EVT_CLIENT_CONNECT,
    EVT_CLIENT_DISCONNECT,
    EVT_CLIENT_ERROR,
    log_event,
)
from .protocol import (
    Msg,
    ProtocolError,
    decode_message_from_buffer,
    encode_message,
)

logger = logging.getLogger("poker.client")

# 心跳发送间隔（秒）：
# 【重点注释】客户端周期性地向服务器发送 ping，服务器据此维护连接活性。
# 若客户端静默（断网/断电/崩溃），服务器在 IDLE_TIMEOUT（server.py）后即可
# 判定连接死亡并回收资源；同时心跳也能让网络中间设备（NAT 会话、路由器
# 空闲表项）保持连接不被清理，防止长时间无操作后被外部断开。
HEARTBEAT_INTERVAL: float = 15.0
# 重连初始等待间隔（秒），指数退避的基础值
RECONNECT_BASE_DELAY: float = 0.5
# 重连最大等待间隔（秒），指数退避的上限
RECONNECT_MAX_DELAY: float = 10.0
# 每次重连时连接服务器与等待加入响应的超时（秒）
RECONNECT_TIMEOUT: float = 5.0


class ClientError(Exception):
    """客户端错误。"""


def query_server_status(host: str, port: int, timeout: float = 5.0) -> Dict[str, Any]:
    """查询服务器运行状态（不加入房间）。

    供 status 子命令等监控用途调用：建立临时 TCP 连接，发送 status_req
    消息并同步等待 status_resp 响应，收到后关闭连接返回状态字典。

    Args:
        host: 服务器地址。
        port: 服务器端口。
        timeout: 连接与等待响应的超时秒数。

    Returns:
        服务器状态字典（在线人数、连接数、运行时长等，见 GameServer.get_status）。

    Raises:
        ClientError: 连接失败、超时或响应格式非法时抛出。
    """
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        raise ClientError(f"无法连接到 {host}:{port}: {exc}") from exc
    try:
        # 发送状态查询请求
        sock.sendall(encode_message(Msg.status_req()))
        sock.settimeout(timeout)
        buffer = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data = sock.recv(4096)
            except socket.timeout:
                break
            if not data:
                break
            buffer.extend(data)
            while True:
                try:
                    msg = decode_message_from_buffer(buffer)
                except ProtocolError as exc:
                    raise ClientError(f"协议错误: {exc}") from exc
                if msg is None:
                    break
                # 收到状态响应即返回（其余消息如 error 一并透出处理）
                if msg.get("type") == "status_resp":
                    status = msg.get("status")
                    if isinstance(status, dict):
                        return status
                    raise ClientError("服务器状态响应格式非法")
                if msg.get("type") == "error":
                    raise ClientError(str(msg.get("message", "服务器返回错误")))
        raise ClientError("等待服务器状态响应超时")
    finally:
        sock.close()


class GameClient:
    """游戏客户端：连接服务器、收发消息、缓存本地状态。

    Attributes:
        player_id: 服务器分配的玩家 ID，join 成功前为 None。
        name: 本玩家昵称。
        state: 最近一次收到的桌状态快照。
        hole_cards: 本玩家底牌列表。
        turn_options: 轮到本玩家行动时的可选项字典。
    """

    def __init__(self) -> None:
        """初始化客户端，尚未连接。"""
        self.player_id: Optional[int] = None
        self.name: str = ""
        # 是否为房主（首位加入者）：用于 UI 展示房主专属的引导提示
        self.is_host: bool = False
        self.state: Optional[Dict[str, Any]] = None
        self.hole_cards: List[Card] = []
        self.turn_options: Optional[Dict[str, Any]] = None

        self._sock: Optional[socket.socket] = None
        self._recv_buffer: bytearray = bytearray()
        # 入站消息队列：UI 主线程从中取消息处理
        self._inbox: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._running: bool = False
        # 连接是否成功建立（join_ok 收到后置 True）
        self._joined: bool = False
        # join 失败原因，供 connect 返回
        self._join_error: str = ""
        # 断开事件是否已记录日志：
        # 【重点注释】主动 disconnect() 与读取线程退出（服务器关闭/网络错误）可能
        # 各自触发一次断开日志，用该标记保证只记录一条 EVT_CLIENT_DISCONNECT
        self._disconnect_logged: bool = False
        # 是否已主动请求断开：
        # 【重点注释】disconnect() 关闭 socket 后，读取线程会因 recv 报错退出。
        # 该标记让读取线程据此把退出原因写为"主动断开"，避免日志里把主动退出
        # 误记为"网络错误"，保证客户端断开原因在多次运行间一致可分析
        self._disconnect_requested: bool = False

        # ---- 断线重连相关状态 ----
        # 服务器地址与端口（重连时复用）
        self._host: str = ""
        self._port: int = 0
        # 是否启用自动重连（connect 时由调用方指定）
        self._auto_reconnect: bool = False
        # 重连最大尝试次数（0 表示不限制，一直重试到主动退出）
        self._max_reconnect_attempts: int = 10
        # 心跳线程引用
        self._heartbeat_thread: Optional[threading.Thread] = None

    # ---------- 连接与加入 ----------

    def connect(
        self,
        host: str,
        port: int,
        name: str,
        timeout: float = 10.0,
        auto_reconnect: bool = True,
        max_reconnect_attempts: int = 10,
    ) -> bool:
        """连接服务器并发送 join 请求，同步等待加入结果。

        Args:
            host: 服务器地址（支持 IP / 域名）。
            port: 服务器端口。
            name: 玩家昵称。
            timeout: 连接与等待 join 结果的超时秒数。
            auto_reconnect: 是否启用断线自动重连。启用后，网络异常导致的断开
                会自动以指数退避间隔重连并重新加入（携带 reconnect 标记，接管
                旧席位），无需玩家手动操作。
            max_reconnect_attempts: 重连最大尝试次数（0 表示不限制）。

        Returns:
            True 表示加入成功，False 表示失败（错误信息见 last_error）。

        Raises:
            ClientError: 网络连接失败时抛出。
        """
        self.name = name.strip()
        if not self.name:
            raise ClientError("昵称不能为空")

        # 保存连接目标与重连配置，供断线后的自动重连使用
        self._host = host
        self._port = port
        self._auto_reconnect = auto_reconnect
        self._max_reconnect_attempts = max_reconnect_attempts
        # 重置断开相关标记（可能从上次运行/重连中恢复）
        self._disconnect_requested = False
        self._disconnect_logged = False
        # 标记连接流程已激活：读取/心跳线程据此决定是否继续运行
        self._running = True

        # 建立连接并完成首次加入
        if not self._establish(host, port, self.name, timeout, raise_on_fail=True):
            return False

        # 启动后台读取线程与心跳线程
        self._start_worker_threads()

        # 统一事件日志：客户端成功连接并加入房间（含分配的玩家 ID）
        log_event(
            EVT_CLIENT_CONNECT,
            f"客户端 {self.name} 连接并加入房间 {host}:{port}",
            host=host,
            port=port,
            player_id=self.player_id,
            is_host=self.is_host,
        )
        return True

    def _establish(
        self,
        host: str,
        port: int,
        name: str,
        timeout: float,
        raise_on_fail: bool,
        is_reconnect: bool = False,
    ) -> bool:
        """建立 TCP 连接并完成加入（供首次连接与重连复用）。

        Args:
            host: 服务器地址。
            port: 服务器端口。
            name: 玩家昵称。
            timeout: 连接与等待加入响应的超时秒数。
            raise_on_fail: 为 True 时网络层失败抛出 ClientError（首次连接语义）；
                为 False 时返回 False（重连语义，由重连循环处理）。
            is_reconnect: 是否为重连加入。为 True 时 join 携带 reconnect 标记，
                允许接管服务器上残留的同名死连接席位。

        Returns:
            True 表示加入成功，False 表示失败（仅 raise_on_fail=False 时返回）。

        Raises:
            ClientError: raise_on_fail=True 且网络连接失败/超时时抛出。
        """
        # 若存在残留 socket（上次连接未清理），先关闭避免泄漏
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

        # 建立 TCP 连接（timeout 仅用于限制连接建立阶段的等待时长）
        try:
            self._sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            # 统一事件日志：网络连接失败（记录目标地址与错误详情）
            log_event(
                EVT_CLIENT_ERROR,
                f"无法连接到服务器 {host}:{port}",
                host=host,
                port=port,
                error=str(exc),
            )
            if raise_on_fail:
                raise ClientError(f"无法连接到 {host}:{port}: {exc}") from exc
            return False

        # 【重点注释】清除连接阶段残留的超时设置：
        # socket.create_connection 的 timeout 参数会被保留到返回的 socket 上，
        # 导致后续 recv() 空闲超过 timeout 秒就抛 socket.timeout（OSError 子类）。
        # 游戏协议中 recv 必须无限期阻塞：等待开局、玩家思考、对方行动都可能
        # 远超任意超时值，因此连接建立后必须恢复为阻塞模式（无超时）。
        self._sock.settimeout(None)

        # 清空 join 同步状态
        self._join_error = ""
        self._joined = False

        # 发送 join 消息（重连时携带 reconnect 标记以接管旧席位）
        try:
            self._send_raw(Msg.join(self.name, reconnect=is_reconnect))
        except ClientError as exc:
            # 统一事件日志：发送加入请求失败（连接异常）
            log_event(
                EVT_CLIENT_ERROR,
                f"发送加入请求失败 {host}:{port}",
                host=host,
                port=port,
                error=str(exc),
            )
            if raise_on_fail:
                raise
            return False

        # 【重点注释】同步等待 join_ok / join_fail 响应：
        # _establish 可能被两条路径调用——首次连接时读取线程尚未启动，重连时
        # 调用者就是读取线程本身（_try_reconnect），两种情况下都没有后台线程
        # 在消费消息，因此必须在这里用带超时的循环直接读取 socket 并解析。
        # 阻塞超时期间临时启用 socket 超时，收到响应或超时后恢复为无限阻塞。
        if not self._wait_join_response(timeout):
            if raise_on_fail:
                raise ClientError("等待加入响应超时")
            return False

        if not self._joined:
            # 统一事件日志：服务器拒绝加入（记录拒绝原因）
            log_event(
                EVT_CLIENT_ERROR,
                f"加入房间失败 {host}:{port}",
                host=host,
                port=port,
                reason=self._join_error or "未知原因",
            )
            return False
        return True

    def _wait_join_response(self, timeout: float) -> bool:
        """在超时时间内同步读取 socket，直到收到 join_ok / join_fail。

        Args:
            timeout: 最大等待秒数。

        Returns:
            True 表示在超时内收到了 join 响应（结果见 self._joined）；
            False 表示超时或连接中断。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # 临时设置接收超时：超过剩余等待时间则报 socket.timeout，
            # 循环会继续判断是否已超时并退出
            self._sock.settimeout(min(remaining, 1.0))
            try:
                data = self._sock.recv(4096)
            except socket.timeout:
                # 单次小超时：未收到数据，回到循环判断总超时
                continue
            except OSError as exc:
                logger.warning("等待加入响应时连接异常: %s", exc)
                return False
            finally:
                # 恢复无限阻塞模式（游戏对局中的 recv 必须无限期等待）
                self._sock.settimeout(None)
            if not data:
                # 服务器在 join 响应前就关闭了连接
                logger.warning("等待加入响应时服务器关闭连接")
                return False
            try:
                messages = self._parse(data)
            except ProtocolError as exc:
                logger.warning("等待加入响应时协议错误: %s", exc)
                return False
            for msg in messages:
                # _handle_incoming 会处理 join_ok（置 _joined=True）与
                # join_fail（置 _join_error），并唤醒 join 同步事件
                self._handle_incoming(msg)
            # 收到任一 join 响应即结束等待
            if self._joined or self._join_error:
                return True
        return False

    def _start_worker_threads(self) -> None:
        """启动读取线程与心跳线程（首次连接与重连成功后均需调用）。"""
        # 启动后台读取线程
        reader = threading.Thread(target=self._reader_loop, daemon=True)
        reader.start()
        # 启动心跳线程（若尚未运行）
        if self._heartbeat_thread is None or not self._heartbeat_thread.is_alive():
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop, daemon=True
            )
            self._heartbeat_thread.start()

    def disconnect(self) -> None:
        """断开连接，发送 leave 后关闭 socket。

        同时置位 _disconnect_requested 与 _running=False，让读取线程（若在
        重连中）立即放弃重试，心跳线程停止发送。
        """
        # 记录断开前是否处于已连接状态，从未连接时无需写断开日志
        was_connected = self._sock is not None
        # 标记主动断开，供读取线程据此正确写断开原因（见 __init__ 注释），
        # 并阻止断线后的自动重连继续尝试
        self._disconnect_requested = True
        self._auto_reconnect = False
        if self._sock is not None:
            try:
                self._send_raw(Msg.leave())
            except Exception:  # pylint: disable=broad-except
                pass
        self._running = False
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        # 统一事件日志：客户端主动断开（与读取线程异常退出共享同一标记，避免重复记录）
        if was_connected and not self._disconnect_logged:
            self._disconnect_logged = True
            log_event(
                EVT_CLIENT_DISCONNECT,
                f"客户端 {self.name} 主动断开连接",
                player_id=self.player_id if self.player_id is not None else "-",
                reason="主动断开",
            )

    @property
    def last_error(self) -> str:
        """返回最近一次 join 错误信息。"""
        return self._join_error

    @property
    def is_connected(self) -> bool:
        """是否仍处于连接状态。"""
        return self._running and self._sock is not None

    # ---------- 发送接口 ----------

    def send_action(self, action: str, amount: int = 0) -> None:
        """发送游戏行动。

        Args:
            action: 行动名称（fold/check/call/raise/all_in）。
            amount: 加注目标金额，仅 raise 使用。
        """
        self._send_raw(Msg.action(action, amount))

    def send_start(self) -> None:
        """请求房主开局。"""
        self._send_raw(Msg.start())

    def send_reset(self) -> None:
        """请求重置房间（仅房主调用有效）。

        发送 reset_req 后，服务器会校验权限并广播重置结果；若房主已转移
        （例如旧房主离开后自动交接），本请求会被服务器拒绝并返回错误提示。
        """
        self._send_raw(Msg.reset_req())

    def send_chat(self, text: str) -> None:
        """发送聊天。"""
        self._send_raw(Msg.chat(text))

    def send_ready(self) -> None:
        """发送准备就绪。"""
        self._send_raw(Msg.ready())

    def _send_raw(self, message: dict) -> None:
        """底层发送：编码并写入 socket。

        Args:
            message: 消息字典。

        Raises:
            ClientError: 连接已关闭或发送失败时抛出。
        """
        if self._sock is None:
            raise ClientError("未连接到服务器")
        try:
            data = encode_message(message)
            self._sock.sendall(data)
        except (OSError, ProtocolError) as exc:
            raise ClientError(f"发送失败: {exc}") from exc

    # ---------- 接收循环 ----------

    def _reader_loop(self) -> None:
        """后台线程：持续读取 socket 并解析消息入队。

        该线程是连接存活的关键：
        - recv 返回空字节 = 服务器关闭连接
        - recv 抛 socket.timeout = 空闲超时，不是断开信号，继续等待
        - recv 抛其他 OSError = 网络异常，视为断开

        断线后的处理策略：
        - 若启用了自动重连且不是主动断开，则进入指数退避重连循环
          （_try_reconnect），重连成功后继续读取新连接上的消息；
        - 重连失败（达到次数上限）或主动断开时，向 UI 发送 _disconnected
          消息并附上原因，便于排查。
        """
        # 记录本次线程最终退出的原因，随 _disconnected 消息带给 UI
        exit_reason: str = "正常结束"
        try:
            # 外层循环：每轮对应一次"建立连接后的持续读取"会话
            # （首次连接 + 每次成功重连都会进入新的一轮）
            while self._running and self._sock is not None:
                exit_reason = self._read_session()
                # 主动断开：不再重连，直接结束
                if self._disconnect_requested:
                    break
                # 意外断开：若启用自动重连，尝试重连；成功则回到外层循环
                if self._auto_reconnect:
                    if self._try_reconnect():
                        continue
                break
        finally:
            self._running = False
            # 统一事件日志：连接断开（服务器关闭或网络错误），随原因一并记录
            if not self._disconnect_logged:
                self._disconnect_logged = True
                log_event(
                    EVT_CLIENT_DISCONNECT,
                    f"客户端 {self.name} 连接断开",
                    player_id=self.player_id if self.player_id is not None else "-",
                    reason=exit_reason,
                )
            # 通知 UI 连接断开，并附带具体原因
            self._inbox.put({"type": "_disconnected", "reason": exit_reason})

    def _read_session(self) -> str:
        """读取一次连接会话：持续 recv 直到连接断开，返回断开原因。

        Returns:
            断开原因字符串；若为 _RECONNECTED，表示本次会话异常结束但重连
            已在 _reader_loop 外层处理。
        """
        # 记录本次会话退出的原因（网络错误 / 服务器关闭 / 协议错误等）
        exit_reason: str = "读取会话结束"
        while self._running and self._sock is not None:
            try:
                data = self._sock.recv(4096)
            except socket.timeout:
                # 【重点注释】socket.timeout 仅表示"一段时间内没有数据到达"，
                # 并非连接断开的信号。对局中等待对方行动可能持续很久，
                # 因此超时后应继续阻塞等待，而不是退出。
                logger.debug("recv 空闲超时，继续等待")
                continue
            except OSError as exc:
                # 其他网络错误（连接重置、对端关闭等）：视为断开。
                # 若是对端主动调用 disconnect() 关闭 socket 导致的报错，
                # 退出原因应记为"主动断开"，而不是误导性的网络错误
                if self._disconnect_requested:
                    exit_reason = "主动断开"
                else:
                    exit_reason = f"网络错误: {exc}"
                break
            if not data:
                # 服务器关闭连接（收到 FIN）
                exit_reason = "服务器关闭连接"
                break
            try:
                messages = self._parse(data)
            except ProtocolError as exc:
                logger.warning("协议错误: %s", exc)
                # 统一事件日志：客户端侧协议错误（帧损坏或协议不匹配）
                log_event(
                    EVT_CLIENT_ERROR,
                    f"客户端协议错误: {exc}",
                    error=str(exc),
                )
                self._inbox.put({"type": "error", "message": f"协议错误: {exc}"})
                exit_reason = f"协议错误: {exc}"
                break
            for msg in messages:
                self._handle_incoming(msg)
        return exit_reason

    def _try_reconnect(self) -> bool:
        """按指数退避间隔尝试重连，直到成功或达到次数上限。

        重连流程：向 UI 发送 _reconnecting 提示 → 等待退避间隔 → 调用
        _establish（携带 reconnect 标记以接管旧席位）→ 成功后重置断开标记、
        重启读取/心跳线程，向 UI 发送 _reconnected。

        Returns:
            True 表示重连成功（_reader_loop 继续读取新连接）；False 表示放弃。
        """
        # 通知 UI 进入重连状态（界面会显示"正在重连..."）
        self._inbox.put({"type": "_reconnecting"})
        delay: float = RECONNECT_BASE_DELAY
        attempts: int = 0
        while self._running and not self._disconnect_requested:
            # 达到重连次数上限则放弃（0 表示不限制）
            if self._max_reconnect_attempts and attempts >= self._max_reconnect_attempts:
                logger.warning("重连失败：达到次数上限 %d", self._max_reconnect_attempts)
                return False
            time.sleep(delay)
            attempts += 1
            try:
                ok = self._establish(
                    self._host, self._port, self.name, RECONNECT_TIMEOUT,
                    raise_on_fail=False, is_reconnect=True,
                )
            except ClientError:
                ok = False
            if ok:
                # 重连成功：重置断开标记，使后续真实断开能被正确记录
                self._disconnect_requested = False
                self._disconnect_logged = False
                self._running = True
                logger.info("重连成功（第 %d 次尝试）", attempts)
                # 重启读取/心跳线程（读取线程即本线程自身，故只重启心跳）
                if self._heartbeat_thread is None or not self._heartbeat_thread.is_alive():
                    self._heartbeat_thread = threading.Thread(
                        target=self._heartbeat_loop, daemon=True
                    )
                    self._heartbeat_thread.start()
                # 通知 UI 重连成功
                self._inbox.put({"type": "_reconnected"})
                return True
            # 指数退避：0.5s → 1s → 2s → ... → 上限 10s
            delay = min(delay * 2, RECONNECT_MAX_DELAY)
        return False

    def _heartbeat_loop(self) -> None:
        """心跳线程：周期性地向服务器发送 ping 维持连接活性。

        【重点注释】发送失败不在此处理：若 socket 已不可用，读取线程会因
        recv 报错/返回空而发现断开并进入重连流程，此处无需重复报错。
        发送失败时打印调试日志即可，不影响主流程。
        """
        while self._running and not self._disconnect_requested:
            time.sleep(HEARTBEAT_INTERVAL)
            # 再次检查状态，避免休眠期间已断开还发送无效数据
            if not self._running or self._disconnect_requested or self._sock is None:
                continue
            try:
                self._send_raw(Msg.ping())
                logger.debug("心跳 ping 已发送")
            except ClientError as exc:
                logger.debug("心跳发送失败（交由读取线程处理断开）: %s", exc)

    def _parse(self, data: bytes) -> List[dict]:
        """将新字节追加到缓冲区并解析出完整消息。

        Args:
            data: 新接收字节。

        Returns:
            本次解析出的消息列表。
        """
        self._recv_buffer.extend(data)
        messages: List[dict] = []
        while True:
            msg = decode_message_from_buffer(self._recv_buffer)
            if msg is None:
                break
            messages.append(msg)
        return messages

    def _handle_incoming(self, msg: dict) -> None:
        """处理一条入站消息：更新本地缓存并入队供 UI 处理。

        对部分关键消息（join_ok、state、deal_hole、turn）在此处直接更新本地
        缓存，UI 可直接读取 client.state / client.hole_cards 等属性，无需
        重复解析。

        Args:
            msg: 入站消息字典。
        """
        msg_type = msg.get("type", "")

        if msg_type == "join_ok":
            # 加入成功：记录 player_id、房主标记与初始状态
            self.player_id = int(msg.get("player_id", 0))
            self.is_host = bool(msg.get("is_host", False))
            self.state = msg.get("state")
            self._joined = True
        elif msg_type == "join_fail":
            # 加入失败：记录原因（_wait_join_response 据此结束等待）
            self._join_error = str(msg.get("reason", "加入失败"))
            self._joined = False
        elif msg_type == "state":
            # 状态更新：缓存最新快照
            self.state = msg.get("state")
            # 状态变化后清空过期的 turn_options（若不再轮到自己）
            if self.turn_options is not None and self.state is not None:
                current_id = self.state.get("current_player_id")
                if current_id != self.player_id:
                    self.turn_options = None
        elif msg_type == "deal_hole":
            # 底牌下发：反序列化为 Card 对象
            cards_data = msg.get("cards", [])
            self.hole_cards = [Card.from_dict(c) for c in cards_data]
        elif msg_type == "turn":
            # 轮到自己行动：缓存可选项
            self.turn_options = msg.get("options")
        elif msg_type == "_disconnected":
            pass

        # 所有消息都入队，供 UI 做事件驱动刷新（如日志、聊天、错误提示）
        self._inbox.put(msg)

    # ---------- UI 消费接口 ----------

    def poll_message(self, timeout: float = 0.0) -> Optional[dict]:
        """从入站队列取一条消息，无消息时返回 None。

        Args:
            timeout: 阻塞等待秒数，0 表示非阻塞。

        Returns:
            消息字典或 None。
        """
        try:
            return self._inbox.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain_messages(self) -> List[dict]:
        """取出队列中当前所有消息（非阻塞）。

        Returns:
            消息列表，可能为空。
        """
        messages: List[dict] = []
        while True:
            try:
                messages.append(self._inbox.get_nowait())
            except queue.Empty:
                break
        return messages

    @property
    def is_my_turn(self) -> bool:
        """当前是否轮到自己行动。"""
        return self.turn_options is not None and bool(
            self.turn_options.get("can_act")
        )
