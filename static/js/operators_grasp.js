(function () {
  "use strict";
  var api = window.operatorsApi;
  if (typeof api !== "function") {
    throw new Error('operators_core.js must load before this module');
  }

  window.__operatorsLastGraspPlan = null;
  window.__operatorsGraspPointsBaseLink_m = null;
  /** Ultimo URL immagine noto per anteprima VLA (preset o ``image_url_used`` dal worker). */
  window.__operatorsVlaInputUrl = null;
  var _START_VARIANT_KEY = "go2StartVariant";

  function setGraspPhase(msg) {
    var el = document.getElementById("graspPhase");
    if (el) {
      el.textContent = msg;
    }
  }

  function _startVariantSelectIds() {
    return ["opStartVariant", "graspCoachStartVariant"];
  }

  window.operatorsStartVariant = function () {
    var stored = "";
    try {
      stored = String(window.localStorage.getItem(_START_VARIANT_KEY) || "").trim();
    } catch (_e) {}
    if (stored === "frontal" || stored === "lateral") {
      return stored;
    }
    var sel = document.getElementById("opStartVariant") || document.getElementById("graspCoachStartVariant");
    var v = sel ? String(sel.value || "").trim() : "";
    return v === "frontal" ? "frontal" : "lateral";
  };

  window.operatorsStartVariantPayload = function () {
    return { start_variant: window.operatorsStartVariant() };
  };

  window.operatorsStartVariantChanged = function () {
    var v = window.operatorsStartVariant();
    _startVariantSelectIds().forEach(function (id) {
      var el = document.getElementById(id);
      if (el) {
        el.value = v;
      }
    });
    try {
      window.localStorage.setItem(_START_VARIANT_KEY, v);
    } catch (_e2) {}
    if (typeof window.operatorsGraspRefreshStartPoseBadge === "function") {
      window.operatorsGraspRefreshStartPoseBadge();
    }
  };

  window.operatorsStartVariantInit = function () {
    var v = "lateral";
    try {
      var stored = String(window.localStorage.getItem(_START_VARIANT_KEY) || "").trim();
      if (stored === "frontal" || stored === "lateral") {
        v = stored;
      }
    } catch (_e3) {}
    _startVariantSelectIds().forEach(function (id) {
      var el = document.getElementById(id);
      if (el) {
        el.value = v;
      }
    });
  };

  function statusLabelIt(st) {
    if (st === "pass") return "OK";
    if (st === "warn") return "ATTENZIONE";
    if (st === "fail") return "BLOCCATO";
    return "—";
  }

  /** Tab Presa → card «Validazione presa» (#graspValidationPanel). */
  window.operatorsGraspRenderValidation = function (vui) {
    vui = vui || {};
    var banner = document.getElementById("graspValidationBanner");
    var body = document.getElementById("graspValidationBody");
    var panel = document.getElementById("graspValidationPanel");
    if (banner) {
      banner.textContent = vui.banner_it || "In attesa health + piano.";
      banner.className = "op-grasp-validation-banner op-grasp-validation-banner--" + (vui.banner_level || "idle");
    }
    if (body) {
      var rows = vui.checks || [];
      if (!rows.length) {
        body.innerHTML = '<tr><td colspan="3" class="muted small">Nessun controllo — premi Health worker.</td></tr>';
      } else {
        var html = "";
        var i;
        for (i = 0; i < rows.length; i++) {
          var c = rows[i];
          html +=
            "<tr><td>" +
            (c.label_it || "") +
            '</td><td class="op-grasp-val-status op-grasp-val-status--' +
            (c.status || "idle") +
            '">' +
            statusLabelIt(c.status) +
            "</td><td class=\"muted small\">" +
            (c.detail_it || "") +
            "</td></tr>";
        }
        body.innerHTML = html;
      }
    }
    if (panel) {
      panel.classList.toggle("op-grasp-validation--ready", !!vui.can_execute_phased);
    }
    window.operatorsGraspSyncExecuteButtons(vui);
  };

  window.operatorsGraspSyncExecuteButtons = function (vui) {
    vui = vui || {};
    var phased = document.getElementById("graspBtnExecutePhased");
    var ik = document.getElementById("graspBtnExecuteIk");
    var fk = document.getElementById("graspBtnExecuteFk");
    if (phased) {
      phased.disabled = !vui.can_execute_phased;
      phased.title = vui.can_execute_phased ? "" : vui.banner_it || "Validazione non OK";
    }
    if (ik) {
      ik.disabled = !vui.can_execute_ik;
    }
    if (fk) {
      fk.disabled = true;
    }
  };

  /**
   * Barra avanzamento tab Presa (fasi 0..4). step=-1 azzera; indeterminate per ciclo server lungo.
   */
  window.operatorsGraspProgressUi = function (o) {
    o = o || {};
    var step = typeof o.step === "number" ? o.step : -1;
    var label = o.label != null ? String(o.label) : "";
   
    var err = !!o.error;
    var indet = !!o.indeterminate;
    var fill = document.getElementById("graspProgressFill");
    var cap = document.getElementById("graspProgressCaption");
    var track = document.getElementById("graspProgressTrack");
    var card = document.getElementById("graspProgressCard");
    if (track) {
      track.classList.toggle("is-indeterminate", indet && !err);
      if (err) {
        track.classList.remove("is-indeterminate");
      }
    }
    if (card) {
      card.classList.toggle("op-grasp-progress--error", err);
      card.classList.toggle("op-grasp-progress--busy", step >= 0 && !err && step < 4);
    }
    var pct;
    if (step < 0) {
      pct = 0;
    } else if (step >= 4) {
      pct = 100;
    } else {
      pct = Math.round(((step + 1) / 5) * 100);
    }
    if (fill) {
      if (indet && !err) {
        fill.style.width = "42%";
        fill.style.transform = "none";
      } else {
        fill.style.width = pct + "%";
        fill.style.transform = "none";
      }
    }
    if (cap) {
      cap.textContent = label || (step < 0 ? "Pronto" : "…");
    }
    if (track) {
      track.setAttribute("aria-valuenow", String(indet ? 0 : pct));
    }
    var n = 5;
    var i;
    for (i = 0; i < n; i++) {
      var chip = document.querySelector(".op-grasp-step-chips [data-grasp-step=\"" + i + '"]');
      if (!chip) {
        continue;
      }
      chip.classList.remove("is-error");
      if (err && i === Math.max(0, Math.min(4, step))) {
        chip.classList.add("is-error");
      }
      chip.classList.toggle("is-done", step >= 0 && i < step && !err);
      chip.classList.toggle("is-active", step >= 0 && i === step && !err);
    }
  };

  window.operatorsGraspProgressReset = function () {
    window.__operatorsGraspPipelineBusy = false;
    window.operatorsGraspProgressUi({ step: -1, label: "Pronto" });
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

  function graspPeekImageUrlFromBody() {
    try {
      var b = graspPlanObject();
      if (b.image_url && typeof b.image_url === "string") {
        return b.image_url.trim();
      }
      if (b.camera_jpg_url && typeof b.camera_jpg_url === "string") {
        return b.camera_jpg_url.trim();
      }
    } catch (e) {
      return null;
    }
    return null;
  }

  window.operatorsUpdateVlaInputLabel = function (text) {
    var el = document.getElementById("graspVlaInputLabel");
    if (el) {
      el.textContent = text || "—";
    }
  };

  function graspResolveVlaPreviewUrl() {
    var u = window.__operatorsVlaInputUrl;
    if (u && typeof u === "string" && u.indexOf("http") === 0) {
      return u;
    }
    var fromBody = graspPeekImageUrlFromBody();
    if (fromBody) {
      return fromBody;
    }
    return null;
  }

  function operatorsResolveNxJpegBase() {
    var inp = document.getElementById("operatorsNxJpegBase");
    var v = inp && inp.value && String(inp.value).trim();
    if (v) {
      return v.replace(/\/+$/, "");
    }
    var sr = (window.__OPERATORS_SCRIPT_ROOT__ || "").replace(/\/+$/, "");
    var origin = window.location.origin || "";
    return (origin + sr).replace(/\/+$/, "");
  }

  window.operatorsSyncNxBaseFromBrowser = function () {
    var inp = document.getElementById("operatorsNxJpegBase");
    if (!inp) {
      return;
    }
    var sr = (window.__OPERATORS_SCRIPT_ROOT__ || "").replace(/\/+$/, "");
    inp.value = (window.location.origin || "") + sr;
    setGraspPhase(
      "Base JPEG = host del browser. Il worker RTX deve usare l'IP LAN della NX se carichi la dashboard dal PC."
    );
  };

  var _MJPEG_LOADING_BY_IMG = {
    cam0Preview: "cam0MjpegLoading",
    cam6Preview: "cam6MjpegLoading",
    graspDockCam0: "graspDockCam0Loading",
    graspDockCam6: "graspDockCam6Loading",
  };

  function operatorsMjpegLoadingSet(imgId, visible) {
    var lid = _MJPEG_LOADING_BY_IMG[imgId];
    if (!lid) {
      return;
    }
    var el = document.getElementById(lid);
    if (!el) {
      return;
    }
    el.hidden = !visible;
  }

  function operatorsWireMjpegStream(img) {
    if (!img || !img.id) {
      return;
    }
    var id = img.id;
    function hideBar() {
      operatorsMjpegLoadingSet(id, false);
    }
    img.addEventListener("load", hideBar);
    img.addEventListener("error", hideBar);
  }

  window.operatorsBumpMjpegStreams = function () {
    var c0 = document.getElementById("cam0Preview");
    var c6 = document.getElementById("cam6Preview");
    var q = "?_=" + Date.now();
    operatorsMjpegLoadingSet("cam0Preview", true);
    operatorsMjpegLoadingSet("cam6Preview", true);
    if (c0) {
      c0.src = api("/stream/robot/camera/0.mjpg") + q;
    }
    if (c6) {
      c6.src = api("/stream/robot/camera/6.mjpg") + q;
    }
  };

  function operatorsGraspPresetOpenvlaSelected() {
    var ta = document.getElementById("graspPlanJson");
    if (!ta) {
      return;
    }
    var rad = document.querySelector('input[name="openvlaImgSrc"]:checked');
    var mode = rad ? rad.value : "6";
    var base = operatorsResolveNxJpegBase();
    var sr = (window.__OPERATORS_SCRIPT_ROOT__ || "").replace(/\/+$/, "");
    var image_url;
    var logicalDev = 6;
    if (mode === "vla") {
      if (
        !window.__operatorsOpenvlaJpegUrls ||
        !window.__operatorsOpenvlaJpegUrls.vla_frame_configured
      ) {
        setGraspPhase(
          "vla_frame: imposta GO2_VLA_SNAPSHOT_V4L_INDEX sulla NX (RGB) e riavvia la dashboard."
        );
        return;
      }
      image_url = base + sr + "/api/robot/vla_frame.jpg";
      logicalDev = 0;
    } else {
      var cam = parseInt(mode, 10);
      if (!isFinite(cam)) {
        cam = 6;
      }
      image_url = base + sr + "/api/robot/camera/" + cam + ".jpg";
      logicalDev = cam;
    }
    var instInp = document.getElementById("operatorsOpenvlaInstruction");
    var instruction =
      (instInp && instInp.value && String(instInp.value).trim()) || "pick up the white box";
    ta.value = JSON.stringify(
      {
        instruction: instruction,
        image_url: image_url,
        logical_camera_device: logicalDev,
      },
      null,
      2
    );
    window.__operatorsVlaInputUrl = image_url;
    if (window.operatorsRefreshGraspPreviewFrame) {
      window.operatorsRefreshGraspPreviewFrame();
    }
    if (window.operatorsUpdateVlaInputLabel) {
      window.operatorsUpdateVlaInputLabel(
        "Prossimo «Piano»: worker GET → " +
          image_url +
          " · logical_camera_device=" +
          logicalDev
      );
    }
    setGraspPhase(
      "JSON OpenVLA pronto. L'anteprima deve essere a colori. Poi «Piano» (il worker userà lo stesso image_url)."
    );
  }

  /** @param {number} camId 0 o 6 — seleziona il radio e rigenera il JSON */
  window.operatorsGraspPresetOpenvla = function (camId) {
    var n = parseInt(String(camId), 10);
    if (n === 0 || n === 6) {
      var rd = document.querySelector('input[name="openvlaImgSrc"][value="' + n + '"]');
      if (rd) {
        rd.checked = true;
      }
    }
    operatorsGraspPresetOpenvlaSelected();
  };

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
    /* OpenVLA: se il worker manda solo vettore azione, replica euristica base_link (allineata a openvla_runtime). */
    if (!window.__operatorsGraspMarkerBaseLink_m && Array.isArray(plan.openvla_action_7dof) && plan.openvla_action_7dof.length >= 3) {
      var ox = 0.42;
      var oy = 0.0;
      var oz = 0.18;
      var sc = 0.04;
      var a0 = Number(plan.openvla_action_7dof[0]);
      var a1 = Number(plan.openvla_action_7dof[1]);
      var a2 = Number(plan.openvla_action_7dof[2]);
      if (isFinite(a0 + a1 + a2)) {
        window.__operatorsGraspMarkerBaseLink_m = [ox + a0 * sc, oy + a1 * sc, oz + a2 * sc];
      }
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
      if (window.operatorsUpdateGraspTargetDebug) {
        window.operatorsUpdateGraspTargetDebug(null);
      }
      if (window.operatorsScene3dRefreshGraspLayer) {
        window.operatorsScene3dRefreshGraspLayer();
      }
      return;
    }
    graspApplyMarkerFromPlan(plan);
    window.__operatorsGraspPointsBaseLink_m = window.operatorsExtractGraspPointsBaseLink(plan);
    window.operatorsGraspDrawOverlay();
    if (window.operatorsUpdateGraspTargetDebug) {
      window.operatorsUpdateGraspTargetDebug(plan);
    }
    if (window.operatorsScene3dRefreshGraspLayer) {
      window.operatorsScene3dRefreshGraspLayer();
    }
  };

  /** Sintesi testuale: dove sta il target 3D / se stub (non sostituisce tab 3D). */
  window.operatorsUpdateGraspTargetDebug = function (plan) {
    var el = document.getElementById("graspTargetDebug");
    var odp = document.getElementById("openvlaDebugPre");
    if (!el) {
      return;
    }
    if (!plan || typeof plan !== "object") {
      el.textContent = "Nessun piano in cache — premi «Piano».";
      if (odp) {
        odp.textContent = "—";
      }
      return;
    }
    var lines = [];
    var be = plan.backend != null ? String(plan.backend) : "(assente nel JSON)";
    lines.push("backend: " + be);
    if (plan.backend === "stub") {
      lines.push(
        ">>> STUB: grasp_display NON viene dalla camera — punto 3D fisso di test. IK/FK verso coordinate sbagliate può colpire persone/animali."
      );
    }
    if (plan.image_url_used) {
      lines.push("image_url_used: " + plan.image_url_used);
    } else if (plan.image_url) {
      lines.push("image_url (richiesta): " + plan.image_url);
    }
    if (plan.grasp_display_base_link_m && plan.grasp_display_base_link_m.length >= 3) {
      lines.push("grasp_display_base_link_m [m]: " + JSON.stringify(plan.grasp_display_base_link_m));
    }
    if (plan.openvla_fk_tool_tip_base_link_m && plan.openvla_fk_tool_tip_base_link_m.length >= 3) {
      lines.push("openvla_fk_tool_tip_base_link_m [m]: " + JSON.stringify(plan.openvla_fk_tool_tip_base_link_m));
    }
    if (plan.openvla_joint_space) {
      lines.push("openvla_joint_space: " + plan.openvla_joint_space);
    }
    if (plan.openvla_heatmap_gaussian) {
      lines.push("overlay: heatmap gaussiana (euristica o dal worker)");
    }
    if (plan.openvla_heatmap_png_b64) {
      lines.push("overlay: heatmap PNG (base64 dal server se presente)");
    }
    if (plan.operators_debug_bbox_norm || plan.openvla_bbox_norm || plan.openvla_bbox_xyxy_pixels) {
      lines.push(
        "overlay: bbox " +
          (plan.openvla_bbox_xyxy_pixels ? "pixel " : "") +
          (plan.openvla_bbox_norm ? "norm server " : "") +
          (plan.operators_debug_bbox_norm ? "euristica " : "")
      );
    }
    if (plan.openvla_debug && typeof plan.openvla_debug === "object") {
      var od = plan.openvla_debug;
      if (od.predict_walltime_s != null) {
        lines.push("openvla predict_walltime_s: " + od.predict_walltime_s);
      }
      if (od.instruction != null) {
        lines.push("openvla instruction: " + String(od.instruction).slice(0, 120));
      }
    }
    lines.push(
      "2D: overlay su anteprima (cerchi, bbox, heatmap). «Ridisegna overlay» se cambi dimensione finestra."
    );
    lines.push("3D nel mondo robot: tab «3D» (marker). GET " + api("/api/arm/last_plan_debug") + " include openvla_debug.");
    el.textContent = lines.join("\n");
    if (odp) {
      if (plan.openvla_debug && typeof plan.openvla_debug === "object") {
        try {
          odp.textContent = JSON.stringify(plan.openvla_debug, null, 2);
        } catch (e) {
          odp.textContent = String(plan.openvla_debug);
        }
      } else {
        odp.textContent = "(nessun oggetto openvla_debug nel piano — worker stub o OPENVLA_UI_OVERLAY=0)";
      }
    }
  };

  function operatorsDrawBoxOnOverlay(img, canvas, xyxy) {
    if (!img || !canvas || !xyxy || xyxy.length < 4) {
      return;
    }
    var w = img.clientWidth || 320;
    var h = img.clientHeight || 240;
    canvas.width = w;
    canvas.height = h;
    var nw = img.naturalWidth || w;
    var nh = img.naturalHeight || h;
    var sx = w / Math.max(nw, 1);
    var sy = h / Math.max(nh, 1);
    var ctx = canvas.getContext("2d");
    if (!ctx) {
      return;
    }
    ctx.clearRect(0, 0, w, h);
    var x0 = xyxy[0] * sx;
    var y0 = xyxy[1] * sy;
    var x1 = xyxy[2] * sx;
    var y1 = xyxy[3] * sy;
    ctx.strokeStyle = "rgba(34, 197, 94, 0.95)";
    ctx.lineWidth = 3;
    ctx.strokeRect(x0, y0, x1 - x0, y1 - y0);
    ctx.font = "12px Inter,sans-serif";
    ctx.fillStyle = "rgba(220, 252, 231, 0.95)";
    ctx.fillText("detect", x0 + 4, y0 + 14);
  }

  window.operatorsBoxDetectCamera = function (cam) {
    var pre = document.getElementById("boxDetectPre");
    var img = document.getElementById("cam" + cam + "Preview");
    var cv = document.getElementById("cam" + cam + "YoloCanvas");
    if (pre) {
      pre.textContent = "GET /api/vision/box_detect?camera=" + cam + " …";
    }
    fetch(api("/api/vision/box_detect?camera=" + cam + "&_=" + Date.now()), { cache: "no-store" })
      .then(function (r) {
        return r.json();
      })
      .then(function (j) {
        if (pre) {
          pre.textContent = JSON.stringify(j, null, 2);
        }
        var det = j.detection;
        if (img && cv && det && det.ok && det.bbox_xyxy) {
          operatorsDrawBoxOnOverlay(img, cv, det.bbox_xyxy);
        } else if (cv && img) {
          var ctx2 = cv.getContext("2d");
          cv.width = img.clientWidth || 1;
          cv.height = img.clientHeight || 1;
          if (ctx2) {
            ctx2.clearRect(0, 0, cv.width, cv.height);
          }
        }
      })
      .catch(function (e) {
        if (pre) {
          pre.textContent = String(e);
        }
      });
  };

  window.operatorsArmEmergencyHold = function () {
    if (
      !window.confirm(
        "E-STOP SOFTWARE (braccio D1): interrompe d1_arm_command in corso e comanda HOLD sulla posa letta. Non spegne i motori. Confermi?"
      )
    ) {
      return;
    }
    setGraspPhase("POST /api/arm/emergency_hold…");
    fetch(api("/api/arm/emergency_hold"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: "ARM_ESTOP_HOLD" }),
    })
      .then(function (r) {
        return r.json().then(function (j) {
          return { status: r.status, j: j };
        });
      })
      .then(function (o) {
        setGraspPhase(JSON.stringify(o.j, null, 2) + "\nHTTP " + o.status);
      })
      .catch(function (e) {
        setGraspPhase("Errore: " + String(e));
      });
  };

  function operatorsArmPostConfirm(url, confirmToken, confirmMessage, extraBody) {
    if (!window.confirm(confirmMessage)) {
      return;
    }
    setGraspPhase("POST " + url + "…");
    var body = { confirm: confirmToken };
    if (extraBody && typeof extraBody === "object") {
      Object.keys(extraBody).forEach(function (k) {
        body[k] = extraBody[k];
      });
    }
    fetch(api(url), {
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
      })
      .catch(function (e) {
        setGraspPhase("Errore: " + String(e));
      });
  };

  window.operatorsArmGotoHome = function () {
    operatorsArmPostConfirm(
      "/api/arm/goto_home",
      "ARM_GOTO_HOME",
      "Portare i 7 servo ai valori di «Home numerica» (default 0°, oppure D1_HOME_SERVO_DEG_7). Confermi?"
    );
  };

  window.operatorsArmGotoTrueZero = function () {
    operatorsArmPostConfirm(
      "/api/arm/goto_true_zero",
      "ARM_GOTO_TRUE_ZERO",
      "Portare il braccio alla posa ZERO registrata (true_zero_pose.json). File assente = errore API. Confermi?"
    );
  };

  window.operatorsArmGotoSavedStart = function () {
    var variant = window.operatorsStartVariant();
    var label = variant === "frontal" ? "frontale" : "laterale";
    operatorsArmPostConfirm(
      "/api/arm/goto_saved_start",
      "ARM_GOTO_SAVED_START",
      "Portare il braccio alla posa START " + label + " (start_alignment_" + variant + ".json). Confermi?",
      window.operatorsStartVariantPayload()
    );
  };

  window.operatorsArmGotoZeroThenStart = function () {
    operatorsArmPostConfirm(
      "/api/arm/goto_zero_then_start",
      "ARM_GOTO_ZERO_THEN_START",
      "Due movimenti in sequenza: ZERO salvato, poi START " + window.operatorsStartVariant() + ". Confermi?",
      window.operatorsStartVariantPayload()
    );
  };

  window.operatorsRefreshGraspPreviewFrame = function () {
    var img = document.getElementById("graspWristImg");
    if (!img) {
      return;
    }
    var resolved = graspResolveVlaPreviewUrl();
    var sr = (window.__OPERATORS_SCRIPT_ROOT__ || "").replace(/\/$/, "");
    if (resolved) {
      var sep = resolved.indexOf("?") >= 0 ? "&" : "?";
      img.src = resolved + sep + "ts=" + Date.now();
    } else {
      img.src = sr + "/api/robot/camera/0.jpg?ts=" + Date.now();
    }
    if (window.operatorsUpdateVlaInputLabel) {
      if (resolved) {
        window.operatorsUpdateVlaInputLabel(
          "Anteprima = questo URL (stesso che userà il worker se nel JSON c'è image_url). " + resolved
        );
      } else {
        window.operatorsUpdateVlaInputLabel(
          "Nessun image_url nel JSON: anteprima fallback cam 0. Il worker OpenVLA usa WORKER_CAMERA_JPG_URL (vedi GET worker /health)."
        );
      }
    }
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
    var nw = img.naturalWidth || w;
    var nh = img.naturalHeight || h;
    var scaleX = w / Math.max(nw, 1);
    var scaleY = h / Math.max(nh, 1);

    function drawHeatGaussian(hm) {
      if (!hm || typeof hm.cx !== "number" || typeof hm.cy !== "number") {
        return;
      }
      var cx = hm.cx * w;
      var cy = hm.cy * h;
      var rad = Math.max(w, h) * (typeof hm.sigma === "number" ? hm.sigma : 0.14) * 3;
      var g = ctx.createRadialGradient(cx, cy, 0, cx, cy, Math.max(rad, 8));
      g.addColorStop(0, "rgba(251, 146, 60, 0.55)");
      g.addColorStop(0.35, "rgba(234, 88, 12, 0.22)");
      g.addColorStop(1, "rgba(234, 88, 12, 0)");
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, w, h);
    }

    function drawBboxNorm(bb, stroke, label) {
      if (!bb || typeof bb.x !== "number") {
        return;
      }
      ctx.strokeStyle = stroke || "#22d3ee";
      ctx.lineWidth = 3;
      ctx.strokeRect(bb.x * w, bb.y * h, bb.w * w, bb.h * h);
      ctx.font = "12px Inter,sans-serif";
      ctx.fillStyle = "#e0f2fe";
      ctx.fillText(label || bb.label || "bbox", bb.x * w + 4, bb.y * h + 15);
    }

    function drawBboxPixels(xyxy, stroke) {
      if (!Array.isArray(xyxy) || xyxy.length < 4) {
        return;
      }
      var x0 = Number(xyxy[0]) * scaleX;
      var y0 = Number(xyxy[1]) * scaleY;
      var x1 = Number(xyxy[2]) * scaleX;
      var y1 = Number(xyxy[3]) * scaleY;
      ctx.strokeStyle = stroke || "#c084fc";
      ctx.lineWidth = 3;
      ctx.strokeRect(x0, y0, x1 - x0, y1 - y0);
      ctx.font = "12px Inter,sans-serif";
      ctx.fillStyle = "#f5d0fe";
      ctx.fillText("bbox px", x0 + 4, y0 + 15);
    }

    if (plan.openvla_heatmap_gaussian) {
      drawHeatGaussian(plan.openvla_heatmap_gaussian);
    }
    var hmB64 = plan.openvla_heatmap_png_b64;
    if (hmB64 && typeof hmB64 === "string") {
      var src = hmB64.indexOf("data:") === 0 ? hmB64 : "data:image/png;base64," + hmB64;
      var hImg = new Image();
      hImg.onload = function () {
        try {
          ctx.save();
          ctx.globalAlpha = 0.52;
          ctx.drawImage(hImg, 0, 0, w, h);
          ctx.restore();
        } catch (e) {}
      };
      hImg.src = src;
    }
    if (plan.operators_debug_bbox_norm) {
      drawBboxNorm(plan.operators_debug_bbox_norm, "#22d3ee", "OpenVLA (euristica)");
    }
    var bn = plan.openvla_bbox_norm;
    if (bn && typeof bn === "object" && typeof bn.x === "number") {
      drawBboxNorm(bn, "#4ade80", "server norm");
    }
    if (plan.openvla_bbox_xyxy_pixels) {
      drawBboxPixels(plan.openvla_bbox_xyxy_pixels, "#c084fc");
    }

    var pts = operatorsCollectOverlayPoints(plan);
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
      var bits = [];
      bits.push(pts.length ? pts.length + " punto(s)" : "nessun punto overlay");
      if (plan.openvla_heatmap_gaussian) {
        bits.push("heatmap gauss.");
      }
      if (plan.openvla_heatmap_png_b64) {
        bits.push("heatmap PNG");
      }
      if (plan.operators_debug_bbox_norm) {
        bits.push("bbox eurist.");
      }
      if (plan.openvla_bbox_xyxy_pixels) {
        bits.push("bbox px");
      }
      dbg.textContent = bits.join(" · ");
    }
  };

  window.operatorsGraspEc2Start = function () {
    setGraspPhase("POST /api/grasp/ec2/start…");
    window.operatorsGraspProgressUi({ step: 0, label: "Avvio EC2 g5 (può richiedere 2–4 min)…" });
    return fetch(api("/api/grasp/ec2/start"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ wait_health: true }),
    })
      .then(function (r) {
        return r.json().then(function (j) {
          return { status: r.status, j: j };
        });
      })
      .then(function (o) {
        setGraspPhase(JSON.stringify(o.j, null, 2) + "\nHTTP " + o.status);
        if (o.status >= 400 || !o.j || o.j.ok === false) {
          window.operatorsGraspProgressUi({
            step: 0,
            label: "Avvio EC2 fallito — controlla credenziali AWS sulla NX",
            error: true,
          });
          return Promise.reject(new Error("EC2 start: HTTP " + o.status));
        }
        window.operatorsGraspProgressUi({ step: 0, label: "EC2 avviato — verifica worker…" });
        return o.j;
      });
  };

  window.operatorsGraspEc2Stop = function () {
    setGraspPhase("POST /api/grasp/ec2/stop…");
    return fetch(api("/api/grasp/ec2/stop"), { method: "POST" })
      .then(function (r) {
        return r.json().then(function (j) {
          setGraspPhase(JSON.stringify(j, null, 2));
          return j;
        });
      });
  };

  /** Se health dice worker down ma EC2 control è disponibile, avvia istanza e ripete health. */
  window.operatorsGraspEnsureWorker = function (health) {
    if (health && health.worker_reachable) {
      return Promise.resolve(health);
    }
    if (health && health.ec2_control_available && health.cloud_mode) {
      return window.operatorsGraspEc2Start().then(function () {
        return window.operatorsGraspHealth();
      });
    }
    return Promise.reject(new Error("Grasp health: worker non disponibile"));
  };

  window.operatorsGraspHealth = function () {
    setGraspPhase("GET /api/grasp/health…");
    window.operatorsGraspProgressUi({ step: 0, label: "Verifica worker grasp…" });
    return fetch(api("/api/grasp/health?_=" + Date.now()), { cache: "no-store" })
      .then(function (r) {
        return r.json();
      })
      .then(function (j) {
        setGraspPhase(JSON.stringify(j, null, 2));
        if (j && j.validation_ui) {
          window.operatorsGraspRenderValidation(j.validation_ui);
        }
        if (j && j.ok !== false && j.worker_reachable) {
          window.operatorsGraspProgressUi({ step: 1, label: "Worker OK — invio piano…" });
          return j;
        }
        window.operatorsGraspProgressUi({ step: 0, label: "Worker in errore o non raggiungibile", error: true });
        if (j && j.validation_ui) {
          window.operatorsGraspRenderValidation(j.validation_ui);
        }
        return Promise.reject(new Error("Grasp health: worker non disponibile"));
      })
      .catch(function (e) {
        var msg = String((e && e.message) || e);
        if (msg.indexOf("Grasp health:") === 0) {
          return Promise.reject(e);
        }
        setGraspPhase("Errore: " + msg);
        window.operatorsGraspProgressUi({ step: 0, label: msg, error: true });
        return Promise.reject(e);
      });
  };

  window.operatorsGraspPlan = function () {
    var body;
    try {
      body = graspPlanObject();
    } catch (e) {
      setGraspPhase(String(e));
      return Promise.reject(e);
    }
    setGraspPhase("POST /api/grasp/plan…");
    window.operatorsGraspProgressUi({ step: 1, label: "Invio piano al worker OpenVLA / AnyGrasp…" });
    return fetch(api("/api/grasp/plan"), {
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
        if (o.j && o.j.validation_ui) {
          window.operatorsGraspRenderValidation(o.j.validation_ui);
        }
        var ass = o.j && o.j.grasp_assessment;
        if (ass && ass.label_it) {
          window.operatorsGraspProgressUi({
            step: 2,
            label: "Piano · " + ass.label_it + (ass.execution_allowed ? " · eseguibile" : " · solo preview"),
          });
        }
        setGraspPhase(JSON.stringify(o.j, null, 2) + "\nHTTP " + o.status);
        if (o.status >= 400 || (o.j && o.j.ok === false)) {
          if (o.j && o.j.reason === "worker_stub_plan_rejected") {
            window.operatorsGraspProgressUi({
              step: 1,
              label: "STUB rifiutato — worker deve essere planner/auto",
              error: true,
            });
          }
          window.operatorsGraspProgressUi({
            step: 1,
            label: "Piano rifiutato (HTTP " + o.status + ")",
            error: true,
          });
          throw new Error("Grasp plan: HTTP " + o.status);
        }
        window.operatorsGraspProgressUi({
          step: 2,
          label: "Piano pronto — anteprima / overlay",
        });
        fetch(api("/api/arm/last_plan_debug?_=" + Date.now()), { cache: "no-store" })
          .then(function (r) {
            return r.json();
          })
          .then(function (dbg) {
            if (!dbg || dbg.reason === "no_cached_plan") {
              return;
            }
            var extra = "";
            if (dbg.stub_plan) {
              extra = " · STUB — IK pericoloso / fittizio";
            } else if (dbg.ik_executable && dbg.openvla_d1_executable) {
              extra = " · Braccio: IK o FK D1 possibili";
            } else if (dbg.ik_executable) {
              extra = " · Braccio: prova IK (FK D1 no)";
            } else if (dbg.openvla_d1_executable) {
              extra = " · Braccio: prova FK D1 (IK no)";
            } else {
              extra = " · Braccio: piano senza target IK né d1_rad — non eseguibile";
            }
            window.operatorsGraspProgressUi({
              step: 2,
              label: "Piano pronto" + extra,
            });
            if (dbg.hint_motion_it && window.operatorsAppendIssueLog) {
              window.operatorsAppendIssueLog("warn", "Presa · piano", dbg.hint_motion_it, dbg);
            }
          })
          .catch(function () {});
        var used =
          o.j && typeof o.j === "object"
            ? o.j.image_url_used || o.j.image_url
            : null;
        if (used && typeof used === "string") {
          window.__operatorsVlaInputUrl = used;
        }
        if (window.operatorsUpdateVlaInputLabel) {
          if (o.j && o.j.image_url_used) {
            window.operatorsUpdateVlaInputLabel(
              "VLA: processato JPEG → " + o.j.image_url_used + " (campo image_url_used nel JSON sotto)."
            );
          } else if (used) {
            window.operatorsUpdateVlaInputLabel("Immagine usata (se nota): " + used);
          }
        }
        window.operatorsApplyGraspVisualization(o.j);
        window.operatorsRefreshGraspPreviewFrame();
        var img = document.getElementById("graspWristImg");
        if (img) {
          img.onload = function () {
            window.operatorsGraspDrawOverlay();
          };
        }
        return o;
      })
      .catch(function (e) {
        var msg = String((e && e.message) || e);
        if (msg.indexOf("Grasp plan:") === 0) {
          return Promise.reject(e);
        }
        setGraspPhase("Errore: " + msg);
        window.operatorsGraspProgressUi({ step: 1, label: msg, error: true });
        return Promise.reject(e);
      });
  };

  /** Rigenera JSON da campi UI → health worker → POST plan. Barra avanzamento su #graspProgress*. */
  window.operatorsGraspStartPresaPipeline = function () {
    if (window.__operatorsGraspPipelineBusy) {
      setGraspPhase("Pipeline già in corso — attendi o premi Azzera.");
      return;
    }
    operatorsGraspPresetOpenvlaSelected();
    var body;
    try {
      body = graspPlanObject();
    } catch (e) {
      window.operatorsGraspProgressUi({ step: 0, label: String(e.message || e), error: true });
      return;
    }
    if (!body || typeof body.image_url !== "string" || !String(body.image_url).trim()) {
      window.operatorsGraspProgressUi({
        step: 0,
        label: "Scegli camera (o vla_frame) e istruzione — JSON incompleto",
        error: true,
      });
      setGraspPhase("Imposta log.0 / log.6 / vla e campo EN sotto, poi riprova.");
      return;
    }
    window.__operatorsGraspPipelineBusy = true;
    window.operatorsGraspHealth()
      .then(function (h) {
        return window.operatorsGraspEnsureWorker(h);
      })
      .then(function () {
        return window.operatorsGraspPlan();
      })
      .catch(function () {
        /* UI già impostata da health/plan */
      })
      .finally(function () {
        window.__operatorsGraspPipelineBusy = false;
      });
  };

  function graspFrontSetBadge(state, text) {
    var b = document.getElementById("graspFrontBadge");
    if (!b) {
      return;
    }
    b.classList.remove("is-idle", "is-ok", "is-fail", "is-run");
    b.classList.add(state);
    b.textContent = text;
  }

  function graspFrontDrawDetection(det) {
    var img = document.getElementById("graspFrontImg");
    var cv = document.getElementById("graspFrontCanvas");
    if (!img || !cv) {
      return;
    }
    var w = img.clientWidth || img.naturalWidth || 320;
    var h = img.clientHeight || img.naturalHeight || 240;
    cv.width = w;
    cv.height = h;
    var ctx = cv.getContext("2d");
    if (!ctx) {
      return;
    }
    ctx.clearRect(0, 0, w, h);
    if (!det || !det.ok || !det.bbox_xyxy || det.bbox_xyxy.length < 4) {
      return;
    }
    var fw = (det.frame_size_px && det.frame_size_px[0]) || img.naturalWidth || 640;
    var fh = (det.frame_size_px && det.frame_size_px[1]) || img.naturalHeight || 480;
    var sx = w / Math.max(fw, 1);
    var sy = h / Math.max(fh, 1);
    var bx = det.bbox_xyxy;
    var x0 = bx[0] * sx;
    var y0 = bx[1] * sy;
    var x1 = bx[2] * sx;
    var y1 = bx[3] * sy;
    ctx.lineWidth = 3;
    ctx.strokeStyle = "rgba(34, 197, 94, 0.96)";
    ctx.strokeRect(x0, y0, x1 - x0, y1 - y0);
    var ccx = (x0 + x1) / 2;
    var ccy = (y0 + y1) / 2;
    ctx.beginPath();
    ctx.moveTo(ccx - 9, ccy);
    ctx.lineTo(ccx + 9, ccy);
    ctx.moveTo(ccx, ccy - 9);
    ctx.lineTo(ccx, ccy + 9);
    ctx.stroke();
    var conf = Math.round((Number(det.confidence) || 0) * 100);
    var label = (det.label || "oggetto") + " · " + conf + "%";
    ctx.font = "13px Inter,sans-serif";
    var tw = ctx.measureText(label).width + 12;
    var ty = Math.max(0, y0 - 19);
    ctx.fillStyle = "rgba(22, 101, 52, 0.92)";
    ctx.fillRect(x0, ty, tw, 19);
    ctx.fillStyle = "#dcfce7";
    ctx.fillText(label, x0 + 6, ty + 14);
  }

  window.operatorsGraspFrontLoadFrame = function () {
    return new Promise(function (resolve) {
      var img = document.getElementById("graspFrontImg");
      if (!img) {
        resolve(false);
        return;
      }
      var sr = (window.__OPERATORS_SCRIPT_ROOT__ || "").replace(/\/$/, "");
      img.onload = function () {
        resolve(true);
      };
      img.onerror = function () {
        resolve(false);
      };
      img.src = sr + "/api/robot/camera/6.jpg?ts=" + Date.now();
    });
  };

  function graspFrontApplyDetection(det) {
    graspFrontDrawDetection(det);
    if (det && det.ok) {
      var conf = Math.round((Number(det.confidence) || 0) * 100);
      var be = String(det.backend || "");
      var heur = be.indexOf("floor") >= 0 || be.indexOf("contour") >= 0 || be.indexOf("classic") >= 0;
      graspFrontSetBadge("is-ok", "OGGETTO VISTO · " + conf + "%" + (heur ? " (euristico)" : ""));
    } else {
      graspFrontSetBadge("is-fail", "oggetto NON rilevato — riposiziona oggetto/cane");
    }
  }

  /** Rileva l'oggetto sulla camera frontale (logical 6) e lo disegna con bbox + badge. */
  window.operatorsGraspFrontDetect = function () {
    var btn = document.getElementById("graspFrontDetectBtn");
    if (btn) {
      btn.disabled = true;
    }
    graspFrontSetBadge("is-run", "analisi frontale…");
    return window.operatorsGraspFrontLoadFrame()
      .then(function () {
        return fetch(api("/api/vision/box_detect?camera=6&_=" + Date.now()), { cache: "no-store" }).then(function (r) {
          return r.json();
        });
      })
      .then(function (j) {
        graspFrontApplyDetection(j && j.detection);
        return j;
      })
      .catch(function (e) {
        graspFrontSetBadge("is-fail", "errore: " + String(e));
      })
      .finally(function () {
        if (btn) {
          btn.disabled = false;
        }
      });
  };

  function graspFront3dSummary(plan) {
    var el = document.getElementById("graspFront3d");
    if (!el) {
      return;
    }
    var pts = plan && (plan.operators_grasp_points_base_link_m || plan.grasp_points_base_link_m);
    var g = plan && (plan.grasp_center_base_link_m || (plan.targets && plan.targets.grasp));
    if (g && g.length >= 3) {
      el.innerHTML =
        "Punti 3D presa (base_link): centro <strong>x=" +
        Number(g[0]).toFixed(3) +
        " y=" +
        Number(g[1]).toFixed(3) +
        " z=" +
        Number(g[2]).toFixed(3) +
        " m</strong>" +
        (pts && pts.length ? " · " + pts.length + " punti" : "") +
        " → marker visibili nella tab <strong>3D</strong>.";
    } else if (pts && pts.length) {
      el.innerHTML = "Punti 3D presa: <strong>" + pts.length + " punti</strong> → vedi marker nella tab <strong>3D</strong>.";
    }
  }

  var _GRASP_FULL_STEP_ORDER = ["front_detect", "goto_start", "start_pose_check", "wrist_plan", "execute_phased"];

  window.operatorsGraspRefreshStartPoseBadge = function () {
    var badge = document.getElementById("graspStartPoseBadge");
    if (!badge) {
      return Promise.resolve(null);
    }
    badge.className = "op-grasp-front-badge is-run";
    badge.textContent = "START: verifica…";
    var variant = window.operatorsStartVariant();
    return fetch(api("/api/arm/at_start_check?start_variant=" + encodeURIComponent(variant)), { cache: "no-store" })
      .then(function (r) {
        return r.json().then(function (j) {
          return { status: r.status, j: j };
        });
      })
      .then(function (o) {
        var j = o.j || {};
        var vLabel = variant === "frontal" ? "frontale" : "laterale";
        if (j.ok) {
          badge.className = "op-grasp-front-badge is-ok";
          badge.textContent =
            "START " + vLabel + ": OK (max err " + (j.max_error_deg != null ? j.max_error_deg : "?") + "°)";
        } else {
          badge.className = "op-grasp-front-badge is-fail";
          badge.textContent =
            "START " + vLabel + ": NON in posa" +
            (j.max_error_deg != null ? " (max err " + j.max_error_deg + "°)" : "") +
            " — usa «Prendi oggetto» (non dry-run)";
        }
        return j;
      })
      .catch(function () {
        badge.className = "op-grasp-front-badge is-fail";
        badge.textContent = "START: verifica non disponibile";
      });
  };

  function graspFullSetStepClass(stepName, cls) {
    var li = document.querySelector('#graspFullSteps [data-step="' + stepName + '"]');
    if (!li) {
      return;
    }
    li.classList.remove("is-idle", "is-run", "is-ok", "is-fail", "is-skip");
    li.classList.add(cls);
  }

  function graspFullResetStoryboard(runningFirst) {
    var i;
    for (i = 0; i < _GRASP_FULL_STEP_ORDER.length; i++) {
      graspFullSetStepClass(_GRASP_FULL_STEP_ORDER[i], "is-idle");
    }
    if (runningFirst) {
      graspFullSetStepClass(_GRASP_FULL_STEP_ORDER[0], "is-run");
    }
  }

  function graspFullStepLabel(stepName, fallback) {
    var li = document.querySelector('#graspFullSteps [data-step="' + stepName + '"] .op-grasp-story-txt');
    return (li && li.textContent) || fallback || stepName;
  }

  function graspFullRenderSteps(out) {
    var steps = (out && out.steps) || [];
    var byName = {};
    var i;
    for (i = 0; i < steps.length; i++) {
      if (steps[i] && steps[i].step) {
        byName[steps[i].step] = steps[i];
      }
    }
    for (i = 0; i < _GRASP_FULL_STEP_ORDER.length; i++) {
      var name = _GRASP_FULL_STEP_ORDER[i];
      var s = byName[name];
      var li = document.querySelector('#graspFullSteps [data-step="' + name + '"]');
      if (!li) {
        continue;
      }
      var txt = li.querySelector(".op-grasp-story-txt");
      if (!s) {
        graspFullSetStepClass(name, "is-idle");
        continue;
      }
      var detail = s.label_it || s.hint_it || s.reason || "";
      if (s.skipped) {
        graspFullSetStepClass(name, "is-skip");
      } else if (s.ok) {
        graspFullSetStepClass(name, "is-ok");
      } else {
        graspFullSetStepClass(name, "is-fail");
      }
      if (txt && detail) {
        txt.textContent = graspFullStepLabel(name, name).split(" — ")[0] + " — " + detail;
      }
      if (name === "front_detect" && s) {
        var fdet = s.detection;
        if (fdet && fdet.ok) {
          window.operatorsGraspFrontLoadFrame().then(function () {
            graspFrontApplyDetection(fdet);
          });
        } else {
          graspFrontSetBadge("is-fail", "frontale: oggetto NON rilevato");
        }
      }
    }
    var failed = out && out.failed_step;
    if (failed) {
      graspFullSetStepClass(failed, "is-fail");
    }
  }

  /**
   * Sequenza presa completa (1 comando): frontale → START → pose estimation polso → presa a fasi.
   * @param {boolean} [forceDryRun] se true forza l'anteprima senza muovere il braccio.
   */
  window.operatorsGraspRunFull = function (forceDryRun) {
    var instInp = document.getElementById("graspFullInstruction");
    var instruction =
      (instInp && instInp.value && String(instInp.value).trim()) || "prendi l'oggetto davanti al braccio";
    var dryCb = document.getElementById("graspFullDryRun");
    var dryRun = forceDryRun === true || !!(dryCb && dryCb.checked);
    var summary = document.getElementById("graspFullSummary");
    var btn = document.getElementById("graspFullRunBtn");

    if (!dryRun) {
      if (
        !window.confirm(
          "SEQUENZA COMPLETA: fold + START, poi piano dal polso e PRESA («" +
            instruction +
            "»). Area libera e cane stabile? Continuare?"
        )
      ) {
        return;
      }
    } else if (
      !window.confirm(
        "ANTEPRIMA (dry-run): il braccio NON si muove. Se non sei già in START, il flusso si ferma prima del piano polso. Continuare?"
      )
    ) {
      return;
    }

    var body = { instruction: instruction };
    if (!dryRun) {
      body.confirm = "RUN_FULL_GRASP";
    }
    if (btn) {
      btn.disabled = true;
    }
    graspFullResetStoryboard(true);
    graspFrontSetBadge("is-run", "frontale: analisi in corso…");
    window.operatorsGraspFrontLoadFrame();
    if (summary) {
      summary.textContent = dryRun
        ? "Anteprima in corso (nessun movimento braccio)…"
        : "Sequenza in corso: frontale → START → polso → presa…";
    }
    setGraspPhase("POST /api/grasp/run_full…");
    window.operatorsGraspProgressUi({
      step: 1,
      label: dryRun ? "Anteprima sequenza completa…" : "Sequenza presa completa…",
      indeterminate: true,
    });
    fetch(api("/api/grasp/run_full"), {
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
        var out = o.j || {};
        setGraspPhase(JSON.stringify(out, null, 2) + "\nHTTP " + o.status);
        graspFullRenderSteps(out);
        if (out.plan && typeof out.plan === "object") {
          window.__operatorsLastGraspPlan = out.plan;
          window.operatorsApplyGraspVisualization(out.plan);
          window.operatorsRefreshGraspPreviewFrame();
          graspFront3dSummary(out.plan);
        }
        if (out.validation_ui) {
          window.operatorsGraspRenderValidation(out.validation_ui);
        }
        window.operatorsGraspRefreshStartPoseBadge();
        if (out.ok) {
          window.operatorsGraspProgressUi({
            step: 4,
            label: dryRun ? "Anteprima completata" : "Presa completata",
          });
          if (summary) {
            summary.textContent = dryRun
              ? "Anteprima OK — controlla overlay e validazione, poi lancia senza dry-run."
              : "Sequenza completata: verifica fisicamente la presa.";
          }
        } else {
          var fs = out.failed_step || "?";
          window.operatorsGraspProgressUi({
            step: fs === "execute_phased" ? 3 : fs === "wrist_plan" ? 2 : 1,
            label: "Sequenza interrotta su «" + fs + "»",
            error: true,
          });
          if (summary) {
            var failedStep = (out.steps || []).filter(function (s) {
              return s && s.step === out.failed_step;
            })[0];
            summary.textContent =
              "Interrotta su «" + fs + "»: " + ((failedStep && (failedStep.hint_it || failedStep.reason)) || "vedi JSON sotto.");
          }
        }
      })
      .catch(function (e) {
        setGraspPhase("Errore: " + String(e));
        window.operatorsGraspProgressUi({ step: 1, label: String(e), error: true });
        if (summary) {
          summary.textContent = "Errore di rete: " + String(e);
        }
      })
      .finally(function () {
        if (btn) {
          btn.disabled = false;
        }
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
    window.operatorsGraspProgressUi({ step: 3, label: "Execute sul worker (nessun DDS braccio)…" });
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
        if (o.status >= 400 || (o.j && o.j.ok === false)) {
          window.operatorsGraspProgressUi({ step: 3, label: "Execute fallito", error: true });
        } else {
          window.operatorsGraspProgressUi({
            step: 4,
            label: "Execute OK — usa i pulsanti D1 sotto se devi muovere il braccio",
          });
        }
        if (o.j && typeof o.j === "object") {
          var exUsed = o.j.image_url_used || o.j.image_url;
          if (exUsed && typeof exUsed === "string") {
            window.__operatorsVlaInputUrl = exUsed;
          }
          if (window.operatorsUpdateVlaInputLabel && o.j.image_url_used) {
            window.operatorsUpdateVlaInputLabel("VLA (execute): " + o.j.image_url_used);
          }
          window.operatorsApplyGraspVisualization(o.j);
          window.operatorsRefreshGraspPreviewFrame();
        }
      })
      .catch(function (e) {
        setGraspPhase("Errore: " + String(e));
        window.operatorsGraspProgressUi({ step: 3, label: String(e), error: true });
      });
  };

  window.operatorsGraspExecutePhased = function () {
    var lp = window.__operatorsLastGraspPlan;
    var vui = lp && lp.validation_ui;
    if (vui && !vui.can_execute_phased) {
      alert(vui.banner_it || "Validazione non OK — controlla la tabella sopra.");
      return;
    }
    var ass = lp && lp.grasp_assessment;
    if (ass && ass.execution_allowed === false && ass.tier !== "heuristic_preview_only") {
      if (
        !window.confirm(
          "Assessment: " +
            (ass.label_it || ass.tier) +
            " — esecuzione non consigliata. Continuare comunque?"
        )
      ) {
        return;
      }
    } else if (
      !window.confirm(
        "Eseguire sequenza a fasi (pre_grasp → approach → grasp → lift) dall'ultimo piano? Area libera?"
      )
    ) {
      return;
    }
    setGraspPhase("POST /api/grasp/execute_phased…");
    window.operatorsGraspProgressUi({ step: 3, label: "Sequenza presa a fasi…" });
    fetch(api("/api/grasp/execute_phased"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: "EXECUTE_PHASED_GRASP" }),
    })
      .then(function (r) {
        return r.json().then(function (j) {
          return { status: r.status, j: j };
        });
      })
      .then(function (o) {
        setGraspPhase(JSON.stringify(o.j, null, 2) + "\nHTTP " + o.status);
        var gvTxt = "";
        try {
          var sl = (o.j && o.j.step_log) || [];
          for (var i = 0; i < sl.length; i++) {
            var gv = sl[i] && sl[i].grasp_verify;
            if (gv && gv.ok) {
              gvTxt = " · presa: " + (gv.grasp_detected ? "OK oggetto" : "a vuoto") +
                " (pinza " + Number(gv.gripper_deg_achieved).toFixed(1) + "°)";
            } else if (gv && !gv.ok) {
              gvTxt = " · verifica presa n/d";
            }
          }
        } catch (e) {}
        if (o.j && o.j.ok) {
          window.operatorsGraspProgressUi({ step: 4, label: "Sequenza completata (" + (o.j.stages_run || "?") + " fasi)" + gvTxt });
        } else {
          window.operatorsGraspProgressUi({ step: 3, label: "Sequenza fallita" + gvTxt, error: true });
        }
      })
      .catch(function (e) {
        setGraspPhase("Errore: " + String(e));
        window.operatorsGraspProgressUi({ step: 3, label: String(e), error: true });
      });
  };

  window.operatorsExecuteLastPlanIkArm = function () {
    var lp = window.__operatorsLastGraspPlan;
    var vui = lp && lp.validation_ui;
    if (vui && !vui.can_execute_ik) {
      alert(vui.banner_it || "IK bloccato — validazione non OK.");
      return;
    }
    var ass = lp && lp.grasp_assessment;
    if (ass && ass.execution_allowed === false && lp.backend !== "stub") {
      if (
        !window.confirm(
          "Assessment: " + (ass.label_it || "") + " — IK singolo non validato 3D. Continuare?"
        )
      ) {
        return;
      }
    }
    if (lp && lp.backend === "stub") {
      if (
        !window.confirm(
          "ATTENZIONE: ultimo piano è STUB — il punto 3D NON è dalla camera. IK può andare dove non ti aspetti (es. verso il cane). Continuare?"
        )
      ) {
        return;
      }
    } else if (
      !window.confirm(
        "MUOVERE il braccio D1 con IK verso il punto 3D dell'ultimo piano (grasp_display / FK tip)? Area libera?"
      )
    ) {
      return;
    }
    setGraspPhase("POST /api/arm/execute_last_plan_ik…");
    window.operatorsGraspProgressUi({ step: 3, label: "Movimento braccio (IK)…" });
    fetch(api("/api/arm/execute_last_plan_ik"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: "MOVE_IK_CACHED" }),
    })
      .then(function (r) {
        return r.json().then(function (j) {
          return { status: r.status, j: j };
        });
      })
      .then(function (o) {
        setGraspPhase(JSON.stringify(o.j, null, 2) + "\nHTTP " + o.status);
        if (o.j && o.j.ok !== false) {
          window.operatorsGraspProgressUi({ step: 4, label: "IK completato (verifica fisica)" });
        } else {
          window.operatorsGraspProgressUi({ step: 3, label: "IK fallito", error: true });
        }
      })
      .catch(function (e) {
        setGraspPhase("Errore: " + String(e));
        window.operatorsGraspProgressUi({ step: 3, label: String(e), error: true });
      });
  };

  window.operatorsOpenvlaExecuteD1Arm = function () {
    var lp = window.__operatorsLastGraspPlan;
    if (lp && lp.backend === "stub") {
      if (
        !window.confirm(
          "ATTENZIONE: ultimo piano è STUB — non contiene giunti VLA reali. FK potrebbe non essere valido. Continuare?"
        )
      ) {
        return;
      }
    } else if (!window.confirm("MUOVERE il braccio D1 verso l'ultimo piano OpenVLA (giunti FK)? Area libera?")) {
      return;
    }
    setGraspPhase("POST /api/arm/openvla_execute_last_plan_d1…");
    window.operatorsGraspProgressUi({ step: 3, label: "Movimento braccio (FK OpenVLA)…" });
    fetch(api("/api/arm/openvla_execute_last_plan_d1"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: "MOVE_D1_OPENVLA" }),
    })
      .then(function (r) {
        return r.json().then(function (j) {
          return { status: r.status, j: j };
        });
      })
      .then(function (o) {
        setGraspPhase(JSON.stringify(o.j, null, 2) + "\nHTTP " + o.status);
        if (o.j && o.j.ok !== false) {
          window.operatorsGraspProgressUi({ step: 4, label: "FK D1 completato" });
        } else {
          window.operatorsGraspProgressUi({ step: 3, label: "FK D1 fallito", error: true });
        }
      })
      .catch(function (e) {
        setGraspPhase("Errore: " + String(e));
        window.operatorsGraspProgressUi({ step: 3, label: String(e), error: true });
      });
  };

  window.operatorsWireMjpegStream = operatorsWireMjpegStream;
  window.operatorsMjpegLoadingSet = operatorsMjpegLoadingSet;

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

  function graspFrontAutoLoad() {
    if (document.getElementById("graspFrontImg") && window.operatorsGraspFrontLoadFrame) {
      window.operatorsGraspFrontLoadFrame();
    }
  }

  /* ---- Switch a 3 flussi: Locale · VLA AWS · Teaching --------------------- */
  var _GRASP_FLOW_CAPTIONS = {
    guided:
      "<strong>Locale:</strong> sequenza completa in un comando, piano dal polso sulla NX (Orbbec metrico / GraspGen). È il flusso del laboratorio, <strong>senza</strong> OpenVLA; ricade sul worker solo se il locale fallisce.",
    vla:
      "<strong>VLA AWS:</strong> piano dal worker EC2 (OpenVLA) e poi esecuzione manuale (fasi / IK / FK). Health worker → Piano VLA → Esegui. Richiede worker raggiungibile + validazione verde.",
    teach:
      "<strong>Teaching:</strong> coach GPT-nano (OpenAI vision) passo-passo, con memoria e feedback. Usalo per recovery dopo un fallimento o per insegnare micro-correzioni, non come piano primario."
  };

  function graspSetFlow(flow) {
    if (!_GRASP_FLOW_CAPTIONS[flow]) {
      flow = "guided";
    }
    var btns = document.querySelectorAll(".op-grasp-flow-btn");
    var i;
    for (i = 0; i < btns.length; i++) {
      var on = btns[i].getAttribute("data-grasp-flow") === flow;
      btns[i].classList.toggle("is-active", on);
      btns[i].setAttribute("aria-selected", on ? "true" : "false");
    }
    var panels = document.querySelectorAll("[data-grasp-flow-panel]");
    for (i = 0; i < panels.length; i++) {
      var match = panels[i].getAttribute("data-grasp-flow-panel") === flow;
      panels[i].hidden = !match;
    }
    var cap = document.getElementById("graspFlowCaption");
    if (cap) {
      cap.innerHTML = _GRASP_FLOW_CAPTIONS[flow];
    }
    // Validazione + log riguardano l'esecuzione del piano (locale/VLA): nascoste nel teaching.
    var planScoped = ["graspValidationPanel", "graspLogDetails", "graspProgressCard"];
    var hideForTeach = flow === "teach";
    for (i = 0; i < planScoped.length; i++) {
      var node = document.getElementById(planScoped[i]);
      if (node) {
        node.hidden = hideForTeach;
      }
    }
    try {
      window.localStorage.setItem("go2GraspFlow", flow);
    } catch (e) {
      /* storage non disponibile: ignora */
    }
  }
  window.operatorsGraspSetFlow = graspSetFlow;

  function graspFlowInit() {
    var btns = document.querySelectorAll(".op-grasp-flow-btn");
    if (!btns.length) {
      return;
    }
    var i;
    for (i = 0; i < btns.length; i++) {
      btns[i].addEventListener("click", function () {
        graspSetFlow(this.getAttribute("data-grasp-flow"));
      });
    }
    var saved = null;
    try {
      saved = window.localStorage.getItem("go2GraspFlow");
    } catch (e) {
      saved = null;
    }
    graspSetFlow(saved || "guided");
  }

  function graspTabInit() {
    graspFrontAutoLoad();
    graspFlowInit();
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", graspTabInit);
  } else {
    graspTabInit();
  }

})();
