"""德州扑克游戏启动入口。

提供四种模式：
1. server : 独立服务器（部署用，常驻后台，供外部玩家连接）
2. host   : 作为房主创建房间（启动服务器 + 自动以客户端身份加入）
3. join   : 作为普通玩家加入已有房间
4. status : 查询服务器运行状态（监控，无需加入房间）

用法示例：
    # 独立服务器：公网部署
    python main.py server --port 8888 --public-ip 1.2.3.4

    # 房主：在本地 8888 端口开局
    python main.py host --name Alice --port 8888

    # 其他玩家：加入服务器 IP
    python main.py join --host 192.168.1.100 --port 8888 --name Bob

    # 监控服务器状态
    python main.py status --host 192.168.1.100

    # 无参数则进入交互式选择菜单
    python main.py

运行环境要求：
- Python 3.9+
- 仅使用标准库，无需安装第三方依赖
"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import time
from typing import Optional

from core.event_log import setup_event_logging
from network.client import ClientError, GameClient, query_server_status
from network.server import GameServer
from ui.cli import PokerCLI
from web.gateway import WebGateway

# 默认端口与初始筹码，集中配置便于调整
DEFAULT_PORT: int = 8888
DEFAULT_STARTING_CHIPS: int = 1000
# Web 界面默认端口（server 模式同时提供 Web 访问时使用）
DEFAULT_WEB_PORT: int = 8000


# ---------- 环境变量配置辅助 ----------
# 支持通过环境变量配置服务器网络参数（部署场景便于在 shell/systemd 中管理），
# 命令行参数优先级高于环境变量，环境变量优先级高于默认值。

def _env_str(name: str, default: str) -> str:
    """读取字符串环境变量，未设置时返回默认值。

    Args:
        name: 环境变量名。
        default: 未设置时的默认值。

    Returns:
        环境变量值或默认值。
    """
    value = os.environ.get(name)
    return value if value else default


def _env_int(name: str, default: int) -> int:
    """读取整数环境变量，未设置或非法时返回默认值。

    Args:
        name: 环境变量名。
        default: 未设置时的默认值。

    Returns:
        解析后的整数值或默认值。
    """
    value = os.environ.get(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        # 环境变量非法时回退默认值并提示，避免程序崩溃
        print(f"[配置] 环境变量 {name}={value} 不是合法整数，已使用默认值 {default}")
        return default


def _setup_stdio_robust() -> None:
    """加固标准输入输出流的编码，避免因字符编码问题导致进程崩溃。

    问题背景：
    - 在 IDE 集成终端 / 管道环境下，Python 进程的 sys.stdout 通常使用系统
      本地编码（Windows 上多为 GBK/cp936），而游戏界面包含 ♠♥♦♣、框线字符
      等 GBK 无法表示的 Unicode 符号。
    - 此时 print("你的底牌: A♠ K♣") 会抛 UnicodeEncodeError，进程瞬间崩溃，
      表现为用户看到的"莫名中断"；随后 main 的 finally 发送 leave，服务器
      又会误记为"客户端主动离开"。
    修复方案：
    - 将 stdout / stderr / stdin 统一改为 UTF-8 编码，并开启 errors="replace"
      容错（无法编码的字符替换为 ? 而非抛异常）。
    - 现代终端（Windows Terminal、IDE 集成终端）按 UTF-8 解码，中文与花色
      符号都能正常显示；即使是旧终端，也只会显示少量占位符而不会崩溃。
    """
    # 需要加固的三个标准流：输出两个 + 输入一个
    streams = (sys.stdout, sys.stderr, sys.stdin)
    for stream in streams:
        # 某些环境下标准流可能为 None（如 pythonw 运行），跳过即可
        if stream is None:
            continue
        # reconfigure 是 Python 3.7+ 提供的动态修改流配置的接口
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            # 【重点注释】encoding 改为 utf-8 解决中文乱码，
            # errors="replace" 保证任何字符都不会让 print/read 抛异常
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, UnicodeError):
            # Windows 控制台（WindowsConsoleIO）不支持随意改编码等场景，
            # 保持原样即可，不影响程序运行
            pass


def main() -> int:
    """程序入口：解析参数并启动对应模式。

    Returns:
        进程退出码，0 表示正常退出。
    """
    # 在一切输出之前加固标准流编码，防止渲染花色符号时崩溃
    _setup_stdio_robust()

    # 配置日志输出：INFO 级别以上打印到控制台。
    # 【重点注释】logging 默认只显示 WARNING 及以上，若不在此提升级别，
    # 服务器记录"玩家断开原因"等 info 日志将不可见，排查断连问题会无从下手
    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] %(message)s",
    )
    # 【重点注释】初始化统一事件日志（控制台 + 文件自动轮转）。
    # 挂机等待期间的所有关键事件（连接/加入/离开/断开/开局/行动等）都会按
    # 统一格式 "时间 | 级别 | 事件类型 | 描述 | 上下文" 写入 logs/poker.log，
    # 同时保留 INFO 级别以上事件在控制台输出，便于实时观察。
    setup_event_logging()
    parser = argparse.ArgumentParser(
        description="局域网命令行德州扑克游戏（支持独立服务器部署）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  独立服务器（同时提供命令行客户端与 Web 浏览器访问）:\n"
            "    python main.py server --port 8888 --public-ip 1.2.3.4\n"
            "    Web 浏览器访问 http://<服务器IP>:8000，命令行客户端连接 8888 端口\n"
            "  独立 Web 界面: python main.py web --web-port 8000 --game-port 8888\n"
            "  房主: python main.py host --name Alice --port 8888\n"
            "  玩家: python main.py join --host 192.168.1.100 --name Bob\n"
            "  监控: python main.py status --host 192.168.1.100\n"
            "  交互: python main.py\n"
            "\n"
            "服务器网络参数可用环境变量配置（命令行优先）：\n"
            "  POKER_PORT / POKER_BIND / POKER_CHIPS / POKER_MAX_CONNECTIONS\n"
            "  POKER_PUBLIC_IP / POKER_CONSOLE（0 关闭服务器控制台）\n"
            "  POKER_WEB_PORT / POKER_NO_WEB（1 关闭 server 模式自带的 Web 界面）\n"
        ),
    )
    # 子命令：host / join / server / status
    subparsers = parser.add_subparsers(dest="mode")

    # host 子命令
    host_parser = subparsers.add_parser("host", help="创建房间（房主）")
    host_parser.add_argument("--name", "-n", required=False, default="", help="你的昵称")
    # 【重点注释】以下配置项 default 均为 None：None 表示"用户未显式指定"，
    # 由各 run 函数回退到环境变量（POKER_*）再回退到程序默认值，
    # 实现"命令行 > 环境变量 > 默认值"的优先级。
    host_parser.add_argument("--port", "-p", type=int, default=None, help="监听端口")
    host_parser.add_argument("--chips", type=int, default=None, help="初始筹码")
    host_parser.add_argument(
        "--bind", default=None, help="监听地址，默认 0.0.0.0（所有网卡）"
    )
    host_parser.add_argument(
        "--public-ip", default=None,
        help="对外公告的 IP 地址（云服务器/NAT 环境手动指定公网 IP）",
    )

    # join 子命令
    join_parser = subparsers.add_parser("join", help="加入房间")
    join_parser.add_argument("--host", required=False, default="", help="房主 IP 地址")
    join_parser.add_argument("--port", "-p", type=int, default=None, help="端口")
    join_parser.add_argument("--name", "-n", required=False, default="", help="你的昵称")

    # server 子命令：独立部署服务器（不含自动加入的房主）
    server_parser = subparsers.add_parser("server", help="独立服务器模式（部署用）")
    server_parser.add_argument("--port", "-p", type=int, default=None, help="监听端口")
    server_parser.add_argument(
        "--bind", default=None, help="监听地址，默认 0.0.0.0（所有网卡）"
    )
    server_parser.add_argument("--chips", type=int, default=None, help="新玩家初始筹码")
    server_parser.add_argument(
        "--max-connections", type=int, default=None,
        help="最大同时连接数（防止资源耗尽），默认 50",
    )
    server_parser.add_argument(
        "--public-ip", default=None,
        help="对外公告的 IP 地址（云服务器/NAT 环境手动指定公网 IP）",
    )
    server_parser.add_argument(
        "--no-console", action="store_true",
        help="关闭交互式服务器控制台（后台运行时使用）",
    )
    server_parser.add_argument(
        "--web-port", type=int, default=None,
        help="Web 界面端口，默认 8000（浏览器访问 http://<IP>:<port>）",
    )
    server_parser.add_argument(
        "--no-web", action="store_true",
        help="禁用随服务器一起启动的 Web 界面（仅提供命令行客户端访问）",
    )

    # status 子命令：远程查询服务器状态
    status_parser = subparsers.add_parser("status", help="查询服务器运行状态（监控）")
    status_parser.add_argument("--host", required=False, default="127.0.0.1", help="服务器 IP")
    status_parser.add_argument("--port", "-p", type=int, default=None, help="端口")

    # web 子命令：浏览器访问界面（内嵌游戏服务器 + HTTP/SSE 网关）
    web_parser = subparsers.add_parser("web", help="Web 界面模式（浏览器访问）")
    web_parser.add_argument(
        "--host", default="0.0.0.0",
        help="HTTP 服务监听地址，默认 0.0.0.0（所有网卡可访问）",
    )
    web_parser.add_argument(
        "--web-port", type=int, default=None,
        help="HTTP 服务端口，默认 8000（浏览器访问 http://<IP>:<port>）",
    )
    web_parser.add_argument(
        "--game-port", type=int, default=None,
        help="游戏服务器 TCP 端口，默认 8888（若无监听则自动内嵌启动）",
    )
    web_parser.add_argument(
        "--public-ip", default=None,
        help="对外公告的 IP 地址（透传给内嵌游戏服务器）",
    )

    args = parser.parse_args()

    # 无子命令时进入交互式菜单
    if not args.mode:
        return _interactive_menu()

    if args.mode == "host":
        return _run_host(args)
    if args.mode == "join":
        return _run_join(args)
    if args.mode == "server":
        return _run_server(args)
    if args.mode == "status":
        return _run_status(args)
    if args.mode == "web":
        return _run_web(args)

    parser.print_help()
    return 1


# ---------- 交互式菜单 ----------

def _interactive_menu() -> int:
    """无命令行参数时的交互式模式选择。

    Returns:
        进程退出码。
    """
    print("=" * 50)
    print("      局域网命令行德州扑克")
    print("=" * 50)
    print("请选择模式：")
    print("  1. 创建房间（房主）")
    print("  2. 加入房间")
    print("  3. 退出")
    print("-" * 50)

    choice = _prompt("请输入选项 (1/2/3): ").strip()
    if choice == "1":
        return _interactive_host()
    if choice == "2":
        return _interactive_join()
    print("再见！")
    return 0


def _interactive_host() -> int:
    """交互式创建房间流程。"""
    name = _prompt("请输入你的昵称: ").strip()
    if not name:
        print("昵称不能为空")
        return 1
    port_str = _prompt(f"监听端口（回车默认 {DEFAULT_PORT}）: ").strip()
    try:
        port = int(port_str) if port_str else DEFAULT_PORT
    except ValueError:
        print("端口必须为整数")
        return 1

    # 构造一个简易 args 对象传给 _run_host
    args = argparse.Namespace(
        name=name, port=port, chips=DEFAULT_STARTING_CHIPS,
        bind="0.0.0.0", public_ip=None,
    )
    return _run_host(args)


def _interactive_join() -> int:
    """交互式加入房间流程。"""
    host = _prompt("请输入房主 IP 地址: ").strip()
    if not host:
        print("IP 地址不能为空")
        return 1
    port_str = _prompt(f"端口（回车默认 {DEFAULT_PORT}）: ").strip()
    try:
        port = int(port_str) if port_str else DEFAULT_PORT
    except ValueError:
        print("端口必须为整数")
        return 1
    name = _prompt("请输入你的昵称: ").strip()
    if not name:
        print("昵称不能为空")
        return 1

    args = argparse.Namespace(host=host, port=port, name=name)
    return _run_join(args)


def _prompt(text: str) -> str:
    """安全读取一行用户输入，处理 EOF。

    Args:
        text: 提示文本。

    Returns:
        用户输入的字符串（可能为空）。
    """
    try:
        return input(text)
    except EOFError:
        return ""


# ---------- host / join 实现 ----------

def _run_host(args: argparse.Namespace) -> int:
    """启动房主模式：先起服务器，再以客户端身份加入，最后进入 CLI。

    Args:
        args: 命令行参数（name, port, chips, bind, public_ip）。

    Returns:
        进程退出码。
    """
    name = (args.name or "").strip() or _prompt("请输入你的昵称: ").strip()
    if not name:
        print("昵称不能为空")
        return 1

    # 配置解析：命令行 > 环境变量 > 默认值
    port = args.port if args.port is not None else _env_int("POKER_PORT", DEFAULT_PORT)
    chips = args.chips if args.chips is not None else _env_int("POKER_CHIPS", DEFAULT_STARTING_CHIPS)
    bind = args.bind if args.bind is not None else _env_str("POKER_BIND", "0.0.0.0")
    public_ip = args.public_ip if args.public_ip is not None else _env_str("POKER_PUBLIC_IP", "")
    # public_ip 为空字符串时视为未指定，使用自动探测地址
    public_ip = public_ip or None

    # 创建并启动服务器（后台线程）
    server = GameServer(
        host=bind,
        port=port,
        starting_chips=chips,
        public_ip=public_ip,
    )

    # 在守护线程中运行服务器的 accept 循环
    server_thread = threading.Thread(target=server.start, daemon=True)
    server_thread.start()

    # 显示连接地址（优先使用管理员指定的公网 IP，否则自动探测本机 IP）
    advertised_ip = server.public_ip or server.get_local_ip()
    print("=" * 50)
    print(f"房间已创建！其他玩家可用以下命令加入：")
    print(f"  python main.py join --host {advertised_ip} --port {server.port} --name <昵称>")
    print(f"  （若同机测试可用 127.0.0.1）")
    print("=" * 50)

    # 房主以客户端身份连接本地服务器
    client = GameClient()
    try:
        ok = client.connect("127.0.0.1", server.port, name)
    except ClientError as exc:
        print(f"连接服务器失败: {exc}")
        server.stop()
        return 1
    if not ok:
        print(f"加入房间失败: {client.last_error}")
        server.stop()
        return 1

    # 启动 CLI 交互
    cli = PokerCLI(client)
    try:
        cli.run()
    except KeyboardInterrupt:
        print("\n收到中断信号，正在退出...")
    finally:
        client.disconnect()
        server.stop()
    return 0


def _run_join(args: argparse.Namespace) -> int:
    """启动加入模式：连接到房主服务器并进入 CLI。

    Args:
        args: 命令行参数（host, port, name）。

    Returns:
        进程退出码。
    """
    host = (args.host or "").strip() or _prompt("请输入房主 IP 地址: ").strip()
    if not host:
        print("IP 地址不能为空")
        return 1
    name = (args.name or "").strip() or _prompt("请输入你的昵称: ").strip()
    if not name:
        print("昵称不能为空")
        return 1
    # 配置解析：命令行 > 环境变量 > 默认值
    port = args.port if args.port is not None else _env_int("POKER_PORT", DEFAULT_PORT)

    client = GameClient()
    try:
        ok = client.connect(host, port, name)
    except ClientError as exc:
        print(f"连接服务器失败: {exc}")
        print("请确认房主 IP 与端口正确，且房主已启动房间。")
        return 1
    if not ok:
        print(f"加入房间失败: {client.last_error}")
        return 1

    cli = PokerCLI(client)
    try:
        cli.run()
    except KeyboardInterrupt:
        print("\n收到中断信号，正在退出...")
    finally:
        client.disconnect()
    return 0


# ---------- server / status 实现 ----------

def _probe_port(hosts: tuple, port: int, timeout: float = 0.5) -> bool:
    """尝试 TCP 连接候选地址列表，任一成功即认为端口已就绪。

    Args:
        hosts: 候选主机地址（如 ("127.0.0.1",) 或 (bind, "127.0.0.1")）。
        port: 目标端口。
        timeout: 单次连接超时（秒）。

    Returns:
        True 表示至少一个地址可建立 TCP 连接。
    """
    # 遍历所有候选地址，忽略连接失败（说明该地址尚未就绪），继续尝试下一个
    for host in hosts:
        try:
            # create_connection 内部完成 socket 创建与 connect，失败抛 OSError
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def _wait_port_ready(
    hosts: tuple, port: int, server_thread: threading.Thread, timeout: float = 10.0
) -> bool:
    """轮询等待端口就绪，同时监测服务器线程是否提前退出。

    Args:
        hosts: 候选探测地址。
        port: 目标端口。
        server_thread: 运行 GameServer.start() 的后台线程。
        timeout: 最长等待时间（秒）。

    Returns:
        True 表示端口已就绪；False 表示超时或服务器线程已退出（启动失败）。
    """
    # 计算等待截止时间（单调时钟，避免系统时间调整影响判断）
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # 端口已可连接 → 服务器监听就绪
        if _probe_port(hosts, port):
            return True
        # 【重点注释】服务器线程提前结束说明 GameServer.start() 内部抛出异常
        # （典型场景：端口已被占用导致 bind 失败），此时继续等待没有意义
        if not server_thread.is_alive():
            return False
        # 每 0.1 秒探测一次，避免空转占用 CPU
        time.sleep(0.1)
    return False


def _run_server(args: argparse.Namespace) -> int:
    """启动独立服务器模式（部署用）：运行服务器并可选开启控制台。

    与 host 模式的区别：服务器不自动加入一名房主，首个连接的玩家成为房主。
    适合部署在云服务器 / 公网主机上，由外部玩家各自连接。

    【重点注释】server 模式同时提供两种访问方式：
    - 命令行客户端：连接游戏 TCP 端口（--port，默认 8888）
    - Web 浏览器：访问 Web 界面端口（--web-port，默认 8000，可用 --no-web 关闭）
    游戏服务器与 Web 网关在同一进程内并行运行（各自独立线程），
    因此一条 `python main.py server ...` 命令即可同时服务两类客户端。

    Args:
        args: 命令行参数（port, bind, chips, max_connections, public_ip,
              no_console, web_port, no_web）。

    Returns:
        进程退出码。
    """
    # 配置解析：命令行 > 环境变量 > 默认值
    port = args.port if args.port is not None else _env_int("POKER_PORT", DEFAULT_PORT)
    bind = args.bind if args.bind is not None else _env_str("POKER_BIND", "0.0.0.0")
    chips = args.chips if args.chips is not None else _env_int("POKER_CHIPS", DEFAULT_STARTING_CHIPS)
    max_connections = args.max_connections if args.max_connections is not None \
        else _env_int("POKER_MAX_CONNECTIONS", 50)
    public_ip = args.public_ip if args.public_ip is not None else _env_str("POKER_PUBLIC_IP", "")
    public_ip = public_ip or None
    # 环境变量 POKER_CONSOLE=0 时同样关闭控制台（后台运行场景）
    console_disabled = args.no_console or _env_str("POKER_CONSOLE", "1") == "0"
    # Web 界面参数：默认随服务器一起启动（POKER_NO_WEB=1 或 --no-web 时关闭）
    web_port = args.web_port if args.web_port is not None else _env_int("POKER_WEB_PORT", DEFAULT_WEB_PORT)
    no_web = args.no_web or _env_str("POKER_NO_WEB", "0") == "1"

    # 创建服务器实例
    server = GameServer(
        host=bind,
        port=port,
        starting_chips=chips,
        max_connections=max_connections,
        public_ip=public_ip,
    )

    # 游戏服务器在后台线程运行 accept 循环（主线程需同时承载 Web 网关与控制台）
    server_thread = threading.Thread(target=server.start, daemon=True)
    server_thread.start()

    # 等待游戏端口就绪：若线程提前退出（如端口被占用），给出明确报错并退出
    # 【重点注释】bind 为 0.0.0.0 时用 127.0.0.1 探测即可；若指定了具体网卡
    # 地址则同时探测该地址，确保能准确判断服务器是否真正开始监听
    probe_hosts = ("127.0.0.1",)
    if bind and bind != "0.0.0.0":
        probe_hosts = (bind, "127.0.0.1")
    if not _wait_port_ready(probe_hosts, port, server_thread):
        if not server_thread.is_alive():
            print(f"[服务器] 启动失败：端口 {port} 可能已被占用，可用 --port 指定其他端口。")
            return 1
        print(f"[服务器] 等待端口 {port} 就绪超时")
        server.stop()
        return 1

    # Web 网关：auto_start=True 时检测到游戏端口已有监听 → 直接复用，不重复启动
    web_gateway: Optional[WebGateway] = None
    if not no_web:
        web_gateway = WebGateway(
            host=bind,
            web_port=web_port,
            game_port=port,
            public_ip=public_ip,
            starting_chips=chips,
            max_connections=max_connections,
            auto_start=True,
        )

        def _run_web_gateway(gw: WebGateway) -> None:
            """在后台线程运行 Web 网关；端口占用等启动错误需在此捕获并提示。"""
            try:
                # serve_forever 阻塞，直到 stop() 被调用
                gw.start()
            except OSError as exc:
                # 【重点注释】ThreadingHTTPServer 绑定失败（web 端口被占用）会在
                # 本线程内抛 OSError，必须在这里捕获，否则用户看不到任何报错
                print(f"[服务器] Web 界面启动失败: {exc}")
                print("提示：可用 --web-port 指定其他端口，或加 --no-web 仅运行命令行服务器。")

        threading.Thread(target=_run_web_gateway, args=(web_gateway,), daemon=True).start()

    # 交互式控制台（status/players/quit），后台运行时用 --no-console 关闭
    if not console_disabled:
        console_thread = threading.Thread(target=server.run_console, daemon=True)
        console_thread.start()

    try:
        # 主线程挂起等待，直到收到 Ctrl+C 中断信号
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\n收到中断信号，正在停止服务器...")
    finally:
        # 统一停止：游戏服务器 + Web 网关（关闭所有会话与内嵌资源）
        server.stop()
        if web_gateway is not None:
            web_gateway.stop()
    return 0


def _run_status(args: argparse.Namespace) -> int:
    """查询服务器运行状态（监控用途）。

    通过 status_req 消息远程读取服务器状态并打印，不加入房间。

    Args:
        args: 命令行参数（host, port）。

    Returns:
        进程退出码，0 表示查询成功。
    """
    # 配置解析：命令行 > 环境变量 > 默认值
    port = args.port if args.port is not None else _env_int("POKER_PORT", DEFAULT_PORT)
    host = (args.host or "").strip() or "127.0.0.1"

    try:
        st = query_server_status(host, port, timeout=5.0)
    except ClientError as exc:
        print(f"[监控] 查询服务器 {host}:{port} 失败: {exc}")
        print("请确认服务器已启动，且 IP 与端口正确。")
        return 1

    # 格式化输出状态信息
    print("=" * 46)
    print("服务器状态监控")
    print("=" * 46)
    print(f"服务器地址     : {st.get('server', '-')}（对外 {st.get('advertised_ip', '-')}）")
    print(f"在线玩家       : {st.get('online_players', 0)}/{st.get('max_players', 0)}")
    print(f"当前连接数     : {st.get('connections', 0)}/{st.get('max_connections', 0)}")
    print(f"累计连接数     : {st.get('total_connections', 0)}（峰值 {st.get('peak_connections', 0)}）")
    print(f"拒绝连接数     : {st.get('rejected_connections', 0)}")
    # 将秒数转换为 时:分:秒 便于阅读
    uptime = int(st.get("uptime_seconds", 0) or 0)
    hours, rem = divmod(uptime, 3600)
    minutes, seconds = divmod(rem, 60)
    print(f"运行时长       : {hours} 时 {minutes} 分 {seconds} 秒")
    print(f"当前局数       : 第 {st.get('hand_number', 0)} 局（{st.get('game_state', '-')}）")
    print("-" * 46)
    players = st.get("players", [])
    if players:
        print(f"在线玩家列表（{len(players)} 人）:")
        for p in players:
            pid = p.get("player_id", "?")
            pname = p.get("name", "?")
            chips = p.get("chips", 0)
            host_mark = "（房主）" if pid == st.get("host_player_id") else ""
            print(f"  #{pid} {pname}  筹码={chips} {host_mark}")
    else:
        print("当前没有玩家在线")
    print("=" * 46)
    return 0


def _run_web(args: argparse.Namespace) -> int:
    """运行 Web 界面模式：浏览器访问德州扑克（内嵌游戏服务器 + HTTP 网关）。

    启动流程：
    1. 创建 WebGateway，若目标游戏端口无监听则自动内嵌启动 GameServer；
    2. 启动 HTTP 服务器（线程化），提供静态页面、REST API 与 SSE 实时推送；
    3. 打印浏览器访问地址后进入阻塞事件循环，Ctrl+C 优雅退出。

    Args:
        args: 命令行参数（host, web-port, game-port, public-ip）。

    Returns:
        进程退出码，0 表示正常退出。
    """
    # 配置解析：命令行 > 环境变量 > 默认值
    web_port = args.web_port if args.web_port is not None else _env_int("POKER_WEB_PORT", 8000)
    game_port = args.game_port if args.game_port is not None else _env_int("POKER_PORT", DEFAULT_PORT)
    public_ip = args.public_ip or _env_str("POKER_PUBLIC_IP", "").strip() or None
    host = (args.host or "").strip() or "0.0.0.0"

    print(f"[Web] 正在启动 Web 界面（HTTP {host}:{web_port}，游戏端口 {game_port}）...")
    # 创建网关：自动确保游戏服务器可用（复用已有监听或内嵌启动）
    gateway = WebGateway(
        host=host,
        web_port=web_port,
        game_port=game_port,
        public_ip=public_ip,
        auto_start=True,
    )
    try:
        gateway.start()
    except OSError as exc:
        # 端口被占用等启动失败场景：给出明确错误与解决建议
        print(f"[Web] 启动失败: {exc}")
        print(f"提示：端口 {web_port} 可能已被占用，可用 --web-port 指定其他端口。")
        return 1
    except KeyboardInterrupt:
        print("\n[Web] 收到中断信号，正在关闭...")
        gateway.stop()
    return 0


if __name__ == "__main__":
    # 入口：调用 main 并以返回值作为退出码
    sys.exit(main())
