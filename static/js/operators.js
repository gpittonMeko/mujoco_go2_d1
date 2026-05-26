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

  window.__operatorsLastGraspPlan = null;
  window.__operatorsGraspPointsBaseLink_m = null;
  /** Ultimo URL immagine noto per anteprima VLA (preset o ``image_url_used`` dal worker). */
  window.__operatorsVlaInputUrl = null;

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
    set("urlBoxDetect6", "/api/vision/box_detect?camera=6");
  }

  function formatCameraStatusForHuman(j) {
    var showJsonEl = document.getElementById("cameraStatusShowJson");
    var showJson = showJsonEl && showJsonEl.checked;
    var sum = j.camera_summary;
    var lines = [];
    if (sum && typeof sum === "object") {
      lines.push("—— log.0 (polso / spesso Orbbec) ——");
      var s0 = sum["0"];
      if (s0) {
        lines.push(
          "Device: " +
            s0.device_path +
            "   sysfs: " +
            (s0.sysfs_name || "—") +
            "   stream: " +
            (s0.stream_kind || "—") +
            (s0.color_ok ? "   [OK colore]" : "   [NON è RGB — probabile IR/depth]")
        );
        if (s0.error) {
          lines.push("Cache: " + s0.error);
        }
        if (s0.fix_it) {
          lines.push("");
          lines.push("Cosa fare: " + s0.fix_it);
        }
      }
      lines.push("");
      lines.push("—— log.6 (frontale / spesso RealSense) ——");
      var s6 = sum["6"];
      if (s6) {
        lines.push(
          "Device: " +
            s6.device_path +
            "   sysfs: " +
            (s6.sysfs_name || "—") +
            "   stream: " +
            (s6.stream_kind || "—") +
            (s6.color_ok ? "   [OK colore]" : "   [controlla stream]")
        );
        if (s6.error) {
          lines.push("Cache: " + s6.error);
        }
      }
      lines.push("");
      lines.push("Endpoint: GET /api/cameras/status");
    }
    if (j.dashboard_http_origin) {
        lines.push("");
        lines.push("—— Endpoint JPEG (path assoluti lato server) ——");
        lines.push("Origin rilevato: " + j.dashboard_http_origin);
        var ou = j.openvla_jpeg_urls;
        if (ou) {
          lines.push("log.0 JPEG: " + ou.logical_0_jpg);
          lines.push("log.6 JPEG: " + ou.logical_6_jpg);
          lines.push(
            "vla_frame: " +
              ou.vla_frame_jpg +
              (ou.vla_frame_configured ? " (GO2_VLA_SNAPSHOT_V4L_INDEX OK)" : " (non configurato sulla NX)")
          );
        }
      }
      if (
        j.orbbec_rgb_v4l_sysfs_hints &&
        j.orbbec_rgb_v4l_sysfs_hints.length &&
        j.camera_summary &&
        j.camera_summary["0"] &&
        j.camera_summary["0"].color_ok === false
      ) {
        lines.push("");
        lines.push("—— Suggerimento Orbbec (sysfs nome «rgb/color») ——");
        for (var hi = 0; hi < Math.min(4, j.orbbec_rgb_v4l_sysfs_hints.length); hi++) {
          var hh = j.orbbec_rgb_v4l_sysfs_hints[hi];
          lines.push("/dev/video" + hh.v4l_index + " — " + (hh.sysfs_name || ""));
        }
      }
      if (j.fix_log0_export_hint_sh) {
        lines.push("");
        lines.push("—— Comando NX (log.0 non RGB) ——");
        lines.push(j.fix_log0_export_hint_sh + "   # poi riavvia la dashboard");
        if (j.fix_log0_sysfs_name) {
          lines.push(" sysfs: " + j.fix_log0_sysfs_name);
        }
      }
      if (j.orbbec_logical_0_probe_debug && typeof j.orbbec_logical_0_probe_debug === "object") {
        lines.push("");
        lines.push("—— Debug auto-scelta V4L Orbbec (log.0) ——");
        lines.push(JSON.stringify(j.orbbec_logical_0_probe_debug, null, 2));
      }
      if (j.runtime_v4l_by_logical && typeof j.runtime_v4l_by_logical === "object") {
        lines.push("");
        lines.push("—— Override V4L da dashboard (senza riavvio) ——");
        lines.push(JSON.stringify(j.runtime_v4l_by_logical, null, 2));
      }
      if (j.video_index_env_lock && typeof j.video_index_env_lock === "object") {
        lines.push("");
        lines.push("—— Variabili GO2_VIDEO_INDEX_* attive sulla NX ——");
        lines.push(JSON.stringify(j.video_index_env_lock, null, 2));
      }
    var human = lines.length ? lines.join("\n") : "";
    if (!showJson) {
      return human || JSON.stringify({ ok: j.ok, cameras: j.cameras, note: "camera_summary assente" }, null, 2);
    }
    var slim = {
      ok: j.ok,
      go2_local: j.go2_local,
      camera_summary: j.camera_summary,
      cameras: j.cameras,
      v4l_index_by_logical: j.v4l_index_by_logical,
      sysfs_card_name_by_logical: j.sysfs_card_name_by_logical,
      v4l_usb_auto_map: j.v4l_usb_auto_map,
      depth_v4l_index_by_logical: j.depth_v4l_index_by_logical,
      v4l_usb_inventory: j.v4l_usb_inventory,
      v4l_usb_inventory_error: j.v4l_usb_inventory_error,
      dashboard_http_origin: j.dashboard_http_origin,
      openvla_jpeg_urls: j.openvla_jpeg_urls,
      openvla_vla_frame: j.openvla_vla_frame,
      orbbec_rgb_v4l_sysfs_hints: j.orbbec_rgb_v4l_sysfs_hints,
      orbbec_logical_0_probe_debug: j.orbbec_logical_0_probe_debug,
      v4l_nodes_detail: j.v4l_nodes_detail,
      v4l_nodes_detail_note_it: j.v4l_nodes_detail_note_it,
      v4l_nodes_detail_error: j.v4l_nodes_detail_error,
      runtime_v4l_by_logical: j.runtime_v4l_by_logical,
      video_index_env_lock: j.video_index_env_lock,
      v4l_pick_candidates: j.v4l_pick_candidates,
      v4l_pick_note_it: j.v4l_pick_note_it,
    };
    return (human ? human + "\n\n—— JSON ——\n" : "") + JSON.stringify(slim, null, 2);
  }

  function operatorsUpdateCameraBanner(j) {
    var wrap = document.getElementById("cameraLog0Warn");
    var tx = document.getElementById("cameraLog0WarnText");
    var s0 = j && j.camera_summary && j.camera_summary["0"];
    if (!wrap || !tx) {
      return;
    }
    if (s0 && s0.color_ok === false && s0.fix_it) {
      wrap.style.display = "block";
      tx.textContent = s0.fix_it;
    } else if (s0 && s0.color_ok === false) {
      wrap.style.display = "block";
      tx.textContent =
        "Il frame non risulta colore (stream_kind=" +
        String(s0.stream_kind) +
        "). Verifica GO2_VIDEO_INDEX_0 su un nodo RGB Orbbec.";
    } else {
      wrap.style.display = "none";
      tx.textContent = "";
    }
  }

  function operatorsUpdateCamLiveBadges(j) {
    function setBadge(id, summary, logical) {
      var el = document.getElementById(id);
      if (!el || !summary) {
        return;
      }
      var s = summary[String(logical)];
      if (!s) {
        el.textContent = "Stato camera non disponibile (GO2_LOCAL=0?)";
        el.style.color = "#94a3b8";
        return;
      }
      var ok = s.color_ok === true ? "RGB OK" : s.color_ok === false ? "⚠ non RGB (prob. depth/IR)" : "stream ?";
      el.textContent =
        "log." +
        logical +
        " → " +
        (s.device_path || "") +
        " · " +
        String(s.sysfs_name || "—").slice(0, 56) +
        " · " +
        ok;
      el.style.color = s.color_ok === false ? "#fecaca" : s.color_ok ? "#86efac" : "#cbd5e1";
    }
    var sum = j && j.camera_summary;
    setBadge("cam0LiveBadge", sum, 0);
    setBadge("cam6LiveBadge", sum, 6);
    if (j && j.openvla_jpeg_urls) {
      window.__operatorsOpenvlaJpegUrls = j.openvla_jpeg_urls;
    }
    var vr = document.getElementById("openvlaImgSrcVla");
    if (vr && j && j.openvla_jpeg_urls) {
      vr.disabled = !j.openvla_jpeg_urls.vla_frame_configured;
    }
    var hint = document.getElementById("cameraLog0ExportHint");
    if (hint) {
      var code = hint.querySelector("code");
      if (j && j.fix_log0_export_hint_sh && code) {
        hint.style.display = "block";
        code.textContent = j.fix_log0_export_hint_sh;
        var sn = hint.querySelector(".fix-sysfs");
        if (sn) {
          sn.textContent = j.fix_log0_sysfs_name || "";
        }
      } else {
        hint.style.display = "none";
      }
    }
  }

  window.__operatorsCamPickerLists = { 0: [], 6: [] };

  function operatorsUpdateCamPickers(j) {
    function upd(logical) {
      var k = String(logical);
      var wrap = document.getElementById(logical === 0 ? "cam0Picker" : "cam6Picker");
      var lab = document.getElementById(logical === 0 ? "cam0PickLabel" : "cam6PickLabel");
      var meta = document.getElementById(logical === 0 ? "cam0PickMeta" : "cam6PickMeta");
      if (!wrap || !lab || !meta) {
        return;
      }
      if (!j || !j.go2_local) {
        wrap.classList.add("is-disabled");
        lab.textContent = "—";
        meta.textContent = "Selezione V4L solo sulla NX (GO2_LOCAL=1).";
        return;
      }
      var list = (j.v4l_pick_candidates && j.v4l_pick_candidates[k]) || [];
      window.__operatorsCamPickerLists[logical] = list.slice();
      var lock = j.video_index_env_lock && j.video_index_env_lock[k];
      var cur = null;
      if (j.v4l_index_by_logical && j.v4l_index_by_logical[k] != null) {
        cur = parseInt(String(j.v4l_index_by_logical[k]), 10);
      }
      if (lock) {
        wrap.classList.add("is-disabled");
        lab.textContent = "/dev/video" + (isFinite(cur) ? cur : "—");
        meta.textContent = "Bloccato da GO2_VIDEO_INDEX_" + k + " sull'ambiente NX.";
        return;
      }
      wrap.classList.remove("is-disabled");
      if (!list.length) {
        lab.textContent = "Nessun device USB per questo slot";
        meta.textContent =
          logical === 0 ? "Collega Orbbec / Sonix (0735:0269, 2bc5:080b)." : "Collega RealSense (8086:0b3a).";
        return;
      }
      var pos = 0;
      if (isFinite(cur)) {
        var ix = list.indexOf(cur);
        pos = ix >= 0 ? ix : 0;
      }
      window["__operatorsCamPickerPos" + k] = pos;
      var v = list[pos];
      lab.textContent = "/dev/video" + v + " (" + (pos + 1) + "/" + list.length + ")";
      var rt = j.runtime_v4l_by_logical && j.runtime_v4l_by_logical[k];
      meta.textContent =
        "Nodo RGB: usa ◀ ▶ · " + (rt != null ? "override UI attivo" : "mappa automatica / default");
    }
    upd(0);
    upd(6);
  }

  window.operatorsCamPickerStep = function (logical, delta) {
    var j = window.operatorsLastCameraStatus;
    if (!j || !j.go2_local) {
      return;
    }
    var k = String(logical);
    if (j.video_index_env_lock && j.video_index_env_lock[k]) {
      return;
    }
    var list = window.__operatorsCamPickerLists[logical] || [];
    if (!list.length) {
      return;
    }
    var curV4l =
      j.v4l_index_by_logical && j.v4l_index_by_logical[k] != null
        ? parseInt(String(j.v4l_index_by_logical[k]), 10)
        : NaN;
    var pos = 0;
    if (isFinite(curV4l)) {
      var ix = list.indexOf(curV4l);
      pos = ix >= 0 ? ix : 0;
    } else if (typeof window["__operatorsCamPickerPos" + k] === "number") {
      pos = window["__operatorsCamPickerPos" + k];
    }
    pos = (pos + delta + list.length) % list.length;
    var nextV = list[pos];
    window["__operatorsCamPickerPos" + k] = pos;
    var pre = document.getElementById("cameraStatusPre");
    if (pre) {
      pre.textContent = "Applico log." + k + " → /dev/video" + nextV + "…";
    }
    fetch(api("/api/cameras/runtime_map"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ logical: parseInt(String(logical), 10), v4l_index: nextV }),
    })
      .then(function (r) {
        return r.json().then(function (jj) {
          return { status: r.status, jj: jj };
        });
      })
      .then(function (o) {
        if (!o.jj.ok) {
          var err = (o.jj.errors && o.jj.errors.join("; ")) || JSON.stringify(o.jj);
          if (pre) {
            pre.textContent = "Errore mappa camera: " + err;
          }
          return;
        }
        window.operatorsBumpMjpegStreams();
        window.operatorsRefreshCamerasStatus();
      })
      .catch(function (e) {
        if (pre) {
          pre.textContent = String(e);
        }
      });
  };

  function operatorsRenderV4lNodesDetail(j) {
    var wrap = document.getElementById("v4lNodesDetailWrap");
    var note = document.getElementById("v4lNodesDetailNote");
    if (!wrap) {
      return;
    }
    var rows = j && j.v4l_nodes_detail;
    if (j && j.v4l_nodes_detail_error && note) {
      note.textContent = "Errore inventario esteso: " + String(j.v4l_nodes_detail_error);
    } else if (note && j && j.v4l_nodes_detail_note_it) {
      note.textContent = j.v4l_nodes_detail_note_it;
    }
    if (!rows || !rows.length) {
      wrap.innerHTML =
        '<p class="muted small">Nessun nodo V4L in elenco (serve GO2_LOCAL=1 sulla NX e almeno una camera USB).</p>';
      return;
    }
    function esc(s) {
      return String(s == null ? "" : s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/"/g, "&quot;");
    }
    var html = [
      '<table class="v4l-nodes-table"><thead><tr>',
      "<th>Dev</th><th>Famiglia</th><th>USB</th><th>sysfs</th><th>guess</th>",
      "<th>→ log RGB</th><th>→ log depth</th><th>slot mappa auto</th><th>JPEG</th>",
      "</tr></thead><tbody>",
    ];
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      var fam = String(r.device_family || "");
      var famCls = fam.indexOf("orbbec") >= 0 ? "fam-ob" : fam.indexOf("realsense") >= 0 ? "fam-rs" : "";
      var ppath = r.preview_jpg_path || "/api/cameras/v4l/" + r.v4l_index + "/preview.jpg";
      var prev = api(ppath);
      html.push("<tr>");
      html.push("<td><strong>/dev/video" + esc(r.v4l_index) + "</strong></td>");
      html.push('<td class="' + famCls + '">' + esc(fam) + "</td>");
      html.push("<td>" + esc(r.usb_vid_pid) + "</td>");
      html.push("<td>" + esc(r.sysfs_name) + "</td>");
      html.push("<td>" + esc(r.sysfs_stream_guess) + "</td>");
      html.push(
        "<td>" +
          (r.maps_as_rgb_for_logical && r.maps_as_rgb_for_logical.length
            ? esc(r.maps_as_rgb_for_logical.join(", "))
            : "—") +
          "</td>"
      );
      html.push(
        "<td>" +
          (r.maps_as_depth_for_logical && r.maps_as_depth_for_logical.length
            ? esc(r.maps_as_depth_for_logical.join(", "))
            : "—") +
          "</td>"
      );
      html.push(
        "<td>" +
          (r.dashboard_logical_slots && r.dashboard_logical_slots.length
            ? esc(r.dashboard_logical_slots.join(", "))
            : "—") +
          "</td>"
      );
      html.push(
        '<td><img class="v4l-thumb" alt="preview ' +
          esc(r.v4l_index) +
          '" src="' +
          prev +
          "?_=" +
          Date.now() +
          '" loading="lazy" width="140" /></td>'
      );
      html.push("</tr>");
    }
    html.push("</tbody></table>");
    wrap.innerHTML = html.join("");
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
        window.operatorsLastCameraStatus = j;
        operatorsUpdateCameraBanner(j);
        operatorsUpdateCamLiveBadges(j);
        operatorsUpdateCamPickers(j);
        operatorsRenderV4lNodesDetail(j);
        pre.textContent = formatCameraStatusForHuman(j);
      })
      .catch(function (e) {
        pre.textContent = String(e);
      });
  };

  document.addEventListener("change", function (ev) {
    if (
      ev.target &&
      ev.target.id === "cameraStatusShowJson" &&
      window.operatorsLastCameraStatus &&
      document.getElementById("cameraStatusPre")
    ) {
      document.getElementById("cameraStatusPre").textContent = formatCameraStatusForHuman(
        window.operatorsLastCameraStatus
      );
    }
  });

  function setGraspPhase(msg) {
    var el = document.getElementById("graspPhase");
    if (el) {
      el.textContent = msg;
    }
  }

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

  function operatorsArmPostConfirm(url, confirmToken, confirmMessage) {
    if (!window.confirm(confirmMessage)) {
      return;
    }
    setGraspPhase("POST " + url + "…");
    fetch(api(url), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: confirmToken }),
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
    operatorsArmPostConfirm(
      "/api/arm/goto_saved_start",
      "ARM_GOTO_SAVED_START",
      "Portare il braccio alla posa START registrata (start_alignment.json, arm_at_start). Confermi?"
    );
  };

  window.operatorsArmGotoZeroThenStart = function () {
    operatorsArmPostConfirm(
      "/api/arm/goto_zero_then_start",
      "ARM_GOTO_ZERO_THEN_START",
      "Due movimenti in sequenza: ZERO salvato, poi START salvato. Confermi?"
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

  window.operatorsGraspHealth = function () {
    setGraspPhase("GET /api/grasp/health…");
    window.operatorsGraspProgressUi({ step: 0, label: "Verifica worker grasp…" });
    return fetch(api("/api/grasp/health?_=" + Date.now()), { cache: "no-store" })
      .then(function (r) {
        return r.json();
      })
      .then(function (j) {
        setGraspPhase(JSON.stringify(j, null, 2));
        if (j && j.ok !== false) {
          window.operatorsGraspProgressUi({ step: 1, label: "Worker OK — invio piano…" });
          return j;
        }
        window.operatorsGraspProgressUi({ step: 0, label: "Worker in errore o non raggiungibile", error: true });
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
        setGraspPhase(JSON.stringify(o.j, null, 2) + "\nHTTP " + o.status);
        if (o.status >= 400 || (o.j && o.j.ok === false)) {
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

  window.operatorsExecuteLastPlanIkArm = function () {
    var lp = window.__operatorsLastGraspPlan;
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

  function tick() {
    window.operatorsPollSportLast();
    if (document.getElementById("stackStatus")) {
      var p = document.querySelector('.tab-panel[data-tab="stato"]');
      if (p && p.classList.contains("active")) {
        window.operatorsRefreshStack();
        var pollMc = document.getElementById("missionConsolePoll");
        if (pollMc && pollMc.checked && window.operatorsMissionConsoleRefresh) {
          window.operatorsMissionConsoleRefresh();
        }
      }
    }
    if (document.getElementById("cameraStatusPre")) {
      var sc = document.querySelector('.tab-panel[data-tab="scene"]');
      var gr = document.querySelector('.tab-panel[data-tab="grasp"]');
      if (
        (sc && sc.classList.contains("active")) ||
        (gr && gr.classList.contains("active"))
      ) {
        window.operatorsRefreshCamerasStatus();
      }
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    fillStreamUrlCodes();
    operatorsWireMjpegStream(document.getElementById("cam0Preview"));
    operatorsWireMjpegStream(document.getElementById("cam6Preview"));
    operatorsWireMjpegStream(document.getElementById("graspDockCam0"));
    operatorsWireMjpegStream(document.getElementById("graspDockCam6"));
    window.operatorsBumpMjpegStreams();
    document.addEventListener("click", function (ev) {
      var t = ev.target;
      if (!t || !t.closest) {
        return;
      }
      var reset = t.closest(".op-grasp-progress-reset");
      if (reset && window.operatorsGraspProgressReset) {
        window.operatorsGraspProgressReset();
        return;
      }
      var jmp = t.closest("[data-op-jump-tab]");
      if (jmp) {
        var tab = jmp.getAttribute("data-op-jump-tab");
        if (tab && window.operatorsSwitchTab) {
          window.operatorsSwitchTab(tab);
          if (tab === "3d" && window.operatorsScene3dStart) {
            window.operatorsScene3dStart();
          }
        }
        return;
      }
      var dockRefresh = t.closest("#graspDockRefreshBtn");
      if (dockRefresh && window.operatorsGraspDockBumpPreviews) {
        window.operatorsGraspDockBumpPreviews();
      }
    });
    var mpub = document.getElementById("missionBoxPickPublicBase");
    if (mpub && !String(mpub.value || "").trim()) {
      var sr0 = (window.__OPERATORS_SCRIPT_ROOT__ || "").replace(/\/+$/, "");
      mpub.value = (window.location.origin || "") + sr0;
    }
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
    window.operatorsGraspDockBumpPreviews();
  });
})();
