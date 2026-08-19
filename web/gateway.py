"""Web 网关服务器：将命令行德州扑克扩展到浏览器访问。

整体架构（模块化设计，游戏核心逻辑零改动）：
```
浏览器（单页应用）
    │  REST API（操作：加入/行动/聊天/控制）
    │  SSE 长连接（实时状态推送）
    ▼
WebGateway（本模块）
    │  每个浏览器会话 = 一个内部 GameClient（复用现有 TCP 协议）
    ▼
GameServer（内嵌启动，或复用外部已有服务器）
    │  承载全部权威游戏规则
    ▼
core/game.py（德州扑克核心逻辑）
```

核心特性：
- 零第三方依赖：仅使用 Python 标准库（http.server / threading / queue）。
- 实时数据同步：SSE（Server-Sent Events）长连接，服务器有状态变化立即推送，
  无轮询开销，延迟低；EventSource 被 Chrome/Firefox/Safari/Edge 全支持。
- 服务器控制模块：启动 / 暂停 / 恢复 / 停止 游戏服务器（REST 接口）。
- 会话隔离：每个浏览器标签页独立 Session（内部一个 GameClient），
  断线重连、心跳保活等能力直接复用 network.client 的既有实现。

HTTP API 一览：
    POST /api/join          加入房间（创建会话），body: {"name": "昵称"}
    POST /api/action        发送行动，body: {"session_id","action","amount"}
    POST /api/start         房主开局
    POST /api/reset         房主重置房间
    POST /api/chat          发送聊天
    POST /api/control       服务器控制，body: {"action": "start|pause|resume|stop"}
    GET  /api/status        查询服务器运行状态
    GET  /api/events        建立 SSE 长连接（按会话推送实时消息）
    GET  /                   静态页面（index.html）
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import queue
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

from core.event_log import (
    EVT_GENERAL,
    EVT_SERVER_START,
    EVT_SERVER_STOP,
    log_event,
)
from network.client import ClientError, GameClient
from network.server import GameServer

# 本模块的日志记录器（在 main 的 logging.basicConfig 下自动生效）
logger = logging.getLogger("poker.web")

# 静态资源根目录：与网关同级的 static 目录
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# 服务器控制命令（HTTP body 的 action 字段取值）
CONTROL_START: str = "start"    # 启动游戏服务器
CONTROL_PAUSE: str = "pause"    # 暂停牌局（拒绝所有行动）
CONTROL_RESUME: str = "resume"  # 恢复牌局
CONTROL_STOP: str = "stop"      # 停止游戏服务器

# SSE 心跳注释行发送间隔（秒）：代理/防火墙可能因长时间静默断开空闲连接，
# 周期性发送注释行让连接保持活跃
SSE_HEARTBEAT_SECONDS: float = 15.0


class Session:
    """一个浏览器会话：内部持有一个连接到游戏服务器的 GameClient。

    会话将服务器推送的消息（state/turn/log/chat 等）转发到 events 队列，
    再由 SSE 长连接发送给浏览器。浏览器断开后由网关清理并关闭内部客户端。

    Attributes:
        session_id: 会话唯一 ID（浏览器在后续请求中携带）。
        name: 玩家昵称（从 GameClient 同步）。
        client: 内部游戏客户端（复用现有网络协议）。
        events: 待推送给浏览器的消息队列。
        created_at: 会话创建时间戳。
        closed: 是否已关闭（关闭后转发线程与 SSE 连接停止工作）。
    """

    def __init__(self, session_id: str, client: GameClient) -> None:
        """初始化会话。

        Args:
            session_id: 唯一会话 ID。
            client: 已成功加入房间的内部游戏客户端。
        """
        self.session_id: str = session_id
        self.client: GameClient = client
        self.name: str = client.name
        # SSE 推送队列：服务器消息在此排队等待发送给浏览器
        self.events: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.created_at: float = time.time()
        self.closed: bool = False


class WebGateway:
    """Web 网关：HTTP REST API + SSE 推送 + 游戏服务器生命周期管理。

    通过内嵌启动（或连接外部已有）GameServer 承载权威游戏状态，
    浏览器侧每个标签页对应一个 Session（内部一个 GameClient），
    实现"浏览器 ↔ WebGateway ↔ GameServer"三层解耦的模块化架构。

    Attributes:
        host: HTTP 服务监听地址。
        web_port: HTTP 服务端口。
        game_host: 游戏服务器地址（内嵌启动时始终为 127.0.0.1）。
        game_port: 游戏服务器 TCP 端口。
        public_ip: 对外公告 IP（透传给内嵌 GameServer）。
        starting_chips: 玩家初始筹码（透传）。
        max_connections: 游戏服务器最大连接数（透传）。
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        web_port: int = 8000,
        game_host: str = "127.0.0.1",
        game_port: int = 8888,
        public_ip: Optional[str] = None,
        starting_chips: int = 1000,
        max_connections: int = 50,
        auto_start: bool = True,
    ) -> None:
        """初始化网关。

        Args:
            host: HTTP 服务监听地址，0.0.0.0 表示对外可访问。
            web_port: HTTP 服务端口。
            game_host: 内部客户端连接游戏服务器所用地址。
            game_port: 游戏服务器 TCP 端口（若该端口无监听则内嵌启动）。
            public_ip: 对外公告 IP，透传给内嵌 GameServer 用于提示。
            starting_chips: 玩家初始筹码。
            max_connections: 游戏服务器连接上限。
            auto_start: 为 True 时网关启动自动确保游戏服务器可用。
        """
        self.host: str = host
        self.web_port: int = web_port
        self.game_host: str = game_host
        self.game_port: int = game_port
        self.public_ip: Optional[str] = public_ip
        self.starting_chips: int = starting_chips
        self.max_connections: int = max_connections

        # 活动会话表：session_id -> Session（线程安全访问）
        self._sessions: Dict[str, Session] = {}
        self._lock: threading.Lock = threading.Lock()
        # 暂停标记：暂停期间所有行动请求被拒绝（牌局冻结，不丢状态）
        self._paused: bool = False

        # 内嵌游戏服务器（仅当目标端口无监听时由本网关启动）
        self._embedded_server: Optional[GameServer] = None
        self._embedded_thread: Optional[threading.Thread] = None
        # HTTP 服务器实例（由 start/stop 管理）
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._started: bool = False

        if auto_start:
            self._ensure_server()

    # ---------- 生命周期管理 ----------

    def start(self) -> None:
        """启动 HTTP 服务器并进入事件循环（阻塞）。

        端口绑定失败（被占用）时抛出 OSError，由调用方捕获处理。
        """
        # 创建线程化 HTTP 服务器：每个请求一个线程，天然支持并发 SSE 与 POST
        handler_factory = self._make_handler()
        self._httpd = ThreadingHTTPServer((self.host, self.web_port), handler_factory)
        self._started = True
        log_event(EVT_SERVER_START, f"Web 网关已启动: {self.host}:{self.web_port}")
        logger.info("Web 界面已启动: http://%s:%d", self._local_ip(), self.web_port)
        try:
            # serve_forever 阻塞直到 stop() 被调用
            self._httpd.serve_forever()
        finally:
            # 退出循环后统一清理：关闭所有会话与内嵌服务器
            self._close_all_sessions()
            self.stop_embedded()

    def stop(self) -> None:
        """停止 HTTP 服务器（优雅关闭，等待当前请求处理完成）。"""
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        self._started = False
        self._close_all_sessions()
        self.stop_embedded()
        log_event(EVT_SERVER_STOP, "Web 网关已停止")

    # ---------- 游戏服务器控制模块 ----------

    def _ensure_server(self) -> bool:
        """确保游戏服务器可用：端口有监听则复用，否则内嵌启动。

        Returns:
            True 表示服务器可用。
        """
        if self._port_in_use(self.game_host, self.game_port):
            # 已有服务器在监听（独立部署场景）：直接复用，不重复启动
            logger.info("检测到游戏服务器已在 %s:%d 监听，直接复用", self.game_host, self.game_port)
            return True
        if self._embedded_server is not None:
            # 已内嵌启动过（且未被停止），无需重复启动
            return True
        # 内嵌启动 GameServer（绑定 0.0.0.0，允许远程 CLI 客户端直接连入）
        self._embedded_server = GameServer(
            host="0.0.0.0",
            port=self.game_port,
            starting_chips=self.starting_chips,
            max_connections=self.max_connections,
            public_ip=self.public_ip,
        )
        self._embedded_thread = threading.Thread(
            target=self._embedded_server.start, daemon=True
        )
        self._embedded_thread.start()
        # 等待端口就绪（最多 5 秒），避免浏览器连接时服务器尚未完成监听
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if self._port_in_use("127.0.0.1", self.game_port):
                logger.info("内嵌游戏服务器已启动: 0.0.0.0:%d", self.game_port)
                return True
            time.sleep(0.05)
        logger.error("内嵌游戏服务器启动超时")
        return False

    def stop_embedded(self) -> None:
        """停止内嵌游戏服务器（若由本网关启动）。"""
        if self._embedded_server is not None:
            try:
                self._embedded_server.stop()
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("停止内嵌服务器异常: %s", exc)
            self._embedded_server = None
            self._embedded_thread = None

    def handle_control(self, action: str) -> Dict[str, Any]:
        """处理服务器控制命令（启动/暂停/恢复/停止）。

        Args:
            action: 控制命令，取 CONTROL_START/PAUSE/RESUME/STOP。

        Returns:
            响应字典，包含 ok 与必要的状态信息。

        Raises:
            ValueError: 未知控制命令。
        """
        if action == CONTROL_START:
            # 启动：确保游戏服务器可用（端口已监听或内嵌启动）
            ok = self._ensure_server()
            return {"ok": ok, "running": ok}
        if action == CONTROL_PAUSE:
            # 暂停：冻结牌局，暂停期间所有行动请求被拒绝
            self._paused = True
            # 广播暂停事件给所有在线会话，前端同步更新控制按钮状态
            self._broadcast_control("paused")
            log_event(EVT_GENERAL, "Web 控制台：游戏已暂停")
            return {"ok": True, "paused": True}
        if action == CONTROL_RESUME:
            # 恢复：解除冻结
            self._paused = False
            self._broadcast_control("resumed")
            log_event(EVT_GENERAL, "Web 控制台：游戏已恢复")
            return {"ok": True, "paused": False}
        if action == CONTROL_STOP:
            # 停止：关闭内嵌服务器与所有在线会话
            self.stop_embedded()
            self._close_all_sessions()
            log_event(EVT_GENERAL, "Web 控制台：游戏服务器已停止")
            return {"ok": True, "running": False}
        raise ValueError(f"未知控制命令: {action}")

    # ---------- 会话管理 ----------

    def create_session(self, name: str) -> Dict[str, Any]:
        """创建浏览器会话：内部客户端连接游戏服务器并加入房间。

        Args:
            name: 玩家昵称。

        Returns:
            成功返回 {"ok": True, "session_id", "player_id", "is_host", "state"}；
            失败返回 {"ok": False, "error"}。

        Raises:
            ClientError: 网络连接失败时抛出（由调用方转换为 HTTP 错误）。
        """
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "昵称不能为空"}
        # 确保游戏服务器可用（页面可能在服务器停止后仍请求加入）
        if not self._ensure_server():
            return {"ok": False, "error": "游戏服务器不可用"}
        # 创建内部客户端并连接：复用完整协议（心跳、重连、席位接管等）
        client = GameClient()
        try:
            ok = client.connect(
                self.game_host,
                self.game_port,
                name,
                timeout=5.0,
                auto_reconnect=True,
                max_reconnect_attempts=3,
            )
        except ClientError as exc:
            # 连接层异常（拒绝连接等）：清理并返回错误
            return {"ok": False, "error": str(exc)}
        if not ok:
            # 服务器拒绝加入（昵称冲突、房间已满等）
            return {"ok": False, "error": client.last_error or "加入失败"}
        # 分配唯一会话 ID 并登记
        session_id = uuid.uuid4().hex[:12]
        session = Session(session_id, client)
        with self._lock:
            self._sessions[session_id] = session
        # 启动转发线程：把内部客户端的消息搬到会话的 SSE 队列
        threading.Thread(target=self._session_forwarder, args=(session,), daemon=True).start()
        logger.info("Web 会话 %s：玩家 %s 已加入", session_id, name)
        return {
            "ok": True,
            "session_id": session_id,
            "player_id": client.player_id,
            "name": name,
            "is_host": client.is_host,
            "paused": self._paused,
            "state": client.state,
        }

    def get_session(self, session_id: Optional[str]) -> Optional[Session]:
        """按 ID 获取活动会话，不存在返回 None。"""
        if not session_id:
            return None
        with self._lock:
            return self._sessions.get(session_id)

    def close_session(self, session_id: str) -> None:
        """关闭指定会话：断开内部客户端并从会话表移除。"""
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return
        # 标记关闭：转发线程与 SSE 循环据此停止
        session.closed = True
        try:
            session.client.disconnect()
        except Exception:  # pylint: disable=broad-except
            pass
        logger.info("Web 会话 %s 已关闭", session_id)

    def _close_all_sessions(self) -> None:
        """关闭所有活动会话（服务器停止/网关停止时调用）。"""
        with self._lock:
            # 先取出全部会话对象再清空表，避免并发 join 期间反复加锁
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.closed = True
            try:
                session.client.disconnect()
            except Exception:  # pylint: disable=broad-except
                pass

    def _get_session_unlocked(self, session_id: str) -> Optional[Session]:
        """获取会话（调用方须已持有锁）。"""
        return self._sessions.get(session_id)

    def _session_forwarder(self, session: Session) -> None:
        """会话转发线程：将内部客户端的入站消息搬到 SSE 推送队列。

        这是 Web 与游戏核心"实时数据同步"的关键路径：GameClient 的消息
        队列（state/turn/log/chat/hand_over 等）由本线程及时搬到浏览器。
        """
        while not session.closed:
            try:
                msg = session.client.poll_message(timeout=0.3)
            except Exception as exc:  # pylint: disable=broad-except
                # 读取异常（连接已关闭等）：结束转发，等待浏览器侧处理
                logger.debug("会话转发异常: %s", exc)
                break
            if msg is not None:
                session.events.put(msg)

    # ---------- 内部工具 ----------

    @staticmethod
    def _port_in_use(host: str, port: int) -> bool:
        """探测目标端口是否已被监听（判断是否已有服务器在运行）。"""
        try:
            sock = socket.create_connection((host, port), timeout=0.5)
            sock.close()
            return True
        except OSError:
            return False

    def _local_ip(self) -> str:
        """获取本机局域网 IP 用于提示访问地址。"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # 连接一个外部地址（不实际发包）触发系统选路，得到本机出口 IP
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except OSError:
            return self.host

    def _broadcast_control(self, action: str) -> None:
        """向所有在线会话广播服务器控制事件（暂停/恢复状态变化）。"""
        event = {"type": "server_control", "action": action}
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            session.events.put(event)

    # ---------- HTTP 处理 ----------

    def _make_handler(self):
        """创建 HTTP 请求处理器类（绑定本网关实例）。"""
        # 【重点注释】闭包引用：handler 类方法中所有 gateway 均指向本实例
        gateway = self

        class _Handler(BaseHTTPRequestHandler):
            # 抑制默认的访问日志（由统一事件日志替代，减少噪音）
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass

            # ---- 路由分发 ----
            def do_GET(self) -> None:  # noqa: N802（HTTP 方法名保持大写）
                self._route_get()

            def do_POST(self) -> None:  # noqa: N802
                self._route_post()

            def _route_get(self) -> None:
                path = self.path.split("?", 1)[0]
                if path == "/":
                    self._serve_static("index.html")
                elif path.startswith("/static/"):
                    self._serve_static(path[len("/static/"):])
                elif path == "/api/status":
                    self._api_status()
                elif path == "/api/events":
                    # SSE 长连接：需要 session_id 参数
                    query = dict(
                        part.split("=", 1) for part in self.path.split("?", 1)[-1].split("&") if "=" in part
                    )
                    self._api_events(query.get("session_id", ""))
                elif path == "/api/help":
                    self._send_json(200, {"ok": True, "text": self._help_text()})
                else:
                    self._send_json(404, {"ok": False, "error": "接口不存在"})

            def _route_post(self) -> None:
                path = self.path.split("?", 1)[0]
                try:
                    body = self._read_json_body()
                except ValueError as exc:
                    self._send_json(400, {"ok": False, "error": str(exc)})
                    return
                if path == "/api/join":
                    self._api_join(body)
                elif path == "/api/action":
                    self._api_action(body)
                elif path == "/api/start":
                    self._api_command(body, "start")
                elif path == "/api/reset":
                    self._api_command(body, "reset")
                elif path == "/api/chat":
                    self._api_chat(body)
                elif path == "/api/control":
                    self._api_control(body)
                else:
                    self._send_json(404, {"ok": False, "error": "接口不存在"})

            # ---- 具体 API 实现 ----

            def _api_status(self) -> None:
                """GET /api/status：查询游戏服务器运行状态。"""
                status: Dict[str, Any] = {"running": False, "paused": gateway._paused}
                gw = gateway
                if gw._embedded_server is not None:
                    try:
                        status = gw._embedded_server.get_status()
                        status["running"] = True
                        status["paused"] = gw._paused
                    except Exception:  # pylint: disable=broad-except
                        status = {"running": False, "paused": gw._paused}
                elif gateway._port_in_use(gateway.game_host, gateway.game_port):
                    # 复用外部服务器：查询其状态
                    from network.client import query_server_status
                    try:
                        status = query_server_status(gateway.game_host, gateway.game_port, timeout=2.0)
                        status["running"] = True
                        status["paused"] = gateway._paused
                    except ClientError as exc:
                        status = {"running": False, "error": str(exc), "paused": gateway._paused}
                self._send_json(200, {"ok": True, "status": status})

            def _api_join(self, body: Dict[str, Any]) -> None:
                """POST /api/join：创建会话并加入房间。"""
                try:
                    result = gateway.create_session(str(body.get("name", "")))
                except ClientError as exc:
                    self._send_json(502, {"ok": False, "error": str(exc)})
                    return
                if not result.get("ok"):
                    self._send_json(400, result)
                    return
                self._send_json(200, result)

            def _api_action(self, body: Dict[str, Any]) -> None:
                """POST /api/action：发送游戏行动。"""
                session = gateway.get_session(body.get("session_id"))
                if session is None:
                    self._send_json(404, {"ok": False, "error": "会话不存在，请刷新页面重新加入"})
                    return
                if session.closed:
                    self._send_json(404, {"ok": False, "error": "会话已关闭，请刷新页面重新加入"})
                    return
                # 暂停控制：牌局冻结期间拒绝一切行动（防止状态漂移）
                if gateway._paused:
                    self._send_json(409, {"ok": False, "error": "游戏已暂停，请等待恢复"})
                    return
                action = str(body.get("action", "")).strip().lower()
                amount = body.get("amount", 0)
                try:
                    amount = int(amount)
                except (TypeError, ValueError):
                    amount = 0
                # 参数校验：加注必须携带正金额
                if action == "raise" and amount <= 0:
                    self._send_json(400, {"ok": False, "error": "加注金额必须为正数"})
                    return
                try:
                    # 发送行动（规则校验由服务器权威执行，错误会以 error 消息推回）
                    session.client.send_action(action, amount)
                except ClientError as exc:
                    self._send_json(502, {"ok": False, "error": str(exc)})
                    return
                self._send_json(200, {"ok": True})

            def _api_command(self, body: Dict[str, Any], command: str) -> None:
                """POST /api/start 与 /api/reset：房主专属命令（开局/重置）。"""
                session = gateway.get_session(body.get("session_id"))
                if session is None:
                    self._send_json(404, {"ok": False, "error": "会话不存在，请刷新页面重新加入"})
                    return
                if session.closed:
                    self._send_json(404, {"ok": False, "error": "会话已关闭，请刷新页面重新加入"})
                    return
                try:
                    if command == "start":
                        session.client.send_start()
                    else:
                        session.client.send_reset()
                except ClientError as exc:
                    self._send_json(502, {"ok": False, "error": str(exc)})
                    return
                self._send_json(200, {"ok": True})

            def _api_chat(self, body: Dict[str, Any]) -> None:
                """POST /api/chat：发送聊天消息。"""
                session = gateway.get_session(body.get("session_id"))
                if session is None:
                    self._send_json(404, {"ok": False, "error": "会话不存在，请刷新页面重新加入"})
                    return
                text = str(body.get("text", "")).strip()
                if not text:
                    self._send_json(400, {"ok": False, "error": "聊天内容不能为空"})
                    return
                try:
                    session.client.send_chat(text)
                except ClientError as exc:
                    self._send_json(502, {"ok": False, "error": str(exc)})
                    return
                self._send_json(200, {"ok": True})

            def _api_control(self, body: Dict[str, Any]) -> None:
                """POST /api/control：服务器控制（启动/暂停/恢复/停止）。"""
                action = str(body.get("action", "")).strip().lower()
                try:
                    result = gateway.handle_control(action)
                except ValueError as exc:
                    self._send_json(400, {"ok": False, "error": str(exc)})
                    return
                self._send_json(200, result)

            def _api_events(self, session_id: str) -> None:
                """GET /api/events：SSE 长连接推送（实时数据同步核心）。

                连接建立后持续推送：初始 join_ok 等积压消息、实时 state/turn/
                log/chat/hand_over，以及周期心跳注释行。浏览器断开或会话关闭
                时（写失败）结束本请求。
                """
                session = gateway.get_session(session_id)
                if session is None:
                    self._send_json(404, {"ok": False, "error": "会话不存在，请重新加入"})
                    return
                # 设置 SSE 响应头（text/event-stream 为 SSE 标准 MIME）
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")  # 禁用 Nginx 缓冲，保证实时性
                self.end_headers()
                # 连接建立先推送初始问候（含暂停状态，前端据此初始化控制按钮）
                self._write_sse({"type": "_hello", "paused": gateway._paused})
                last_heartbeat = time.monotonic()
                # 持续循环：从会话推送队列取消息并发送，直到会话关闭
                while not session.closed:
                    try:
                        msg = session.events.get(timeout=1.0)
                    except queue.Empty:
                        # 无消息时按需发送心跳注释行维持连接（防止代理超时断开）
                        if time.monotonic() - last_heartbeat >= SSE_HEARTBEAT_SECONDS:
                            try:
                                self._write_sse({"type": "_heartbeat"})
                            except (OSError, ConnectionError):
                                break
                            last_heartbeat = time.monotonic()
                        continue
                    try:
                        self._write_sse(msg)
                    except (OSError, ConnectionError):
                        # 浏览器断开：停止推送并关闭会话，避免资源泄漏
                        break
                gateway.close_session(session_id)

            def _write_sse(self, msg: Dict[str, Any]) -> None:
                """以 SSE 事件格式写入一条消息（data: JSON）。"""
                data = json.dumps(msg, ensure_ascii=False)
                # 一次性写入避免多次系统调用，提升吞吐
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()

            def _read_json_body(self) -> Dict[str, Any]:
                """读取并解析 JSON 请求体。"""
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length <= 0:
                    raise ValueError("请求体不能为空")
                if length > 1024 * 1024:
                    raise ValueError("请求体过大")
                raw = self.rfile.read(length)
                try:
                    data = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as exc:
                    raise ValueError(f"JSON 解析失败: {exc}") from exc
                if not isinstance(data, dict):
                    raise ValueError("请求体必须是 JSON 对象")
                return data

            def _send_json(self, code: int, payload: Dict[str, Any]) -> None:
                """发送 JSON 响应。"""
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def _serve_static(self, rel_path: str) -> None:
                """提供静态资源（HTML/CSS/JS），带目录穿越防护。"""
                # 归一化并确保路径不越出 static 目录（防 ../ 攻击）
                full = os.path.normpath(os.path.join(STATIC_DIR, rel_path))
                if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
                    self._send_json(404, {"ok": False, "error": "文件不存在"})
                    return
                content_type, _ = mimetypes.guess_type(full)
                if content_type is None:
                    content_type = "application/octet-stream"
                # 文本资源统一按 UTF-8 输出，避免中文乱码
                if content_type.startswith("text/") or content_type.endswith(("json", "javascript")):
                    content_type += "; charset=utf-8"
                try:
                    with open(full, "rb") as fh:
                        content = fh.read()
                except OSError as exc:
                    self._send_json(500, {"ok": False, "error": f"读取文件失败: {exc}"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(content)))
                # 开发期禁用缓存，便于改代码即时生效
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(content)

            @staticmethod
            def _help_text() -> str:
                """返回内置帮助文本（供 /api/help 使用）。"""
                return (
                    "德州扑克 Web 版使用说明：\n"
                    "1. 输入昵称并点击『进入牌桌』加入游戏；首位加入者自动成为房主。\n"
                    "2. 房主点击『开始游戏』发牌；每局结束后可再次点击开始下一局。\n"
                    "3. 轮到自己行动时点击对应按钮：跟注/让牌/加注/全下/弃牌。\n"
                    "4. 聊天输入框发送消息，与其他玩家即时交流。\n"
                    "5. 『控制台』面板可启动/暂停/恢复/停止游戏服务器（管理员功能）。\n"
                )

        return _Handler

    # 便捷引用：让 handler 内部统一通过 gateway 访问本实例
    # （在 _make_handler 闭包外无法直接引用，这里定义在 _make_handler 内）
    # 说明：handler 通过闭包捕获 gateway 变量，无需实例属性


# 顶层便捷函数：用于独立运行 web 网关（python -m web.gateway）
def main() -> None:
    """以独立进程方式运行 Web 网关（供调试/部署）。"""
    import argparse

    parser = argparse.ArgumentParser(description="德州扑克 Web 网关（调试模式）")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP 监听地址")
    parser.add_argument("--web-port", type=int, default=8000, help="HTTP 端口")
    parser.add_argument("--game-port", type=int, default=8888, help="游戏服务器端口")
    parser.add_argument("--public-ip", default=None, help="对外公告 IP")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    gateway = WebGateway(
        host=args.host,
        web_port=args.web_port,
        game_port=args.game_port,
        public_ip=args.public_ip,
    )
    try:
        gateway.start()
    except KeyboardInterrupt:
        gateway.stop()


if __name__ == "__main__":
    main()
