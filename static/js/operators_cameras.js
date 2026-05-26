(function () {
  "use strict";
  var api = window.operatorsApi;
  if (typeof api !== "function") {
    throw new Error('operators_core.js must load before this module');
  }

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

  window.operatorsFillStreamUrlCodes = fillStreamUrlCodes;

})();
