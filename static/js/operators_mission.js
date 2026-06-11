(function () {
  "use strict";
  var api = window.operatorsApi;
  if (typeof api !== "function") {
    throw new Error('operators_core.js must load before this module');
  }

  window.operatorsMissionBoxPickCycle = function () {
    var pre = document.getElementById("missionBoxPickPre");
    var sr = (window.__OPERATORS_SCRIPT_ROOT__ || "").replace(/\/+$/, "");
    var origin = window.location.origin || "";
    var pubInp = document.getElementById("missionBoxPickPublicBase");
    var pub = (pubInp && pubInp.value && String(pubInp.value).trim()) || origin + sr;
    var camEl = document.getElementById("missionBoxPickCam");
    var cam = camEl ? parseInt(camEl.value, 10) : 6;
    if (cam !== 0 && cam !== 6) {
      cam = 6;
    }
    var instEl = document.getElementById("missionBoxPickInstruction");
    var inst = (instEl && instEl.value && String(instEl.value).trim()) || "pick up the white box";
    var execEl = document.getElementById("missionBoxPickExecMode");
    var execMode = execEl ? String(execEl.value || "openvla_then_ik") : "openvla_then_ik";
    var reqDet = document.getElementById("missionBoxPickRequireDet");
    var requireBox = !!(reqDet && reqDet.checked);
    if (pre) {
      pre.textContent = "Ciclo in corso… (stand, detect, crouch, START braccio, plan OpenVLA, movimento — 1–3 min)";
    }
    window.operatorsGraspProgressUi({
      step: 0,
      label: "Ciclo laboratorio in corso (può durare 1–3 min)…",
      indeterminate: true,
    });
    fetch(api("/api/mission/box_pick_cycle"), {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({
        confirm: "LAB_BOX_PICK_CYCLE",
        dashboard_public_base: pub,
        logical_camera: cam,
        instruction: inst,
        execute_mode: execMode,
        require_box_detect: requireBox,
        plan_timeout_s: 150,
        pause_after_stand_s: 2.5,
        pause_after_detect_s: 1.0,
        pause_after_crouch_s: 2.0,
        pause_after_start_s: 1.5,
        goto_start_delay_ms: 120,
      }),
    })
      .then(function (r) {
        return r.text().then(function (t) {
          return { status: r.status, t: t };
        });
      })
      .then(function (o) {
        var txt = o.t;
        try {
          txt = JSON.stringify(JSON.parse(o.t), null, 2);
        } catch (e) {}
        if (pre) {
          pre.textContent = "HTTP " + o.status + "\n\n" + txt;
        }
        setGraspPhase(
          o.status >= 400 ? "Ciclo lab: errore HTTP " + o.status : "Ciclo lab: controlla missionBoxPickPre (steps)."
        );
        if (o.status >= 400) {
          window.operatorsGraspProgressUi({
            step: 2,
            label: "Ciclo laboratorio: errore HTTP " + o.status,
            error: true,
          });
        } else {
          window.operatorsGraspProgressUi({
            step: 4,
            label: "Ciclo laboratorio terminato — leggi JSON sotto",
            indeterminate: false,
          });
        }
      })
      .catch(function (e) {
        if (pre) {
          pre.textContent = String(e);
        }
        window.operatorsGraspProgressUi({ step: 1, label: String(e), error: true });
      });
  };

  window.operatorsMissionConsoleRefresh = function () {
    var pre = document.getElementById("missionConsolePre");
    var hint = document.getElementById("missionRestartHint");
    var form = document.getElementById("missionRestartForm");
    var cmd = document.getElementById("missionRestartCmd");
    if (pre) {
      pre.textContent = "Carico…";
    }
    fetch(api("/api/mission/console?_=" + Date.now()), { cache: "no-store" })
      .then(function (r) {
        return r.json();
      })
      .then(function (j) {
        if (pre) {
          pre.textContent = JSON.stringify(j, null, 2);
        }
        var rs = (j && j.restart) || {};
        var en = rs.dashboard_restart_api_enabled;
        if (hint) {
          hint.textContent = en
            ? "API riavvio abilitata: token = GO2_MISSION_ADMIN_TOKEN sulla NX."
            : rs.dashboard_restart_api_hint_it ||
              "Riavvio via API non configurato (manca GO2_MISSION_ADMIN_TOKEN sulla NX).";
        }
        if (form) {
          form.style.display = en ? "block" : "none";
        }
        if (cmd) {
          cmd.textContent = [
            rs.soft_kill_example_it || "",
            "",
            rs.ssh_hint_it || "",
          ].join("\n");
        }
      })
      .catch(function (e) {
        if (pre) {
          pre.textContent = String(e);
        }
      });
  };

  window.operatorsMissionDashboardRestart = function () {
    var inp = document.getElementById("missionAdminToken");
    var tok = (inp && inp.value) || "";
    fetch(api("/api/mission/dashboard_restart"), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
        "X-Mission-Token": tok,
      },
      body: JSON.stringify({ token: tok }),
    })
      .then(function (r) {
        return r.text().then(function (t) {
          var j;
          try {
            j = JSON.parse(t);
          } catch (e) {
            j = { _parse_error: String(e), _raw: t.slice(0, 600) };
          }
          return { code: r.status, j: j };
        });
      })
      .then(function (x) {
        window.alert("HTTP " + x.code + " — " + JSON.stringify(x.j).slice(0, 800));
        if (window.operatorsMissionConsoleRefresh) {
          window.operatorsMissionConsoleRefresh();
        }
      })
      .catch(function (e) {
        window.alert(String(e));
      });
  };

  window.operatorsRefreshStack = function () {
    var pre = document.getElementById("stackStatus");
    if (!pre) {
      return;
    }
    pre.textContent = "Carico…";
    fetch(api("/api/nx/stack/status?_=" + Date.now()), { cache: "no-store" })
      .then(function (r) {
        return r.json();
      })
      .then(function (j) {
        pre.textContent = JSON.stringify(j, null, 2);
      })
      .catch(function (e) {
        pre.textContent = String(e);
      });
  };

  window.operatorsGraspDockBumpPreviews = function () {
    var sr = window.__OPERATORS_SCRIPT_ROOT__ || "";
    var g0 = document.getElementById("graspDockCam0");
    var g6 = document.getElementById("graspDockCam6");
    var q = "?_=" + Date.now();
    operatorsMjpegLoadingSet("graspDockCam0", true);
    operatorsMjpegLoadingSet("graspDockCam6", true);
    if (g0) {
      g0.src = sr + "/stream/robot/camera/0.mjpg" + q;
    }
    if (g6) {
      g6.src = sr + "/stream/robot/camera/6.mjpg" + q;
    }
  };

  window.operatorsSessionMemoryRefresh = function () {
    var pre = document.getElementById("opSessionMemPre");
    var t0 = typeof performance !== "undefined" ? performance.now() : 0;
    fetch(api("/api/operator_session/memory?lines=48"), { cache: "no-store" })
      .then(function (r) {
        return r.json().then(function (j) {
          return { j: j, r: r };
        });
      })
      .then(function (pack) {
        var foot =
          typeof window.operatorsHttpTimingFooterLines === "function"
            ? window.operatorsHttpTimingFooterLines(pack.r, t0)
            : "";
        if (pre) {
          pre.textContent = JSON.stringify(pack.j, null, 2) + foot;
        }
      })
      .catch(function (e) {
        if (pre) {
          pre.textContent = String(e);
        }
      });
  };

  window.operatorsSessionMemorySave = function () {
    var pre = document.getElementById("opSessionMemPre");
    var ti = document.getElementById("opSessionMemTitle");
    var no = document.getElementById("opSessionMemNote");
    var tg = document.getElementById("opSessionMemTags");
    var title = ti && ti.value ? String(ti.value).trim() : "";
    var note = no && no.value ? String(no.value).trim() : "";
    var tags = [];
    if (tg && tg.value) {
      String(tg.value)
        .split(",")
        .forEach(function (p) {
          var s = String(p).trim();
          if (s) {
            tags.push(s);
          }
        });
    }
    if (!note && !title) {
      if (pre) {
        pre.textContent = JSON.stringify(
          { ok: false, reason: "missing_note_or_title" },
          null,
          2
        );
      }
      return;
    }
    var body = { title: title || undefined, note: note || "", tags: tags.length ? tags : undefined };
    var t0 = typeof performance !== "undefined" ? performance.now() : 0;
    fetch(api("/api/operator_session/memory"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(function (r) {
        return r.json().then(function (j) {
          return { j: j, r: r };
        });
      })
      .then(function (pack) {
        var foot =
          typeof window.operatorsHttpTimingFooterLines === "function"
            ? window.operatorsHttpTimingFooterLines(pack.r, t0)
            : "";
        var j = pack.j;
        if (pre) {
          pre.textContent = JSON.stringify(j, null, 2) + foot;
        }
        if (j && j.ok && no) {
          no.value = "";
        }
        if (j && j.ok && ti) {
          ti.value = "";
        }
        if (j && j.ok && tg) {
          tg.value = "";
        }
      })
      .catch(function (e) {
        if (pre) {
          pre.textContent = String(e);
        }
      });
  };

})();
