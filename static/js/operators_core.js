(function () {
  "use strict";
  var SR = window.__OPERATORS_SCRIPT_ROOT__ || "";
  function api(path) {
    if (!path) path = "/";
    if (path.charAt(0) !== "/") path = "/" + path;
    return SR + path;
  }
  window.operatorsApi = api;

  /**
   * Footer testuale con tempi (browser → NX): round-trip totale, tempo elaborazione server da header.
   * Richiede risposta fetch ``response`` e ``t0 = performance.now()`` preso prima del fetch.
   */
  window.operatorsHttpTimingFooterLines = function (response, t0) {
    var lines = [];
    if (typeof performance !== "undefined" && t0 != null) {
      lines.push("client_round_trip_ms: " + Math.round(performance.now() - t0));
    }
    var srv = null;
    try {
      if (response && response.headers && response.headers.get) {
        srv = response.headers.get("X-Dashboard-Server-Ms");
      }
    } catch (e) {
      srv = null;
    }
    if (srv != null && String(srv).trim() !== "") {
      lines.push("server_process_ms (HTTP header): " + srv);
      var rt = Math.round(performance.now() - t0);
      var sn = parseFloat(srv);
      if (!isNaN(sn) && !isNaN(rt)) {
        lines.push("estimated_browser_network_ms: " + Math.max(0, Math.round(rt - sn)));
      }
    }
    return lines.length ? "\n--- HTTP timing ---\n" + lines.join("\n") : "";
  };

  /** Registro errori/avvisi tab Moto (``#operatorsErrorLogPre``). */
  var _opErrPlaceholder = "—";

  window.operatorsClearIssueLog = function () {
    var pre = document.getElementById("operatorsErrorLogPre");
    if (pre) {
      pre.textContent = _opErrPlaceholder;
    }
  };

  /**
   * @param {"err"|"warn"} level
   * @param {string} source es. "Braccio · live"
   * @param {string} title
   * @param {*} detail opzionale (oggetto o stringa)
   */
  window.operatorsAppendIssueLog = function (level, source, title, detail) {
    var pre = document.getElementById("operatorsErrorLogPre");
    if (!pre) {
      return;
    }
    var tag = level === "warn" ? "WARN" : "ERR";
    var block =
      "[" +
      tag +
      " " +
      new Date().toLocaleTimeString() +
      "] " +
      source +
      " — " +
      title;
    if (detail !== undefined && detail !== null && detail !== "") {
      try {
        block +=
          "\n" + (typeof detail === "string" ? detail : JSON.stringify(detail, null, 2));
      } catch (e) {
        block += "\n" + String(detail);
      }
    }
    var prev = pre.textContent || "";
    if (!prev || prev === _opErrPlaceholder) {
      pre.textContent = block;
    } else {
      pre.textContent = block + "\n\n—\n\n" + prev;
    }
    if (pre.textContent.length > 14000) {
      pre.textContent = pre.textContent.slice(0, 14000) + "\n… [troncato]";
    }
  };
  function setPill(id, text, cls) {
    var el = document.getElementById(id);
    if (!el) {
      return;
    }
    el.textContent = text;
    el.className = "pill " + (cls || "");
  }

  window.operatorsSwitchTab = function (name) {
    document.querySelectorAll(".tab-panel").forEach(function (p) {
      p.classList.toggle("active", p.getAttribute("data-tab") === name);
    });
    document.querySelectorAll("nav.tab-bar .tab-bar-btn").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-tab") === name);
    });
    if (
      (name === "scene" || name === "robot" || name === "grasp") &&
      window.operatorsRefreshCamerasStatus
    ) {
      window.operatorsRefreshCamerasStatus();
    }
    if (name === "stato") {
      if (window.operatorsMissionConsoleRefresh) {
        window.operatorsMissionConsoleRefresh();
      }
      if (window.operatorsRefreshStack) {
        window.operatorsRefreshStack();
      }
    }
    if (name === "3d" && window.operatorsScene3dOnTabShown) {
      window.operatorsScene3dOnTabShown();
    }
    if (name === "grasp" && window.operatorsGraspDockBumpPreviews) {
      window.operatorsGraspDockBumpPreviews();
    }
    if (name === "calib" && window.operatorsCalibrationOnTabShown) {
      window.operatorsCalibrationOnTabShown();
    }
    if (name !== "calib" && window.operatorsCalibrationOnTabHidden) {
      window.operatorsCalibrationOnTabHidden();
    }
    if (name === "movement") {
      if (window.operatorsMovementOnTabShown) {
        window.operatorsMovementOnTabShown();
      }
      if (window.operatorsArmJointsOnTabShown) {
        window.operatorsArmJointsOnTabShown();
      }
    } else {
      if (window.operatorsMovementOnTabHidden) {
        window.operatorsMovementOnTabHidden();
      }
      if (window.operatorsArmJointsOnTabHidden) {
        window.operatorsArmJointsOnTabHidden();
      }
    }
  };

  window.operatorsBaseMotion = function (mode) {
    var u =
      api("/api/base/accompany_mode") +
      "?mode=" +
      encodeURIComponent(mode) +
      "&enable=1&sync=1&_=" +
      Date.now();
    setPill("basePill", mode + "…", "warn");
    fetch(u, { method: "GET", cache: "no-store" })
      .then(function (r) {
        return r.json();
      })
      .then(function (j) {
        setPill("basePill", (j && j.ok ? "OK " : "ERR ") + mode, j && j.ok ? "ok" : "err");
      })
      .catch(function (e) {
        setPill("basePill", "rete: " + String(e).slice(0, 40), "err");
      });
  };

  window.operatorsPollSportLast = function () {
    fetch(api("/api/base/sport_last?_=" + Date.now()), { cache: "no-store" })
      .then(function (r) {
        return r.json();
      })
      .then(function (j) {
        var el = document.getElementById("sportLast");
        if (el) {
          el.textContent = JSON.stringify(j).slice(0, 400);
        }
      })
      .catch(function () {});
  };

})();
