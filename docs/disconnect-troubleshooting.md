# 客户端自动断开问题排查报告

> 问题编号：Disconnect-001
> 状态：已解决
> 相关模块：`network/client.py`、`network/server.py`、`ui/cli.py`

## 一、问题现象

在终端（Trae 集成终端 / trae-sandbox 伪终端环境）中同时打开两个窗口运行游戏：

```powershell
# 窗口 1（房主）
python main.py host --name Alice --port 8888
# 窗口 2（玩家）
python main.py join --host 127.0.0.1 --port 8888 --name Bob
```

双方成功加入房间后，**约 10 秒后（用户感知为 3-5 秒）两个客户端同时自动退出**，
服务器控制台记录：

```
[INFO] 玩家断开: Alice（剩余 1 人），原因: 客户端主动离开
[INFO] 玩家断开: Bob（剩余 0 人），原因: 客户端主动离开
[INFO] 服务器已关闭
```

其中"客户端主动离开"是服务器收到客户端 `leave` 消息时的记录，即**两个客户端进程
都自行调用 `disconnect()` 发送了离开消息**，而非服务器主动断开。用户未输入任何
退出命令。

## 二、排查过程

### 阶段 1：排查进程崩溃（编码问题）—— 已修复，但非主因

**假设**：客户端渲染含 `♠♥♦♣` 花色的界面时，因 GBK 编码无法表示而抛
`UnicodeEncodeError`，进程崩溃退出，随后 finally 发送 leave。

**验证**：真实复现脚本启动两个进程，开局发牌后在 `print("你的底牌: A♠ K♣")` 处
崩溃，`UnicodeEncodeError: 'gbk' codec can't encode character '\u2663'`。

**修复**：`main.py` 新增 `_setup_stdio_robust()`，将 stdin/stdout/stderr 统一为
UTF-8 + `errors="replace"`；CLI 主循环增加异常兜底。

**结论**：编码崩溃是真实问题且已修复，但**不是本次断连的主因**——修复后断连
依然存在（两窗口仍自动退出，且无 Traceback）。

### 阶段 2：排查终端刷新与输入干扰 —— 已修复，但非主因

**假设**：`os.system("cls")` 每次渲染派生 cmd 子进程，与输入线程争抢控制台句柄，
导致 `sys.stdin.readline()` 异常/EOF，触发退出。

**修复**：`clear_screen()` 改用 ANSI 转义序列；渲染节流（`MIN_RENDER_INTERVAL`）；
输入线程异常恢复 + EOF 多次确认。

**结论**：这些是真实交互问题并已修复，但**仍未解决断连**——随后用伪终端实验
证实 `sys.stdin.isatty() == True`，readline 正常阻塞等待输入，**输入线程根本
不会产生 EOF**，此方向与主因无关。

### 阶段 3：定位真正的根因（socket 残留超时）—— 关键突破

**关键实验**：用两个真实终端进程（trae-sandbox 伪终端，与用户环境一致）复现，
**不进行任何输入操作**，仅空闲等待：

1. 加入房间后约 10 秒，join 进程自动退出（退出码 0），输出仅
   `已退出游戏，再见！`——**没有** `[提示] 输入流已关闭`（排除 EOF 路径），
   **没有** `[系统] 与服务器断开连接`（因 `running=False` 后退出过快来不及渲染）。
2. 随后 host 进程也退出（`服务器已关闭`）。

**根因确认**：`network/client.py` 的 `connect()` 使用
`socket.create_connection((host, port), timeout=10.0)` 建立连接。该 `timeout`
参数在连接建立后**一直保留在 socket 上**，导致后续 `recv()` 空闲超过 10 秒就
抛出 `socket.timeout`（`OSError` 子类）。`_reader_loop()` 中 `except OSError`
将其捕获后 `break` → reader 线程结束 → 发送 `_disconnected` → CLI 退出 →
`disconnect()` 发送 leave → 服务器记录"客户端主动离开"。

**为什么此前自动化测试未暴露**：所有自动化复现都通过 stdin 管道持续喂命令，
每次命令都会触发服务器广播，客户端 `recv()` 持续有数据，永远不会触发 10 秒
空闲超时。只有"空闲等待"（等待开局、等待对方行动）场景才会触发。

**为什么用户感知为 3-5 秒**：用户从看到界面开始计时，叠加开局操作与渲染时间，
实际断开发生在连接建立后 10 秒（无服务器消息时）。

## 三、根因

| 项目 | 内容 |
|------|------|
| 触发条件 | 客户端 `recv()` 空闲超过 10 秒（等待开局、等待对方行动时） |
| 根因代码 | `network/client.py` `connect()` 中的 `timeout=10.0` 残留到连接 socket |
| 错误路径 | `socket.timeout` → `except OSError` 被误判为断开 → reader 退出 → CLI 退出 → 发送 leave |
| 服务器记录 | "客户端主动离开"（服务器确实收到了 leave，属正常清理） |
| 核心矛盾 | 游戏协议要求 `recv` 无限期阻塞，但 socket 上残留了连接阶段的 10 秒超时 |

## 四、解决方案

### 4.1 核心修复（`network/client.py`）

`connect()` 中连接建立成功后立即恢复阻塞模式：

```python
self._sock = socket.create_connection((host, port), timeout=timeout)
# timeout 仅用于限制连接建立阶段的等待；连接建立后必须恢复阻塞模式，
# 否则 recv() 空闲超时会被误判为连接断开（表现为约 10 秒后自动退出）
self._sock.settimeout(None)
```

### 4.2 防御性修复（`network/client.py` `_reader_loop`）

- 单独捕获 `socket.timeout`：空闲超时**不是断开信号**，记录日志后 `continue`
  继续等待，杜绝未来任何超时设置导致误判断开。
- 其他 `OSError` 才视为断开，并将退出原因（网络错误/服务器关闭/协议错误）
  随 `_disconnected` 消息带给 UI。

### 4.3 一致性加固（`network/server.py`）

`accept()` 后显式 `sock.settimeout(None)`，防止任何上游继承的超时设置影响
服务器侧 recv（当前默认阻塞，此改动为防御性）。

### 4.4 可观测性（`ui/cli.py`）

CLI 显示断开原因：`[系统] 与服务器断开连接（<原因>）`，杜绝"莫名退出"，
任何断开都能在界面上看到具体原因。

## 五、验证结果

| 验证项 | 方法 | 结果 |
|--------|------|------|
| 真实终端复现 | 两个 trae-sandbox 伪终端进程空闲等待 20 秒 | 双方均持续存活，不再断开 |
| 空闲存活回归 | 自动化脚本空闲 15 秒（> 旧 10 秒周期） | `PASS: 空闲 15 秒双方均未自动断开` |
| 完整对局回归 | 自动化脚本喂命令打完一整局 | `PASS: 完整对局打完且正常退出` |
| 单元测试 | `python -m unittest discover -s tests` | 84 个全部通过 |

## 六、防复发措施

1. **连接阶段超时与通信阶段超时分离**：`timeout` 只用于
   `socket.create_connection` 建立连接，建立后立即 `settimeout(None)`。
2. **超时语义明确**：代码中任何 `recv` 超时都只应视为"暂无可读数据"，
   不应视为连接断开（已在 `_reader_loop` 中显式处理）。
3. **断开原因可观测**：所有断开路径都记录具体原因并展示给用户，
   不再有"莫名退出"。
4. **自动化防护**：新增空闲存活回归场景（空闲等待超过任意 socket 超时值），
   防止未来引入同类回归。

## 七、经验教训

- 这次问题持续多次未能解决，根因是**排查方向集中在"客户端如何退出"的表现
  层（崩溃、输入、刷新），而非"客户端为什么认为自己应该退出"的逻辑层**。
- 关键突破点是：在**与用户完全相同的终端环境**中，用**空闲无操作**的真实进程
  复现，而非持续喂命令的自动化脚本——空闲场景暴露了 `recv` 超时这一此前
  从未触发的路径。
- 服务器日志"客户端主动离开"只是现象，真正的根因在客户端 `recv` 超时
  被误判为断开；排查时应先还原"leave 消息由谁发出、因何发出"的完整链路。
