/* ==========================================================================
 * api.js —— REST API 调用模块
 * 封装所有与 Web 网关的 HTTP 交互（加入/行动/聊天/控制/状态）。
 * 统一处理 JSON 解析、错误信息提取，供 actions.js / app.js 调用。
 * ========================================================================== */
(function () {
  "use strict";

  /** 通用请求封装：POST JSON 并解析响应 */
  function post(path, payload) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {})
    }).then(handleResponse);
  }

  /** 通用 GET 请求封装 */
  function get(path) {
    return fetch(path).then(handleResponse);
  }

  /** 解析 HTTP 响应：非 2xx 时抛出带 error 信息的异常 */
  function handleResponse(response) {
    return response.json().catch(function () {
      // 响应不是合法 JSON（网关异常）时给出兜底错误信息
      throw new Error("服务器返回异常（HTTP " + response.status + "）");
    }).then(function (data) {
      if (!response.ok || data.ok === false) {
        throw new Error(data.error || ("请求失败（HTTP " + response.status + "）"));
      }
      return data;
    });
  }

  /** API 接口集合 */
  window.API = {
    /** 加入房间：创建会话并加入，返回 {session_id, player_id, is_host, state} */
    join: function (name) {
      return post("/api/join", { name: name });
    },

    /** 发送游戏行动：action 取值 fold/check/call/raise/all_in */
    action: function (sessionId, action, amount) {
      return post("/api/action", {
        session_id: sessionId,
        action: action,
        amount: amount || 0
      });
    },

    /** 房主开局（服务器会校验房主权限） */
    start: function (sessionId) {
      return post("/api/start", { session_id: sessionId });
    },

    /** 房主重置房间（服务器会校验房主权限） */
    reset: function (sessionId) {
      return post("/api/reset", { session_id: sessionId });
    },

    /** 发送聊天消息 */
    chat: function (sessionId, text) {
      return post("/api/chat", { session_id: sessionId, text: text });
    },

    /** 服务器控制：start / pause / resume / stop */
    control: function (action) {
      return post("/api/control", { action: action });
    },

    /** 查询服务器运行状态 */
    status: function () {
      return get("/api/status");
    }
  };
})();
