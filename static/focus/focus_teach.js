(function () {
  "use strict";

  var SR = window.__FOCUS_SCRIPT_ROOT__ || "";
  var logLines = [];
  var activeVariant = "j90";
  var STEP_INDEX = { move90: 0, rgbd: 1, ik: 2, execute: 3, verify: 4, teach: 5 };
  var SVC_LABELS = {
    comando: "comando",
    detect: "detect",
    depth: "depth",
    ik: "IK",
    execute: "execute",
  };
  var svcState = {
    comando: "idle",
    detect: "idle",
    depth: "idle",
    ik: "idle",
    execute: "idle",
  };
  var SLOT_DEFS = [
    { id: "wrist_rgb", camera: "wrist", kind: "rgb", label: "Polso RGB" },
    { id: "front_rgb", camera: "front", kind: "rgb", label: "Frontale RGB" },
  ];
  var STREAM_STATE_KEY = "focus.teach.streamGrid.v1";
  var STREAM_CAROUSEL_KEY = "focus.teach.streamCarousel.v1";
  var streamCatalog = null;
  var streamState = loadStreamState();
  var streamCarousel = loadCarouselState();
  var TUNE_HISTORY_KEY = "focus.teach.tuningHistory.v1";
  var tuneHistory = loadTuneHistory();
  var detectorCfg = null;

  function resetSvcAck() {
    Object.keys(SVC_LABELS).forEach(function (k) { setSvcAck(k, "idle"); });
    setExecutePhase("waiting");
  }

  function setSvcAck(key, state) {
    svcState[key] = state || "idle";
    var el = $("svcAck" + key.charAt(0).toUpperCase() + key.slice(1));
    if (!el && key === "ik") el = $("svcAckIk");
    if (!el) el = document.querySelector('[data-svc="' + key + '"]');
    if (!el) return;
    var sym = { idle: " ", run: "~", ok: "\u2713", err: "\u2717" };
    el.className = "svc-ack" + (state && state !== "idle" ? " " + state : "");
    el.textContent = "[" + (sym[state] || "?") + "] " + (SVC_LABELS[key] || key);
  }

  function setExecutePhase(phase) {
    var el = $("svcExecLine");
    if (!el) return;
    el.className = "svc-exec-line";
    if (phase === "running") el.classList.add("running");
    if (phase === "completed") el.classList.add("completed");
    if (phase === "failed") el.classList.add("failed");
    el.textContent = "EXECUTE: " + phase + "...";
  }

  function applyFullSequenceSteps(steps) {
    if (!Array.isArray(steps)) return;
    steps.forEach(function (st) {
      if (st.phase === "move_90") setSvcAck("comando", st.ok ? "ok" : "err");
      if (st.phase === "rgbd_scan_ik") {
        setSvcAck("detect", st.ok ? "ok" : "err");
        setSvcAck("depth", st.ok ? "ok" : "err");
        setSvcAck("ik", st.ok ? "ok" : "err");
      }
      if (st.phase === "execute_phased") {
        setSvcAck("ik", st.ok ? "ok" : "err");
        setSvcAck("execute", st.ok ? "ok" : "err");
        setExecutePhase(st.ok ? "completed" : "failed");
      }
      if (st.phase === "close" && st.ok) setExecutePhase("completed");
      if (st.phase === "lift") {
        setExecutePhase(st.ok ? "completed" : "failed");
      }
    });
  }

  function executeAckReady() {
    return svcState.execute === "ok" && svcState.ik === "ok";
  }

  // #region agent log
  var DBG = {
    sessionId: "7c69a6",
    ingest: "http://127.0.0.1:7916/ingest/d92fe5a3-25e7-4f47-9434-0f427e3439d8",
    seq: 0,
    activeButton: null,
    buttons: {
      btnTeachStatus: { label: "Aggiorna stato", apis: ["GET /api/focus/status", "GET /api/pick/teach/samples", "GET /api/arm/status"] },
      btnSnapshot: { label: "Foto + detect", apis: ["POST /api/pick/snapshot"] },
      btnScanLeft90: { label: "Muovi 90 sinistra", apis: ["POST /api/presets/scan/goto variant=j90_left"] },
      btnScanRight90: { label: "Muovi 90 destra", apis: ["POST /api/presets/scan/goto variant=j90"] },
      btnStartGrasp: { label: "Inizia presa", apis: ["POST /api/pick/grasp/close_and_lift"] },
      btnAutoGrasp: { label: "Presa automatica", apis: ["POST /api/pick/full_sequence"] },
      btnGripperClose: { label: "Chiudi pinza", apis: ["POST /api/pick/gripper/close"] },
      btnTeachPosition: { label: "Teaching posizione", apis: ["POST /api/arm/joints/release", "GET /api/arm/servo_snapshot", "POST /api/pick/teach/finish", "POST /api/pick/teach/build_model"] },
      btnMarkGraspFailed: { label: "Presa fallita teach", apis: ["POST /api/arm/joints/release", "GET /api/arm/servo_snapshot", "POST /api/pick/teach/finish"] },
      btnManualTeachLeft: { label: "Teaching completo sinistra", apis: ["POST /api/presets/scan/goto", "POST /api/pick/snapshot", "POST /api/arm/joints/release", "POST /api/pick/teach/finish"] },
      btnManualTeachRight: { label: "Teaching completo destra", apis: ["POST /api/presets/scan/goto", "POST /api/pick/snapshot", "POST /api/arm/joints/release", "POST /api/pick/teach/finish"] },
      btnBuildTeachModel: { label: "Ricrea modello teach", apis: ["POST /api/pick/teach/build_model"] },
      btnTeachCancel: { label: "Annulla flusso", apis: ["POST /api/grasp/teach_cancel"] },
      btnGraspLeft: { label: "Solo avvicina sinistra", apis: ["POST /api/pick/grasp/goto scan_variant=j90_left"] },
      btnGraspRight: { label: "Solo avvicina destra", apis: ["POST /api/pick/grasp/goto scan_variant=j90"] },
      btnArmCouple: { label: "Coppia ON", apis: ["POST /api/arm/joints/couple"] },
      btnArmRelease: { label: "Release giunti", apis: ["POST /api/arm/joints/release"] },
      btnGotoHome: { label: "Torna Folded (ZERO)", apis: ["POST /api/arm/goto_true_zero"] },
      btnClearLog: { label: "Pulisci log", apis: [] },
    },
  };

  function dbgBtn(buttonId, phase, data, hypothesisId) {
    var meta = DBG.buttons[buttonId] || { label: buttonId, apis: [] };
    var row = {
      sessionId: DBG.sessionId,
      id: "btn_" + String(++DBG.seq),
      timestamp: Date.now(),
      location: "focus_teach.js:" + (buttonId || "unknown"),
      message: phase,
      data: Object.assign(
        {
          buttonId: buttonId,
          buttonLabel: meta.label,
          apis: meta.apis,
          activeVariant: activeVariant,
        },
        data || {}
      ),
      hypothesisId: hypothesisId || "BTN",
    };
    fetch(DBG.ingest, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Debug-Session-Id": DBG.sessionId },
      body: JSON.stringify(row),
    }).catch(function () {});
    fetch(api("/api/focus/debug/log"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(row),
    }).catch(function () {});
  }
  // #endregion

  function api(path) {
    if (!path) path = "/";
    if (path.charAt(0) !== "/") path = "/" + path;
    return SR + path;
  }

  function $(id) { return document.getElementById(id); }

  function numVal(id, fallback) {
    var el = $(id);
    if (!el) return fallback;
    var v = Number(el.value);
    return Number.isFinite(v) ? v : fallback;
  }

  function escHtml(v) {
    return String(v)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function loadStreamState() {
    var base = { slots: {} };
    SLOT_DEFS.forEach(function (slot) {
      base.slots[slot.id] = { label: slot.label, panel: null, camera: slot.camera, kind: slot.kind };
    });
    try {
      var raw = localStorage.getItem(STREAM_STATE_KEY);
      if (!raw) return base;
      var parsed = JSON.parse(raw);
      SLOT_DEFS.forEach(function (slot) {
        if (parsed && parsed.slots && parsed.slots[slot.id]) {
          base.slots[slot.id] = Object.assign({}, base.slots[slot.id], parsed.slots[slot.id]);
        }
      });
      return base;
    } catch (e) {
      return base;
    }
  }

  function saveStreamState() {
    try { localStorage.setItem(STREAM_STATE_KEY, JSON.stringify(streamState)); } catch (e) {}
  }

  function loadCarouselState() {
    var base = { wrist: 0, front: 0 };
    try {
      var raw = localStorage.getItem(STREAM_CAROUSEL_KEY);
      if (!raw) return base;
      var parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object") {
        if (typeof parsed.wrist === "number") base.wrist = Math.max(0, parsed.wrist | 0);
        if (typeof parsed.front === "number") base.front = Math.max(0, parsed.front | 0);
      }
      return base;
    } catch (e) {
      return base;
    }
  }

  function saveCarouselState() {
    try { localStorage.setItem(STREAM_CAROUSEL_KEY, JSON.stringify(streamCarousel)); } catch (e) {}
  }

  function loadTuneHistory() {
    try {
      var raw = localStorage.getItem(TUNE_HISTORY_KEY);
      if (!raw) return [];
      var rows = JSON.parse(raw);
      return Array.isArray(rows) ? rows.slice(-40) : [];
    } catch (e) {
      return [];
    }
  }

  function saveTuneHistory() {
    try { localStorage.setItem(TUNE_HISTORY_KEY, JSON.stringify(tuneHistory.slice(-40))); } catch (e) {}
  }

  function pushTuneHistory(row) {
    tuneHistory.push(row);
    if (tuneHistory.length > 40) tuneHistory = tuneHistory.slice(tuneHistory.length - 40);
    saveTuneHistory();
    renderTuneHistory();
  }

  function preferredPanel(kind, camera) {
    var defaults = streamCatalog && streamCatalog.default_panels && streamCatalog.default_panels[camera || "wrist"];
    if (defaults && defaults[kind]) return String(defaults[kind]);
    return ({ rgb: "color", depth: "depth", ir: "ir1", meta: "ir2" }[kind]) || "color";
  }

  function streamUrl(panel, camera) {
    return api(
      "/api/pick/vision/stream.mjpg?panel=" + encodeURIComponent(panel || "color") +
      "&camera=" + encodeURIComponent(camera || "wrist") +
      "&_=" + Date.now()
    );
  }

  function cameraLabel(role) {
    return role === "front" ? "Frontale" : "Polso";
  }

  function renderDetectionMonitor(payload, sourceLabel) {
    var img = $("detectMonitorImg");
    var meta = $("detectMonitorMeta");
    if (!payload) return;
    var det = payload.last_detection || payload.detection || null;
    var preview = payload.preview_url || null;
    if (img && preview) img.src = api(preview + (preview.indexOf("?") >= 0 ? "&" : "?") + "_=" + Date.now());
    if (!meta) return;
    if (!det) {
      meta.textContent = "Nessuna detection disponibile.";
      return;
    }
    var lines = [];
    if (sourceLabel) lines.push("source: " + sourceLabel);
    lines.push("camera: " + cameraLabel((payload.detect_camera || det.detect_camera || ((payload.camera_select || {}).detect_camera) || "wrist")));
    lines.push("label: " + (det.label || "-"));
    lines.push("confidence: " + (det.confidence != null ? Number(det.confidence).toFixed(3) : "-"));
    lines.push("orientation_deg: " + (det.orientation_deg != null ? det.orientation_deg : "-"));
    lines.push("method: " + (det.detect_method || "-"));
    lines.push("grip_center_px: " + (det.grip_center_px ? JSON.stringify(det.grip_center_px) : "-"));
    lines.push("bbox_xyxy: " + (det.bbox_xyxy ? JSON.stringify(det.bbox_xyxy) : "-"));
    if (payload.depth_m != null) lines.push("depth_m: " + Number(payload.depth_m).toFixed(3));
    lines.push("at: " + (det.at || "-"));
    meta.textContent = lines.join("\n");
  }

  function applyDetectorForm(cfg) {
    if (!cfg) return;
    detectorCfg = cfg;
    var p = cfg.params || {};
    if ($("detectorMode")) $("detectorMode").value = cfg.mode || "hsv_strict";
    if ($("detLumTarget")) $("detLumTarget").value = p.D1_COLOR_BOX_COMP_TARGET_V_MED != null ? p.D1_COLOR_BOX_COMP_TARGET_V_MED : 92;
    if ($("detMinArea")) $("detMinArea").value = Math.round((p.D1_COLOR_BOX_MIN_AREA_FRAC != null ? p.D1_COLOR_BOX_MIN_AREA_FRAC : 0.004) * 1000);
    if ($("detCompGain")) $("detCompGain").value = Math.round((p.D1_COLOR_BOX_COMP_MAX_GAIN != null ? p.D1_COLOR_BOX_COMP_MAX_GAIN : 2.4) * 10);
    if ($("detCompBeta")) $("detCompBeta").value = p.D1_COLOR_BOX_COMP_BETA != null ? p.D1_COLOR_BOX_COMP_BETA : 6;
    syncDetectorRangeLabels();
    var meta = $("detectorLabMeta");
    if (meta) {
      var st = cfg.detector_status || {};
      meta.textContent = [
        "mode=" + (cfg.mode || "-") + " | backend=" + (st.pick_detect_backend || "-") + " | color_only=" + (!!st.color_only),
        "model=" + (st.model_path || "none") + " | exists=" + (!!st.model_exists),
        "comp_target_v=" + (p.D1_COLOR_BOX_COMP_TARGET_V_MED != null ? p.D1_COLOR_BOX_COMP_TARGET_V_MED : "-") +
          " | comp_gain=" + (p.D1_COLOR_BOX_COMP_MAX_GAIN != null ? p.D1_COLOR_BOX_COMP_MAX_GAIN : "-") +
          " | comp_beta=" + (p.D1_COLOR_BOX_COMP_BETA != null ? p.D1_COLOR_BOX_COMP_BETA : "-"),
      ].join("\n");
    }
  }

  function syncDetectorRangeLabels() {
    if ($("detCropBottom") && $("detCropBottomVal")) $("detCropBottomVal").textContent = String(numVal("detCropBottom", 10)) + "%";
    if ($("detLumTarget") && $("detLumTargetVal")) $("detLumTargetVal").textContent = String(numVal("detLumTarget", 92));
    if ($("detCompBeta") && $("detCompBetaVal")) $("detCompBetaVal").textContent = String(numVal("detCompBeta", 6));
    if ($("detCompGain") && $("detCompGainVal")) $("detCompGainVal").textContent = (numVal("detCompGain", 24) / 10).toFixed(1);
    if ($("detMinArea") && $("detMinAreaVal")) $("detMinAreaVal").textContent = (numVal("detMinArea", 4) / 1000).toFixed(4);
  }

  function loadDetectorConfig() {
    return json("/api/pick/detector/config?_=" + Date.now()).then(function (j) {
      applyDetectorForm(j);
      return json("/api/pick/vision/crop?_=" + Date.now()).then(function (crop) {
        var fr = (crop && crop.crop_fracs) || {};
        if ($("detCropBottom")) $("detCropBottom").value = fr.bottom != null ? Math.round(Number(fr.bottom) * 100) : 10;
        syncDetectorRangeLabels();
        return j;
      });
    }).catch(function (e) {
      var meta = $("detectorLabMeta");
      if (meta) meta.textContent = "Errore config detector: " + String(e);
      return { ok: false, reason: String(e) };
    });
  }

  function applyDetectorConfig() {
    var mode = ($("detectorMode") && $("detectorMode").value) || "hsv_strict";
    var presetByMode = {
      hsv_strict: { hmin: 95, hmax: 130, smin: 45, vmin: 35, solidity: 0.55 },
      hsv_robust: { hmin: 90, hmax: 145, smin: 25, vmin: 20, solidity: 0.38 },
      yolo_classic: { hmin: 90, hmax: 145, smin: 25, vmin: 20, solidity: 0.30 },
    };
    var modePreset = presetByMode[mode] || presetByMode.hsv_strict;
    var params = {
      D1_COLOR_BOX_H_MIN: modePreset.hmin,
      D1_COLOR_BOX_H_MAX: modePreset.hmax,
      D1_COLOR_BOX_S_MIN: modePreset.smin,
      D1_COLOR_BOX_V_MIN: modePreset.vmin,
      D1_COLOR_BOX_MIN_AREA_FRAC: numVal("detMinArea", 4) / 1000,
      D1_COLOR_BOX_MIN_SOLIDITY: modePreset.solidity,
      D1_COLOR_BOX_COMP_TARGET_V_MED: numVal("detLumTarget", 92),
      D1_COLOR_BOX_COMP_MAX_GAIN: numVal("detCompGain", 24) / 10,
      D1_COLOR_BOX_COMP_BETA: numVal("detCompBeta", 6),
    };
    var cropBottom = numVal("detCropBottom", 10) / 100;
    pill("applico detector...", "warn");
    return post("/api/pick/detector/config", { mode: mode, params: params }).then(function (j) {
      applyDetectorForm(j);
      return post("/api/pick/vision/crop", { crop_fracs: { top: 0.0, left: 0.0, right: 0.0, bottom: cropBottom } }).then(function (crop) {
        log("detector config applicata", { detector: j, crop: crop });
        pill("detector ok", j && j.ok ? "ok" : "warn");
        return j;
      });
    }).catch(function (e) {
      pill("detector err", "err");
      log(String(e), null, "error");
    });
  }

  function detectorDetect() {
    return snapshot().then(function (j) {
      if (j && j.ok) {
        var meta = $("detectorLabMeta");
        if (meta) {
          var d = j.detection || {};
          meta.textContent = [
            "detect_ok=" + (!!j.detection_ok) + " reason=" + (d.reason || "-"),
            "method=" + (d.detect_method || "-") + " orient=" + (d.orientation_deg != null ? d.orientation_deg : "-"),
            "bbox=" + (d.bbox_xyxy ? JSON.stringify(d.bbox_xyxy) : "-"),
          ].join("\n");
        }
      }
      return j;
    });
  }

  function detectorDetectDepth() {
    pill("detect+depth...", "warn");
    return post("/api/pick/detect/metric", { detect_camera: "wrist", instruction: instruction() }).then(function (j) {
      log("detect metric", j, j.ok ? "info" : "warn");
      if (j.metric_viz_url && $("detectMonitorImg")) {
        $("detectMonitorImg").src = api(j.metric_viz_url + (j.metric_viz_url.indexOf("?") >= 0 ? "&" : "?") + "_=" + Date.now());
      }
      renderDetectionMonitor(j, "metric_detect");
      var meta = $("detectorLabMeta");
      if (meta) {
        var d = j.detection || {};
        meta.textContent = [
          "metric_ok=" + (!!j.ok) + " detect_ok=" + (!!j.detection_ok) + " reason=" + (j.reason || d.reason || "-"),
          "depth_m=" + (j.depth_m != null ? Number(j.depth_m).toFixed(3) : "-") + " source=" + (j.depth_source || "-"),
          "orient=" + (d.orientation_deg != null ? d.orientation_deg : "-") + " bbox=" + (d.bbox_xyxy ? JSON.stringify(d.bbox_xyxy) : "-"),
        ].join("\n");
      }
      pill(j.ok ? "depth ok" : "depth warn", j.ok ? "ok" : "warn");
      return j;
    }).catch(function (e) {
      pill("depth err", "err");
      log(String(e), null, "error");
    });
  }

  function loadDetectionMonitor() {
    return json("/api/pick/detection/last?_=" + Date.now()).then(function (j) {
      renderDetectionMonitor(j, "last_detection");
      return j;
    });
  }

  function loadRgbHealth() {
    var cam = currentDetectCamera();
    return json("/api/pick/vision/rgb_health?camera=" + encodeURIComponent(cam) + "&_=" + Date.now()).then(function (j) {
      var el = $("rgbHealthStatus");
      if (!el) return j;
      if (!j || j.ok === false) {
        el.textContent = "RGB health: errore";
        return j;
      }
      var cc = j.camera_cache || {};
      var pc = j.panel_cache || {};
      el.textContent = "RGB health " + cameraLabel(cam) + ": mode=" + (j.color_source_mode || "-") +
        " | cache.available=" + (!!cc.available) +
        " | panel.age_s=" + (pc.age_s != null ? pc.age_s : "-") +
        " | panel.color=" + (!!pc.has_color);
      return j;
    });
  }

  function resetRgbCamera() {
    var cam = currentDetectCamera();
    pill("reset realsense...", "warn");
    log("Reset RealSense richiesto", { camera: cam });
    return post("/api/pick/vision/realsense/reset", { camera: cam }).then(function (j) {
      log("Reset RealSense", j, j.ok ? "info" : "error");
      pill(j.ok ? "realsense reset ok" : "realsense reset err", j.ok ? "ok" : "err");
      return loadRgbHealth().then(function () { return loadDetectionMonitor().then(function () { return j; }); });
    }).catch(function (e) {
      pill("realsense reset err", "err");
      log(String(e), null, "error");
    });
  }

  function currentDetectCamera() {
    var el = $("focusDetectCamera");
    return el && el.value === "front" ? "front" : "wrist";
  }

  function currentGraspCamera() {
    var el = $("focusGraspCamera");
    return el && el.value === "front" ? "front" : "wrist";
  }

  function applyCameraSelection(sel) {
    var detect = sel && sel.detect_camera === "front" ? "front" : "wrist";
    var grasp = sel && sel.grasp_camera === "front" ? "front" : "wrist";
    var detEl = $("focusDetectCamera");
    var graEl = $("focusGraspCamera");
    if (detEl) detEl.value = detect;
    if (graEl) graEl.value = grasp;
  }

  function saveCameraSelection() {
    var body = { detect_camera: currentDetectCamera(), grasp_camera: currentGraspCamera() };
    return post("/api/pick/camera/select", body).then(function (j) {
      if (j && j.ok) applyCameraSelection(j);
      return j;
    });
  }

  function loadCameraSelection() {
    return json("/api/pick/camera/select?_=" + Date.now()).then(function (j) {
      if (j && j.ok) applyCameraSelection(j);
      return j;
    }).catch(function () { return { ok: false }; });
  }

  function renderStreamGrid() {
    var grid = $("focusStreamGrid");
    if (!grid) return;
    grid.innerHTML =
      '<article class="focus-stream-card" data-slot="wrist_rgb">' +
        '<div class="focus-stream-top"><strong>Polso RGB</strong><small>D456 - solo colore SDK</small></div>' +
        '<img id="focusStreamImg-wrist_rgb" alt="Polso RGB" src="' + streamUrl("color", "wrist") + '" />' +
        '<div class="focus-stream-controls"><div class="focus-stream-meta">Detection e presa: solo camera polso.</div></div>' +
      '</article>' +
      '<article class="focus-stream-card" data-slot="front_rgb">' +
        '<div class="focus-stream-top"><strong>Frontale RGB</strong><small>D435i - monitor</small></div>' +
        '<img id="focusStreamImg-front_rgb" alt="Frontale RGB" src="' + streamUrl("color", "front") + '" />' +
        '<div class="focus-stream-controls"><div class="focus-stream-meta">Solo monitor: esclusa dalla detection presa.</div></div>' +
      '</article>';
    return;
    var streams = streamCatalog && Array.isArray(streamCatalog.streams) ? streamCatalog.streams : [];
    var cameras = ["wrist", "front"];
    var html = cameras.map(function (cameraRole) {
      var slots = SLOT_DEFS.filter(function (s) { return s.camera === cameraRole; });
      var idx = streamCarousel[cameraRole] || 0;
      if (idx >= slots.length) idx = 0;
      streamCarousel[cameraRole] = idx;
      var slotDef = slots[idx];
      var slot = streamState.slots[slotDef.id] || { label: slotDef.label, panel: null, camera: slotDef.camera, kind: slotDef.kind };
      var panel = slot.panel || preferredPanel(slotDef.kind, slotDef.camera);
      var options = ['<option value="">Auto</option>'];
      streams.forEach(function (s) {
        var selected = panel === s.key ? ' selected' : '';
        var txt = (s.label || s.key) + (s.description ? (" · " + s.description) : "");
        options.push('<option value="' + escHtml(s.key) + '"' + selected + '>' + escHtml(txt) + '</option>');
      });
      var meta = "camera: " + cameraLabel(slotDef.camera) + " · source: " + (slot.panel || "auto") + " · label: " + (slot.label || slotDef.label);
      return (
        '<article class="focus-stream-card" data-slot="' + slotDef.id + '">' +
          '<div class="focus-stream-top"><strong>' + escHtml(slot.label || slotDef.label) + '</strong><small>' + escHtml(cameraLabel(slotDef.camera) + " · " + slotDef.kind.toUpperCase()) + '</small></div>' +
          '<img id="focusStreamImg-' + slotDef.id + '" alt="' + escHtml(slot.label || slotDef.label) + '" src="' + streamUrl(panel, slotDef.camera) + '" />' +
          '<div class="focus-stream-controls">' +
            '<div class="focus-stream-nav">' +
              '<button type="button" data-stream-nav="prev" data-camera-role="' + slotDef.camera + '">&larr; precedente</button>' +
              '<button type="button" data-stream-nav="next" data-camera-role="' + slotDef.camera + '">successivo &rarr;</button>' +
            '</div>' +
            '<label>Etichetta<input type="text" data-stream-label="' + slotDef.id + '" value="' + escHtml(slot.label || slotDef.label) + '" /></label>' +
            '<label>Feed<select data-stream-panel="' + slotDef.id + '">' + options.join('') + '</select></label>' +
            '<div class="focus-stream-meta" id="focusStreamMeta-' + slotDef.id + '">' + escHtml(meta) + '</div>' +
          '</div>' +
        '</article>'
      );
    }).join('');
    grid.innerHTML = html;
    saveCarouselState();

    SLOT_DEFS.forEach(function (slotDef) {
      var labelEl = grid.querySelector('[data-stream-label="' + slotDef.id + '"]');
      var panelEl = grid.querySelector('[data-stream-panel="' + slotDef.id + '"]');
      if (labelEl) {
        labelEl.onchange = function () {
          streamState.slots[slotDef.id] = streamState.slots[slotDef.id] || { camera: slotDef.camera, kind: slotDef.kind };
          streamState.slots[slotDef.id].label = labelEl.value || slotDef.label;
          saveStreamState();
          renderStreamGrid();
        };
      }
      if (panelEl) {
        panelEl.onchange = function () {
          streamState.slots[slotDef.id] = streamState.slots[slotDef.id] || { camera: slotDef.camera, kind: slotDef.kind };
          streamState.slots[slotDef.id].panel = panelEl.value || null;
          saveStreamState();
          renderStreamGrid();
        };
      }
    });

    grid.querySelectorAll("[data-stream-nav]").forEach(function (btn) {
      btn.onclick = function () {
        var role = btn.getAttribute("data-camera-role") || "wrist";
        var dir = btn.getAttribute("data-stream-nav") === "prev" ? -1 : 1;
        var slots = SLOT_DEFS.filter(function (s) { return s.camera === role; });
        var idx = streamCarousel[role] || 0;
        idx = (idx + dir + slots.length) % slots.length;
        streamCarousel[role] = idx;
        saveCarouselState();
        renderStreamGrid();
      };
    });
  }

  function refreshStreamCatalog() {
    return json('/api/pick/vision/streams?_=' + Date.now()).then(function (j) {
      if (!j || !j.ok) throw new Error((j && (j.reason || j.hint_it)) || 'stream catalog unavailable');
      streamCatalog = j;
      SLOT_DEFS.forEach(function (slotDef) {
        streamState.slots[slotDef.id] = streamState.slots[slotDef.id] || { label: slotDef.label, panel: null, camera: slotDef.camera, kind: slotDef.kind };
        if (!streamState.slots[slotDef.id].label) streamState.slots[slotDef.id].label = slotDef.label;
        if (!streamState.slots[slotDef.id].panel) streamState.slots[slotDef.id].panel = preferredPanel(slotDef.kind, slotDef.camera);
      });
      saveStreamState();
      renderStreamGrid();
      return j;
    }).catch(function (e) {
      var grid = $("focusStreamGrid");
      if (grid) {
        grid.innerHTML = '<div class="focus-stream-card"><div class="focus-stream-top"><strong>Stream non disponibili</strong><small>5056</small></div><div class="focus-stream-controls"><div class="focus-stream-meta">' + escHtml(String(e)) + '</div></div></div>';
      }
      return { ok: false, error: String(e) };
    });
  }

  function now() {
    return new Date().toLocaleTimeString("it-IT", { hour12: false });
  }

  function labelForVariant(variant) {
    return variant === "j90" ? "90\u00b0 a destra" : "90\u00b0 a sinistra";
  }

  function kindFromOk(ok) {
    return ok ? "ok" : "err";
  }

  function pill(text, kind) {
    var el = $("teachPill");
    if (!el) return;
    el.textContent = text;
    el.className = "pill " + (kind || "");
  }

  function updateSteps(currentStep, failed) {
    var chips = document.querySelectorAll("#teachStepRail .step-chip");
    var cur = STEP_INDEX[currentStep];
    chips.forEach(function (chip) {
      var idx = STEP_INDEX[chip.getAttribute("data-step")];
      chip.className = "step-chip";
      if (cur == null) return;
      if (idx < cur) chip.classList.add("done");
      if (idx === cur) chip.classList.add(failed ? "err" : "on");
    });
  }

  function setProgress(pct, title, kind, currentStep) {
    pct = Math.max(0, Math.min(100, Number(pct) || 0));
    var titleEl = $("teachProgressTitle");
    var pctEl = $("teachProgressPct");
    var fill = $("teachProgressFill");
    if (titleEl) titleEl.textContent = title || "Pronto";
    if (pctEl) pctEl.textContent = Math.round(pct) + "%";
    if (fill) {
      fill.style.width = pct + "%";
      fill.className = "progress-fill " + (kind || "");
    }
    updateSteps(currentStep, kind === "err");
  }

  function log(message, data, level) {
    var prefix = "[" + now() + "] " + (level || "info").toUpperCase() + " ";
    logLines.push(prefix + message);
    if (data != null) {
      try {
        logLines.push(JSON.stringify(data, null, 2));
      } catch (e) {
        logLines.push(String(data));
      }
    }
    if (logLines.length > 260) logLines = logLines.slice(logLines.length - 260);
    var el = $("teachLog");
    if (el) {
      el.textContent = logLines.join("\n");
      el.scrollTop = el.scrollHeight;
    }
  }

  function summary(text) {
    var el = $("teachSummary");
    if (el) el.textContent = text;
  }

  function renderTuneHistory() {
    var el = $("tuneHistory");
    if (!el) return;
    if (!tuneHistory.length) {
      el.textContent = "Storico tuning vuoto.";
      return;
    }
    el.textContent = tuneHistory.slice().reverse().map(function (r) {
      var ts = r.ts || now();
      var kind = r.kind || "info";
      if (kind === "detect") {
        return "[" + ts + "] detect ok=" + (!!r.detection_ok) + " conf=" + (r.confidence != null ? Number(r.confidence).toFixed(3) : "-") + " orient=" + (r.orientation_deg != null ? r.orientation_deg : "-");
      }
      if (kind === "nudge") {
        return "[" + ts + "] nudge J" + r.joint + " delta=" + r.delta_deg + " ok=" + (!!r.ok);
      }
      if (kind === "cycle") {
        var err = r.error_cm != null ? Number(r.error_cm).toFixed(1) + "cm" : "-";
        return "[" + ts + "] cycle lato=" + (r.side || "-") + " esito=" + (r.result || "-") + " err=" + err + (r.note ? " note=" + r.note : "");
      }
      return "[" + ts + "] " + JSON.stringify(r);
    }).join("\n");
  }

  function loadTuneCycles() {
    return json("/api/pick/tuning/cycles?limit=30&_=" + Date.now()).then(function (j) {
      if (!j || j.ok === false || !Array.isArray(j.items)) return j;
      var rows = j.items.map(function (r) {
        return {
          ts: r.at || now(),
          kind: "cycle",
          side: r.side || "unknown",
          result: r.result || "unknown",
          error_cm: r.error_cm,
          note: r.note || "",
        };
      });
      var locals = tuneHistory.filter(function (r) {
        return r.kind === "detect" || r.kind === "nudge";
      });
      tuneHistory = rows.concat(locals).slice(-40);
      saveTuneHistory();
      renderTuneHistory();
      return j;
    });
  }

  function saveTuneCycle() {
    var side = (($("tuneSide") && $("tuneSide").value) || activeVariant || "j90").trim();
    var result = (($("tuneResult") && $("tuneResult").value) || "undershoot").trim();
    var errorCm = Number((($("tuneErrorCm") && $("tuneErrorCm").value) || 0));
    var note = (($("tuneNote") && $("tuneNote").value) || "").trim();
    var body = { side: side, result: result, error_cm: errorCm, note: note };
    return post("/api/pick/tuning/cycles", body).then(function (j) {
      if (j && j.ok && j.saved) {
        pushTuneHistory({
          ts: j.saved.at || now(),
          kind: "cycle",
          side: j.saved.side,
          result: j.saved.result,
          error_cm: j.saved.error_cm,
          note: j.saved.note || "",
        });
      }
      return loadPresetInfo().then(function () { return j; });
    });
  }

  function loadPresetInfo() {
    return json("/api/pick/preset?_=" + Date.now()).then(function (j) {
      var el = $("tunePresetMeta");
      if (el) {
        var off = j.joint_offset_deg || [];
        var txt = "offset: [" + off.map(function (v) { return Number(v).toFixed(2); }).join(", ") + "]";
        if (j.manual_orient_offset_deg != null) txt += " | orient manuale: " + Number(j.manual_orient_offset_deg).toFixed(2);
        el.textContent = txt;
      }
      return j;
    });
  }

  function tuneCaptureFeedback() {
    var cam = currentDetectCamera();
    return post("/api/pick/snapshot", { detect_camera: cam }).then(function (j) {
      var det = j.last_detection || j.detection || {};
      var img = $("tuneDetectImg");
      if (img) img.src = api((j.preview_url || "/api/pick/scene.jpg") + "&_=" + Date.now());
      var meta = $("tuneDetectMeta");
      if (meta) {
        meta.textContent = "camera=" + cam + " | detect=" + (!!j.detection_ok) + " | conf=" + (det.confidence != null ? Number(det.confidence).toFixed(3) : "-") + " | orient=" + (det.orientation_deg != null ? det.orientation_deg : "-");
      }
      pushTuneHistory({
        ts: now(),
        kind: "detect",
        detection_ok: !!j.detection_ok,
        confidence: det.confidence,
        orientation_deg: det.orientation_deg,
      });
      renderDetectionMonitor(j, "snapshot");
      return j;
    });
  }

  function tuneNudge(sign) {
    var jointEl = $("tuneJoint");
    var deltaEl = $("tuneDelta");
    var joint = Number((jointEl && jointEl.value) || 0) || 0;
    var baseDelta = Number((deltaEl && deltaEl.value) || 0.5) || 0.5;
    var delta = Math.abs(baseDelta) * (sign < 0 ? -1 : 1);
    return post("/api/pick/preset/nudge", { joint: joint, delta_deg: delta }).then(function (j) {
      pushTuneHistory({ ts: now(), kind: "nudge", joint: joint, delta_deg: delta, ok: !!j.ok, reason: j.reason || null });
      return loadPresetInfo().then(function () { return j; });
    });
  }

  function releasePayload() {
    return { confirm: "ARM_RELEASE_JOINTS", ack_gravity_risk: true };
  }

  function json(path, opts) {
    var btn = DBG.activeButton;
    var method = (opts && opts.method) || "GET";
    if (btn && path.indexOf("/api/focus/debug/log") < 0) {
      dbgBtn(btn, "api_start", { path: path, method: method }, "API");
    }
    return fetch(api(path), Object.assign({ cache: "no-store", credentials: "same-origin" }, opts || {}))
      .then(function (r) {
        return r.text().then(function (t) {
          var j = {};
          try { j = t ? JSON.parse(t) : {}; } catch (e) { j = { ok: false, raw: t.slice(0, 400) }; }
          j._http_status = r.status;
          if (!r.ok && j.ok !== false) j.ok = false;
          if (btn && path.indexOf("/api/focus/debug/log") < 0) {
            dbgBtn(btn, "api_done", {
              path: path,
              method: method,
              http: r.status,
              ok: j.ok,
              reason: j.reason || j.hint_it || null,
              phase: j.phase || null,
            }, "API");
          }
          return j;
        });
      });
  }

  function post(path, body) {
    return json(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
  }

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  function countdown(sec, label, pct, step) {
    var p = Promise.resolve();
    for (var t = sec; t >= 1; t -= 1) {
      (function (n) {
        p = p.then(function () {
          pill(label + " " + n + "s", "warn");
          setProgress(pct, label + " - " + n + "s", "warn", step);
          log(label + " - " + n + " secondi");
          return sleep(1000);
        });
      })(t);
    }
    return p;
  }

  function jointFeedback() {
    return json("/api/arm/servo_snapshot?_=" + Date.now()).then(function (j) {
      if (j.ok && j.servo_deg) {
        return { ok: true, servo_deg: j.servo_deg, _http_status: j._http_status };
      }
      return {
        ok: false,
        reason: j.reason || "no_feedback",
        _http_status: j._http_status,
      };
    });
  }

  function instruction() {
    return (($("teachInstruction") && $("teachInstruction").value) || "").trim();
  }

  function bindBtn(buttonId, fn) {
    var el = $(buttonId);
    if (!el) return;
    el.addEventListener("click", function () {
      DBG.activeButton = buttonId;
      dbgBtn(buttonId, "click_start", {});
      try {
        var ret = fn.apply(null, arguments);
        if (ret && typeof ret.then === "function") {
          ret.then(function (j) {
            dbgBtn(buttonId, "handler_done", {
              ok: j && j.ok,
              reason: (j && (j.reason || j.hint_it || j.phase)) || null,
              http: j && j._http_status,
            });
            DBG.activeButton = null;
            return j;
          }).catch(function (e) {
            dbgBtn(buttonId, "handler_error", { error: String(e) });
            DBG.activeButton = null;
          });
        } else {
          dbgBtn(buttonId, ret === undefined ? "handler_cancelled" : "handler_sync_done", {});
          DBG.activeButton = null;
        }
      } catch (e) {
        dbgBtn(buttonId, "handler_throw", { error: String(e) });
        DBG.activeButton = null;
        throw e;
      }
    });
  }

  function status() {
    return Promise.all([
      json("/api/focus/status?_=" + Date.now()).catch(function (e) { return { ok: false, error: String(e) }; }),
      json("/api/pick/teach/samples?_=" + Date.now()).catch(function (e) { return { ok: false, error: String(e) }; }),
      json("/api/arm/status?_=" + Date.now()).catch(function (e) { return { ok: false, error: String(e) }; }),
      json("/api/cameras/status?_=" + Date.now()).catch(function (e) { return { ok: false, error: String(e) }; }),
    ]).then(function (rows) {
      var focus = rows[0];
      var samples = rows[1];
      var arm = rows[2];
      var cams = rows[3];
      var n = Number(samples.count || (focus.teach && focus.teach.count) || 0);
      var model = !!(samples.has_active_model || (focus.teach && focus.teach.has_active_model));
      var coupled = !!(arm.arm_coupled || (focus.arm && focus.arm.arm_coupled));
      var cam0 = cams && cams.cameras && cams.cameras["0"];
      var cam6 = cams && cams.cameras && cams.cameras["6"];
      var teachWp = (samples.teach_model && samples.teach_model.scan_waypoint) || "";
      var teachOn90 = /90/.test(String(teachWp));
      var variantWarn = teachOn90 && activeVariant === "j90_left"
        ? " ATTENZIONE: teach su «" + teachWp + "» → usa 90° a DESTRA, non sinistra."
        : "";
      pill(coupled ? (model ? "pronto" : "modello assente") : "braccio libero", coupled && model ? "ok" : "warn");
      summary(
        "Braccio: " + (coupled ? "coppia ON" : "coppia OFF") +
        " | sample teach: " + n +
        " | modello: " + (model ? "attivo" : "da ricreare") +
        (cam0 && cam6 ? " | cam0: " + (cam0.stream_kind || "?") + " / cam6: " + (cam6.stream_kind || "?") : "") +
        (teachWp ? " | teach scan: " + teachWp : "") +
        " | flag lato: " + labelForVariant(activeVariant) +
        variantWarn
      );
      var camStatus = $("teachCamStatus");
      if (camStatus) {
        var cam0Txt = cam0 ? (cam0.stream_kind || "?") + " @ " + (cam0.device_path || "?") : "non disponibile";
        var cam6Txt = cam6 ? (cam6.stream_kind || "?") + " @ " + (cam6.device_path || "?") : "non disponibile";
        camStatus.textContent = "log.0: " + cam0Txt + " | log.6: " + cam6Txt;
      }
      if (model) setProgress(0, "Pronto - flag lato " + labelForVariant(activeVariant), "ok", null);
      else setProgress(n > 0 ? 65 : 0, n > 0 ? "Sample presenti, ricrea modello" : "In attesa teaching", "warn", n > 0 ? "teach" : null);
      return { focus: focus, samples: samples, arm: arm };
    }).catch(function (e) {
      pill("rete", "err");
      setProgress(0, "Errore rete", "err", null);
      log(String(e), null, "error");
    });
  }

  function snapshot() {
    var detectCamera = currentDetectCamera();
    pill("foto...", "warn");
    setProgress(25, "Foto RGB manuale", "warn", "rgbd");
    setSvcAck("detect", "run");
    setSvcAck("depth", "run");
    log("Foto manuale + detect", { detect_camera: detectCamera });
    return post("/api/pick/snapshot", { detect_camera: detectCamera }).then(function (j) {
      log("snapshot completata", j, j.ok ? "info" : "warn");
      if (j.ok && j.detection_ok) {
        var det = j.detection || j.last_detection || {};
        pill("detect ok", "ok");
        setProgress(40, "Detect ok", "ok", "rgbd");
        setSvcAck("detect", "ok");
        setSvcAck("depth", "ok");
        setExecutePhase("waiting");
        summary("Detect: " + (det.label || "oggetto") + " conf=" + (det.confidence != null ? Number(det.confidence).toFixed(3) : "-"));
      } else {
        pill("detect no", "warn");
        setProgress(25, "Detect non valido", "warn", "rgbd");
        setSvcAck("detect", "err");
        setSvcAck("depth", j.ok ? "err" : "idle");
        summary(j.hint_it || j.reason || "Oggetto non rilevato.");
      }
      renderDetectionMonitor(j, "snapshot");
      return j;
    }).catch(function (e) {
      pill("foto err", "err");
      setProgress(25, "Foto fallita", "err", "rgbd");
      log(String(e), null, "error");
    });
  }

  function move90(variant, ask) {
    activeVariant = variant;
    if (ask && !confirm("Muovere il braccio a " + labelForVariant(variant) + "? Questo non fa scan camera e non controlla il pezzo.")) return;
    pill("muovo " + labelForVariant(variant), "warn");
    setProgress(8, "Muovo a " + labelForVariant(variant), "warn", "move90");
    setSvcAck("comando", "run");
    setExecutePhase("waiting");
    log("POST /api/presets/scan/goto - solo movimento 90, nessun controllo camera/pezzo", { variant: variant });
    return post("/api/presets/scan/goto", { variant: variant }).then(function (j) {
      log("movimento 90 completato", j, j.ok ? "info" : "error");
      pill(j.ok ? "90 ok" : "90 err", kindFromOk(j.ok));
      setSvcAck("comando", j.ok ? "ok" : "err");
      setProgress(j.ok ? 18 : 8, j.ok ? "Posizione " + labelForVariant(variant) + " raggiunta" : "Movimento 90 fallito", j.ok ? "ok" : "err", "move90");
      return j;
    }).catch(function (e) {
      setSvcAck("comando", "err");
      pill("errore", "err");
      setProgress(8, "Movimento 90 fallito", "err", "move90");
      log(String(e), null, "error");
    });
  }

  function runSafeCycle() {
    var detectCamera = currentDetectCamera();
    var graspCamera = currentGraspCamera();
    var variants = ["j90_left", "j90"];
    var report = [];
    pill("test ciclo", "warn");
    setProgress(4, "Test ciclo sicuro avviato", "warn", "move90");
    log("Test ciclo sicuro avviato", { detect_camera: detectCamera, grasp_camera: graspCamera });
    return post("/api/arm/joints/couple", { with_power: true }).then(function (couple) {
      report.push({ step: "couple", ok: !!(couple && couple.ok), reason: couple && couple.reason });
      if (!couple || !couple.ok) throw new Error("coppia non disponibile");
      var p = Promise.resolve();
      variants.forEach(function (variant, idx) {
        p = p.then(function () {
          setProgress(10 + idx * 40, "Muovo " + labelForVariant(variant), "warn", "move90");
          return post("/api/presets/scan/goto", { variant: variant });
        }).then(function (mv) {
          report.push({ step: "scan_" + variant, ok: !!(mv && mv.ok), reason: mv && mv.reason });
          if (!mv || !mv.ok) throw new Error("scan fail " + variant + ": " + (mv && mv.reason));
          setProgress(22 + idx * 40, "Snapshot " + labelForVariant(variant), "warn", "rgbd");
          return post("/api/pick/snapshot", { detect_camera: detectCamera });
        }).then(function (snap) {
          report.push({ step: "snapshot_" + variant, ok: !!(snap && snap.ok), detect_ok: !!(snap && snap.detection_ok), reason: snap && snap.reason });
          renderDetectionMonitor(snap, "safe_cycle");
          if (!snap || !snap.ok) throw new Error("snapshot fail " + variant + ": " + (snap && snap.reason));
          setProgress(34 + idx * 40, "Goto presa " + labelForVariant(variant), "warn", "execute");
          return post("/api/pick/grasp/goto", { scan_variant: variant, grasp_camera: graspCamera });
        }).then(function (gt) {
          report.push({ step: "goto_" + variant, ok: !!(gt && gt.ok), reason: gt && gt.reason });
          if (!gt || !gt.ok) throw new Error("goto fail " + variant + ": " + (gt && gt.reason));
        });
      });
      return p;
    }).then(function () {
      setProgress(100, "Test ciclo sicuro completato", "ok", "verify");
      pill("test ciclo ok", "ok");
      log("Test ciclo sicuro completato", { report: report });
      return { ok: true, report: report };
    }).catch(function (e) {
      pill("test ciclo err", "err");
      setProgress(45, "Test ciclo interrotto", "err", "execute");
      log("Test ciclo sicuro fallito", { error: String(e), report: report }, "error");
      return { ok: false, reason: String(e), report: report };
    });
  }

  function graspGoto(variant) {
    var graspCamera = currentGraspCamera();
    activeVariant = variant;
    if (!confirm(
      "Avvicinare alla presa da " + labelForVariant(variant) + "?\n\n" +
      "Usa lo STESSO lato usato per Muovi 90° e per il teach (es. Punto SCANSIONE 90 = 90° a destra).\n" +
      "La pinza resta aperta."
    )) return;
    pill("avvicino...", "warn");
    setProgress(72, "Avvicinamento manuale", "warn", "execute");
    setSvcAck("ik", "run");
    setExecutePhase("running");
    return post("/api/pick/grasp/goto", { scan_variant: variant, grasp_camera: graspCamera }).then(function (j) {
      log("avvicinamento completato", j, j.ok ? "info" : "error");
      pill(j.ok ? "avvicina ok" : "avvicina err", kindFromOk(j.ok));
      if (j.ok) {
        setProgress(82, "Sulla presa, pinza aperta", "ok", "execute");
        setSvcAck("ik", "ok");
        setSvcAck("execute", "ok");
        setExecutePhase("completed");
      } else {
        var failMsg = graspFailMessage(j);
        setProgress(72, "Avvicinamento fallito: " + failMsg, "err", "execute");
        setSvcAck("ik", "err");
        setSvcAck("execute", "err");
        setExecutePhase("failed");
        summary(failMsg + (j.reason === "plane_busy:joint" ? " Chiudi tab «Braccio D1 · giunti» o premi «Annulla flusso»." : ""));
      }
      return j;
    }).catch(function (e) {
      setSvcAck("ik", "err");
      setSvcAck("execute", "err");
      setExecutePhase("failed");
      pill("errore", "err");
      setProgress(72, "Avvicinamento fallito", "err", "execute");
      log(String(e), null, "error");
    });
  }

  function stepFromPhase(phase) {
    if (phase === "move_90") return "move90";
    if (phase === "rgbd_scan_ik") return "ik";
    if (phase === "execute_phased") return "execute";
    if (phase === "close") return "verify";
    return "verify";
  }

  function graspFailMessage(j) {
    if (!j || j.ok) return "";
    var ga = j.grasp_assessment || ((j.metric_plan || {}).grasp_assessment) || {};
    if (ga.label_it) return String(ga.label_it);
    var steps = j.steps;
    if (Array.isArray(steps)) {
      for (var i = steps.length - 1; i >= 0; i -= 1) {
        var st = steps[i] || {};
        var v = st.validation_ui || st.grasp_assessment || {};
        if (v.label_it) return String(v.label_it);
        if (st.reason) return String(st.reason);
      }
    }
    if (j.hint_it) return String(j.hint_it);
    return String(j.reason || j.phase || "errore");
  }

  function startGrasp() {
    if (!executeAckReady()) {
      summary("Prima: Muovi 90° → Foto+detect → Solo avvicina (ACK execute deve essere ✓).");
      if (!confirm(
        "ACK execute non verificato.\n\n" +
        "Flusso consigliato:\n1) Muovi 90°\n2) Foto+detect\n3) Solo avvicina\n4) Inizia presa\n\n" +
        "Continuare comunque (chiudi pinza + rialzo sulla posa attuale)?"
      )) return;
    } else if (!confirm(
      "Chiudere pinza e rialzare il braccio?\n\n" +
      "Usa solo dopo «Solo avvicina» riuscito (pinza aperta sul pezzo)."
    )) return;
    pill("chiudo...", "warn");
    setProgress(88, "Chiusura pinza", "warn", "verify");
    setExecutePhase("running");
    log("Inizia presa: chiudi + rialzo (posa corrente)", { execute_ack: svcState.execute });
    return post("/api/pick/grasp/close_and_lift", { lift: true }).then(function (j) {
      log("close_and_lift", j, j.ok ? "info" : "error");
      if (j.ok) {
        pill("presa ok", "ok");
        setProgress(100, "Presa completata (chiuso + rialzo)", "ok", "verify");
        setExecutePhase("completed");
        summary("Presa completata: pinza chiusa e braccio rialzato.");
      } else {
        pill("presa err", "err");
        var msg = (j.lift && j.lift.reason) || (j.close && j.close.reason) || j.reason || "errore";
        setProgress(88, "Presa fallita: " + msg, "err", "verify");
        setExecutePhase("failed");
        summary(j.hint_it || msg);
      }
      return j;
    }).catch(function (e) {
      setExecutePhase("failed");
      pill("errore", "err");
      log(String(e), null, "error");
    });
  }

  function startAutoGrasp() {
    var variant = activeVariant || "j90";
    var detectCamera = currentDetectCamera();
    var graspCamera = currentGraspCamera();
    if (!confirm(
      "Presa automatica completa (" + labelForVariant(variant) + ")?\n\n" +
      "Rifà tutto: muovi 90 → foto+detect → avvicina → chiudi → rialzo.\n" +
      "Per il flusso manuale usa i pulsanti separati + «Inizia presa»."
    )) return;
    pill("auto...", "warn");
    setProgress(5, "Presa automatica: muovi 90", "warn", "move90");
    resetSvcAck();
    setSvcAck("comando", "run");
    setExecutePhase("running");
    log("Presa automatica", { scan_variant: variant, flow: "full_sequence" });
    return post("/api/pick/full_sequence", {
      scan_variant: variant,
      instruction: instruction(),
      execute: true,
      close: true,
      lift: true,
      detect_camera: detectCamera,
      grasp_camera: graspCamera,
    }).then(function (j) {
      log("presa automatica completata", j, j.ok ? "info" : "error");
      applyFullSequenceSteps(j.steps);
      if (j.ok) {
        pill("auto ok", "ok");
        setProgress(100, "Presa automatica completata", "ok", "verify");
        setExecutePhase("completed");
      } else {
        pill("auto err", "err");
        var failMsg = graspFailMessage(j);
        setProgress(70, "Presa automatica fallita: " + failMsg, "err", stepFromPhase(j.phase));
        setExecutePhase("failed");
        summary("Presa automatica fallita: " + failMsg);
      }
      return j;
    }).catch(function (e) {
      setExecutePhase("failed");
      pill("errore", "err");
      setProgress(70, "Presa automatica fallita", "err", "verify");
      log(String(e), null, "error");
    });
  }

  function gripperCloseOnly() {
    if (!confirm("Chiudere solo la pinza (senza rialzo) sulla posa attuale?")) return;
    pill("chiudo...", "warn");
    setProgress(90, "Chiusura pinza", "warn", "verify");
    return post("/api/pick/gripper/close", {}).then(function (j) {
      log("chiudi pinza", j, j.ok ? "info" : "error");
      pill(j.ok ? "pinza chiusa" : "chiusura err", kindFromOk(j.ok));
      setProgress(j.ok ? 92 : 90, j.ok ? "Pinza chiusa" : "Chiusura fallita", j.ok ? "ok" : "err", "verify");
      if (!j.ok) summary(j.reason || "Chiusura pinza fallita.");
      return j;
    }).catch(function (e) {
      pill("errore", "err");
      log(String(e), null, "error");
    });
  }

  function buildTeachModel() {
    pill("modello...", "warn");
    setProgress(85, "Ricreo modello teach", "warn", "teach");
    return post("/api/pick/teach/build_model", {}).then(function (j) {
      log("build modello teach", j, j.ok ? "info" : "error");
      pill(j.ok ? "modello ok" : "modello err", kindFromOk(j.ok));
      setProgress(j.ok ? 100 : 85, j.ok ? "Modello teach attivo" : "Modello teach fallito", j.ok ? "ok" : "err", "teach");
      return status();
    }).catch(function (e) {
      pill("modello err", "err");
      setProgress(85, "Modello teach fallito", "err", "teach");
      log(String(e), null, "error");
    });
  }

  function teachPosition(reason) {
    if (!confirm("Avviare teaching posizione presa?\n\nTra 5 secondi rilascio completamente i giunti. Poi hai 20 secondi per portare il braccio sulla presa reale.")) return;
    var reasonText = reason || "teaching posizione da pulsante";
    pill("teach posizione", "warn");
    setProgress(5, "Teaching posizione: preparati", "warn", "teach");
    log("Teaching posizione avviato", { reason: reasonText, scan_variant: activeVariant });
    return countdown(5, "Preparati al release", 18, "teach").then(function () {
      setProgress(30, "Release completo giunti", "warn", "teach");
      log("Release completo giunti braccio");
      return post("/api/arm/joints/release", releasePayload());
    }).then(function (rel) {
      log("release giunti", rel, rel.ok ? "info" : "warn");
      return countdown(20, "Porta il braccio sulla presa", 55, "teach");
    }).then(function () {
      setProgress(70, "Leggo posizione insegnata", "warn", "teach");
      return jointFeedback();
    }).then(function (fb) {
      log("feedback posa insegnata", fb, fb.ok ? "info" : "error");
      if (!fb.ok || !fb.servo_deg) throw new Error(fb.reason || "feedback giunti non disponibile");
      setProgress(78, "Memorizzo posizione presa", "warn", "teach");
      return post("/api/pick/teach/finish", {
        vision_at_scan: null,
        servo_deg: fb.servo_deg,
        scan_variant: activeVariant,
        reason: reasonText,
      });
    }).then(function (fin) {
      log("posizione teach salvata", fin, fin.ok ? "info" : "error");
      if (!fin.ok) throw new Error(fin.reason || "salvataggio teach fallito");
      return buildTeachModel();
    }).catch(function (e) {
      pill("teach stop", "err");
      setProgress(45, "Teaching posizione interrotto", "err", "teach");
      log(String(e), null, "error");
    });
  }

  function manualTeach(variant) {
    var detectCamera = currentDetectCamera();
    activeVariant = variant;
    if (!confirm("Avviare teaching completo " + labelForVariant(variant) + "? Questo include movimento 90 e foto prima del release.")) return;
    var visionAtScan = null;
    pill("teach completo", "warn");
    setProgress(8, "Teaching: muovi " + labelForVariant(variant), "warn", "move90");
    return post("/api/presets/scan/goto", { variant: variant }).then(function (scan) {
      log("move90 teaching", scan, scan.ok ? "info" : "error");
      if (!scan.ok) throw new Error(scan.reason || "movimento 90 fallito");
      setProgress(25, "Teaching: foto", "warn", "rgbd");
      return post("/api/pick/snapshot", { detect_camera: detectCamera });
    }).then(function (snap) {
      visionAtScan = snap.last_detection || snap.detection || null;
      log("snapshot teaching", snap, snap.ok ? "info" : "warn");
      return countdown(5, "Preparati al release", 35, "teach");
    }).then(function () {
      setProgress(42, "Release giunti", "warn", "teach");
      return post("/api/arm/joints/release", releasePayload());
    }).then(function (rel) {
      log("release giunti", rel, rel.ok ? "info" : "warn");
      return countdown(20, "Muovi il braccio sulla presa", 55, "teach");
    }).then(function () {
      setProgress(70, "Leggo feedback giunti", "warn", "teach");
      return jointFeedback();
    }).then(function (fb) {
      if (!fb.ok || !fb.servo_deg) throw new Error(fb.reason || "feedback giunti non disponibile");
      return post("/api/pick/teach/finish", { vision_at_scan: visionAtScan, servo_deg: fb.servo_deg, scan_variant: activeVariant });
    }).then(function (fin) {
      log("teach finish", fin, fin.ok ? "info" : "error");
      if (!fin.ok) throw new Error(fin.reason || "salvataggio teach fallito");
      return buildTeachModel();
    }).catch(function (e) {
      pill("teach stop", "err");
      setProgress(45, "Teaching completo interrotto", "err", "teach");
      log(String(e), null, "error");
    });
  }

  function armCouple() {
    pill("coppia...", "warn");
    setProgress(5, "Attivo coppia braccio", "warn", null);
    setSvcAck("comando", "run");
    return post("/api/arm/joints/couple", { with_power: true }).then(function (j) {
      log("coppia braccio", j, j.ok ? "info" : "error");
      pill(j.ok ? "coppia on" : "coppia err", kindFromOk(j.ok));
      setSvcAck("comando", j.ok ? "ok" : "err");
      return status();
    });
  }

  function gotoHome() {
    if (!confirm(
      "Tornare alla posa Folded calibrata (ZERO file)?\n\n" +
      "Il braccio si muove gradualmente (traiettoria interpolata), non un salto istantaneo.\n" +
      "Target: data/true_zero_pose.json (non la Home 0°).\n\n" +
      "Assicurati che l'area sia libera e che la coppia sia ON."
    )) return;
    pill("folded...", "warn");
    setProgress(15, "Movimento verso Folded", "warn", null);
    setSvcAck("comando", "run");
    setExecutePhase("running");
    return post("/api/arm/goto_true_zero", { confirm: "ARM_GOTO_TRUE_ZERO" }).then(function (j) {
      log("goto folded (true_zero) completato", j, j.ok ? "info" : "error");
      pill(j.ok ? "folded ok" : "folded err", kindFromOk(j.ok));
      setSvcAck("comando", j.ok ? "ok" : "err");
      setExecutePhase(j.ok ? "completed" : "failed");
      if (j.ok) {
        var folded = j.target_servo_deg_7 || j.target_servo_deg;
        if (folded) summary("Folded ZERO raggiunta: [" + folded.join(", ") + "] deg");
        setProgress(100, "Posa Folded raggiunta", "ok", null);
      } else {
        setProgress(15, "Folded fallita: " + (j.hint_it || j.reason || "errore"), "err", null);
        summary(j.hint_it || j.reason || "Movimento Folded fallito.");
      }
      return j;
    }).catch(function (e) {
      setSvcAck("comando", "err");
      setExecutePhase("failed");
      pill("folded err", "err");
      log(String(e), null, "error");
    });
  }

  function armRelease() {
    if (!confirm("Rilasciare i giunti del braccio? ATTENZIONE: il braccio diventa libero e puo' cadere per gravita'.")) return;
    pill("release...", "warn");
    setProgress(10, "Release giunti", "warn", "teach");
    return post("/api/arm/joints/release", releasePayload()).then(function (j) {
      log("release manuale", j, j.ok ? "info" : "error");
      pill(j.ok ? "release ok" : "release err", kindFromOk(j.ok));
      return status();
    });
  }

  function cancel() {
    return Promise.all([
      post("/api/grasp/teach_cancel", { reason_it: "annullato da focus dashboard" }),
      post("/api/arm/motion/reset", { confirm: "RESET_ARM_MOTION" }),
    ]).then(function (rows) {
      var j = { teach: rows[0], motion: rows[1], ok: !!(rows[0] && rows[0].ok && rows[1] && rows[1].ok) };
      pill("annullato", "warn");
      setProgress(0, "Flusso annullato", "warn", null);
      log("annulla flusso", j, j.ok ? "info" : "warn");
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    bindBtn("btnTeachStatus", function () { return status().then(function (s) { log("stato aggiornato", s); return s; }); });
    bindBtn("btnSnapshot", snapshot);
    bindBtn("btnScanLeft90", function () { return move90("j90_left", true); });
    bindBtn("btnScanRight90", function () { return move90("j90", true); });
    bindBtn("btnStartGrasp", startGrasp);
    bindBtn("btnAutoGrasp", function () { activeVariant = "j90_left"; return startAutoGrasp(); });
    bindBtn("btnGripperClose", gripperCloseOnly);
    bindBtn("btnSafeCycle", runSafeCycle);
    bindBtn("btnTeachPosition", function () { return teachPosition("teaching posizione da pulsante"); });
    bindBtn("btnMarkGraspFailed", function () { return teachPosition("operatore: presa fallita"); });
    bindBtn("btnGraspLeft", function () { return graspGoto("j90_left"); });
    bindBtn("btnGraspRight", function () { return graspGoto("j90"); });
    bindBtn("btnManualTeachLeft", function () { return manualTeach("j90_left"); });
    bindBtn("btnManualTeachRight", function () { return manualTeach("j90"); });
    bindBtn("btnBuildTeachModel", buildTeachModel);
    bindBtn("btnTeachCancel", cancel);
    bindBtn("btnArmCouple", armCouple);
    bindBtn("btnArmRelease", armRelease);
    bindBtn("btnGotoHome", gotoHome);
    bindBtn("btnClearLog", function () {
      logLines = [];
      var el = $("teachLog");
      if (el) el.textContent = "-";
    });
    var reloadBtn = $("btnFocusReloadStreams");
    if (reloadBtn) reloadBtn.addEventListener("click", refreshStreamCatalog);
    var detectSel = $("focusDetectCamera");
    var graspSel = $("focusGraspCamera");
    if (detectSel) detectSel.addEventListener("change", function () { saveCameraSelection(); });
    if (graspSel) graspSel.addEventListener("change", function () { saveCameraSelection(); });
    var btnTuneCap = $("btnTuneCapture");
    if (btnTuneCap) btnTuneCap.addEventListener("click", tuneCaptureFeedback);
    var btnTuneInfo = $("btnTunePresetInfo");
    if (btnTuneInfo) btnTuneInfo.addEventListener("click", loadPresetInfo);
    var btnTuneMinus = $("btnTuneNudgeMinus");
    if (btnTuneMinus) btnTuneMinus.addEventListener("click", function () { tuneNudge(-1); });
    var btnTunePlus = $("btnTuneNudgePlus");
    if (btnTunePlus) btnTunePlus.addEventListener("click", function () { tuneNudge(1); });
    var btnTuneSaveCycle = $("btnTuneSaveCycle");
    if (btnTuneSaveCycle) btnTuneSaveCycle.addEventListener("click", saveTuneCycle);
    var btnDetectRefresh = $("btnDetectRefresh");
    if (btnDetectRefresh) btnDetectRefresh.addEventListener("click", loadDetectionMonitor);
    var btnDetectSnapshot = $("btnDetectSnapshot");
    if (btnDetectSnapshot) btnDetectSnapshot.addEventListener("click", function () { return snapshot(); });
    var btnRgbReset = $("btnRgbReset");
    if (btnRgbReset) btnRgbReset.addEventListener("click", resetRgbCamera);
    ["detCropBottom", "detLumTarget", "detCompBeta", "detCompGain", "detMinArea"].forEach(function (id) {
      var el = $(id);
      if (el) el.addEventListener("input", syncDetectorRangeLabels);
    });
    var btnDetectorApply = $("btnDetectorApply");
    if (btnDetectorApply) btnDetectorApply.addEventListener("click", applyDetectorConfig);
    var btnDetectorDetect = $("btnDetectorDetect");
    if (btnDetectorDetect) btnDetectorDetect.addEventListener("click", detectorDetect);
    var btnDetectorDetectDepth = $("btnDetectorDetectDepth");
    if (btnDetectorDetectDepth) btnDetectorDetectDepth.addEventListener("click", detectorDetectDepth);
    dbgBtn("page", "dom_ready", { buttons: Object.keys(DBG.buttons) });
    resetSvcAck();
    log("Dashboard presa caricata (ACK servizi + Home attivi)");
    loadCameraSelection();
    refreshStreamCatalog();
    renderTuneHistory();
    loadTuneCycles();
    loadPresetInfo();
    loadDetectionMonitor();
    loadRgbHealth();
    syncDetectorRangeLabels();
    loadDetectorConfig();
    status();
    setInterval(status, 5000);
    setInterval(loadDetectionMonitor, 4000);
    setInterval(loadRgbHealth, 6000);
  });
})();
