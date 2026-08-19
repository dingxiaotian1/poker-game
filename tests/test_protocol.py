"""网络协议帧编解码与消息构造单元测试。"""
from __future__ import annotations

import unittest

from network.protocol import (
    HEADER_SIZE,
    MAX_MESSAGE_BYTES,
    Msg,
    ProtocolError,
    decode_message_from_buffer,
    encode_message,
)


class TestEncodeDecode(unittest.TestCase):
    """帧编解码往返测试。"""

    def test_roundtrip_simple(self) -> None:
        """简单消息编解码往返应无损。"""
        original = Msg.join("Alice")
        data = encode_message(original)
        # 数据应以 4 字节长度头开始
        self.assertEqual(len(data) > HEADER_SIZE, True)

        buf = bytearray(data)
        decoded = decode_message_from_buffer(buf)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["type"], "join")
        self.assertEqual(decoded["name"], "Alice")
        # 缓冲区应被完全消费
        self.assertEqual(len(buf), 0)

    def test_roundtrip_with_chinese(self) -> None:
        """含中文的消息应正确编解码。"""
        original = Msg.chat_broadcast("玩家1", "你好，世界！")
        data = encode_message(original)
        buf = bytearray(data)
        decoded = decode_message_from_buffer(buf)
        self.assertEqual(decoded["sender"], "玩家1")
        self.assertEqual(decoded["text"], "你好，世界！")

    def test_multiple_messages_in_one_buffer(self) -> None:
        """一条缓冲区含多条消息应能依次解析。"""
        msg1 = Msg.join("A")
        msg2 = Msg.chat("hello")
        msg3 = Msg.start()
        # 拼接三条消息的字节流
        buf = bytearray(encode_message(msg1) + encode_message(msg2) + encode_message(msg3))

        first = decode_message_from_buffer(buf)
        self.assertEqual(first["type"], "join")
        second = decode_message_from_buffer(buf)
        self.assertEqual(second["type"], "chat")
        third = decode_message_from_buffer(buf)
        self.assertEqual(third["type"], "start")
        # 全部消费完
        self.assertEqual(len(buf), 0)

    def test_partial_message_returns_none(self) -> None:
        """不完整消息应返回 None 且不消费缓冲区。"""
        full = encode_message(Msg.join("Bob"))
        # 仅保留长度头 + 部分载荷
        partial = full[:HEADER_SIZE + 2]
        buf = bytearray(partial)
        result = decode_message_from_buffer(buf)
        self.assertIsNone(result)
        # 缓冲区长度不变
        self.assertEqual(len(buf), len(partial))

    def test_partial_header_returns_none(self) -> None:
        """长度头不完整应返回 None。"""
        buf = bytearray(b"\x00")  # 仅 1 字节
        self.assertIsNone(decode_message_from_buffer(buf))

    def test_message_missing_type_raises(self) -> None:
        """缺少 type 字段应抛出 ProtocolError。"""
        with self.assertRaises(ProtocolError):
            encode_message({"name": "x"})


class TestProtocolErrors(unittest.TestCase):
    """协议错误处理测试。"""

    def test_oversized_message_raises(self) -> None:
        """超限消息应抛出 ProtocolError。"""
        # 构造一个超过最大限制的巨大消息
        big = {"type": "chat", "text": "x" * (MAX_MESSAGE_BYTES + 10)}
        with self.assertRaises(ProtocolError):
            encode_message(big)

    def test_illegal_length_raises(self) -> None:
        """长度头为 0 应抛出 ProtocolError。"""
        # 构造长度为 0 的帧
        buf = bytearray(b"\x00\x00\x00\x00")
        with self.assertRaises(ProtocolError):
            decode_message_from_buffer(buf)

    def test_invalid_json_raises(self) -> None:
        """载荷非合法 JSON 应抛出 ProtocolError。"""
        import struct
        payload = b"not a json"
        buf = bytearray(struct.pack(">I", len(payload)) + payload)
        with self.assertRaises(ProtocolError):
            decode_message_from_buffer(buf)

    def test_non_dict_payload_raises(self) -> None:
        """载荷 JSON 非对象应抛出 ProtocolError。"""
        import struct
        payload = b"[1,2,3]"
        buf = bytearray(struct.pack(">I", len(payload)) + payload)
        with self.assertRaises(ProtocolError):
            decode_message_from_buffer(buf)


class TestMsgFactory(unittest.TestCase):
    """消息工厂方法测试。"""

    def test_action_message(self) -> None:
        """action 消息应含 action 与 amount 字段。"""
        msg = Msg.action("raise", 150)
        self.assertEqual(msg["type"], "action")
        self.assertEqual(msg["action"], "raise")
        self.assertEqual(msg["amount"], 150)

    def test_join_ok_message(self) -> None:
        """join_ok 消息应含 player_id 与 state。"""
        msg = Msg.join_ok(7, {"players": []})
        self.assertEqual(msg["player_id"], 7)
        self.assertIn("state", msg)

    def test_all_factory_methods_have_type(self) -> None:
        """所有工厂方法生成的消息都应含 type 字段且可编码。"""
        messages = [
            Msg.join("X"),
            Msg.action("fold"),
            Msg.start(),
            Msg.ready(),
            Msg.chat("hi"),
            Msg.leave(),
            Msg.ping(),
            Msg.join_ok(1, {}),
            Msg.join_fail("reason"),
            Msg.state({}),
            Msg.turn({}),
            Msg.deal_hole([]),
            Msg.log("text"),
            Msg.error("err"),
            Msg.showdown([]),
            Msg.hand_over(),
            Msg.chat_broadcast("X", "hi"),
            Msg.player_joined("X", 2),
            Msg.player_left("X", 1),
            Msg.pong(),
            Msg.kick("bye"),
        ]
        for msg in messages:
            # 每条都应能成功编码
            data = encode_message(msg)
            self.assertIsInstance(data, bytes)
            self.assertGreater(len(data), HEADER_SIZE)


if __name__ == "__main__":
    unittest.main()
