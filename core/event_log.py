"""统一事件日志模块。

为整个程序提供格式一致、易于阅读与事后分析的关键事件日志能力，
用于追踪程序在运行期间（含挂机等待状态）发生的所有关键事件。

统一日志格式（一行一条）：
    2026-08-12 14:30:01.123 | INFO  | JOIN          | 玩家 Bob 加入房间 | player_id=2, count=3

各字段说明：
- 时间戳：精确到毫秒，便于按时间顺序排序与定位问题发生时刻。
- 级别：INFO / WARNING / ERROR 等，便于按严重程度快速过滤。
- 事件类型：本模块顶部定义的统一事件类别常量（JOIN/LEAVE/DISCONNECT 等），
  作为日志检索与统计的主键。
- 描述：自然语言说明"发生了什么 + 结果如何"。
- 上下文：key=value 键值对，补充与事件相关的附加信息（玩家 ID、端口、断开原因等）。

设计要点：
- 基于标准库 logging 实现，无需第三方依赖。
- 文件日志使用 RotatingFileHandler 自动轮转（按大小），避免日志无限增长。
- 通过自定义 Filter 为每条记录注入 event_type 字段，格式统一且不侵入业务代码。
- setup_event_logging() 由 main.py 在启动时调用；测试环境不调用时，日志仅
  输出到控制台，不产生文件副作用。

用法示例：
    from core.event_log import log_event, EVT_JOIN, setup_event_logging
    setup_event_logging()                      # 启动时初始化一次
    log_event(EVT_JOIN, "玩家 Bob 加入房间", player_id=2, count=3)
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, Optional


# ---------- 事件类型常量（统一枚举，便于检索与统计） ----------
EVT_SERVER_START = "SERVER_START"        # 服务器启动
EVT_SERVER_STOP = "SERVER_STOP"          # 服务器停止
EVT_CONNECT = "CONNECT"                  # 客户端 TCP 连接建立
EVT_JOIN = "JOIN"                        # 玩家成功加入房间
EVT_JOIN_FAIL = "JOIN_FAIL"              # 玩家加入失败
EVT_LEAVE = "LEAVE"                      # 玩家主动离开
EVT_DISCONNECT = "DISCONNECT"            # 玩家断开连接（含具体原因）
EVT_HOST_CHANGE = "HOST_CHANGE"          # 房主变更（原房主离开后的交接）
EVT_HAND_START = "HAND_START"            # 一局开始
EVT_HAND_END = "HAND_END"                # 一局结束（含结果摘要）
EVT_ACTION = "ACTION"                    # 玩家行动（弃牌/跟注/加注等）
EVT_CHAT = "CHAT"                        # 聊天消息
EVT_PROTOCOL_ERROR = "PROTOCOL_ERROR"    # 协议错误（帧格式损坏等）
EVT_SEND_FAIL = "SEND_FAIL"              # 消息发送失败
EVT_CLIENT_CONNECT = "CLIENT_CONNECT"    # 客户端连接服务器成功
EVT_CLIENT_DISCONNECT = "CLIENT_DISCONNECT"  # 客户端断开服务器
EVT_CLIENT_ERROR = "CLIENT_ERROR"        # 客户端错误（连接失败等）
EVT_GENERAL = "GENERAL"                  # 通用事件兜底

# 默认日志目录与文件名
DEFAULT_LOG_DIR: str = "logs"
DEFAULT_LOG_FILE: str = "poker.log"
# 单个日志文件上限（5 MB），超过后自动轮转
MAX_LOG_BYTES: int = 5 * 1024 * 1024
# 轮转保留的历史日志文件数量（poker.log.1 ~ poker.log.5）
BACKUP_COUNT: int = 5

# 统一日志格式：时间 | 级别 | 事件类型 | 描述 | 上下文
# %(msecs)03d 单独输出毫秒，配合 datefmt 实现毫秒级时间戳
LOG_FORMAT: str = (
    "%(asctime)s.%(msecs)03d | %(levelname)-5s | %(event_type)-15s | %(message)s"
)
DATE_FORMAT: str = "%Y-%m-%d %H:%M:%S"


class _EventTypeFilter(logging.Filter):
    """为日志记录注入 event_type 字段的过滤器。

    业务代码统一通过 log_event() 写入事件日志（已携带 event_type）；
    若其他代码直接用 logger 打日志缺少该字段，此处补默认值 '-'，
    保证每行日志的格式字段完整、可解析。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # 缺少 event_type 属性时补默认值，避免格式化占位符报错
        if not hasattr(record, "event_type"):
            record.event_type = "-"
        return True


def log_event(
    event_type: str,
    description: str,
    level: int = logging.INFO,
    **context: Any,
) -> None:
    """记录一条统一格式的关键事件日志。

    Args:
        event_type: 事件类型，取本模块顶部定义的事件常量（如 EVT_JOIN）。
        description: 事件的自然语言描述（做什么 + 结果）。
        level: 日志级别，默认 INFO；错误类事件可传 logging.WARNING / ERROR。
        **context: 附加上下文键值对，如 player_id=2、reason=对端断开 等，
                   会自动拼接为 "key=value" 形式追加在描述之后。
    """
    # 将上下文键值对拼成 "key=value" 字符串，多个键用空格分隔
    ctx_text = " ".join(f"{k}={v}" for k, v in context.items())
    # 有上下文时以 " | " 分隔追加到描述后，保证格式统一可解析
    text = f"{description} | {ctx_text}" if ctx_text else description
    # extra 注入 event_type，供统一格式器使用；缺失时由过滤器兜底
    logger.log(level, text, extra={"event_type": event_type})


def setup_event_logging(
    log_dir: str = DEFAULT_LOG_DIR,
    log_file: str = DEFAULT_LOG_FILE,
    console_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
) -> str:
    """配置全局事件日志：控制台输出 + 文件输出（自动轮转）。

    应在程序启动时（main 入口）调用一次。测试环境不调用时，
    logging 仍按默认行为输出到控制台，不会产生文件副作用。

    Args:
        log_dir: 日志文件所在目录，不存在时自动创建。
        log_file: 日志文件名。
        console_level: 控制台日志级别（默认 INFO，屏蔽过细的调试信息）。
        file_level: 文件日志级别（默认 DEBUG，文件保留更详细的信息便于分析）。

    Returns:
        日志文件的绝对路径；目录创建失败时返回空字符串。
    """
    # 组装统一的日志格式器（时间 | 级别 | 事件类型 | 描述）
    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    # 【重点注释】避免重复配置：若本模块 logger 已挂过处理器（多次调用
    # setup_event_logging），先清空再重建，防止同一事件被打印多次
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    # 移除可能存在的旧过滤器，避免累加
    for filt in list(logger.filters):
        logger.removeFilter(filt)

    # 1) 控制台输出：INFO 级别，玩家在终端即可看到关键事件
    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(formatter)
    logger.addHandler(console)

    # 2) 文件输出：DEBUG 级别 + 按大小轮转，方便事后完整复盘
    file_path = ""
    try:
        # 日志目录不存在时自动创建
        os.makedirs(log_dir, exist_ok=True)
        file_path = os.path.join(log_dir, log_file)
        file_handler = RotatingFileHandler(
            file_path,
            maxBytes=MAX_LOG_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(file_level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as exc:
        # 【重点注释】日志目录/文件不可写（如只读目录）时，不应让程序崩溃，
        # 仅在控制台提示，程序其他功能不受影响
        print(f"[日志] 无法写入日志文件: {exc}")

    # 【重点注释】显式设置本 logger 的级别（取控制台/文件级别中的较低者），
    # 防止 INFO 事件被丢弃：
    # - logger 自身级别为 NOTSET 时会继承 root 的级别，而 root 默认是 WARNING，
    #   此时 log_event(INFO) 在到达 handler 之前就被 logger 丢弃，日志文件为空。
    # - 显式设为 DEBUG 后，级别过滤交由各 handler 的 setLevel 控制（控制台 INFO、
    #   文件 DEBUG），语义清晰且不依赖调用方是否配置过 root。
    logger.setLevel(min(console_level, file_level))

    # 统一的 event_type 注入过滤器
    logger.addFilter(_EventTypeFilter())
    # 禁止向父级 logger 冒泡，避免与控制台处理器重复输出
    logger.propagate = False

    return file_path


# 模块级 logger：业务代码统一通过 log_event() 写入事件日志。
# 定义在文件末尾，因为 log_event/setup_event_logging 均在运行时调用，
# 届时本对象已存在，无引用顺序问题。
logger = logging.getLogger("poker.event")
