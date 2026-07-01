(function () {
  "use strict";

  var SR = window.__OPERATORS_SCRIPT_ROOT__ || "";

  function api(path) {
    if (!path) {
      path = "/";
    }
    if (path.charAt(0) !== "/") {
      path = "/" + path;
    }
    return SR + path;
  }

  window.operatorsApi = api;

  window.__operatorsLastGraspPlan = null;
  window.__operatorsGraspPointsBaseLink_m = null;

  function fillStreamUrlCodes() {
    var origin = window.location.origin || "";
    var sr = (window.__OPERATORS_SCRIPT_ROOT__ || "").replace(/\/$/, "");
    var base = origin + sr;
    function set(id, rel) {
      var el = document.getElementById(id);
      if (el) {
        el.textContent = base + rel;
      }
    }
    set("urlOrbbecMjpg", "/stream/robot/camera/0.mjpg");
    set("urlOrbbecJpg", "/api/robot/camera/0.jpg");
    set("urlRsMjpg", "/stream/robot/camera/6.mjpg");
    set("urlRsJpg", "/api/robot/camera/6.jpg");
  }

  window.operatorsRefreshCamerasStatus = function () {
    var pre = document.getElementById("cameraStatusPre");
    if (!pre) {
      return;
    }
    pre.textContent = "Carico…";
    fetch(api("/api/cameras/status?_=" + Date.now()), { cache: "no-store" })
      .then(function (r) {
        return r.json();
      })
      .then(function (j) {
        var slim = {
          ok: j.ok,
          go2_local: j.go2_local,
          v4l_index_by_logical: j.v4l_index_by_logical,
          v4l_usb_auto_map: j.v4l_usb_auto_map,
          depth_v4l_index_by_logical: j.depth_v4l_index_by_logical,
          cameras: j.cameras,
        };
        pre.textContent = JSON.stringify(slim, null, 2);
      })
      .catch(function (e) {
        pre.textContent = String(e);
      });
  };

  function setGraspPhase(msg) {
    var el = document.getElementById("graspPhase");
    if (el) {
      el.textContent = msg;
    }
  }

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
    document.querySelectorAll("nav.tab-bar button").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-tab") === name);
    });
    if (name === "scene" && window.operatorsRefreshCamerasStatus) {
      window.operatorsRefreshCamerasStatus();
    }
    if (name === "stato" && window.operatorsRefreshStack) {
      window.operatorsRefreshStack();
    }
    if (name === "3d" && window.operatorsScene3dOnTabShown) {
      window.operatorsScene3dOnTabShown();
    }
  };

  window.operatorsBaseMotion = function (mode) {
    var u =
      api("/api/base/accompany_mode") +
      "?mode=" +
      encodeURIComponent(mode) +
      "&enable=1&_=" +
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

  function graspPlanObject() {
    var ta = document.getElementById("graspPlanJson");
    var raw = (ta && ta.value.trim()) || "{}";
    try {
      return JSON.parse(raw);
    } catch (e) {
      throw new Error("JSON non valido nel corpo piano: " + e.message);
    }
  }

  function graspApplyMarkerFromPlan(plan) {
    window.__operatorsGraspMarkerBaseLink_m = null;
    if (!plan || typeof plan !== "object") {
      return;
    }
    var keys = [
      "grasp_display_base_link_m",
      "approach_point_base_link_m",
      "target_base_link_m",
      "grasp_center_base_link_m",
      "translation",
      "gripper_translation",
      "approach_translation",
    ];
    function pickFrom(obj) {
      if (!obj || typeof obj !== "object") {
        return;
      }
      for (var i = 0; i < keys.length; i++) {
        var v = obj[keys[i]];
        if (Array.isArray(v) && v.length >= 3) {
          window.__operatorsGraspMarkerBaseLink_m = [Number(v[0]), Number(v[1]), Number(v[2])];
          return true;
        }
      }
      return false;
    }
    if (pickFrom(plan)) {
      return;
    }
    if (plan.data && typeof plan.data === "object") {
      pickFrom(plan.data);
    }
  }

  function operatorsCollectOverlayPoints(plan) {
    var pts = [];
    function add(x, y, lab) {
      if (typeof x !== "number" || typeof y !== "number" || !isFinite(x) || !isFinite(y)) {
        return;
      }
      pts.push({ x: x, y: y, label: lab || "pt" });
    }
    if (!plan || typeof plan !== "object") {
      return pts;
    }
    if (Array.isArray(plan.operators_overlay_points)) {
      plan.operators_overlay_points.forEach(function (p, ix) {
        if (!p) {
          return;
        }
        add(Number(p.x), Number(p.y), p.label || "op_" + ix);
      });
    }
    var gp = plan.grip_point;
    if (gp && typeof gp === "object") {
      if (typeof gp.cx === "number" && typeof gp.cy === "number") {
        add(gp.cx, gp.cy, "grip.cx");
      }
      if (typeof gp.u === "number" && typeof gp.v === "number") {
        add(gp.u, gp.v, "grip.uv");
      }
    }
    function walk(o, depth, path) {
      if (depth > 8 || !o || typeof o !== "object") {
        return;
      }
      if (Array.isArray(o)) {
        if (
          o.length >= 2 &&
          typeof o[0] === "number" &&
          typeof o[1] === "number" &&
          typeof o[2] !== "number"
        ) {
          var pk = (path.split(".").pop() || "").toLowerCase();
          if (/uv|pixel|centroid|proj|coord|img|point|center/.test(pk)) {
            add(o[0], o[1], pk || "arr");
          }
        }
        for (var ai = 0; ai < o.length; ai++) {
          walk(o[ai], depth + 1, path + "[" + ai + "]");
        }
        return;
      }
      for (var k in o) {
        if (!Object.prototype.hasOwnProperty.call(o, k)) {
          continue;
        }
        walk(o[k], depth + 1, path ? path + "." + k : k);
      }
    }
    walk(plan, 0, "");
    if (Array.isArray(plan.operators_overlay_uv)) {
      plan.operators_overlay_uv.forEach(function (p, ix) {
        if (!p) {
          return;
        }
        add(Number(p.u), Number(p.v), p.label || "uv_" + ix);
      });
    }
    return pts;
  }

  /**
   * Estrae triple [x,y,z] in **base_link** (metri) per nuvola 3D.
   * Convenzione consigliata per il worker: `operators_grasp_points_base_link_m: [[x,y,z],...]`
   * oppure `point_cloud` / `points_xyz` (flat N*3 o array di triple).
   */
  window.operatorsExtractGraspPointsBaseLink = function (plan) {
    var MAX = 6000;
    var out = [];
    function push3(x, y, z) {
      if (out.length >= MAX) {
        return;
      }
      var a = Number(x);
      var b = Number(y);
      var c = Number(z);
      if (!isFinite(a + b + c)) {
        return;
      }
      out.push([a, b, c]);
    }
    function consumeArray(arr) {
      if (!Array.isArray(arr) || !arr.length) {
        return;
      }
      var first = arr[0];
      if (typeof first === "number") {
        for (var i = 0; i + 2 < arr.length; i += 3) {
          push3(arr[i], arr[i + 1], arr[i + 2]);
        }
        return;
      }
      for (var j = 0; j < arr.length; j++) {
        var row = arr[j];
        if (!row || typeof row !== "object") {
          continue;
        }
        if (Array.isArray(row) && row.length >= 3 && typeof row[0] === "number") {
          push3(row[0], row[1], row[2]);
        } else if (row.translation && row.translation.length >= 3) {
          push3(row.translation[0], row.translation[1], row.translation[2]);
        } else if (row.center && row.center.length >= 3) {
          push3(row.center[0], row.center[1], row.center[2]);
        }
      }
    }
    if (!plan || typeof plan !== "object") {
      return [];
    }
    if (Array.isArray(plan.operators_grasp_points_base_link_m)) {
      consumeArray(plan.operators_grasp_points_base_link_m);
      return out.slice();
    }
    var topKeys = [
      "point_cloud",
      "points_xyz",
      "scene_points",
      "points",
      "vertices",
      "grasp_candidates",
      "pred_grasps",
      "grasps",
      "topk_grasps",
    ];
    for (var ti = 0; ti < topKeys.length; ti++) {
      var v = plan[topKeys[ti]];
      if (v == null) {
        continue;
      }
      if (Array.isArray(v)) {
        consumeArray(v);
      } else if (typeof v === "object" && Array.isArray(v.points)) {
        consumeArray(v.points);
      }
    }
    if (plan.translation && plan.translation.length >= 3) {
      push3(plan.translation[0], plan.translation[1], plan.translation[2]);
    }
    if (plan.data && typeof plan.data === "object") {
      var d = plan.data;
      if (Array.isArray(d.points)) {
        consumeArray(d.points);
      }
      if (Array.isArray(d.point_cloud)) {
        consumeArray(d.point_cloud);
      }
      if (d.translation && d.translation.length >= 3) {
        push3(d.translation[0], d.translation[1], d.translation[2]);
      }
    }
    return out.slice(0, MAX);
  };

  window.operatorsApplyGraspVisualization = function (plan) {
    if (!plan || typeof plan !== "object") {
      window.__operatorsGraspPointsBaseLink_m = null;
      window.__operatorsGraspMarkerBaseLink_m = null;
      window.operatorsGraspDrawOverlay();
      if (window.operatorsScene3dRefreshGraspLayer) {
        window.operatorsScene3dRefreshGraspLayer();
      }
      return;
    }
    graspApplyMarkerFromPlan(plan);
    window.__operatorsGraspPointsBaseLink_m = window.operatorsExtractGraspPointsBaseLink(plan);
    window.operatorsGraspDrawOverlay();
    if (window.operatorsScene3dRefreshGraspLayer) {
      window.operatorsScene3dRefreshGraspLayer();
    }
  };

  window.operatorsRefreshGraspPreviewFrame = function () {
    var img = document.getElementById("graspWristImg");
    if (!img) {
      return;
    }
    var sr = (window.__OPERATORS_SCRIPT_ROOT__ || "").replace(/\/$/, "");
    img.src = sr + "/api/robot/camera/0.jpg?ts=" + Date.now();
  };

  window.operatorsGraspDrawOverlay = function () {
    var img = document.getElementById("graspWristImg");
    var cv = document.getElementById("graspOverlayCanvas");
    var plan = window.__operatorsLastGraspPlan;
    if (!img || !cv || !plan) {
      return;
    }
    var w = img.clientWidth || 320;
    var h = img.clientHeight || 240;
    cv.width = w;
    cv.height = h;
    var ctx = cv.getContext("2d");
    if (!ctx) {
      return;
    }
    ctx.clearRect(0, 0, w, h);
    var pts = operatorsCollectOverlayPoints(plan);
    var nw = img.naturalWidth || w;
    var nh = img.naturalHeight || h;
    var scaleX = w / Math.max(nw, 1);
    var scaleY = h / Math.max(nh, 1);
    ctx.save();
    ctx.lineWidth = 2;
    for (var i = 0; i < pts.length; i++) {
      var p = pts[i];
      var px = p.x;
      var py = p.y;
      if (px <= 2 && py <= 2 && px >= 0 && py >= 0) {
        px = px * w;
        py = py * h;
      } else {
        px = px * scaleX;
        py = py * scaleY;
      }
      ctx.strokeStyle = "rgba(56, 189, 248, 0.95)";
      ctx.fillStyle = "rgba(56, 189, 248, 0.35)";
      ctx.beginPath();
      ctx.arc(px, py, 10, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      ctx.font = "11px Inter,sans-serif";
      ctx.fillStyle = "rgba(248,250,252,0.95)";
      ctx.fillText(p.label || "pt", px + 12, py + 4);
    }
    ctx.restore();
    var dbg = document.getElementById("graspOverlayDbg");
    if (dbg) {
      dbg.textContent =
        pts.length === 0
          ? "Nessun punto 2D euristico: aggiungi `operators_overlay_points` nel JSON o grip_point.cx/cy."
          : "Overlay: " + pts.length + " punto(i).";
    }
  };

  window.operatorsGraspHealth = function () {
    setGraspPhase("GET /api/grasp/health…");
    fetch(api("/api/grasp/health?_=" + Date.now()), { cache: "no-store" })
      .then(function (r) {
        return r.json();
      })
      .then(function (j) {
        setGraspPhase(JSON.stringify(j, null, 2));
      })
      .catch(function (e) {
        setGraspPhase("Errore: " + String(e));
      });
  };

  window.operatorsGraspPlan = function () {
    var body;
    try {
      body = graspPlanObject();
    } catch (e) {
      setGraspPhase(String(e));
      return;
    }
    setGraspPhase("POST /api/grasp/plan…");
    fetch(api("/api/grasp/plan"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(function (r) {
        return r.json().then(function (j) {
          return { status: r.status, j: j };
        });
      })
      .then(function (o) {
        window.__operatorsLastGraspPlan = o.j;
        setGraspPhase(JSON.stringify(o.j, null, 2) + "\nHTTP " + o.status);
        window.operatorsApplyGraspVisualization(o.j);
        window.operatorsRefreshGraspPreviewFrame();
        var img = document.getElementById("graspWristImg");
        if (img) {
          img.onload = function () {
            window.operatorsGraspDrawOverlay();
          };
        }
      })
      .catch(function (e) {
        setGraspPhase("Errore: " + String(e));
      });
  };

  window.operatorsGraspExecute = function () {
    var body = {};
    try {
      body = graspPlanObject();
    } catch (e) {
      setGraspPhase(String(e));
      return;
    }
    var cb = document.getElementById("graspExecuteMergeLast");
    if (cb && cb.checked && window.__operatorsLastGraspPlan && typeof window.__operatorsLastGraspPlan === "object") {
      var merged = {};
      var last = window.__operatorsLastGraspPlan;
      var k;
      for (k in last) {
        if (Object.prototype.hasOwnProperty.call(last, k)) {
          merged[k] = last[k];
        }
      }
      for (k in body) {
        if (Object.prototype.hasOwnProperty.call(body, k)) {
          merged[k] = body[k];
        }
      }
      body = merged;
    }
    setGraspPhase("POST /api/grasp/execute…");
    fetch(api("/api/grasp/execute"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(function (r) {
        return r.json().then(function (j) {
          return { status: r.status, j: j };
        });
      })
      .then(function (o) {
        setGraspPhase(JSON.stringify(o.j, null, 2) + "\nHTTP " + o.status);
        if (o.j && typeof o.j === "object") {
          window.operatorsApplyGraspVisualization(o.j);
        }
      })
      .catch(function (e) {
        setGraspPhase("Errore: " + String(e));
      });
  };

  window.operatorsGraspPipeline = function () {
    var pre = document.getElementById("graspPipelinePre");
    if (pre) {
      pre.textContent = "Carico…";
    }
    fetch(api("/api/arm/grasp_pipeline?_=" + Date.now()), { cache: "no-store" })
      .then(function (r) {
        return r.json();
      })
      .then(function (j) {
        var slim = {
          ok: j.ok,
          updated_at: j.updated_at,
          environment: j.environment,
          fusion_ready_for_execute: j.fusion_ready_for_execute,
          story: j.narrative_it || j.story,
          sequence_start_ready: j.sequence_start_ready,
          selected_camera: j.selected_camera,
          selected_grasp_assessment: j.selected_grasp_assessment,
        };
        var txt = JSON.stringify(slim, null, 2);
        if (pre) {
          pre.textContent = txt;
        }
      })
      .catch(function (e) {
        if (pre) {
          pre.textContent = String(e);
        }
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

  function tick() {
    window.operatorsPollSportLast();
    if (document.getElementById("stackStatus")) {
      var p = document.querySelector('.tab-panel[data-tab="stato"]');
      if (p && p.classList.contains("active")) {
        window.operatorsRefreshStack();
      }
    }
    if (document.getElementById("cameraStatusPre")) {
      var sc = document.querySelector('.tab-panel[data-tab="scene"]');
      if (sc && sc.classList.contains("active")) {
        window.operatorsRefreshCamerasStatus();
      }
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    fillStreamUrlCodes();
    tick();
    setInterval(tick, 8000);
    var gimg = document.getElementById("graspWristImg");
    if (gimg) {
      gimg.addEventListener("load", function () {
        if (window.__operatorsLastGraspPlan) {
          window.operatorsGraspDrawOverlay();
        }
      });
      window.operatorsRefreshGraspPreviewFrame();
    }
  });
})();
