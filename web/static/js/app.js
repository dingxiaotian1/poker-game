/* ==========================================================================
 * app.js —— 应用入口与消息分发模块
 * 负责：初始化事件绑定、加入房间流程、SSE 连接建立、服务器消息分发
 * （消息 → State 更新 → 渲染 的单向数据流）。
 * ========================================================================== */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  /** 页面加载完成后的初始化入口 */
  function init() {
    window.Actions.bindEvents();
    // 加入输入框回车直接进入
    $("join-name").addEventListener("keydown", function (e) {
      if (e.key === "Enter") joinGame();
    });
    $("join-btn").addEventListener("click", joinGame);
    // 页面加载时先查询一次服务器状态
    window.Actions.refreshStatus();
  }

  /** 加入游戏：调用网关创建会话并建立 SSE 实时通道 */
  function joinGame() {
    var name = $("join-name").value.trim();
    if (!name) {
      showJoinError("请输入昵称");
      return;
    }
    setJoinBusy(true);
    window.API.join(name)
      .then(function (result) {
        // 会话建立成功：填充客户端状态并进入游戏界面
        window.State.name = name;
        window.State.initSession(result);
        switchToGameScreen();
        // 建立 SSE 实时数据通道（核心同步机制）
        window.SSE.connect(result.session_id, onServerMessage, onSseStatus);
        // 渲染初始状态（join 响应已携带当前快照）
        renderInitial(result.state);
      })
      .catch(function (err) {
        showJoinError(err.message || "加入失败，请检查服务器是否运行");
      })
      .finally(function () {
        setJoinBusy(false);
      });
  }

  /** 加入成功后切换到游戏主界面 */
  function switchToGameScreen() {
    $("join-screen").classList.add("hidden");
    $("game-screen").classList.remove("hidden");
    $("my-name").textContent = window.State.name;
  }

  /** 渲染初始状态：先显示历史日志，再渲染牌桌快照 */
  function renderInitial(snapshot) {
    // 历史日志（服务器只返回最近 20 条）
    if (snapshot && snapshot.log && snapshot.log.length) {
      snapshot.log.forEach(function (text) {
        window.Render.addSystemLog(text);
      });
    }
    window.Render.renderAll();
    // 房主提示
    if (window.State.isHost) {
      window.Render.addSystemLog("你是房主：点击『开始游戏』发牌");
    } else {
      window.Render.addSystemLog("等待房主开始游戏...");
    }
  }

  /** SSE 连接状态变化回调 */
  function onSseStatus(status) {
    if (status === "connected") {
      window.Render.showToast("实时连接已建立");
    } else if (status === "error") {
      window.Render.addWarn("实时连接异常，正在自动重试...");
    }
  }

  /**
   * 服务器消息统一分发（单向数据流：消息 → State → 渲染）。
   * @param {Object} msg 服务器推送消息
   */
  function onServerMessage(msg) {
    // 记录旧的房主 ID：applyMessage 会将其更新为新快照值，据此检测房主交接
    var oldHost = window.State.hostPlayerId;
    // 先应用状态类消息（state/turn/deal_hole/server_control/_hello）
    var changed = window.State.applyMessage(msg);
    if (changed) {
      window.Render.renderAll();
    }
    // 房主变更实时提示：房主离开/被接管时服务器会在 state 快照中更新
    // host_player_id，检测到变化即提示所有用户（新老房主都会收到）
    if (msg.type === "state" && msg.state &&
        typeof msg.state.host_player_id === "number" &&
        msg.state.host_player_id !== oldHost) {
      var hostName = playerNameById(msg.state.players, msg.state.host_player_id);
      window.Render.addWarn("[系统] 房主已变更为 " + hostName);
    }
    // 再按类型处理需要输出到消息区的消息
    dispatchToMessageArea(msg);
  }

  /** 将服务器消息渲染到消息聊天区 */
  function dispatchToMessageArea(msg) {
    switch (msg.type) {
      case "log":
        // 服务器游戏日志（开局/下注/弃牌/摊牌等）
        window.Render.addSystemLog(msg.message);
        break;
      case "chat":
      case "chat_bc":
        // 玩家聊天（本人消息金色高亮；房主消息带专属徽章）。
        // 【重点注释】服务器广播的聊天消息类型为 chat_bc（见 network/protocol.py
        // MSG_CHAT_BC），字段与 chat 一致（sender/text），两者合并处理，
        // 否则 Web 端聊天消息会被静默丢弃、无法回显
        var senderId = playerIdByName(window.State.snapshot, msg.sender);
        var isHostSender = senderId != null && senderId === window.State.hostPlayerId;
        window.Render.addChat(msg.sender, msg.text, msg.sender === window.State.name, isHostSender);
        break;
      case "player_joined":
        window.Render.addSystemLog(msg.name + " 加入了房间（在线 " + msg.player_count + " 人）");
        break;
      case "player_left":
        window.Render.addSystemLog(msg.name + " 离开了房间（在线 " + msg.player_count + " 人）");
        break;
      case "hand_over":
        // 本局结束：醒目展示结果摘要
        window.Render.addWarn("──── 本局结束 ────");
        if (msg.summary) {
          window.Render.addWarn("[结果] " + msg.summary);
        }
        if (window.State.isHost) {
          window.Render.addSystemLog("你是房主：点击『开始游戏』开始下一局");
        }
        break;
      case "showdown":
        // 摊牌：展示各玩家手牌与结果（简化：逐条输出）
        if (msg.results && msg.results.length) {
          window.Render.addWarn("──── 摊牌 ────");
          msg.results.forEach(function (r) {
            window.Render.addSystemLog(r);
          });
        }
        break;
      case "error":
        // 服务器错误（非法行动、未轮到你等）
        window.Render.addError("[错误] " + msg.message);
        break;
      case "kick":
        window.Render.addError("[被踢出] " + msg.reason);
        break;
      case "reset_ok":
        window.Render.addWarn("[系统] 房间已重置：对局数清零，所有玩家筹码恢复初始值");
        break;
      case "reset_fail":
        window.Render.addError("[错误] " + msg.reason);
        break;
      case "_disconnected":
        window.Render.addError("[连接断开] " + (msg.reason || "连接已断开"));
        break;
      case "_reconnecting":
        window.Render.addWarn("[系统] 连接断开，正在自动重连...");
        break;
      case "_reconnected":
        window.Render.addSystemLog("[系统] 重连成功，已重新加入房间");
        break;
      case "join_ok":
      case "join_fail":
      case "pong":
      case "_hello":
      case "_heartbeat":
        // 无界面输出（join 结果已由 API 响应处理）
        break;
      default:
        break;
    }
  }

  /** 加入页错误提示 */
  function showJoinError(text) {
    var box = $("join-error");
    box.textContent = text;
    box.classList.remove("hidden");
  }

  /** 按玩家 ID 在状态快照中查找昵称（用于房主变更提示） */
  function playerNameById(players, pid) {
    if (!players) return "未知玩家";
    for (var i = 0; i < players.length; i++) {
      if (players[i].player_id === pid) return players[i].name;
    }
    return "未知玩家";
  }

  /** 按昵称在状态快照中查找玩家 ID（昵称唯一：服务器拒绝重名加入） */
  function playerIdByName(snap, name) {
    if (!snap || !snap.players) return null;
    for (var i = 0; i < snap.players.length; i++) {
      if (snap.players[i].name === name) return snap.players[i].player_id;
    }
    return null;
  }

  /** 加入按钮忙碌状态（防重复点击） */
  function setJoinBusy(busy) {
    $("join-btn").disabled = busy;
    $("join-btn").textContent = busy ? "连接中..." : "进入牌桌";
  }

  // DOM 就绪后启动（脚本在 body 末尾加载，DOM 已可用）
  init();
})();
