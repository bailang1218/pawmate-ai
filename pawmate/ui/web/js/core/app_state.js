/**
 * PawMate frontend state store.
 *
 * Keeps UI status sources in one place so chat, heartbeat, cancellation,
 * and short-lived notices do not overwrite each other directly.
 */
(function () {
  "use strict";

  var DEFAULT_STATE = {
    bridgeReady: false,
    taskRunning: false,
    cancelling: false,
    inputEnabled: true,
    chatStatus: "starting",
    chatStatusText: "starting...",
    heartbeatStatus: "unknown",
    heartbeatPayload: null,
    transientStatus: null,
  };

  var state = clone(DEFAULT_STATE);
  var subscribers = [];
  var transientTimer = null;

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function now() {
    return Date.now();
  }

  function parseJson(payload) {
    if (payload && typeof payload === "object") return payload;
    try {
      return JSON.parse(payload || "{}");
    } catch (e) {
      return {};
    }
  }

  function normalizeChatStatus(value) {
    var raw = String(value || "").trim();
    var lower = raw.toLowerCase();
    if (!raw || lower === "ready") {
      return { status: "ready", text: "ready" };
    }
    if (
      lower.indexOf("starting") === 0 ||
      lower.indexOf("initializing") === 0 ||
      lower.indexOf("loading") === 0
    ) {
      return { status: "starting", text: raw };
    }
    if (lower.indexOf("thinking") === 0) {
      return { status: "thinking", text: raw };
    }
    if (lower.indexOf("working") === 0) {
      return { status: "working", text: raw };
    }
    if (lower.indexOf("error") === 0 || lower.indexOf("failed") === 0) {
      return { status: "error", text: raw };
    }
    return { status: "error", text: raw };
  }

  function normalizeHeartbeatStatus(payload) {
    var data = parseJson(payload);
    if (data.task_running) return "busy";
    var raw = String(data.status || data.engine || "unknown").toLowerCase();
    if (raw === "busy") return "busy";
    if (raw === "idle") return "idle";
    if (raw === "ready") return "ready";
    return "unknown";
  }

  function getActiveTransient(current) {
    var transient = current.transientStatus;
    if (!transient) return null;
    if (transient.expiresAt && transient.expiresAt <= now()) return null;
    return transient;
  }

  function clearExpiredTransient() {
    if (state.transientStatus && state.transientStatus.expiresAt <= now()) {
      state.transientStatus = null;
      notify();
    }
  }

  function scheduleTransientExpiry() {
    if (transientTimer) {
      clearTimeout(transientTimer);
      transientTimer = null;
    }
    if (!state.transientStatus || !state.transientStatus.expiresAt) return;
    var delay = Math.max(0, state.transientStatus.expiresAt - now());
    transientTimer = setTimeout(clearExpiredTransient, delay + 5);
  }

  function notify() {
    clearExpiredTransientSilently();
    var snapshot = getState();
    subscribers.slice().forEach(function (fn) {
      try {
        fn(snapshot);
      } catch (e) {
        console.error("[PawAppState] subscriber failed", e);
      }
    });
  }

  function clearExpiredTransientSilently() {
    if (state.transientStatus && state.transientStatus.expiresAt <= now()) {
      state.transientStatus = null;
    }
  }

  function dispatch(action) {
    if (!action || !action.type) return getState();

    switch (action.type) {
      case "BRIDGE_READY":
        state.bridgeReady = true;
        if (state.chatStatus === "error" && state.chatStatusText === "bridge unavailable") {
          state.chatStatus = "starting";
          state.chatStatusText = "starting...";
        }
        break;

      case "BRIDGE_UNAVAILABLE":
        state.bridgeReady = false;
        state.chatStatus = "error";
        state.chatStatusText = action.text || "bridge unavailable";
        break;

      case "CHAT_STATUS": {
        var chat = normalizeChatStatus(action.value);
        state.chatStatus = chat.status;
        state.chatStatusText = chat.text;
        break;
      }

      case "HEARTBEAT_STATUS":
        state.heartbeatPayload = parseJson(action.payload);
        state.heartbeatStatus = normalizeHeartbeatStatus(state.heartbeatPayload);
        break;

      case "TASK_RUNNING":
        state.taskRunning = !!action.value;
        if (!state.taskRunning) {
          state.cancelling = false;
          if (state.heartbeatStatus === "busy") {
            state.heartbeatStatus = "ready";
            state.heartbeatPayload = Object.assign({}, state.heartbeatPayload || {}, {
              status: "ready",
              engine: "ready",
              task_running: false,
              task_duration_sec: 0,
            });
          }
        }
        break;

      case "TASK_CANCELLING":
        state.cancelling = !!action.value;
        break;

      case "TRANSIENT_STATUS": {
        var ttl = Number(action.ttl || 0);
        state.transientStatus = {
          text: String(action.text || ""),
          expiresAt: ttl > 0 ? now() + ttl : null,
        };
        scheduleTransientExpiry();
        break;
      }

      case "TURN_CANCELLED":
        state.taskRunning = false;
        state.cancelling = false;
        state.chatStatus = "ready";
        state.chatStatusText = "ready";
        break;

      case "INPUT_ENABLED":
        state.inputEnabled = !!action.value;
        break;

      default:
        console.warn("[PawAppState] unknown action", action.type);
        break;
    }

    notify();
    return getState();
  }

  function getDisplayStatus(current) {
    current = current || state;

    var transient = getActiveTransient(current);
    if (transient) return transient.text;

    if (current.cancelling) return "cancelling...";
    if (current.chatStatus === "starting") return current.chatStatusText || "starting...";
    if (current.chatStatus === "thinking") return current.chatStatusText || "thinking...";
    if (current.chatStatus === "working") return current.chatStatusText || "working...";
    if (current.chatStatus === "error") return current.chatStatusText || "error";

    if (current.taskRunning && current.heartbeatStatus === "busy") {
      var duration = Number(current.heartbeatPayload && current.heartbeatPayload.task_duration_sec);
      return duration > 0 ? "busy " + duration + "s" : "busy";
    }
    if (current.heartbeatStatus === "idle") return "idle";

    return "ready";
  }

  function getStopButtonState(current) {
    current = current || state;
    if (!current.taskRunning) {
      return {
        status: "idle",
        disabled: true,
        running: false,
        idle: true,
        cancelling: false,
      };
    }
    if (current.cancelling) {
      return {
        status: "cancelling",
        disabled: true,
        running: false,
        idle: false,
        cancelling: true,
      };
    }
    return {
      status: "running",
      disabled: false,
      running: true,
      idle: false,
      cancelling: false,
    };
  }

  function getInputState(current) {
    current = current || state;
    return {
      enabled: !!(
        current.inputEnabled &&
        current.bridgeReady &&
        current.chatStatus === "ready" &&
        !current.cancelling
      ),
    };
  }

  function getState() {
    clearExpiredTransientSilently();
    return clone(state);
  }

  function subscribe(fn) {
    if (typeof fn !== "function") return function () {};
    subscribers.push(fn);
    fn(getState());
    return function () {
      subscribers = subscribers.filter(function (item) {
        return item !== fn;
      });
    };
  }

  window.PawAppState = {
    dispatch: dispatch,
    subscribe: subscribe,
    getState: getState,
    getDisplayStatus: getDisplayStatus,
    getStopButtonState: getStopButtonState,
    getInputState: getInputState,
    _normalizeChatStatus: normalizeChatStatus,
    _normalizeHeartbeatStatus: normalizeHeartbeatStatus,
  };
})();
