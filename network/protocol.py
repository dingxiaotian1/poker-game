"""网络消息协议模块。

定义客户端与服务器之间交换的消息格式与帧编解码方式。

帧格式（解决 TCP 流边界问题）：
    [4 字节大端长度][UTF-8 编码的 JSON 文本]
长度字段表示后续 JSON 文本的字节数。读取时先读 4 字节长度，再精确读取对应
字节数的载荷，避免粘包/半包问题。

消息体统一为 JSON 对象，必含 "type" 字段标识消息类型，其余字段因类型而异。
所有消息构造函数集中在 Msg 类中，便于维护与查找。
"""
from __future__ import annotations

import json
import struct
from typing import Any, Dict, Optional

# 单条消息最大字节数，防止异常数据撑爆内存（状态快照较大，设为 256KB）
MAX_MESSAGE_BYTES: int = 256 * 1024
# 长度头字节数（固定 4 字节大端无符号整数）
HEADER_SIZE: int = 4
# 结构体格式符：>I 表示大端无符号 4 字节整数
HEADER_FORMAT: str = ">I"


# ---------- 消息类型常量 ----------

# 客户端 → 服务器
MSG_JOIN: str = "join"          # 加入房间
MSG_ACTION: str = "action"      # 游戏行动
MSG_START: str = "start"        # 请求开局（房主）
MSG_READY: str = "ready"        # 准备就绪
MSG_CHAT: str = "chat"          # 聊天
MSG_LEAVE: str = "leave"        # 离开
MSG_PING: str = "ping"          # 心跳
MSG_RESET_REQ: str = "reset_req"  # 请求重置房间（仅房主）

# 服务器 → 客户端
MSG_JOIN_OK: str = "join_ok"        # 加入成功
MSG_JOIN_FAIL: str = "join_fail"    # 加入失败
MSG_STATE: str = "state"            # 全量状态广播
MSG_TURN: str = "turn"              # 轮到你行动
MSG_DEAL_HOLE: str = "deal_hole"    # 发底牌给你
MSG_LOG: str = "log"                # 游戏日志
MSG_ERROR: str = "error"            # 错误提示
MSG_SHOWDOWN: str = "showdown"      # 摊牌结果
MSG_HAND_OVER: str = "hand_over"    # 本局结束
MSG_CHAT_BC: str = "chat_bc"        # 聊天广播
MSG_PLAYER_JOINED: str = "player_joined"
MSG_PLAYER_LEFT: str = "player_left"
MSG_PONG: str = "pong"              # 心跳回复
MSG_KICK: str = "kick"              # 被踢出
MSG_RESET_OK: str = "reset_ok"      # 重置成功（广播）
MSG_RESET_FAIL: str = "reset_fail"  # 重置失败（权限不足等）
MSG_STATUS_REQ: str = "status_req"  # 请求服务器状态（无需加入房间即可查询）
MSG_STATUS_RESP: str = "status_resp"  # 服务器状态响应


class ProtocolError(Exception):
    """协议错误：帧格式错误、JSON 解析失败、消息过大等。"""


# ---------- 帧编解码 ----------

def encode_message(message: Dict[str, Any]) -> bytes:
    """将消息字典编码为可发送的字节流。

    Args:
        message: 消息字典，必须含 "type" 字段。

    Returns:
        长度前缀 + JSON 文本的字节流。

    Raises:
        ProtocolError: 消息缺失 type 字段或体积超限时抛出。
    """
    if "type" not in message:
        raise ProtocolError("消息缺少 type 字段")

    # 序列化为 JSON 文本再编码为 UTF-8 字节
    # ensure_ascii=False 保留中文，减少体积并提升可读性
    payload = json.dumps(message, ensure_ascii=False).encode("utf-8")

    if len(payload) > MAX_MESSAGE_BYTES:
        raise ProtocolError(
            f"消息体积超限: {len(payload)} > {MAX_MESSAGE_BYTES}"
        )

    # 打包 4 字节大端长度头
    header = struct.pack(HEADER_FORMAT, len(payload))
    return header + payload


def decode_message_from_buffer(buffer: bytearray) -> Optional[Dict[str, Any]]:
    """从可变缓冲区中尝试解析一条完整消息。

    若缓冲区中数据不足以构成一条完整消息，返回 None 且不修改缓冲区；
    若解析成功，从缓冲区头部移除已消费的字节并返回消息字典。

    Args:
        buffer: 可变字节缓冲区，调用方持续向其追加收到的数据。

    Returns:
        解析出的消息字典，或 None（数据不完整）。

    Raises:
        ProtocolError: 长度头非法、体积超限、JSON 解析失败时抛出。
    """
    # 数据不足以读取长度头
    if len(buffer) < HEADER_SIZE:
        return None

    # 读取长度头（不消费，先预判）
    payload_len = struct.unpack(HEADER_FORMAT, bytes(buffer[:HEADER_SIZE]))[0]

    # 长度非法或超限，直接抛错让上层关闭连接
    if payload_len <= 0 or payload_len > MAX_MESSAGE_BYTES:
        raise ProtocolError(f"非法消息长度: {payload_len}")

    # 数据不足以读取完整载荷，等待更多数据
    if len(buffer) < HEADER_SIZE + payload_len:
        return None

    # 消费长度头与载荷
    payload_bytes = bytes(buffer[HEADER_SIZE:HEADER_SIZE + payload_len])
    del buffer[:HEADER_SIZE + payload_len]

    try:
        message = json.loads(payload_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProtocolError(f"JSON 解析失败: {exc}") from exc

    if not isinstance(message, dict) or "type" not in message:
        raise ProtocolError("消息格式非法：非对象或缺少 type 字段")
    return message


# ---------- 消息构造工具 ----------

class Msg:
    """消息构造工具集，集中定义所有消息的工厂方法。

    使用工厂方法而非散落的字典字面量，可避免字段拼写错误，且便于重构。
    每个方法返回可直接通过 encode_message 编码的字典。
    """

    # --- 客户端发出的消息 ---

    @staticmethod
    def join(name: str, reconnect: bool = False) -> Dict[str, Any]:
        """构造加入房间消息。

        Args:
            name: 玩家昵称。
            reconnect: 是否为重连加入。为 True 时，若服务器上存在同名玩家且其
                旧连接已断开（死亡/被回收），允许新连接接管其席位，用于断线重连。
                普通首次加入传 False，避免误顶替在线玩家。
        """
        return {"type": MSG_JOIN, "name": name, "reconnect": bool(reconnect)}

    @staticmethod
    def action(action: str, amount: int = 0) -> Dict[str, Any]:
        """构造游戏行动消息。

        Args:
            action: 行动名称（fold/check/call/raise/all_in）。
            amount: 加注目标金额，仅 raise 时使用。
        """
        return {"type": MSG_ACTION, "action": action, "amount": int(amount)}

    @staticmethod
    def start() -> Dict[str, Any]:
        """构造请求开局消息。"""
        return {"type": MSG_START}

    @staticmethod
    def ready() -> Dict[str, Any]:
        """构造准备就绪消息。"""
        return {"type": MSG_READY}

    @staticmethod
    def chat(text: str) -> Dict[str, Any]:
        """构造聊天消息。"""
        return {"type": MSG_CHAT, "text": text}

    @staticmethod
    def leave() -> Dict[str, Any]:
        """构造离开消息。"""
        return {"type": MSG_LEAVE}

    @staticmethod
    def ping() -> Dict[str, Any]:
        """构造心跳消息。"""
        return {"type": MSG_PING}

    @staticmethod
    def reset_req() -> Dict[str, Any]:
        """构造请求重置房间消息（仅房主可发送，服务器会做权限校验）。"""
        return {"type": MSG_RESET_REQ}

    # --- 服务器发出的消息 ---

    @staticmethod
    def join_ok(player_id: int, state: Dict[str, Any], is_host: bool = False) -> Dict[str, Any]:
        """构造加入成功消息。

        Args:
            player_id: 分配的玩家 ID。
            state: 当前桌状态快照。
            is_host: 是否为房主（首位加入者），供 UI 展示专属引导。
        """
        return {
            "type": MSG_JOIN_OK,
            "player_id": player_id,
            "state": state,
            "is_host": is_host,
        }

    @staticmethod
    def join_fail(reason: str) -> Dict[str, Any]:
        """构造加入失败消息。"""
        return {"type": MSG_JOIN_FAIL, "reason": reason}

    @staticmethod
    def state(state: Dict[str, Any]) -> Dict[str, Any]:
        """构造全量状态广播消息。"""
        return {"type": MSG_STATE, "state": state}

    @staticmethod
    def turn(options: Dict[str, Any]) -> Dict[str, Any]:
        """构造"轮到你"消息，附带可执行行动选项。"""
        return {"type": MSG_TURN, "options": options}

    @staticmethod
    def deal_hole(cards: list) -> Dict[str, Any]:
        """构造发底牌消息（仅发给对应玩家）。"""
        return {"type": MSG_DEAL_HOLE, "cards": cards}

    @staticmethod
    def log(message: str) -> Dict[str, Any]:
        """构造游戏日志消息。"""
        return {"type": MSG_LOG, "message": message}

    @staticmethod
    def error(message: str) -> Dict[str, Any]:
        """构造错误提示消息。"""
        return {"type": MSG_ERROR, "message": message}

    @staticmethod
    def showdown(results: list) -> Dict[str, Any]:
        """构造摊牌结果消息。"""
        return {"type": MSG_SHOWDOWN, "results": results}

    @staticmethod
    def hand_over(summary: str = "") -> Dict[str, Any]:
        """构造本局结束消息。

        Args:
            summary: 本局结果摘要（如"Alice 赢得 30 筹码"），供 UI 醒目展示。
        """
        return {"type": MSG_HAND_OVER, "summary": summary}

    @staticmethod
    def chat_broadcast(sender: str, text: str) -> Dict[str, Any]:
        """构造聊天广播消息。"""
        return {"type": MSG_CHAT_BC, "sender": sender, "text": text}

    @staticmethod
    def player_joined(name: str, player_count: int) -> Dict[str, Any]:
        """构造玩家加入通知。"""
        return {"type": MSG_PLAYER_JOINED, "name": name, "player_count": player_count}

    @staticmethod
    def player_left(name: str, player_count: int) -> Dict[str, Any]:
        """构造玩家离开通知。"""
        return {"type": MSG_PLAYER_LEFT, "name": name, "player_count": player_count}

    @staticmethod
    def pong() -> Dict[str, Any]:
        """构造心跳回复消息。"""
        return {"type": MSG_PONG}

    @staticmethod
    def kick(reason: str) -> Dict[str, Any]:
        """构造踢出消息。"""
        return {"type": MSG_KICK, "reason": reason}

    @staticmethod
    def reset_ok() -> Dict[str, Any]:
        """构造重置成功通知消息（随后服务器会广播最新 state 快照）。"""
        return {"type": MSG_RESET_OK}

    @staticmethod
    def reset_fail(reason: str) -> Dict[str, Any]:
        """构造重置失败消息。

        Args:
            reason: 失败原因（如"仅房间创建者可重置房间"）。
        """
        return {"type": MSG_RESET_FAIL, "reason": reason}

    @staticmethod
    def status_req() -> Dict[str, Any]:
        """构造服务器状态查询消息（未加入房间的连接也可发送）。"""
        return {"type": MSG_STATUS_REQ}

    @staticmethod
    def status_resp(status: Dict[str, Any]) -> Dict[str, Any]:
        """构造服务器状态响应消息。

        Args:
            status: get_status() 返回的状态字典（在线人数、连接数、运行时长等）。
        """
        return {"type": MSG_STATUS_RESP, "status": status}
