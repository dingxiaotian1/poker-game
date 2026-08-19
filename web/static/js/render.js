/* ==========================================================================
 * render.js —— DOM 渲染模块
 * 只读 State，将游戏状态渲染为页面元素：指标栏、牌桌、座位、行动区、
 * 消息区、房间按钮、服务器状态灯。渲染逻辑集中于此，便于维护。
 * ========================================================================== */
(function () {
  "use strict";

  // ---- DOM 元素缓存（首次访问时惰性获取）----
  var el = {};
  function byId(id) {
    if (!el[id]) el[id] = document.getElementById(id);
    return el[id];
  }

  // 阶段枚举名 -> 中文展示（与 core.game.GameState 对应）
  var PHASE_NAMES = {
    WAITING: "等待开始",
    PREFLOP: "翻牌前",
    FLOP: "翻牌",
    TURN: "转牌",
    RIVER: "河牌",
    SHOWDOWN: "摊牌",
    HAND_OVER: "本局结束"
  };

  // 花色符号与颜色映射（与 core.card.SUITS 对应）
  var SUIT_SYMBOL = { S: "♠", H: "♥", D: "♦", C: "♣" };
  var RED_SUITS = { H: true, D: true };

  // 点数 -> 显示字符
  var RANK_DISPLAY = { 2:"2",3:"3",4:"4",5:"5",6:"6",7:"7",8:"8",9:"9",10:"10",11:"J",12:"Q",13:"K",14:"A" };

  /** 将一张牌对象渲染为 HTML（大牌用于公共牌，小牌用于座位） */
  function cardHtml(card, small) {
    if (!card) return "";
    var rank = RANK_DISPLAY[card.rank] || card.rank;
    var suit = SUIT_SYMBOL[card.suit] || card.suit;
    var red = RED_SUITS[card.suit] ? " red" : "";
    var cls = small ? "poker-card small" : "poker-card";
    return '<span class="' + cls + red + '">' + rank + '<span class="suit">' + suit + "</span></span>";
  }

  /** 空牌背（未发牌/不可见时占位） */
  function cardBackHtml(small) {
    var cls = small ? "poker-card back small" : "poker-card back";
    return '<span class="' + cls + '"></span>';
  }

  /** 整体重绘：由 app.js 在状态变化后调用 */
  function renderAll() {
    renderStats();
    renderTable();
    renderRoomButtons();
    renderTurn();
    renderMyHostBadge();
  }

  /** 渲染顶栏"我的名字"旁的房主徽章（本人为房主时显示） */
  function renderMyHostBadge() {
    var badge = byId("my-host-tag");
    if (!badge) return;
    // 房主身份对所有用户一致显示：本人是房主时在顶栏名字旁加徽章
    if (window.State.hostPlayerId != null &&
        window.State.hostPlayerId === window.State.playerId) {
      badge.textContent = "(房主)";
      badge.classList.remove("hidden");
    } else {
      badge.classList.add("hidden");
    }
  }

  /** 渲染关键指标栏（对局数/阶段/底池/盲注/我的筹码/在线人数） */
  function renderStats() {
    var snap = window.State.snapshot;
    if (!snap) {
      byId("stat-hand").textContent = "0";
      byId("stat-phase").textContent = "等待开始";
      byId("stat-pot").textContent = "0";
      byId("stat-chips").textContent = "-";
      byId("stat-online").textContent = "0/10";
      return;
    }
    byId("stat-hand").textContent = snap.hand_number;
    byId("stat-phase").textContent = PHASE_NAMES[snap.state_name] || snap.state_name;
    var pot = (snap.pot && snap.pot.total) || 0;
    byId("stat-pot").textContent = pot;
    byId("stat-blinds").textContent = snap.small_blind + "/" + snap.big_blind;
    // 我的筹码：从玩家列表中按 playerId 查找
    var me = findPlayer(snap.players, window.State.playerId);
    byId("stat-chips").textContent = me ? me.chips : "-";
    byId("stat-online").textContent = snap.players.length + "/10";
  }

  /** 渲染牌桌：社区牌、底池、玩家座位 */
  function renderTable() {
    var snap = window.State.snapshot;
    if (!snap) return;

    // 社区公共牌（最多 5 张，未发满时用牌背占位提示轮次）
    var comm = byId("community");
    var cards = snap.community_cards || [];
    var expected = phaseCommunityCount(snap.state_name);
    var html = "";
    for (var i = 0; i < cards.length; i++) {
      html += cardHtml(cards[i], false);
    }
    // 尚未发满的公共牌位用牌背占位（翻牌前显示 0 张，用文字提示）
    for (var j = cards.length; j < expected; j++) {
      html += cardBackHtml(false);
    }
    comm.innerHTML = html || '<span class="placeholder">等待发牌</span>';

    // 底池
    byId("pot-amount").textContent = (snap.pot && snap.pot.total) || 0;

    // 玩家座位
    renderSeats(snap);
  }

  /** 计算当前阶段应有的公共牌数量（用于占位） */
  function phaseCommunityCount(stateName) {
    switch (stateName) {
      case "FLOP": return 3;
      case "TURN": return 4;
      case "RIVER":
      case "SHOWDOWN":
      case "HAND_OVER": return 5;
      default: return 0; // WAITING / PREFLOP
    }
  }

  /** 渲染所有玩家座位 */
  function renderSeats(snap) {
    var box = byId("seats");
    var players = snap.players || [];
    var currentId = snap.current_player_id;
    var html = players.map(function (p) {
      return seatHtml(p, currentId);
    }).join("");
    box.innerHTML = html || '<span class="placeholder">暂无玩家，等待加入...</span>';
  }

  /** 单个座位 HTML：名称(房主标记)/筹码/本局投注/底牌/状态/庄家标 */
  function seatHtml(p, currentId) {
    var me = window.State.playerId;
    var acting = (currentId === p.player_id) ? " acting" : "";
    var folded = p.folded ? " folded" : "";
    var dealer = (window.State.snapshot && window.State.snapshot.dealer_pos >= 0 &&
      seatIndexById(window.State.snapshot, p.player_id) === window.State.snapshot.dealer_pos) ? " dealer" : "";
    var isMe = (p.player_id === me) ? " is-me" : "";
    // 【重点注释】房主专属标识：对"所有用户"显示（非仅房主本人可见）。
    // hostPlayerId 由 state 快照同步（见 state.js），他人界面上同样能看到
    // 谁的座位带 (房主) 徽章
    var host = (p.player_id === window.State.hostPlayerId)
      ? '<span class="host-tag">(房主)</span>' : "";

    // 座位底牌：优先显示摊牌阶段下发的对方手牌；本人始终显示自己的底牌
    var cardHtmlStr = "";
    if (p.hole_cards && p.hole_cards.length) {
      cardHtmlStr = p.hole_cards.map(function (c) { return cardHtml(c, true); }).join("");
    } else if (p.card_count > 0) {
      // 有牌但不可见（他人手牌，未摊牌）：显示牌背
      cardHtmlStr = cardBackHtml(true) + cardBackHtml(true);
    }

    var stateText = "";
    if (p.folded) stateText = '<div class="seat-state">已弃牌</div>';
    else if (p.all_in) stateText = '<div class="seat-state">全下</div>';

    return (
      '<div class="seat' + acting + folded + dealer + isMe + '">' +
        '<div class="seat-name">' + escapeHtml(p.name) + host + "</div>" +
        '<div class="seat-chips">筹码: ' + p.chips + "</div>" +
        '<div class="seat-bet">本局下注: ' + p.current_bet + "</div>" +
        '<div class="seat-cards">' + (cardHtmlStr || "") + "</div>" +
        stateText +
      "</div>"
    );
  }

  /** 按 player_id 在座位数组中的下标（用于判断庄家标记） */
  function seatIndexById(snap, pid) {
    var players = snap.players || [];
    for (var i = 0; i < players.length; i++) {
      if (players[i].player_id === pid) return i;
    }
    return -1;
  }

  /** 渲染行动区：轮到自己时显示可用按钮，否则隐藏 */
  function renderTurn() {
    var bar = byId("action-bar");
    var turn = window.State.turn;
    var paused = window.State.paused;

    if (!turn || !turn.can_act || paused) {
      bar.classList.add("hidden");
      return;
    }
    var options = turn.options || [];

    // 按钮可用性与文案按可选项动态配置
    setActionBtn("fold", options.indexOf("fold") >= 0, "弃牌");
    setActionBtn("check", options.indexOf("check") >= 0, "让牌");
    setActionBtn("call", options.indexOf("call") >= 0, "跟注 " + (turn.call_amount || 0));
    setActionBtn("raise", options.indexOf("raise") >= 0, "加注到");
    setActionBtn("all_in", options.indexOf("all_in") >= 0, "全下");

    // 加注输入框：预设最小加注金额，并限定取值范围
    var raiseInput = byId("raise-amount");
    var minRaise = turn.min_raise_to || 0;
    raiseInput.value = minRaise;
    raiseInput.min = minRaise;
    raiseInput.max = turn.max_raise_to || minRaise;

    bar.classList.remove("hidden");
  }

  /** 设置单个行动按钮的可用性与文案 */
  function setActionBtn(action, enabled, text) {
    var btn = document.querySelector('#action-bar [data-act="' + action + '"]');
    if (!btn) return;
    btn.disabled = !enabled;
    btn.textContent = text;
  }

  /** 渲染房间操作按钮（开始/重置仅房主可用） */
  function renderRoomButtons() {
    var isHost = window.State.isHost;
    byId("room-start").disabled = !isHost;
    byId("room-reset").disabled = !isHost;
  }

  // ---------- 消息区 ----------

  /** 追加一条渲染为 HTML 的日志/消息 */
  function addLog(html, cls) {
    var box = byId("log-box");
    var line = document.createElement("div");
    line.className = "log-line " + (cls || "");
    line.innerHTML = html;
    box.appendChild(line);
    // 自动滚动到底部，保证最新消息可见
    box.scrollTop = box.scrollHeight;
    // 限制消息条数，避免无限增长导致页面卡顿
    while (box.children.length > 300) {
      box.removeChild(box.firstChild);
    }
  }

  /** 追加系统日志（服务器 log 消息） */
  function addSystemLog(text) {
    addLog('<span class="time">' + timeNow() + "</span>" + escapeHtml(text), "msg-system");
  }

  /** 追加聊天消息（含发送者、时间戳；房主消息附带专属徽章） */
  function addChat(who, text, isMe, isHostSender) {
    var cls = isMe ? " who me" : " who";
    // 房主发送的聊天：名字后附加 (房主) 徽章，让所有玩家一眼识别房主身份
    var hostMark = isHostSender ? '<span class="host-tag">(房主)</span>' : "";
    addLog(
      '<span class="time">' + timeNow() + "</span>" +
      '<span class="who' + cls + '">' + escapeHtml(who) + "</span>" +
      hostMark + "：" +
      escapeHtml(text),
      "msg-chat"
    );
  }

  /** 追加错误提示 */
  function addError(text) {
    addLog('<span class="time">' + timeNow() + "</span>" + escapeHtml(text), "msg-error");
  }

  /** 追加醒目提示（金色） */
  function addWarn(text) {
    addLog('<span class="time">' + timeNow() + "</span>" + escapeHtml(text), "msg-warn");
  }

  /** 清空全部消息与聊天记录（仅本地界面操作，不影响游戏状态与服务器） */
  function clearLog() {
    var box = byId("log-box");
    // 直接清空容器内容；此后新日志/聊天仍会正常追加显示
    box.innerHTML = "";
  }

  // ---------- 服务器状态与 Toast ----------

  /** 更新服务器状态灯与文字（running/paused/stopped） */
  function renderServerStatus(status) {
    var light = byId("server-light");
    var text = byId("server-text");
    if (status && status.running) {
      if (status.paused) {
        light.className = "light light-pause";
        text.textContent = "服务器运行中（已暂停）";
      } else {
        light.className = "light light-on";
        text.textContent = "服务器运行中";
      }
    } else {
      light.className = "light light-off";
      text.textContent = "服务器未运行";
    }
  }

  /** 短暂显示一个提示条（toast），1.8 秒后自动消失 */
  function showToast(text) {
    var toast = byId("toast");
    toast.textContent = text;
    toast.classList.remove("hidden");
    clearTimeout(showToast._timer);
    showToast._timer = setTimeout(function () {
      toast.classList.add("hidden");
    }, 1800);
  }

  // ---------- 工具函数 ----------

  function findPlayer(players, pid) {
    if (!players) return null;
    for (var i = 0; i < players.length; i++) {
      if (players[i].player_id === pid) return players[i];
    }
    return null;
  }

  /** HTML 转义，防止聊天/昵称中的特殊字符破坏页面结构 */
  function escapeHtml(str) {
    return String(str == null ? "" : str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /** 当前时间 HH:MM:SS */
  function timeNow() {
    var d = new Date();
    function pad(n) { return (n < 10 ? "0" : "") + n; }
    return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }

  // 暴露渲染接口
  window.Render = {
    renderAll: renderAll,
    addSystemLog: addSystemLog,
    addChat: addChat,
    addError: addError,
    addWarn: addWarn,
    renderServerStatus: renderServerStatus,
    showToast: showToast,
    clearLog: clearLog,
    escapeHtml: escapeHtml,
    cardHtml: cardHtml
  };
})();
