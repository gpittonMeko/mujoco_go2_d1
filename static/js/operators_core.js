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

  function setPillEl(el, text, cls) {
    if (!el) {
      return;
    }
    el.textContent = text;
    el.className = "pill " + (cls || "");
  }

  function setPill(id, text, cls) {
    setPillEl(document.getElementById(id), text, cls);
  }

  function operatorsAbsoluteApi(path) {
    var rel = api(path || "/");
    try {
      if (/^https?:\/\//i.test(rel)) {
        return rel;
      }
      var o = window.location.origin;
      if (!o || o === "null" || o === "undefined") {
        return rel;
      }
      if (rel.charAt(0) !== "/") {
        rel = "/" + rel.replace(/^\/+/, "");
      }
      return new URL(rel, o + "/").href;
    } catch (eAbs) {
      return rel;
    }
  }

  function operatorsSleep(ms) {
    return new Promise(function (resolve) {
      setTimeout(resolve, ms);
    });
  }

  /**
   * Stand / crouch Sport — GET + sync=1, timeout lungo, poll sport_last su HTTP 202/async.
   */
  window.operatorsBaseStandMotion = function (mode, opts) {
    opts = opts || {};
    var m = String(mode || "stand_up").toLowerCase();
    var rpcMs = opts.timeoutMs || 95000;
    var ac = new AbortController();
    var abortTid = setTimeout(function () {
      try {
        ac.abort();
      } catch (eAb) {}
    }, rpcMs);
    var sportUpdatedBefore = null;
    var reqUrl =
      operatorsAbsoluteApi("/api/base/accompany_mode") +
      "?mode=" +
      encodeURIComponent(m) +
      "&enable=1&sync=1&_=" +
      Date.now();

    function pill(text, kind) {
      if (opts.onPill) {
        opts.onPill(text, kind);
      }
    }
    function log(data) {
      if (opts.onLog) {
        opts.onLog(data);
      }
    }
    function issue(level, title, detail) {
      if (window.operatorsAppendIssueLog) {
        window.operatorsAppendIssueLog(level || "err", opts.issueSource || "Base · Sport", title, detail);
      }
    }

    pill(m + "…", "warn");
    if (window.location.protocol === "file:") {
      clearTimeout(abortTid);
      var fileErr =
        "Pagina aperta da file:// — apri http://<IP-NX>:5052/ dalla rete Unitree.";
      pill("ERR file://", "err");
      log(fileErr);
      issue("err", "URL pagina errato", fileErr);
      return Promise.reject(new Error(fileErr));
    }

    return fetch(api("/api/base/sport_last?_=" + Date.now()), {
      cache: "no-store",
      credentials: "same-origin",
    })
      .then(function (rPre) {
        return rPre.ok ? rPre.json() : {};
      })
      .then(function (jPre) {
        sportUpdatedBefore = jPre.updated_at || null;
        return fetch(reqUrl, {
          method: "GET",
          signal: ac.signal,
          cache: "no-store",
          credentials: "same-origin",
        });
      })
      .then(function (res) {
        return res.text().then(function (rawText) {
          return { res: res, rawText: rawText };
        });
      })
      .then(function (pack) {
        var res = pack.res;
        var data = {};
        try {
          data = pack.rawText ? JSON.parse(pack.rawText) : {};
        } catch (eJson) {
          data = { ok: false, reason: "risposta_non_json", raw_preview: String(pack.rawText).slice(0, 200) };
        }
        data._http_status = res.status;
        data._request_url = reqUrl;
        log(data);

        var okish = res.status >= 200 && res.status < 300 && data.ok !== false;

        if (okish && (res.status === 202 || data.async)) {
          pill(m + " · HTTP 202 — poll sport_last…", "warn");
          var loops = 0;
          function pollOnce(snap) {
            var upCh =
              sportUpdatedBefore == null ||
              (snap.updated_at && snap.updated_at !== sportUpdatedBefore);
            if (upCh && snap.mode === m && (snap.result != null || snap.error != null)) {
              log({ async_poll: snap, initial_http: data });
              var rok = !!(snap.result && snap.result.ok);
              pill(m + " · " + (rok ? "OK RPC" : "Sport fallito") + " (poll)", rok ? "ok" : "err");
              if (!rok) {
                issue("err", m + " fallito (background)", snap);
              }
              return snap;
            }
            loops += 1;
            if (loops >= 100) {
              pill(m + " · timeout poll sport_last", "err");
              issue("err", "Timeout poll sport_last", { mode: m, last: snap });
              return snap;
            }
            return operatorsSleep(400)
              .then(function () {
                return fetch(api("/api/base/sport_last?_=" + Date.now()), {
                  cache: "no-store",
                  credentials: "same-origin",
                });
              })
              .then(function (r2) {
                return r2.ok ? r2.json() : {};
              })
              .then(pollOnce);
          }
          return operatorsSleep(250)
            .then(function () {
              return fetch(api("/api/base/sport_last?_=" + Date.now()), {
                cache: "no-store",
                credentials: "same-origin",
              });
            })
            .then(function (rSp) {
              return rSp.ok ? rSp.json() : {};
            })
            .then(pollOnce)
            .then(function () {
              if (window.operatorsPollSportLast) {
                window.operatorsPollSportLast();
              }
              if (window.operatorsMovementPollSportLast) {
                window.operatorsMovementPollSportLast();
              }
              return data;
            });
        }

        if (res.status === 403 && data.reason) {
          pill("blocco: " + String(data.reason).slice(0, 120), "err");
          issue("err", "Sport bloccato (403)", data);
        } else if (!okish) {
          pill(m + " · HTTP " + res.status + " ERR", "err");
          issue("err", m + " fallito", data);
        } else {
          var syncOk = data.ok !== false;
          pill(m + " · HTTP " + res.status + (syncOk ? " OK" : " ERR"), syncOk ? "ok" : "err");
          if (!syncOk) {
            issue("err", m + " fallito", data);
          }
        }

        if (window.operatorsPollSportLast) {
          window.operatorsPollSportLast();
        }
        if (window.operatorsMovementPollSportLast) {
          window.operatorsMovementPollSportLast();
        }
        return data;
      })
      .catch(function (e) {
        var msg = String(e);
        var isAbort = e && e.name === "AbortError";
        var isNet =
          typeof TypeError !== "undefined" &&
          e instanceof TypeError &&
          (msg.indexOf("Failed to fetch") >= 0 || msg.indexOf("NetworkError") >= 0);
        if (isAbort) {
          msg =
            "Timeout dopo ~" +
            Math.floor(rpcMs / 1000) +
            "s — Sport/DDS lento o irraggiungibile.";
        } else if (isNet) {
          msg = "Failed to fetch verso " + reqUrl;
        }
        pill(m + " · rete", "err");
        log(msg);
        issue("err", isAbort ? "Timeout Sport" : "Rete Sport", msg);
        throw e;
      })
      .finally(function () {
        clearTimeout(abortTid);
      });
  };

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
    if (name === "grasp") {
      if (window.operatorsWireGraspDockStreams) {
        window.operatorsWireGraspDockStreams();
      }
      if (window.operatorsGraspDockBumpPreviews) {
        window.operatorsGraspDockBumpPreviews();
      }
      if (window.operatorsGraspRefreshStartPoseBadge) {
        window.operatorsGraspRefreshStartPoseBadge();
      }
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
    window.operatorsBaseStandMotion(mode, {
      issueSource: "Robot · " + mode,
      onPill: function (text, kind) {
        setPill("basePill", text, kind);
      },
      onLog: function (data) {
        var el = document.getElementById("sportLast");
        if (el) {
          try {
            el.textContent = JSON.stringify(data, null, 2).slice(0, 500);
          } catch (eJ) {
            el.textContent = String(data);
          }
        }
      },
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
