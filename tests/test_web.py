"""Web 网关单元/集成测试。

覆盖 Web 界面网关（web/gateway.py）的：
- 静态资源服务（index.html / CSS）
- 会话创建与加入（昵称校验、重名拒绝）
- SSE 实时推送（_hello / join_ok 事件）
- 服务器状态查询
- 服务器控制模块（启动/暂停/恢复/停止）
- 暂停期间行动被拒绝
- 多会话对局集成流程（开局 → 行动 → 本局结束）

全部使用 Python 标准库（http.client）驱动 HTTP 接口，无需浏览器。
"""
from __future__ import annotations

import http.client
import json
import socket
import threading
import time
import unittest
from typing import Any, Callable, Dict, Optional

from web.gateway import WebGateway
from network.server import GameServer


def _free_port() -> int:
    """获取一个空闲端口（用于随机分配 HTTP 与游戏端口）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _two_free_ports() -> tuple:
    """获取两个互不相同的空闲端口。"""
    p1 = _free_port()
    p2 = _free_port()
    while p2 == p1:
        p2 = _free_port()
    return p1, p2


class WebGatewayTest(unittest.TestCase):
    """Web 网关集成测试基类：每个测试启动一台全新网关。"""

    def setUp(self) -> None:
        """启动网关（含内嵌游戏服务器）与 HTTP 服务。"""
        self.web_port, self.game_port = _two_free_ports()
        # auto_start=False：先构造，再手动启动内嵌服务器，便于精确控制测试步骤
        self.gateway = WebGateway(
            host="127.0.0.1",
            web_port=self.web_port,
            game_host="127.0.0.1",
            game_port=self.game_port,
            auto_start=False,
        )
        # 启动游戏服务器（内嵌）
        self.gateway.handle_control("start")
        # 启动 HTTP 服务线程
        self.http_thread = threading.Thread(target=self.gateway.start, daemon=True)
        self.http_thread.start()
        self._wait_http_ready()

    def tearDown(self) -> None:
        """停止网关（含内嵌服务器与所有会话）。"""
        try:
            self.gateway.stop()
        except Exception:  # pylint: disable=broad-except
            pass
        time.sleep(0.1)

    # ---------- HTTP 辅助方法 ----------

    def _request(
        self, method: str, path: str, body: Optional[Dict[str, Any]] = None
    ) -> tuple:
        """发送 HTTP 请求，返回 (状态码, JSON 响应字典)。"""
        conn = http.client.HTTPConnection("127.0.0.1", self.web_port, timeout=5)
        try:
            if body is not None:
                payload = json.dumps(body).encode("utf-8")
                conn.request(
                    method, path, body=payload,
                    headers={"Content-Type": "application/json"},
                )
            else:
                conn.request(method, path)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8")
            code = resp.status
            try:
                data = json.loads(raw)
            except ValueError:
                data = {"_raw": raw}
            return code, data
        finally:
            conn.close()

    def _post(self, path: str, body: Dict[str, Any]) -> tuple:
        """POST JSON 请求。"""
        return self._request("POST", path, body)

    def _get(self, path: str) -> tuple:
        """GET 请求。"""
        return self._request("GET", path)

    def _wait_http_ready(self, timeout: float = 5.0) -> None:
        """轮询 HTTP 端口直到可连接。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                s = socket.create_connection(("127.0.0.1", self.web_port), timeout=0.3)
                s.close()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("HTTP 服务未在超时内就绪")

    def _wait_status_until(
        self, predicate: Callable[[Dict[str, Any]], bool], timeout: float = 8.0
    ) -> Dict[str, Any]:
        """轮询 /api/status 直到满足条件，返回最终状态。"""
        deadline = time.time() + timeout
        last: Dict[str, Any] = {}
        while time.time() < deadline:
            code, data = self._get("/api/status")
            if code == 200:
                last = data.get("status", {})
                if predicate(last):
                    return last
            time.sleep(0.1)
        raise TimeoutError(f"等待状态条件超时，最后状态: {last}")

    def _join(self, name: str) -> Dict[str, Any]:
        """加入房间，断言成功并返回 join 结果。"""
        code, data = self._post("/api/join", {"name": name})
        self.assertEqual(code, 200, f"加入失败: {data}")
        self.assertTrue(data.get("ok"), f"加入未成功: {data}")
        return data

    # ---------- 测试用例 ----------

    def test_index_page_served(self) -> None:
        """主页（index.html）应正常返回且包含核心组件。"""
        code, data = self._get("/")
        self.assertEqual(code, 200)
        raw = data.get("_raw", "")
        # 核心组件：加入界面、游戏界面、控制模块、聊天区
        self.assertIn("德州扑克", raw)
        self.assertIn("join-screen", raw)
        self.assertIn("game-screen", raw)
        self.assertIn("server-controls", raw)
        self.assertIn("log-box", raw)
        self.assertIn("action-bar", raw)

    def test_static_css_served(self) -> None:
        """样式表应正常返回（主题与响应式设计）。"""
        code, data = self._get("/static/css/style.css")
        self.assertEqual(code, 200)
        raw = data.get("_raw", "")
        self.assertIn("--table-green", raw)
        self.assertIn("@media", raw)  # 响应式布局媒体查询

    def test_join_creates_session_and_host(self) -> None:
        """第一个加入的玩家自动成为房主，并返回会话 ID 与初始状态。"""
        result = self._join("Alice")
        self.assertTrue(result.get("session_id"))
        self.assertTrue(result.get("player_id"))
        self.assertTrue(result.get("is_host"), "首位加入者应为房主")
        self.assertIsNotNone(result.get("state"))
        # 清理会话
        self.gateway.close_session(result["session_id"])

    def test_join_empty_name_rejected(self) -> None:
        """空昵称应被拒绝（参数校验）。"""
        code, data = self._post("/api/join", {"name": "  "})
        self.assertEqual(code, 400)
        self.assertIn("昵称", str(data.get("error", "")))

    def test_join_duplicate_name_rejected(self) -> None:
        """重复昵称应被拒绝（复用服务器去重机制）。"""
        self._join("Alice")
        code, data = self._post("/api/join", {"name": "Alice"})
        self.assertEqual(code, 400)
        self.assertTrue(data.get("error"))

    def test_sse_stream_receives_hello_and_join_ok(self) -> None:
        """SSE 长连接应推送 _hello 与 join_ok 事件（实时同步通道）。"""
        result = self._join("Alice")
        conn = http.client.HTTPConnection("127.0.0.1", self.web_port, timeout=5)
        try:
            conn.request("GET", f"/api/events?session_id={result['session_id']}")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            # SSE 必须使用 text/event-stream 内容类型
            ctype = resp.getheader("Content-Type", "")
            self.assertEqual(ctype.split(";")[0], "text/event-stream")
            # 读取若干事件，应包含连接问候与加入成功
            types = self._read_sse_types(resp, count=4, timeout=5.0)
            self.assertIn("_hello", types)
            self.assertIn("join_ok", types)
        finally:
            conn.close()
        self.gateway.close_session(result["session_id"])

    @staticmethod
    def _read_sse_types(resp: Any, count: int, timeout: float = 5.0) -> list:
        """从 SSE 响应流读取若干事件的 type 字段列表。"""
        types: list = []
        deadline = time.time() + timeout
        while len(types) < count and time.time() < deadline:
            line = resp.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if text.startswith("data: "):
                try:
                    msg = json.loads(text[len("data: "):])
                except ValueError:
                    continue
                types.append(msg.get("type"))
        return types

    def test_status_endpoint_reports_running(self) -> None:
        """状态查询应反映内嵌服务器运行与玩家在线情况。"""
        self._join("Alice")
        code, data = self._get("/api/status")
        self.assertEqual(code, 200)
        st = data.get("status", {})
        self.assertTrue(st.get("running"))
        self.assertEqual(st.get("online_players"), 1)

    def test_control_pause_blocks_action(self) -> None:
        """暂停后行动请求应被网关拒绝（409），恢复后放行。"""
        result = self._join("Alice")
        # 暂停服务器
        code, data = self._post("/api/control", {"action": "pause"})
        self.assertEqual(code, 200)
        self.assertTrue(data.get("paused"))
        # 暂停期间行动被拒
        code, data = self._post(
            "/api/action",
            {"session_id": result["session_id"], "action": "fold"},
        )
        self.assertEqual(code, 409)
        self.assertIn("暂停", str(data.get("error", "")))
        # 恢复后行动请求不再被网关拒绝（规则校验交给服务器）
        code, _ = self._post("/api/control", {"action": "resume"})
        self.assertEqual(code, 200)
        code, data = self._post(
            "/api/action",
            {"session_id": result["session_id"], "action": "fold"},
        )
        self.assertEqual(code, 200)
        self.assertTrue(data.get("ok"))
        self.gateway.close_session(result["session_id"])

    def test_control_stop_stops_server(self) -> None:
        """停止控制应关闭内嵌服务器，状态查询显示未运行。"""
        code, data = self._post("/api/control", {"action": "stop"})
        self.assertEqual(code, 200)
        self.assertFalse(data.get("running"))
        code, data = self._get("/api/status")
        self.assertEqual(code, 200)
        self.assertFalse(data.get("status", {}).get("running"))

    def test_unknown_endpoint_returns_404(self) -> None:
        """未知接口应返回 404（错误处理）。"""
        code, data = self._post("/api/unknown", {})
        self.assertEqual(code, 404)
        self.assertIn("不存在", str(data.get("error", "")))

    def test_action_without_session_rejected(self) -> None:
        """无会话的行动请求应返回 404（会话错误处理）。"""
        code, data = self._post(
            "/api/action",
            {"session_id": "nonexistent", "action": "fold"},
        )
        self.assertEqual(code, 404)
        self.assertIn("会话", str(data.get("error", "")))

    def test_two_player_hand_full_flow(self) -> None:
        """集成流程：两浏览器会话开局 → 当前行动者行动 → 本局结束。"""
        # 两名玩家加入（Alice 房主，Bob 玩家）
        a = self._join("Alice")
        b = self._join("Bob")
        self.assertTrue(a.get("is_host"))
        self.assertFalse(b.get("is_host"))

        # 房主开局
        code, data = self._post("/api/start", {"session_id": a["session_id"]})
        self.assertEqual(code, 200, f"开局失败: {data}")

        # 服务器状态应推进到第 1 局
        st = self._wait_status_until(lambda s: s.get("hand_number") == 1)
        self.assertIn(st.get("game_state"), ("PREFLOP", "FLOP", "TURN", "RIVER"))

        # 当前行动者行动（弃牌，2 人局中任意一人弃牌即结束）
        current_id = st.get("current_player_id")
        sid = a["session_id"] if current_id == a["player_id"] else b["session_id"]
        code, data = self._post(
            "/api/action", {"session_id": sid, "action": "fold"},
        )
        self.assertEqual(code, 200, f"行动失败: {data}")

        # 本局应结束
        st2 = self._wait_status_until(
            lambda s: s.get("game_state") == "HAND_OVER"
        )
        self.assertEqual(st2.get("hand_number"), 1)

        # 清理会话
        self.gateway.close_session(a["session_id"])
        self.gateway.close_session(b["session_id"])

    def test_raise_action_validates_amount(self) -> None:
        """加注金额非法时应被网关参数校验拒绝（错误处理）。"""
        result = self._join("Alice")
        code, data = self._post(
            "/api/action",
            {"session_id": result["session_id"], "action": "raise", "amount": 0},
        )
        self.assertEqual(code, 400)
        self.assertIn("金额", str(data.get("error", "")))
        self.gateway.close_session(result["session_id"])

    def test_web_gateway_reuses_external_game_server(self) -> None:
        """server 模式组合验证：外部 GameServer 监听时 Web 网关直接复用。

        对应 main.py 中 server 子命令的实现——先启动游戏服务器（后台线程），
        再创建 WebGateway(auto_start=True)。正确行为：复用已监听的游戏端口，
        而不是内嵌重复启动第二个服务器；Web 页面与加入对局均正常工作。
        """
        # 使用独立端口模拟 server 子命令的部署环境
        game_port = _free_port()
        web_port = _free_port()
        server = GameServer(host="127.0.0.1", port=game_port, starting_chips=1000)
        # 游戏服务器在后台线程运行（与 main.py server 模式一致）
        srv_thread = threading.Thread(target=server.start, daemon=True)
        srv_thread.start()
        try:
            # 等待游戏端口就绪（最多 5 秒）
            deadline = time.time() + 5.0
            ready = False
            while time.time() < deadline:
                try:
                    s = socket.create_connection(("127.0.0.1", game_port), timeout=0.3)
                    s.close()
                    ready = True
                    break
                except OSError:
                    time.sleep(0.05)
            self.assertTrue(ready, "游戏服务器未在超时内就绪")

            # 创建网关：auto_start=True 应复用外部服务器，不重复内嵌启动
            gateway = WebGateway(
                host="127.0.0.1",
                web_port=web_port,
                game_host="127.0.0.1",
                game_port=game_port,
                auto_start=True,
            )
            # 断言未触发内嵌启动（端口已被外部服务器占用，应走复用分支）
            self.assertIsNone(gateway._embedded_server)

            http_thread = threading.Thread(target=gateway.start, daemon=True)
            http_thread.start()
            # 等待 HTTP 服务就绪
            deadline = time.time() + 5.0
            while time.time() < deadline:
                try:
                    s = socket.create_connection(("127.0.0.1", web_port), timeout=0.3)
                    s.close()
                    break
                except OSError:
                    time.sleep(0.05)

            # ① 首页可访问
            conn = http.client.HTTPConnection("127.0.0.1", web_port, timeout=5)
            conn.request("GET", "/")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertIn("德州扑克", resp.read().decode("utf-8"))
            conn.close()

            # ② 通过 Web 加入对局成功（证明 Web 会话连通了外部游戏服务器）
            conn = http.client.HTTPConnection("127.0.0.1", web_port, timeout=5)
            conn.request(
                "POST", "/api/join",
                body=json.dumps({"name": "WebUser"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            data = json.loads(resp.read().decode("utf-8"))
            conn.close()
            self.assertEqual(resp.status, 200)
            self.assertTrue(data.get("ok"), f"Web 加入失败: {data}")
            gateway.stop()
        finally:
            # 清理：停止外部游戏服务器（Web 网关会话已由其 stop() 关闭）
            server.stop()


if __name__ == "__main__":
    unittest.main()
