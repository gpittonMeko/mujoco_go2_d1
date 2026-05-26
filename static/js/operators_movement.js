/**
 * Controlli movimento dashboard operator — base Sport (DDS) + comando velocità + braccio D1.
 * Richiede window.operatorsApi da operators.js (stesso ordine di caricamento / defer).
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

  var pollTimer = null;
  var holdTimer = null;
  var holdVec = null;
  var holdFirst = true;
  var _mvSpeedSlidersBound = false;

  function el(id) {
    return document.getElementById(id);
  }

  function issueErrMov(source, title, detail) {
    if (window.operatorsAppendIssueLog) {
      window.operatorsAppendIssueLog("err", source, title, detail);
    }
  }

  function setPill(text, kind) {
    var p = el("movementStatusPill");
    if (!p) {
      return;
    }
    p.textContent = text;
    p.className = "pill movement-pill " + (kind || "");
  }

  function logPre(objOrText) {
    var pre = el("movementLogPre");
    if (!pre) {
      return;
    }
    if (typeof objOrText === "string") {
      pre.textContent = objOrText;
    } else {
      try {
        pre.textContent = JSON.stringify(objOrText, null, 2);
      } catch (e) {
        pre.textContent = String(objOrText);
      }
    }
  }

  function readSpeed() {
    var s = el("movementSpeedSlider");
    var v = s ? parseFloat(s.value) : 0.35;
    if (!(v >= 0.05)) {
      v = 0.35;
    }
    return v;
  }

  function readYawSpeed() {
    var s = el("movementYawSlider");
    var v = s ? parseFloat(s.value) : 0.45;
    if (!(v >= 0.05)) {
      v = 0.45;
    }
    return v;
  }

  function movementPollSportLast() {
    fetch(api("/api/base/sport_last?_=" + Date.now()), {
      cache: "no-store",
      credentials: "same-origin",
    })
      .then(function (r) {
        return r.json();
      })
      .then(function (j) {
        var out = el("movementSportLastPre");
        if (out) {
          out.textContent = JSON.stringify(j, null, 2).slice(0, 1200);
        }
      })
      .catch(function () {});
  }

  function movementFetchConnectivity() {
    setPill("test DDS…", "warn");
    fetch(api("/api/base/sport_connectivity?_=" + Date.now()), {
      cache: "no-store",
      credentials: "same-origin",
    })
      .then(function (r) {
        return r.json();
      })
      .then(function (j) {
        logPre(j);
        setPill(j && j.ok ? "DDS: OK" : "DDS: problema", j && j.ok ? "ok" : "err");
        if (j && j.ok === false) {
          issueErrMov("Base · DDS", j.reason || "sport_connectivity fallito", j);
        }
      })
      .catch(function (e) {
        logPre(String(e));
        setPill("DDS: rete", "err");
        issueErrMov("Base · DDS", String(e), null);
      });
  }

  function movementPostAccompany(payload) {
    var body = Object.assign({ sync: true }, payload);
    setPill((body.mode || "sport") + "…", "warn");
    fetch(api("/api/base/accompany_mode"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      credentials: "same-origin",
      cache: "no-store",
    })
      .then(function (r) {
        return r.text().then(function (t) {
          return { status: r.status, text: t };
        });
      })
      .then(function (pack) {
        var data = {};
        try {
          data = pack.text ? JSON.parse(pack.text) : {};
        } catch (eJ) {
          data = { ok: false, raw: String(pack.text).slice(0, 400) };
        }
        data._http_status = pack.status;
        logPre(data);
        var okish =
          (pack.status >= 200 && pack.status < 300 && data.ok !== false) ||
          (pack.status === 202 && data.async);
        setPill(
          (body.mode || "sport") + " · HTTP " + pack.status + (data.ok === false ? " ERR" : ""),
          okish ? "ok" : "err"
        );
        if (!okish) {
          issueErrMov("Base · Sport", (body.mode || "sport") + " fallito", data);
        }
        movementPollSportLast();
      })
      .catch(function (e) {
        logPre(String(e));
        setPill("rete / fetch", "err");
        issueErrMov("Base · Sport", String(e), null);
      });
  }

  /** Stand / crouch: GET + sync=1 (come dashboard monolite). */
  function movementBaseStand(mode) {
    var u =
      api("/api/base/accompany_mode") +
      "?mode=" +
      encodeURIComponent(mode) +
      "&enable=1&sync=1&_=" +
      Date.now();
    setPill(mode + "…", "warn");
    fetch(u, {
      method: "GET",
      credentials: "same-origin",
      cache: "no-store",
    })
      .then(function (r) {
        return r.text().then(function (t) {
          return { status: r.status, text: t };
        });
      })
      .then(function (pack) {
        var data = {};
        try {
          data = pack.text ? JSON.parse(pack.text) : {};
        } catch (eJ) {
          data = { ok: false, raw: String(pack.text).slice(0, 400) };
        }
        data._http_status = pack.status;
        logPre(data);
        var okish = pack.status >= 200 && pack.status < 300 && data.ok !== false;
        setPill(mode + " · HTTP " + pack.status, okish ? "ok" : "err");
        if (!okish) {
          issueErrMov("Base · " + mode, "stand/crouch fallito", data);
        }
        movementPollSportLast();
      })
      .catch(function (e) {
        logPre(String(e));
        setPill("rete", "err");
        issueErrMov("Base · stand/crouch", String(e), null);
      });
  }

  function movementVelocityOnce(vx, vy, vyaw, preBalance) {
    movementPostAccompany({
      mode: "velocity",
      vx: vx,
      vy: vy,
      vyaw: vyaw,
      pre_balance: !!preBalance,
      sync: true,
    });
  }

  function movementClearHold() {
    if (holdTimer) {
      clearInterval(holdTimer);
      holdTimer = null;
    }
    holdVec = null;
    holdFirst = true;
  }

  function movementStartHold(vx, vy, vyaw) {
    movementClearHold();
    holdVec = { vx: vx, vy: vy, vyaw: vyaw };
    holdFirst = true;
    function tick() {
      if (!holdVec) {
        return;
      }
      movementVelocityOnce(holdVec.vx, holdVec.vy, holdVec.vyaw, holdFirst);
      holdFirst = false;
    }
    tick();
    holdTimer = setInterval(tick, 220);
  }

  function movementStopHoldAndBrake() {
    movementClearHold();
    movementPostAccompany({ mode: "stop", sync: true });
  }

  function movementServoSnapshot() {
    fetch(api("/api/arm/servo_snapshot?_=" + Date.now()), {
      cache: "no-store",
      credentials: "same-origin",
    })
      .then(function (r) {
        return r.json();
      })
      .then(function (j) {
        var pre = el("movementArmServoPre");
        if (pre) {
          pre.textContent = JSON.stringify(j, null, 2);
        }
        setPill(j && j.ok ? "servo letti" : "servo ?", j && j.ok ? "ok" : "warn");
        if (j && j.ok === false) {
          issueErrMov("Braccio · snapshot (Moto)", j.reason || "no feedback", j);
        }
      })
      .catch(function (e) {
        var pre = el("movementArmServoPre");
        if (pre) {
          pre.textContent = String(e);
        }
        issueErrMov("Braccio · snapshot (Moto)", String(e), null);
      });
  }

  function movementArmPost(path, okMsg) {
    fetch(api(path), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
      credentials: "same-origin",
      cache: "no-store",
    })
      .then(function (r) {
        return r.json();
      })
      .then(function (j) {
        logPre(j);
        setPill(j && j.ok ? okMsg || "OK" : "braccio ERR", j && j.ok ? "ok" : "err");
        if (j && j.ok === false) {
          issueErrMov("Braccio · " + (okMsg || path), j.reason || "POST fallito", j);
        }
      })
      .catch(function (e) {
        logPre(String(e));
        setPill("rete", "err");
        issueErrMov("Braccio · HTTP", String(e), null);
      });
  }

  window.operatorsMovementOnTabShown = function () {
    movementPollSportLast();
    if (pollTimer) {
      clearInterval(pollTimer);
    }
    pollTimer = setInterval(movementPollSportLast, 4000);
    var spd = el("movementSpeedSlider");
    var ysp = el("movementYawSlider");
    var vs = el("movementSpeedVal");
    var vy = el("movementYawVal");
    function bindSlider(inp, lbl) {
      if (!inp || !lbl) {
        return;
      }
      var upd = function () {
        lbl.textContent = inp.value;
      };
      inp.addEventListener("input", upd);
      upd();
    }
    if (!_mvSpeedSlidersBound) {
      _mvSpeedSlidersBound = true;
      bindSlider(spd, vs);
      bindSlider(ysp, vy);
    } else if (vs && spd) {
      vs.textContent = spd.value;
    }
    if (vy && ysp) {
      vy.textContent = ysp.value;
    }
  };

  window.operatorsMovementOnTabHidden = function () {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    movementClearHold();
  };

  window.operatorsMovementFetchConnectivity = movementFetchConnectivity;
  window.operatorsMovementPollSportLast = movementPollSportLast;
  window.operatorsMovementBaseStand = movementBaseStand;
  window.operatorsMovementPostAccompany = movementPostAccompany;
  window.operatorsMovementVelocityOnce = movementVelocityOnce;
  window.operatorsMovementStartHold = movementStartHold;
  window.operatorsMovementStopHoldAndBrake = movementStopHoldAndBrake;
  window.operatorsMovementServoSnapshot = movementServoSnapshot;
  window.operatorsMovementArmHold = function () {
    movementArmPost("/api/arm/hold_pose", "hold DDS");
  };

  window.operatorsMovementDpadDown = function (dir) {
    var sp = readSpeed();
    var yw = readYawSpeed();
    var vx = 0,
      vy = 0,
      vyaw = 0;
    if (dir === "fwd") {
      vx = sp;
    }
    if (dir === "back") {
      vx = -sp;
    }
    if (dir === "left") {
      vy = sp;
    }
    if (dir === "right") {
      vy = -sp;
    }
    if (dir === "rotL") {
      vyaw = yw;
    }
    if (dir === "rotR") {
      vyaw = -yw;
    }
    movementStartHold(vx, vy, vyaw);
  };

  window.operatorsMovementDpadUp = function () {
    movementStopHoldAndBrake();
  };
})();
