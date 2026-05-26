(function () {
  "use strict";

  function api(path) {
    if (window.operatorsApi) {
      return window.operatorsApi(path);
    }
    if (window.dashboardApi) {
      return window.dashboardApi(path);
    }
    var sr = (window.__OPERATORS_SCRIPT_ROOT__ || "").replace(/\/$/, "");
    if (!path || path.charAt(0) !== "/") {
      path = "/" + (path || "");
    }
    return sr + path;
  }

  var _calibPreviewTimer = null;

  function _calibEl(id) {
    return document.getElementById(id);
  }

  function calibProgress(pct, line) {
    var f = _calibEl("calibProgressFill");
    var ln = _calibEl("calibPhaseLine");
    if (f) {
      f.style.width = Math.max(0, Math.min(100, pct)) + "%";
    }
    if (ln) {
      ln.textContent = line || "";
    }
  }

  function calibResult(state, title, sub) {
    var blk = _calibEl("calibResultBlock");
    var t = _calibEl("calibResultTitle");
    var s = _calibEl("calibResultSub");
    if (blk) {
      blk.setAttribute("data-state", state);
    }
    if (t) {
      t.textContent = title || "";
    }
    if (s) {
      s.textContent = sub || "";
    }
  }

  function calibBadge(seen) {
    var b = _calibEl("calibDetectionBadge");
    if (!b) {
      return;
    }
    b.setAttribute("data-seen", seen ? "1" : "0");
    b.textContent = seen ? "TAG 5 · OK" : "TAG 5 · —";
  }

  window.calibPreviewTick = async function () {
    var img = _calibEl("calibPreviewImg");
    var sel = _calibEl("tag5_calib_camera");
    if (!img) {
      return;
    }
    var dev = sel && sel.value !== "" ? sel.value : "0";
    try {
      var r = await fetch(
        api("/api/arm/tag5_preview.jpg?device=" + encodeURIComponent(dev) + "&_=" + Date.now()),
        { cache: "no-store" }
      );
      var seen = (r.headers.get("X-Tag5-Seen") || "").trim() === "1";
      calibBadge(seen);
      if (r.ok) {
        var blob = await r.blob();
        if (window.__calibPreviewObjUrl) {
          try {
            URL.revokeObjectURL(window.__calibPreviewObjUrl);
          } catch (e0) {}
        }
        window.__calibPreviewObjUrl = URL.createObjectURL(blob);
        img.src = window.__calibPreviewObjUrl;
      } else {
        img.removeAttribute("src");
        calibBadge(false);
      }
    } catch (e) {
      calibBadge(false);
    }
  };

  window.calibPreviewStart = function () {
    window.calibPreviewStop();
    void window.calibPreviewTick();
    _calibPreviewTimer = setInterval(function () {
      void window.calibPreviewTick();
    }, 1100);
  };

  window.calibPreviewStop = function () {
    if (_calibPreviewTimer) {
      clearInterval(_calibPreviewTimer);
      _calibPreviewTimer = null;
    }
  };

  window.jointEditorLoad = window.jointEditorLoad || function () {};

  window.calibGoArm3d = function () {
    if (window.operatorsSwitchTab) {
      window.operatorsSwitchTab("3d");
    } else if (window.openOpTab) {
      window.openOpTab("arm3d");
    }
    setTimeout(function () {
      try {
        window.jointEditorLoad();
      } catch (e) {}
    }, 120);
    void window.arm3dManualRefreshNow();
  };

  window.operatorsCalibrationOnTabShown = function () {
    void window.tag5CalibRefresh();
    window.calibPreviewStart();
  };

  window.operatorsCalibrationOnTabHidden = function () {
    window.calibPreviewStop();
  };

  window.calibrationFlowReload = async function () {};

  window.tag5CalibRefresh = async function () {
    calibProgress(0, "");
    try {
      var res = await fetch(api("/api/arm/tag5_calibration?_=" + Date.now()), { cache: "no-store" });
      var j = await res.json();
      var nom = j.nominal_env_m;
      if (nom && nom.length >= 3) {
        var nx = _calibEl("tag5_nom_x");
        var ny = _calibEl("tag5_nom_y");
        var nz = _calibEl("tag5_nom_z");
        if (nx && nx.value === "") {
          nx.value = String(nom[0]);
        }
        if (ny && ny.value === "") {
          ny.value = String(nom[1]);
        }
        if (nz && nz.value === "") {
          nz.value = String(nom[2]);
        }
      }
      var hasFile = !!(j.saved && !j.read_error);
      if (j.read_error) {
        calibResult("err", "Stato", String(j.read_error));
      } else if (hasFile) {
        var ts = (j.saved && j.saved.updated_at) || "";
        calibResult("ok", "Calibrazione su disco", ts ? "Ultimo salvataggio: " + ts : "");
      } else {
        calibResult("idle", "Nessun file", "Premi Calibra con il tag 5 in vista.");
      }
      void window.calibPreviewTick();
    } catch (e) {
      calibResult("err", "Stato", String(e));
    }
  };

  window.tag5CalibSave = async function () {
    calibProgress(8, "Acquisizione frame…");
    calibResult("idle", "In corso…", "");
    var btn = _calibEl("calibBtnSave");
    if (btn) {
      btn.disabled = true;
    }
    try {
      var body = {};
      var nx = _calibEl("tag5_nom_x");
      var ny = _calibEl("tag5_nom_y");
      var nz = _calibEl("tag5_nom_z");
      var sel = _calibEl("tag5_calib_camera");
      var xv = nx && nx.value !== "" ? parseFloat(nx.value) : NaN;
      var yv = ny && ny.value !== "" ? parseFloat(ny.value) : NaN;
      var zv = nz && nz.value !== "" ? parseFloat(nz.value) : NaN;
      if (!isNaN(xv) && !isNaN(yv) && !isNaN(zv)) {
        body.nominal_tag5_arm_base_m = [xv, yv, zv];
      }
      if (sel && sel.value !== "") {
        body.camera_device = parseInt(sel.value, 10);
      }
      calibProgress(35, "Rilevamento AprilTag id 5…");
      var res = await fetch(api("/api/arm/tag5_calibration"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      calibProgress(75, "Salvataggio…");
      var j = await res.json();
      calibProgress(100, j.ok ? "Fatto." : "Interrotto.");
      if (j.ok) {
        calibResult("ok", "Successo", "Offset salvato.");
        try {
          if (window.operatorsScene3dReloadSceneOnce) {
            await window.operatorsScene3dReloadSceneOnce();
          }
        } catch (e1) {}
        try {
          if (typeof arm3dPoll === "function") {
            await arm3dPoll(false);
          }
        } catch (e2) {}
        void window.calibPreviewTick();
      } else {
        calibResult("err", "Fallito", (j.error && String(j.error)) || "Errore");
      }
    } catch (e) {
      calibProgress(100, "");
      calibResult("err", "Fallito", String(e));
    } finally {
      if (btn) {
        btn.disabled = false;
      }
    }
  };

  window.tag5CalibClear = async function () {
    calibProgress(40, "Cancellazione…");
    try {
      var res = await fetch(api("/api/arm/tag5_calibration"), { method: "DELETE" });
      var j = await res.json();
      calibProgress(100, "");
      if (j.ok) {
        calibResult("idle", "File rimosso", "");
        try {
          if (window.operatorsScene3dReloadSceneOnce) {
            await window.operatorsScene3dReloadSceneOnce();
          }
        } catch (e1) {}
        try {
          if (typeof arm3dPoll === "function") {
            await arm3dPoll(false);
          }
        } catch (e2) {}
      } else {
        calibResult("err", "Errore", (j.error && String(j.error)) || "");
      }
      void window.tag5CalibRefresh();
    } catch (e) {
      calibResult("err", "Errore", String(e));
    }
  };

  window.arm3dManualRefreshNow = async function () {
    try {
      if (window.operatorsScene3dReloadSceneOnce) {
        await window.operatorsScene3dReloadSceneOnce();
      } else if (window.operatorsScene3dStart) {
        window.operatorsScene3dStart();
      } else if (typeof arm3dPoll === "function") {
        await arm3dPoll(false);
      }
    } catch (e) {}
  };

  function _calibBindCameraSelect() {
    var sel = document.getElementById("tag5_calib_camera");
    if (sel && !sel._calibBound) {
      sel._calibBound = true;
      sel.addEventListener("change", function () {
        void window.calibPreviewTick();
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _calibBindCameraSelect);
  } else {
    _calibBindCameraSelect();
  }
})();
