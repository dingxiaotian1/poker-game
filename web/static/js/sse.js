/* ==========================================================================
 * sse.js —— 实时数据同步模块（SSE 长连接）
 * 使用 EventSource 建立服务器到浏览器的单向实时推送通道，
 * 接收 state/turn/log/chat/hand_over/server_control 等事件。
 * 浏览器兼容性：EventSource 受 Chrome/Firefox/Safari/Edge 全支持。
 * ========================================================================== */
(function () {
  "use strict";

  var source = null;      // 当前 EventSource 实例
  var retryTimer = null;  // 自动重连定时器句柄

  /**
   * 建立 SSE 连接并注册事件处理回调。
   * @param {string} sessionId 会话 ID
   * @param {function(Object): void} onEvent 每条消息的处理回调
   * @param {function(string): void} onStatus 连接状态变化回调（可选，参数: connected/error/closed）
   */
  function connect(sessionId, onEvent, onStatus) {
    close(); // 先清理可能残留的旧连接

    if (onStatus) onStatus("connecting");
    source = new EventSource(
      "/api/events?session_id=" + encodeURIComponent(sessionId)
    );

    // 正常消息事件：data 为 JSON 字符串，解析后交给业务回调
    source.onmessage = function (event) {
      var msg = null;
      try {
        msg = JSON.parse(event.data);
      } catch (e) {
        // 忽略无法解析的数据（正常不会发生）
        return;
      }
      // 内部心跳注释行（_heartbeat）不交给业务层
      if (msg.type !== "_heartbeat") {
        onEvent(msg);
      }
    };

    // 连接错误：EventSource 会自动重连；仅在长时间失败时通知 UI
    source.onerror = function () {
      if (onStatus) onStatus("error");
      // 当连接已进入 CLOSED 状态（重连失败）时，安排一次手动重试
      if (source && source.readyState === EventSource.CLOSED) {
        scheduleRetry(sessionId, onEvent, onStatus);
      }
    };

    // 连接成功建立（网关推送 _hello 后触发 open）
    source.onopen = function () {
      if (onStatus) onStatus("connected");
    };
  }

  /** 手动重连：仅连接意外关闭时触发，指数退避避免风暴 */
  function scheduleRetry(sessionId, onEvent, onStatus) {
    if (retryTimer) return; // 已有重连定时器在排队
    retryTimer = setTimeout(function () {
      retryTimer = null;
      // 浏览器 EventSource 在 onerror 后仍可能处于连接中，重新建立前先清理
      if (source) {
        try { source.close(); } catch (e) { /* 忽略 */ }
        source = null;
      }
      connect(sessionId, onEvent, onStatus);
    }, 1500);
  }

  /** 主动关闭 SSE 连接（刷新/退出页面时调用） */
  function close() {
    if (retryTimer) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
    if (source) {
      try { source.close(); } catch (e) { /* 忽略 */ }
      source = null;
    }
  }

  window.SSE = { connect: connect, close: close };
})();
