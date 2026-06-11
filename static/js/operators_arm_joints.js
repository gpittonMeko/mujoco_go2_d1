/**
 * Editor giunti D1 (7× slider): session_begin + live_deg (SDK stream) / goto_deg / salvataggi.
 * Dipende da window.operatorsApi (operators.js).
 */
(function () {
  "use strict";

  var api =
    window.operatorsApi ||
    function (p) {
      var SR = window.__OPERATORS_SCRIPT_ROOT__ || "";
      if (!p || p.charAt(0) !== "/") {
        p = "/" + (p || "");
      }
      return SR + p;
    };

  var JOINT_SLIDER_BOUNDS = [
    [-135, 135],
    [-90, 90],
    [-90, 90],
    [-135, 135],
    [-90, 90],
    [-135, 135],
    [0, 90],
  ];

  var JOINT_LABELS = ["J0 · base", "J1", "J2", "J3", "J4", "J5", "Grip"];

  var _jointLiveRaf = 0;

  function jsonFromResponse(r, noteEl) {
    var ct = (r.headers && r.headers.get && r.headers.get("content-type")) || "";
    return r.text().then(function (text) {
      if (!r.ok && ct.indexOf("application/json") === -1 && text.charAt(0) === "<") {
        var msg =
          "HTTP " + r.status + ": risposta HTML (prob. 404 o errore server), atteso JSON. " + (noteEl || "");
        throw new Error(msg);
      }
      try {
        return JSON.parse(text);
      } catch (e) {
        throw new Error((noteEl || "JSON") + ": " + String(e) + " — anteprima: " + text.slice(0, 120));
      }
    });
  }

  function el(id) {
    return document.getElementById(id);
  }

  function issueErr(source, title, detail) {
    if (window.operatorsAppendIssueLog) {
      window.operatorsAppendIssueLog("err", source, title, detail);
    }
  }

  function issueWarn(source, title, detail) {
    if (window.operatorsAppendIssueLog) {
      window.operatorsAppendIssueLog("warn", source, title, detail);
    }
  }

  function operatorsJointSliderDisplay(i) {
    var s = el("opJointSlide" + i);
    var v = el("opJointSlideV" + i);
    if (s && v) {
      v.textContent = parseFloat(s.value).toFixed(1) + "°";
    }
  }

  function operatorsJointSlidersInitDisplay() {
    var j;
    for (j = 0; j < 7; j++) {
      operatorsJointSliderDisplay(j);
    }
    var st = el("opJointLiveStatus");
    var cb = el("opJointLiveEnabled");
    if (st) {
      st.textContent =
        cb && cb.checked ? "Live: i cursori comandano il braccio." : "Live off — muovi solo gli slider.";
    }
  }

  function operatorsJointCollectFromSliders() {
    var out = [];
    var i;
    for (i = 0; i < 7; i++) {
      var s = el("opJointSlide" + i);
      if (!s) {
        return null;
      }
      out.push(parseFloat(s.value));
    }
    return out;
  }

  function operatorsJointSendLivePose() {
    var sd = operatorsJointCollectFromSliders();
    if (!sd) {
      return;
    }
    var st = el("opJointLiveStatus");
    fetch(api("/api/arm/joints/live_deg"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ servo_deg: sd }),
      credentials: "same-origin",
      cache: "no-store",
    })
      .then(function (r) {
        return jsonFromResponse(r, "live_deg");
      })
      .then(function (data) {
        if (st) {
          if (data.ok || data.skipped) {
            st.textContent = "live OK · " + new Date().toLocaleTimeString();
            if (data.skipped) {
              issueWarn("Braccio · live", "Comando non inviato (dry-run / REAL_ARM off)", data.reason || data);
            }
          } else {
            st.textContent = "live: " + (data.reason || "ERR");
            issueErr("Braccio · live", data.reason || "live_deg fallito", data);
          }
        }
      })
      .catch(function (e) {
        if (st) {
          st.textContent = String(e);
        }
        issueErr("Braccio · live", String(e), null);
      });
  }

  function operatorsJointSliderLiveInput(i) {
    operatorsJointSliderDisplay(i);
    var cb = el("opJointLiveEnabled");
    var st = el("opJointLiveStatus");
    if (!cb || !cb.checked) {
      if (st) {
        st.textContent = "Live off.";
      }
      return;
    }
    if (_jointLiveRaf) {
      return;
    }
    _jointLiveRaf = requestAnimationFrame(function () {
      _jointLiveRaf = 0;
      operatorsJointSendLivePose();
    });
  }

  function operatorsJointSliderLiveFlush() {
    var i;
    for (i = 0; i < 7; i++) {
      operatorsJointSliderDisplay(i);
    }
    var cb = el("opJointLiveEnabled");
    if (!cb || !cb.checked) {
      return;
    }
    if (_jointLiveRaf) {
      cancelAnimationFrame(_jointLiveRaf);
      _jointLiveRaf = 0;
    }
    operatorsJointSendLivePose();
  }

  function operatorsJointNudge(i, delta) {
    var s = el("opJointSlide" + i);
    if (!s) {
      return;
    }
    var b = JOINT_SLIDER_BOUNDS[i];
    var v = parseFloat(s.value) + delta;
    v = Math.min(b[1], Math.max(b[0], v));
    s.value = String(v);
    operatorsJointSliderDisplay(i);
    var cb = el("opJointLiveEnabled");
    if (cb && cb.checked) {
      if (_jointLiveRaf) {
        cancelAnimationFrame(_jointLiveRaf);
        _jointLiveRaf = 0;
      }
      operatorsJointSendLivePose();
    }

  }

  function operatorsJointEditorLoad(done) {
    var pre = el("opJointEditorLog");
    fetch(api("/api/arm/servo_snapshot?_=" + Date.now()), {
      cache: "no-store",
      credentials: "same-origin",
    })
      .then(function (r) {
        return jsonFromResponse(r, "servo_snapshot");
      })
      .then(function (data) {
        if (!data.ok || !data.servo_deg) {
          if (pre) {
            pre.textContent = JSON.stringify(data, null, 2);
          }
          issueErr("Braccio · snapshot", data.reason || "servo_snapshot non ok", data);
          if (typeof done === "function") {
            done(data);
          }
          return;
        }
        var sd = data.servo_deg;
        var j;
        for (j = 0; j < 7; j++) {
          var slide = el("opJointSlide" + j);
          if (!slide) {
            continue;
          }
          var b = JOINT_SLIDER_BOUNDS[j];
          var val = parseFloat(sd[j]);
          if (isNaN(val)) {
            val = 0;
          }
          val = Math.min(b[1], Math.max(b[0], val));
          slide.value = String(val);
          operatorsJointSliderDisplay(j);
        }
        if (pre) {
          pre.textContent = "Robot → slider (DDS)\n" + JSON.stringify(sd, null, 2);
        }
        if (typeof done === "function") {
          done(data);
        }
      })
      .catch(function (e) {
        if (pre) {
          pre.textContent = String(e);
        }
        issueErr("Braccio · snapshot", String(e), null);
        if (typeof done === "function") {
          done({ ok: false, reason: String(e) });
        }
      });
  }

  function operatorsJointEditorGoto() {
    if (!window.confirm("Spostare tutti i giunti in modo smooth (interpolato)?")) {
      return;
    }
    var sd = operatorsJointCollectFromSliders();
    if (!sd) {
      return;
    }
    var pre = el("opJointEditorLog");
    fetch(api("/api/arm/joints/goto_deg"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ servo_deg: sd }),
      credentials: "same-origin",
      cache: "no-store",
    })
      .then(function (r) {
        return jsonFromResponse(r, "goto_deg").then(function (data) {
          return { status: r.status, data: data };
        });
      })
      .then(function (pack) {
        if (pre) {
          pre.textContent =
            "goto_deg HTTP " + pack.status + "\n" + JSON.stringify(pack.data, null, 2);
        }
        var d = pack.data;
        if (pack.status >= 400) {
          issueErr("Braccio · smooth (goto)", "HTTP " + pack.status, d);
        } else if (d && d.skipped) {
          issueWarn("Braccio · smooth (goto)", "Nessun movimento (dry-run / gate)", d.reason || d);
        } else if (d && d.ok === false) {
          issueErr("Braccio · smooth (goto)", d.reason || "goto_deg fallito", d);
        }
      })
      .catch(function (e) {
        if (pre) {
          pre.textContent = String(e);
        }
        issueErr("Braccio · smooth (goto)", String(e), null);
      });
  }

  function operatorsJointEditorSaveZero() {
    var sd = operatorsJointCollectFromSliders();
    if (!sd) {
      return;
    }
    var pre = el("opJointEditorLog");
    fetch(api("/api/arm/true_zero"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ op: "save", servo_deg: sd }),
      credentials: "same-origin",
      cache: "no-store",
    })
      .then(function (r) {
        return jsonFromResponse(r, "true_zero save");
      })
      .then(function (data) {
        if (pre) {
          pre.textContent = JSON.stringify(data, null, 2);
        }
        if (!data.ok) {
          issueErr("Braccio · salva ZERO", data.reason || "salvataggio fallito", data);
        }
      })
      .catch(function (e) {
        if (pre) {
          pre.textContent = String(e);
        }
        issueErr("Braccio · salva ZERO", String(e), null);
      });
  }

  function _startVariantBody(extra) {
    var body = extra || {};
    if (typeof window.operatorsStartVariantPayload === "function") {
      Object.keys(window.operatorsStartVariantPayload()).forEach(function (k) {
        body[k] = window.operatorsStartVariantPayload()[k];
      });
    }
    return body;
  }

  function operatorsJointEditorSaveStart() {
    var variant =
      typeof window.operatorsStartVariant === "function" ? window.operatorsStartVariant() : "lateral";
    if (!window.confirm("Salvare START " + variant + " (angoli dagli slider)?")) {
      return;
    }
    var sd = operatorsJointCollectFromSliders();
    if (!sd) {
      return;
    }
    var pre = el("opJointEditorLog");
    fetch(api("/api/alignment/start_pose"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(_startVariantBody({ servo_deg: sd })),
      credentials: "same-origin",
      cache: "no-store",
    })
      .then(function (r) {
        return jsonFromResponse(r, "start_pose").then(function (data) {
          return { status: r.status, data: data };
        });
      })
      .then(function (pack) {
        if (pre) {
          pre.textContent =
            "START HTTP " + pack.status + "\n" + JSON.stringify(pack.data, null, 2);
        }
        var d = pack.data;
        if (pack.status >= 400) {
          issueErr("Allineamento · START", "HTTP " + pack.status, d);
        } else if (d && d.ok === false) {
          issueErr(
            "Allineamento · START",
            d.reason || "POST start_pose fallito (su lite può mancare planner AprilTag)",
            d
          );
        }
      })
      .catch(function (e) {
        if (pre) {
          pre.textContent = String(e);
        }
        issueErr("Allineamento · START", String(e), null);
      });
  }

  function operatorsJointEditorSaveStartLive() {
    var variant =
      typeof window.operatorsStartVariant === "function" ? window.operatorsStartVariant() : "lateral";
    if (
      !window.confirm(
        "Leggere feedback motori e salvare come START " +
          variant +
          " (start_alignment_" +
          variant +
          ".json)?"
      )
    ) {
      return;
    }
    var pre = el("opJointEditorLog");
    fetch(api("/api/alignment/start_pose"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(_startVariantBody({})),
      credentials: "same-origin",
      cache: "no-store",
    })
      .then(function (r) {
        return jsonFromResponse(r, "start_pose").then(function (data) {
          return { status: r.status, data: data };
        });
      })
      .then(function (pack) {
        if (pre) {
          pre.textContent =
            "START da motori HTTP " + pack.status + "\n" + JSON.stringify(pack.data, null, 2);
        }
        var d = pack.data;
        if (pack.status >= 400) {
          issueErr("Allineamento · START motori", "HTTP " + pack.status, d);
        } else if (d && d.ok === false) {
          issueErr("Allineamento · START motori", d.reason || "salvataggio fallito", d);
        } else if (typeof window.operatorsGraspRefreshStartPoseBadge === "function") {
          window.operatorsGraspRefreshStartPoseBadge();
        }
      })
      .catch(function (e) {
        if (pre) {
          pre.textContent = String(e);
        }
        issueErr("Allineamento · START motori", String(e), null);
      });
  }

  function operatorsJointEndLiveSession(done) {
    fetch(api("/api/arm/joints/session_end"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
      credentials: "same-origin",
      cache: "no-store",
    })
      .then(function (r) {
        return jsonFromResponse(r, "session_end");
      })
      .then(function (data) {
        if (typeof done === "function") {
          done(data);
        }
      })
      .catch(function (e) {
        if (typeof done === "function") {
          done({ ok: false, reason: String(e) });
        }
      });
  }

  function operatorsJointBeginLiveSession(servoDeg, done) {
    var body = {};
    if (servoDeg && servoDeg.length >= 6) {
      body.servo_deg = servoDeg;
    }
    fetch(api("/api/arm/joints/session_begin"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      credentials: "same-origin",
      cache: "no-store",
    })
      .then(function (r) {
        return jsonFromResponse(r, "session_begin");
      })
      .then(function (data) {
        if (!data.ok && data.reason === "not_coupled") {
          return fetch(api("/api/arm/joints/couple"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ with_power: false }),
            credentials: "same-origin",
            cache: "no-store",
          })
            .then(function (r2) {
              return jsonFromResponse(r2, "couple");
            })
            .then(function () {
              return fetch(api("/api/arm/joints/session_begin"), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
                credentials: "same-origin",
                cache: "no-store",
              });
            })
            .then(function (r3) {
              return jsonFromResponse(r3, "session_begin_retry");
            });
        }
        return data;
      })
      .then(function (data) {
        if (typeof done === "function") {
          done(data);
        }
      })
      .catch(function (e) {
        if (typeof done === "function") {
          done({ ok: false, reason: String(e) });
        }
      });
  }

  function operatorsJointLiveToggleRefresh() {
    var cb = el("opJointLiveEnabled");
    var st = el("opJointLiveStatus");
    operatorsJointSlidersInitDisplay();
    if (!cb || !cb.checked) {
      operatorsJointEndLiveSession(function () {
        if (st) {
          st.textContent = "Live off.";
        }
      });
      return;
    }
    if (st) {
      st.textContent = "Live: avvio sessione DDS…";
    }
    operatorsJointEditorLoad(function (data) {
      if (!cb.checked) {
        return;
      }
      var sd = data && data.ok && data.servo_deg ? data.servo_deg : operatorsJointCollectFromSliders();
      operatorsJointBeginLiveSession(sd, function (sess) {
        if (!cb.checked) {
          return;
        }
        if (st) {
          if (sess && sess.ok) {
            st.textContent = "Live ON (SDK stream) · " + new Date().toLocaleTimeString();
          } else {
            st.textContent = "sessione: " + ((sess && sess.reason) || "ERR");
            issueWarn("Braccio · live session", (sess && sess.reason) || "session_begin fallito", sess);
          }
        }
        if (sess && sess.ok) {
          operatorsJointSendLivePose();
        }
      });
    });
  }

  window.operatorsJointNudge = operatorsJointNudge;
  window.operatorsJointSliderLiveInput = operatorsJointSliderLiveInput;
  window.operatorsJointSliderLiveFlush = operatorsJointSliderLiveFlush;
  window.operatorsJointEditorLoad = operatorsJointEditorLoad;
  window.operatorsJointEditorGoto = operatorsJointEditorGoto;
  window.operatorsJointEditorSaveZero = operatorsJointEditorSaveZero;
  window.operatorsJointEditorSaveStart = operatorsJointEditorSaveStart;
  window.operatorsJointEditorSaveStartLive = operatorsJointEditorSaveStartLive;
  window.operatorsJointLiveToggleRefresh = operatorsJointLiveToggleRefresh;
  window.operatorsJointSlidersInitDisplay = operatorsJointSlidersInitDisplay;

  /** Chiusura tab: annulla RAF pendente */
  window.operatorsArmJointsOnTabHidden = function () {
    if (_jointLiveRaf) {
      cancelAnimationFrame(_jointLiveRaf);
      _jointLiveRaf = 0;
    }
    var cb = el("opJointLiveEnabled");
    if (cb && cb.checked) {
      operatorsJointEndLiveSession();
    }
  };

  window.operatorsArmJointsOnTabShown = function () {
    operatorsJointSlidersInitDisplay();
    operatorsJointEditorLoad();
  };

  window.__operatorsJointLabels = JOINT_LABELS;
  window.__operatorsJointBounds = JOINT_SLIDER_BOUNDS;
})();
