/* ==========================================================================
 * actions.js —— 用户交互处理模块
 * 负责绑定页面所有按钮/输入事件：行动发送、房间操作、聊天、服务器控制、
 * 确认弹窗。所有用户操作在此统一调用 API，并对错误做统一提示。
 * ========================================================================== */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  /** 确认弹窗：显示标题与说明，用户确认后执行 onOk（用于重置/停止等高危操作） */
  function confirmDialog(title, text, onOk) {
    $("modal-title").textContent = title;
    $("modal-text").textContent = text;
    $("modal-mask").classList.remove("hidden");
    // 绑定确认与取消（先解绑旧事件，避免重复点击叠加回调）
    var okBtn = $("modal-ok");
    var cancelBtn = $("modal-cancel");
    var oldOk = okBtn.onclick;
    var oldCancel = cancelBtn.onclick;
    okBtn.onclick = function () {
      hideDialog();
      okBtn.onclick = oldOk;     // 恢复
      cancelBtn.onclick = oldCancel;
      if (onOk) onOk();
    };
    cancelBtn.onclick = function () {
      hideDialog();
      okBtn.onclick = oldOk;
      cancelBtn.onclick = oldCancel;
    };
  }

  function hideDialog() {
    $("modal-mask").classList.add("hidden");
  }

  /** 发送一个行动（action 按钮统一入口） */
  function sendAction(act) {
    var amount = 0;
    // 加注必须携带目标金额：从输入框读取并做基础校验
    if (act === "raise") {
      amount = parseInt($("raise-amount").value, 10);
      if (!amount || amount <= 0) {
        window.Render.showToast("请输入有效的加注金额");
        return;
      }
    }
    window.API.action(window.State.sessionId, act, amount).catch(function (err) {
      // 服务器拒绝（未轮到你/暂停/金额非法）统一提示
      window.Render.addError("行动失败：" + err.message);
      window.Render.showToast("行动失败");
    });
  }

  /**
   * 低调模式切换：在"图形化主题"与"纯文本文档风"之间切换。
   * 原理：
   *   1. 启用/禁用 id=plain-css 的样式表（plain.css 定义全部纯文本样式）
   *   2. 给 <body> 添加/移除 .plain 类（所有低调样式均以 body.plain 为作用域）
   *   3. 将偏好写入 localStorage，刷新页面后自动恢复
   * @param {boolean} enabled true=启用低调模式
   */
  function togglePlainMode(enabled) {
    var css = $("plain-css");
    var checkbox = $("plain-checkbox");
    var btn = $("plain-toggle-btn");
    if (enabled) {
      // 启用：加载低调样式表 + 挂上 body.plain 作用域类
      css.disabled = false;
      document.body.classList.add("plain");
      if (btn) btn.textContent = "常规模式";
    } else {
      // 关闭：卸载低调样式表，恢复图形化主题
      css.disabled = true;
      document.body.classList.remove("plain");
      if (btn) btn.textContent = "低调模式";
    }
    // 顶栏按钮与加入页勾选框保持状态同步
    if (checkbox) checkbox.checked = enabled;
    // 持久化偏好；localStorage 在禁用/隐私模式下可能抛异常，需容错
    try {
      localStorage.setItem("poker.plain", enabled ? "1" : "0");
    } catch (e) { /* 忽略：无法持久化时仅影响本次会话 */ }
  }

  /** 页面加载时恢复低调模式偏好（在 bindEvents 末尾调用） */
  function restorePlainMode() {
    var saved = "0";
    try {
      saved = localStorage.getItem("poker.plain") || "0";
    } catch (e) { /* 读取失败则按默认（常规模式）处理 */ }
    togglePlainMode(saved === "1");
  }

  /** 绑定页面所有交互事件（app.js 初始化时调用） */
  function bindEvents() {
    // ---- 行动区按钮 ----
    var actionButtons = document.querySelectorAll("#action-bar [data-act]");
    Array.prototype.forEach.call(actionButtons, function (btn) {
      btn.addEventListener("click", function () {
        sendAction(btn.getAttribute("data-act"));
      });
    });

    // ---- 房间操作 ----
    $("room-start").addEventListener("click", function () {
      window.API.start(window.State.sessionId).catch(function (err) {
        window.Render.addError("开局失败：" + err.message);
      });
    });
    $("room-reset").addEventListener("click", function () {
      // 重置属高风险操作：弹确认框，防止误点
      confirmDialog(
        "确认重置房间？",
        "对局数将清零，所有玩家筹码恢复初始值，且玩家列表与房间规则保留。此操作不可撤销。",
        function () {
          window.API.reset(window.State.sessionId).catch(function (err) {
            window.Render.addError("重置失败：" + err.message);
          });
        }
      );
    });
    $("room-players").addEventListener("click", function () {
      showPlayersDialog();
    });

    // ---- 聊天 ----
    function sendChat() {
      var input = $("chat-input");
      var text = input.value.trim();
      if (!text) return;
      window.API.chat(window.State.sessionId, text).catch(function (err) {
        window.Render.addError("发送失败：" + err.message);
      });
      input.value = ""; // 发送后清空输入框，方便连续聊天
      input.focus();
    }
    $("chat-send").addEventListener("click", sendChat);
    $("chat-input").addEventListener("keydown", function (e) {
      if (e.key === "Enter") sendChat();
    });

    // ---- 清空消息与聊天记录（随时清空，本地操作不影响游戏）----
    $("log-clear").addEventListener("click", function () {
      window.Render.clearLog();
    });

    // ---- 服务器控制（启动/暂停/恢复/停止）----
    $("ctl-start").addEventListener("click", function () {
      window.API.control("start").then(function () {
        window.Render.showToast("服务器已启动");
        refreshStatus();
      }).catch(function (err) {
        window.Render.addError("启动失败：" + err.message);
      });
    });
    $("ctl-pause").addEventListener("click", function () {
      window.API.control("pause").then(function () {
        window.Render.addWarn("游戏已暂停（所有行动被冻结）");
      }).catch(function (err) {
        window.Render.addError("暂停失败：" + err.message);
      });
    });
    $("ctl-resume").addEventListener("click", function () {
      window.API.control("resume").then(function () {
        window.Render.addSystemLog("游戏已恢复");
      }).catch(function (err) {
        window.Render.addError("恢复失败：" + err.message);
      });
    });
    $("ctl-stop").addEventListener("click", function () {
      confirmDialog(
        "确认停止服务器？",
        "将停止游戏服务器并断开所有在线玩家，正在进行的对局会丢失。",
        function () {
          window.API.control("stop").then(function () {
            window.Render.showToast("服务器已停止");
            refreshStatus();
          }).catch(function (err) {
            window.Render.addError("停止失败：" + err.message);
          });
        }
      );
    });

    // ---- 帮助开关 ----
    $("help-toggle").addEventListener("click", function () {
      window.PokerHelp.toggleHelp($("in-help-panel"));
    });
    $("help-btn").addEventListener("click", function () {
      window.PokerHelp.toggleHelp($("help-panel"));
    });

    // ---- 加入界面：服务器状态查询 ----
    $("status-btn").addEventListener("click", refreshStatus);

    // ---- 低调模式切换（顶栏按钮 + 加入页勾选框）----
    $("plain-toggle-btn").addEventListener("click", function () {
      // 以当前 body 是否带 .plain 类判断当前状态，点击即取反
      togglePlainMode(!document.body.classList.contains("plain"));
    });
    if ($("plain-checkbox")) {
      $("plain-checkbox").addEventListener("change", function () {
        togglePlainMode($("plain-checkbox").checked);
      });
    }

    // 页面加载时恢复上次保存的低调模式偏好
    restorePlainMode();
  }

  /** 刷新服务器状态（状态灯 + 加入页状态框） */
  function refreshStatus() {
    window.API.status().then(function (data) {
      var st = data.status || {};
      window.Render.renderServerStatus(st);
      // 加入界面的状态详情框
      var box = $("server-status-box");
      var lines = [];
      if (st.running) {
        lines.push("● 运行中" + (st.paused ? "（已暂停）" : ""));
        lines.push("● 在线玩家: " + (st.online_players || 0) + "/" + (st.max_players || "?"));
        lines.push("● 对局数: " + (st.hand_number || 0));
        lines.push("● 地址: " + (st.advertised_ip || "?"));
        lines.push("● 运行时长: " + fmtUptime(st.uptime_seconds));
      } else {
        lines.push("○ 服务器未运行（可点击顶栏『启动』按钮启动）");
      }
      box.innerHTML = lines.join("<br>");
      box.classList.remove("hidden");
    }).catch(function (err) {
      window.Render.renderServerStatus(null);
      var box = $("server-status-box");
      box.textContent = "查询失败: " + err.message;
      box.classList.remove("hidden");
    });
  }

  /** 把秒数格式化为 时:分:秒 */
  function fmtUptime(seconds) {
    if (!seconds && seconds !== 0) return "-";
    var h = Math.floor(seconds / 3600);
    var m = Math.floor((seconds % 3600) / 60);
    var s = Math.floor(seconds % 60);
    return (h > 0 ? h + "时" : "") + m + "分" + s + "秒";
  }

  /** 玩家列表弹窗：从当前快照读取玩家信息 */
  function showPlayersDialog() {
    var snap = window.State.snapshot;
    var players = (snap && snap.players) || [];
    if (!players.length) {
      window.Render.showToast("当前没有玩家");
      return;
    }
    var lines = players.map(function (p) {
      var mark = p.player_id === window.State.playerId ? "（我）" : "";
      // 【重点注释】房主标记对所有用户显示：以服务器同步的 hostPlayerId 为准
      var hostMark = p.player_id === window.State.hostPlayerId ? " [房主]" : "";
      return "· " + p.name + mark + "：筹码 " + p.chips + hostMark;
    });
    confirmDialog("当前玩家（" + players.length + " 人）", lines.join("\n"), null);
  }

  // 暴露接口
  window.Actions = {
    bindEvents: bindEvents,
    refreshStatus: refreshStatus,
    sendAction: sendAction,
    confirmDialog: confirmDialog,
    hideDialog: hideDialog,
    togglePlainMode: togglePlainMode,
    restorePlainMode: restorePlainMode
  };
})();
