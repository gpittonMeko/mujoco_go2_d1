(function () {
  "use strict";

  var SR = window.__FOCUS_SCRIPT_ROOT__ || "";
  var logLines = [];
  var activeVariant = "j90_left";
  var STEP_INDEX = { move90: 0, rgbd: 1, ik: 2, execute: 3, verify: 4, teach: 5 };

  function api(path) {
    if (!path) path = "/";
    if (path.charAt(0) !== "/") path = "/" + path;
    return SR + path;
  }

  function $(id) { return document.getElementById(id); }

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

  function json(path, opts) {
    return fetch(api(path), Object.assign({ cache: "no-store", credentials: "same-origin" }, opts || {}))
      .then(function (r) {
        return r.text().then(function (t) {
          var j = {};
          try { j = t ? JSON.parse(t) : {}; } catch (e) { j = { ok: false, raw: t.slice(0, 400) }; }
          j._http_status = r.status;
          if (!r.ok && j.ok !== false) j.ok = false;
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

  function instruction() {
    return (($("teachInstruction") && $("teachInstruction").value) || "").trim();
  }

  function status() {
    return Promise.all([
      json("/api/focus/status?_=" + Date.now()).catch(function (e) { return { ok: false, error: String(e) }; }),
      json("/api/pick/teach/samples?_=" + Date.now()).catch(function (e) { return { ok: false, error: String(e) }; }),
      json("/api/arm/status?_=" + Date.now()).catch(function (e) { return { ok: false, error: String(e) }; }),
    ]).then(function (rows) {
      var focus = rows[0];
      var samples = rows[1];
      var arm = rows[2];
      var n = Number(samples.count || (focus.teach && focus.teach.count) || 0);
      var model = !!(samples.has_active_model || (focus.teach && focus.teach.has_active_model));
      var coupled = !!(arm.arm_coupled || (focus.arm && focus.arm.arm_coupled));
      pill(coupled ? (model ? "pronto" : "modello assente") : "braccio libero", coupled && model ? "ok" : "warn");
      summary(
        "Braccio: " + (coupled ? "coppia ON" : "coppia OFF") +
        " | sample teach: " + n +
        " | modello: " + (model ? "attivo" : "da ricreare") +
        " | flag lato: " + labelForVariant(activeVariant)
      );
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
    pill("foto...", "warn");
    setProgress(25, "Foto RGB manuale", "warn", "rgbd");
    log("Foto manuale RealSense polso + detect. Questo comando non muove il braccio.");
    return post("/api/pick/snapshot", {}).then(function (j) {
      log("snapshot completata", j, j.ok ? "info" : "warn");
      if (j.ok && j.detection_ok) {
        var det = j.detection || {};
        pill("detect ok", "ok");
        setProgress(40, "Detect ok", "ok", "rgbd");
        summary("Detect: " + (det.label || "oggetto") + " conf=" + (det.confidence != null ? Number(det.confidence).toFixed(3) : "-"));
      } else {
        pill("detect no", "warn");
        setProgress(25, "Detect non valido", "warn", "rgbd");
        summary(j.hint_it || j.reason || "Oggetto non rilevato.");
      }
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
    log("POST /api/presets/scan/goto - solo movimento 90, nessun controllo camera/pezzo", { variant: variant });
    return post("/api/presets/scan/goto", { variant: variant }).then(function (j) {
      log("movimento 90 completato", j, j.ok ? "info" : "error");
      pill(j.ok ? "90 ok" : "90 err", kindFromOk(j.ok));
      setProgress(j.ok ? 18 : 8, j.ok ? "Posizione " + labelForVariant(variant) + " raggiunta" : "Movimento 90 fallito", j.ok ? "ok" : "err", "move90");
      return j;
    }).catch(function (e) {
      pill("errore", "err");
      setProgress(8, "Movimento 90 fallito", "err", "move90");
      log(String(e), null, "error");
    });
  }

  function graspGoto(variant) {
    activeVariant = variant;
    if (!confirm("Avvicinare alla presa da " + labelForVariant(variant) + "? La pinza resta aperta.")) return;
    pill("avvicino...", "warn");
    setProgress(72, "Avvicinamento manuale", "warn", "execute");
    return post("/api/pick/grasp/goto", { scan_variant: variant }).then(function (j) {
      log("avvicinamento completato", j, j.ok ? "info" : "error");
      pill(j.ok ? "avvicina ok" : "avvicina err", kindFromOk(j.ok));
      setProgress(j.ok ? 82 : 72, j.ok ? "Sulla presa, pinza aperta" : "Avvicinamento fallito", j.ok ? "ok" : "err", "execute");
      return j;
    }).catch(function (e) {
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

  function startGrasp() {
    var variant = activeVariant || "j90_left";
    if (!confirm("Eseguire presa completa con flag " + labelForVariant(variant) + "?\n\nFlusso: muovi 90 -> RGB+depth -> IK -> esegui.")) return;
    pill("presa...", "warn");
    setProgress(5, "Presa: muovi 90", "warn", "move90");
    log("Avvio presa completa", { scan_variant: variant, flow: "move90 -> rgb_depth -> ik -> execute" });
    return post("/api/pick/full_sequence", {
      scan_variant: variant,
      instruction: instruction(),
      execute: true,
      close: true,
    }).then(function (j) {
      log("sequenza presa completata", j, j.ok ? "info" : "error");
      if (j.ok) {
        pill("presa ok", "ok");
        setProgress(100, "Presa completata", "ok", "verify");
      } else {
        pill("presa err", "err");
        setProgress(70, "Presa fallita: " + (j.reason || j.phase || "errore"), "err", stepFromPhase(j.phase));
        summary("Presa fallita. Puoi premere Presa fallita -> teaching o Teaching posizione presa.");
      }
      return j;
    }).catch(function (e) {
      pill("errore", "err");
      setProgress(70, "Presa fallita", "err", "verify");
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
      return post("/api/joints/release", {});
    }).then(function (rel) {
      log("release giunti", rel, rel.ok ? "info" : "warn");
      return countdown(20, "Porta il braccio sulla presa", 55, "teach");
    }).then(function () {
      setProgress(70, "Leggo posizione insegnata", "warn", "teach");
      return json("/api/joints/feedback?_=" + Date.now());
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
    activeVariant = variant;
    if (!confirm("Avviare teaching completo " + labelForVariant(variant) + "? Questo include movimento 90 e foto prima del release.")) return;
    var visionAtScan = null;
    pill("teach completo", "warn");
    setProgress(8, "Teaching: muovi " + labelForVariant(variant), "warn", "move90");
    return post("/api/presets/scan/goto", { variant: variant }).then(function (scan) {
      log("move90 teaching", scan, scan.ok ? "info" : "error");
      if (!scan.ok) throw new Error(scan.reason || "movimento 90 fallito");
      setProgress(25, "Teaching: foto", "warn", "rgbd");
      return post("/api/pick/snapshot", {});
    }).then(function (snap) {
      visionAtScan = snap.last_detection || snap.detection || null;
      log("snapshot teaching", snap, snap.ok ? "info" : "warn");
      return countdown(5, "Preparati al release", 35, "teach");
    }).then(function () {
      setProgress(42, "Release giunti", "warn", "teach");
      return post("/api/joints/release", {});
    }).then(function (rel) {
      log("release giunti", rel, rel.ok ? "info" : "warn");
      return countdown(20, "Muovi il braccio sulla presa", 55, "teach");
    }).then(function () {
      setProgress(70, "Leggo feedback giunti", "warn", "teach");
      return json("/api/joints/feedback?_=" + Date.now());
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
    return post("/api/arm/couple", { with_power: true }).then(function (j) {
      log("coppia braccio", j, j.ok ? "info" : "error");
      pill(j.ok ? "coppia on" : "coppia err", kindFromOk(j.ok));
      return status();
    });
  }

  function armRelease() {
    if (!confirm("Rilasciare i giunti del braccio? Il braccio sara' libero manualmente.")) return;
    pill("release...", "warn");
    setProgress(10, "Release giunti", "warn", "teach");
    return post("/api/joints/release", {}).then(function (j) {
      log("release manuale", j, j.ok ? "info" : "error");
      pill(j.ok ? "release ok" : "release err", kindFromOk(j.ok));
      return status();
    });
  }

  function cancel() {
    return post("/api/grasp/teach_cancel", { reason_it: "annullato da focus dashboard" }).then(function (j) {
      pill("annullato", "warn");
      setProgress(0, "Flusso annullato", "warn", null);
      log("annulla flusso", j, j.ok ? "info" : "warn");
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var btn;
    btn = $("btnTeachStatus"); if (btn) btn.addEventListener("click", function () { status().then(function (s) { log("stato aggiornato", s); }); });
    btn = $("btnSnapshot"); if (btn) btn.addEventListener("click", snapshot);
    btn = $("btnScanLeft90"); if (btn) btn.addEventListener("click", function () { move90("j90_left", true); });
    btn = $("btnScanRight90"); if (btn) btn.addEventListener("click", function () { move90("j90", true); });
    btn = $("btnStartGrasp"); if (btn) btn.addEventListener("click", startGrasp);
    btn = $("btnTeachPosition"); if (btn) btn.addEventListener("click", function () { teachPosition("teaching posizione da pulsante"); });
    btn = $("btnMarkGraspFailed"); if (btn) btn.addEventListener("click", function () { teachPosition("operatore: presa fallita"); });
    btn = $("btnGraspLeft"); if (btn) btn.addEventListener("click", function () { graspGoto("j90_left"); });
    btn = $("btnGraspRight"); if (btn) btn.addEventListener("click", function () { graspGoto("j90"); });
    btn = $("btnManualTeachLeft"); if (btn) btn.addEventListener("click", function () { manualTeach("j90_left"); });
    btn = $("btnManualTeachRight"); if (btn) btn.addEventListener("click", function () { manualTeach("j90"); });
    btn = $("btnBuildTeachModel"); if (btn) btn.addEventListener("click", buildTeachModel);
    btn = $("btnTeachCancel"); if (btn) btn.addEventListener("click", cancel);
    btn = $("btnArmCouple"); if (btn) btn.addEventListener("click", armCouple);
    btn = $("btnArmRelease"); if (btn) btn.addEventListener("click", armRelease);
    btn = $("btnClearLog"); if (btn) btn.addEventListener("click", function () { logLines = []; var el = $("teachLog"); if (el) el.textContent = "-"; });
    log("Dashboard presa caricata");
    status();
    setInterval(status, 5000);
  });
})();
