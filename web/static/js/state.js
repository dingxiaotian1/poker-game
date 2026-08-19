/* ==========================================================================
 * state.js —— 客户端状态管理模块
 * 集中维护浏览器侧的权威展示状态（服务器状态快照 + 本人行动选项 + 会话信息）。
 * 提供 applyMessage：将 SSE 推送的消息转化为状态更新（reducer 模式），
 * 保证"唯一数据源"，渲染层只读取 State 不做状态写入。
 * ========================================================================== */
(function () {
  "use strict";

  /** 全局游戏状态（单例） */
  var state = {
    // ---- 会话信息 ----
    sessionId: null,    // 会话 ID（网关分配）
    playerId: null,     // 本人玩家 ID
    name: null,         // 本人昵称
    isHost: false,      // 本人是否为房主
    hostPlayerId: null, // 当前房主的玩家 ID（用于所有用户显示房主专属标识）
    paused: false,      // 服务器是否暂停

    // ---- 服务器最新状态快照（state 消息） ----
    snapshot: null,

    // ---- 本人行动选项（turn 消息） ----
    turn: null,

    // ---- 本人底牌（deal_hole 消息） ----
    myCards: []
  };

  /**
   * 应用一条服务器消息到状态（纯函数式更新）。
   * 返回 true 表示状态发生变化、需要触发渲染；false 表示无需重绘。
   * @param {Object} msg 服务器消息（type 字段区分类型）
   * @returns {boolean}
   */
  function applyMessage(msg) {
    var changed = true;
    switch (msg.type) {
      case "state":
        // 全量状态快照：覆盖更新；若不再轮到自己则清除行动选项
        state.snapshot = msg.state;
        // 【重点注释】同步当前房主 ID：服务器快照始终携带 host_player_id
        // （见 network/server.py 的 _broadcast_state），前端据此为所有用户
        // 渲染房主专属标识，而不仅是房主本人
        if (msg.state && typeof msg.state.host_player_id === "number") {
          state.hostPlayerId = msg.state.host_player_id;
        }
        if (state.turn && state.snapshot) {
          var cur = state.snapshot.current_player_id;
          if (cur !== state.playerId) {
            state.turn = null;
          }
        }
        break;
      case "deal_hole":
        // 服务器下发本人底牌
        state.myCards = msg.cards || [];
        break;
      case "turn":
        // 轮到自己：缓存行动选项（action 接口由 actions.js 直接读取）
        state.turn = msg.options || null;
        break;
      case "server_control":
        // 服务器暂停/恢复状态变化
        state.paused = (msg.action === "paused");
        break;
      case "_hello":
        // SSE 连接建立问候：同步暂停状态
        if (typeof msg.paused === "boolean") {
          state.paused = msg.paused;
        }
        break;
      case "join_ok":
      case "join_fail":
      case "log":
      case "chat":
      case "player_joined":
      case "player_left":
      case "hand_over":
      case "error":
      case "kick":
      case "reset_ok":
      case "reset_fail":
      case "_disconnected":
      case "_reconnecting":
      case "_reconnected":
      case "pong":
        // 无需改动 State 的消息（由渲染层直接追加到消息区）
        changed = false;
        break;
      default:
        // 未知消息类型：不报错（协议向前兼容），无需重绘
        changed = false;
        break;
    }
    return changed;
  }

  /**
   * 会话加入成功后填充会话信息（由 app.js 调用）。
   * @param {Object} joinResult join 接口返回值
   */
  function initSession(joinResult) {
    state.sessionId = joinResult.session_id;
    state.playerId = joinResult.player_id;
    state.name = joinResult.name || "";
    state.isHost = !!joinResult.is_host;
    // 加入响应携带的初始快照同样含 host_player_id（首个玩家加入即为房主）
    state.hostPlayerId = (joinResult.state && joinResult.state.host_player_id) || null;
    state.paused = !!joinResult.paused;
    state.snapshot = joinResult.state || null;
    state.turn = null;
    state.myCards = [];
  }

  window.State = state;
  window.State.applyMessage = applyMessage;
  window.State.initSession = initSession;
})();
