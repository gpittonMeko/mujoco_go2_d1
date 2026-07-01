(function () {
  "use strict";

  window.__graspCoachStepBusy = false;
  window.__graspCoachProgressTimer = null;
  window.__graspCoachLastResponse = null;

  function _dbgAgentLog(location, message, data, hypothesisId) {
    // #region agent log
    try {
      fetch("http://127.0.0.1:7648/ingest/1f6b2724-6bbf-4c6c-a795-45910cf4b1c4", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Debug-Session-Id": "16a61f" },
        body: JSON.stringify({
          sessionId: "16a61f",
          runId: "scan-start-v2",
          hypothesisId: hypothesisId || "H-JS",
          location: location,
          message: message,
          data: data || {},
          timestamp: Date.now(),
        }),
      }).catch(function () {});
    } catch (_eDbg) {}
    // #endregion
  }

  function _fillEl(id, pct) {
    var fill = document.getElementById(id);
    if (fill) {
      fill.style.width = Math.max(0, Math.min(100, pct)) + "%";
    }
  }

  function _labelEl(id, text) {
    var lab = document.getElementById(id);
    if (lab) {
      lab.textContent = text || "";
    }
  }

  function graspCoachProgressStart() {
    var wrap = document.getElementById("graspCoachProgressWrap");
    var fill = document.getElementById("graspCoachProgressFill");
    if (fill) {
      fill.classList.remove("err");
    }
    if (wrap) {
      wrap.hidden = false;
    }
    _fillEl("graspCoachProgressFill", 4);
    _labelEl("graspCoachProgressLabel", "Elaborazione step… (camera, JSON, eventuale IK)");
    if (window.__graspCoachProgressTimer) {
      clearInterval(window.__graspCoachProgressTimer);
    }
    var p = 4;
    window.__graspCoachProgressTimer = setInterval(function () {
      p += 2 + Math.random() * 5;
      if (p > 90) {
        p = 90;
      }
      _fillEl("graspCoachProgressFill", p);
    }, 260);
  }

  function graspCoachProgressDone(ok, timingsMs, errHint, resp) {
    if (window.__graspCoachProgressTimer) {
      clearInterval(window.__graspCoachProgressTimer);
      window.__graspCoachProgressTimer = null;
    }
    var fill = document.getElementById("graspCoachProgressFill");
    if (fill) {
      if (!ok) {
        fill.classList.add("err");
      } else {
        fill.classList.remove("err");
      }
    }
    _fillEl("graspCoachProgressFill", 100);
    var ms =
      timingsMs &&
      timingsMs.openai_http_ms != null &&
      !isNaN(Number(timingsMs.openai_http_ms))
        ? Math.round(Number(timingsMs.openai_http_ms))
        : null;
    var mode = resp && resp.coach_mode ? String(resp.coach_mode) : "";
    var lab = "";
    if (ok) {
      if (mode === "lateral_metric_only") {
        lab = "Step metrico OK (RealSense + IK, senza GPT). Feedback opzionale sotto.";
      } else if (ms != null) {
        lab =
          "Coach GPT — risposta pronta (~" +
          ms +
          " ms OpenAI). Leggi il riquadro italiano sotto.";
      } else {
        lab = "Step completato. Controlla risposta coach sotto.";
      }
    } else {
      lab =
        "Step terminato senza successo" +
        (errHint ? ": " + errHint : "") +
        ". Controlla il JSON sotto.";
    }
    _labelEl("graspCoachProgressLabel", lab);
    window.setTimeout(function () {
      var w = document.getElementById("graspCoachProgressWrap");
      if (w) {
        w.hidden = true;
      }
      _fillEl("graspCoachProgressFill", 0);
      _labelEl("graspCoachProgressLabel", "");
      if (fill) {
        fill.classList.remove("err");
      }
    }, 3800);
  }

  window.operatorsGraspCoachStatus = function () {
    var pre = document.getElementById("graspCoachPre");
    var t0 = typeof performance !== "undefined" ? performance.now() : 0;
    fetch(window.operatorsApi("/api/grasp_coach/status"))
      .then(function (r) {
        return r.json().then(function (j) {
          return { j: j, r: r };
        });
      })
      .then(function (pack) {
        if (pre) {
          var foot =
            typeof window.operatorsHttpTimingFooterLines === "function"
              ? window.operatorsHttpTimingFooterLines(pack.r, t0)
              : "";
          pre.textContent = JSON.stringify(pack.j, null, 2) + foot;
        }
        window.__graspCoachStatus = pack.j;
        if (typeof _graspTeachApplyCoachBadge === "function") {
          var startVar =
            typeof window.operatorsStartVariant === "function"
              ? window.operatorsStartVariant()
              : "lateral";
          _graspTeachApplyCoachBadge(pack.j, startVar);
        }
      })
      .catch(function (e) {
        if (pre) {
          pre.textContent = String(e);
        }
      });
  };

  window.operatorsGraspCoachBlendLabel = function () {
    var el = document.getElementById("graspCoachBlend");
    var lab = document.getElementById("graspCoachBlendVal");
    if (el && lab) {
      lab.textContent = el.value + "%";
    }
  };

  window.operatorsGraspCoachChainLabel = function () {
    var el = document.getElementById("graspCoachChain");
    var lab = document.getElementById("graspCoachChainVal");
    if (el && lab) {
      lab.textContent = el.value;
    }
  };

  function _graspCoachBuildBody() {
    var hero = document.getElementById("graspTeachInstruction");
    var ta = document.getElementById("graspCoachInstruction");
    if (hero && ta && hero.value !== ta.value) {
      ta.value = String(hero.value || "").trim();
    }
    var sess = document.getElementById("graspCoachSession");
    var execEl = document.getElementById("graspCoachExecute");
    var depthEl = document.getElementById("graspCoachDepth");
    var camEl = document.getElementById("graspCoachCam");
    var blendEl = document.getElementById("graspCoachBlend");
    var instr =
      (hero && hero.value && String(hero.value).trim()) ||
      (ta && ta.value && String(ta.value).trim()) ||
      "";
    if (!instr) {
      return null;
    }
    if (typeof window.__graspCoachStepIndex !== "number" || window.__graspCoachStepIndex < 0) {
      window.__graspCoachStepIndex = 0;
    }
    var body = {
      instruction: instr,
      execute: !!(execEl && execEl.checked),
      include_depth: !!(depthEl && depthEl.checked),
      logical_camera_rgb: 0,
      step_index: window.__graspCoachStepIndex,
    };
    if (typeof window.operatorsStartVariantPayload === "function") {
      Object.keys(window.operatorsStartVariantPayload()).forEach(function (k) {
        body[k] = window.operatorsStartVariantPayload()[k];
      });
    }
    if (blendEl) {
      var pct = parseInt(blendEl.value, 10);
      if (!isNaN(pct) && pct > 0) {
        body.approach_blend_override = pct / 100;
      }
    }
    if (sess && sess.value && String(sess.value).trim()) {
      body.session_note = String(sess.value).trim();
    }
    return body;
  }

  function _graspCoachRenderMetricPanel(j) {
    var mg = (j && j.metric_grounding) || {};
    var elObj = document.getElementById("graspCoachMetObj");
    var elConf = document.getElementById("graspCoachMetConf");
    var elDepth = document.getElementById("graspCoachMetDepth");
    var elReach = document.getElementById("graspCoachMetReach");
    var elTgt = document.getElementById("graspCoachMetTarget");
    var elSt = document.getElementById("graspCoachMetStatus");
    if (!elSt) {
      return;
    }
    var metricOk = Boolean((j && j.ok && mg.ok !== false) || (mg && mg.ok === true));
    var partialRgb = Boolean(mg && mg.partial_rgb_ok);
    if (metricOk) {
      if (elObj) {
        elObj.textContent = (mg.label || "oggetto") + (mg.backend ? " (" + mg.backend + ")" : "");
        elObj.style.color = "#30d070";
      }
      if (elConf) {
        elConf.textContent = mg.confidence != null ? Number(mg.confidence).toFixed(2) : "—";
      }
      if (elDepth) {
        elDepth.textContent = mg.depth_m != null ? Number(mg.depth_m).toFixed(3) + " m" : "—";
      }
      if (elReach) {
        elReach.textContent =
          mg.reach_m != null
            ? Number(mg.reach_m).toFixed(3) +
              " m" +
              (mg.reachable === false ? " FUORI REACH" : " OK")
            : "—";
        if (mg.reachable === false && elReach) {
          elReach.style.color = "#d05050";
        } else if (elReach) {
          elReach.style.color = "#30d070";
        }
      }
      var tgt = mg.grasp_display_base_link_m || (j.interpreted && j.interpreted.target_xyz_base_link_m);
      if (elTgt) {
        elTgt.textContent =
          tgt && tgt.length >= 3
            ? "x=" + Number(tgt[0]).toFixed(3) + " y=" + Number(tgt[1]).toFixed(3) + " z=" + Number(tgt[2]).toFixed(3)
            : "—";
      }
      elSt.textContent =
        (j.label_it || "Acquisizione OK — target metrico pronto") +
        (mg.teach_calib_applied
          ? " · offset calib #" +
            (mg.teach_calib_sample_id || "?") +
            (mg.teach_calib_delta_servo_deg
              ? " Δ[" + mg.teach_calib_delta_servo_deg.slice(0, 6).map(function (x) { return Number(x).toFixed(1); }).join("° ") + "°]"
              : "")
          : "");
      elSt.style.color = "#30d070";
    } else if (partialRgb || j.object_visible === true) {
      if (elObj) {
        elObj.textContent = (mg.label || "oggetto") + " (RGB sì, depth no)";
        elObj.style.color = "#e0a040";
      }
      if (elConf) {
        elConf.textContent = mg.confidence != null ? Number(mg.confidence).toFixed(2) : "—";
      }
      if (elDepth) {
        elDepth.textContent = "depth insufficiente";
      }
      if (elReach) {
        elReach.textContent = "—";
      }
      if (elTgt) {
        elTgt.textContent = "—";
      }
      elSt.textContent = j.label_it || "RGB OK — depth Orbbec da rifare";
      elSt.style.color = "#e0a040";
    } else {
      if (elObj) {
        elObj.textContent = "non rilevato";
        elObj.style.color = "#d05050";
      }
      if (elConf) {
        elConf.textContent = "—";
      }
      if (elDepth) {
        elDepth.textContent = "—";
      }
      if (elReach) {
        elReach.textContent = "—";
        elReach.style.color = "";
      }
      if (elTgt) {
        elTgt.textContent = "—";
      }
      var why = mg.reason || j.reason || "nessun oggetto nel frame polso";
      if (why === "no_depth_support" || why === "depth_failed") {
        why = "depth assente nel bbox (centro oggetto spesso a 0) — riprova";
      } else if (why === "orbbec_busy") {
        why = "Orbbec occupato — attendi 3s e riprova acquisizione";
      } else if (why === "no_aligned_frame" || why === "capture_failed") {
        why = "cattura SDK Orbbec fallita — attendi 5s e riprova";
      }
      if (mg.hint_it || j.hint_it) {
        why = mg.hint_it || j.hint_it;
      }
      if (mg.calib_fallback || (mg.detection && mg.detection.calib_fallback)) {
        why = "calib colore stretta — fallback default (" + (mg.reason || "ok") + ")";
      }
      if (j && j.reason === "openai_failed") {
        why = "coach LLM non raggiungibile (in laterale usa solo Orbbec — riprova)";
      }
      if (j && j.preview_only && j.reason === "preview_error") {
        why = "errore cattura Orbbec — attendi 10s e riprova";
      }
      var hint = mg.hint_it || j.hint_it || "";
      elSt.textContent = "Acquisizione fallita: " + why + (hint ? " — " + hint : "");
      elSt.style.color = "#d0a000";
    }
  }

  function _graspCoachDrawViz(j) {
    var wrap = document.getElementById("graspCoachVizWrap");
    var canvas = document.getElementById("graspCoachViz");
    var statusEl = document.getElementById("graspCoachVizStatus");
    if (!wrap || !canvas) {
      return;
    }
    var interp = (j && j.interpreted) || {};
    var visible = j && typeof j.object_visible !== "undefined" ? j.object_visible : interp.object_visible;
    var px = (j && j.object_pixel_norm) || interp.object_pixel_norm || null;
    var blocked = interp.nogo_blocked;
    var nogo = interp.nogo_zone;
    var b64 = j && j.rgb_preview_b64;
    wrap.hidden = false;
    if (statusEl) {
      var parts = [];
      if (mg && mg.ok === true) {
        visible = true;
      } else if (mg && mg.ok === false) {
        visible = false;
      }
      parts.push(
        visible === true ? "Oggetto: visto" : visible === false ? "Oggetto: NON visto" : "Oggetto: —"
      );
      if (px) {
        parts.push("pos frame u=" + px[0].toFixed(2) + " v=" + px[1].toFixed(2));
      }
      if (interp.tool_tip_base_link_m_now) {
        var t = interp.tool_tip_base_link_m_now;
        parts.push("punta x=" + t[0].toFixed(2) + " y=" + t[1].toFixed(2) + " z=" + t[2].toFixed(2));
      }
      var src = interp.target_source || j.target_source;
      if (src) {
        parts.push("target: " + (src === "metric_orbbec" ? "DEPTH metrica" : "modello (stima)"));
      }
      if (j.motion && typeof j.motion.tcp_reach_error_m !== "undefined") {
        parts.push("errore TCP=" + (j.motion.tcp_reach_error_m * 100).toFixed(1) + "cm" + (j.motion.tcp_reach_ok ? " ok" : " ALTO"));
      }
      if (j.gripper_command_source === "metric_plan_autoclose") {
        parts.push("chiusura: auto (piano metrico)");
      }
      var gv = j.grasp_verify;
      if (gv && gv.ok) {
        parts.push(
          "PRESA: " + (gv.grasp_detected ? "OK oggetto" : "a vuoto") +
          " (pinza " + Number(gv.gripper_deg_achieved).toFixed(1) + "°)"
        );
      } else if (gv && !gv.ok) {
        parts.push("verifica presa: n/d (" + (gv.reason || "?") + ")");
      }
      if (blocked) {
        parts.push("BLOCCATO: " + (nogo || "no-go"));
      }
      statusEl.textContent = parts.join(" · ");
      statusEl.style.color = blocked ? "#e0a000" : visible === false ? "#d05050" : "var(--muted)";
    }
    var ctx = canvas.getContext("2d");
    var mg = (j && j.metric_grounding) || null;

    function drawOverlays() {
      // bbox + asse di presa dalla detection metrica (in pixel sul frame originale → scala al canvas)
      if (mg && mg.frame_size_px && mg.frame_size_px[0] > 0 && mg.frame_size_px[1] > 0) {
        var sx = canvas.width / mg.frame_size_px[0];
        var sy = canvas.height / mg.frame_size_px[1];
        if (mg.bbox_xyxy && mg.bbox_xyxy.length >= 4) {
          var b = mg.bbox_xyxy;
          ctx.strokeStyle = "#40b0ff";
          ctx.lineWidth = 2;
          ctx.strokeRect(b[0] * sx, b[1] * sy, (b[2] - b[0]) * sx, (b[3] - b[1]) * sy);
          ctx.fillStyle = "#40b0ff";
          ctx.font = "12px sans-serif";
          var tag = (mg.label || "obj") + (mg.confidence != null ? " " + Number(mg.confidence).toFixed(2) : "");
          ctx.fillText(tag, b[0] * sx + 2, Math.max(12, b[1] * sy - 3));
        }
        if (mg.grip_axis_px && mg.grip_axis_px.length >= 4) {
          var a = mg.grip_axis_px;
          ctx.strokeStyle = "#ffd000";
          ctx.lineWidth = 2;
          ctx.beginPath();
          ctx.moveTo(a[0] * sx, a[1] * sy);
          ctx.lineTo(a[2] * sx, a[3] * sy);
          ctx.stroke();
        }
      }
      drawMarker();
    }

    function drawMarker() {
      if (!px) {
        return;
      }
      var cx = px[0] * canvas.width;
      var cy = px[1] * canvas.height;
      ctx.strokeStyle = visible === false ? "#d05050" : "#30d070";
      ctx.lineWidth = 3;
      ctx.beginPath();
      ctx.arc(cx, cy, 16, 0, 2 * Math.PI);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(cx - 22, cy);
      ctx.lineTo(cx + 22, cy);
      ctx.moveTo(cx, cy - 22);
      ctx.lineTo(cx, cy + 22);
      ctx.stroke();
    }

    // Sorgente immagine: preferisci l'immagine Orbbec ANNOTATA dal server (bbox sul frame corretto).
    // Fallback all'anteprima RGB UVC + marker (senza bbox, che è su un frame diverso).
    var vizUrl = j && j.metric_viz_url;
    if (vizUrl) {
      var imgA = new Image();
      imgA.onload = function () {
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(imgA, 0, 0, canvas.width, canvas.height);
        drawOverlays();
      };
      imgA.onerror = function () {
        // se l'immagine annotata non c'è, ripiega sull'anteprima b64
        if (b64) {
          var imgB = new Image();
          imgB.onload = function () {
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            ctx.drawImage(imgB, 0, 0, canvas.width, canvas.height);
            drawMarker();
          };
          imgB.src = "data:image/jpeg;base64," + b64;
        } else {
          ctx.fillStyle = "#111";
          ctx.fillRect(0, 0, canvas.width, canvas.height);
          drawMarker();
        }
      };
      imgA.src =
        (typeof window.operatorsApi === "function" ? window.operatorsApi(vizUrl) : vizUrl) +
        (vizUrl.indexOf("?") >= 0 ? "&" : "?") +
        "t=" +
        Date.now();
    } else if (b64) {
      var img = new Image();
      img.onload = function () {
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        drawOverlays();
      };
      img.onerror = function () {
        ctx.fillStyle = "#111";
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        drawOverlays();
      };
      img.src = "data:image/jpeg;base64," + b64;
    } else {
      ctx.fillStyle = "#111";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      drawOverlays();
    }

    // Pannello traiettoria IK + check collisione cane
    var stagesEl = document.getElementById("graspCoachStages");
    if (stagesEl) {
      if (mg && mg.ik_stages && mg.ik_stages.length) {
        var lateral = j && j.lateral_grasp_mode;
        var html =
          '<div style="font-size:0.74rem;color:var(--muted);margin-bottom:2px;">Traiettoria IK' +
          (lateral ? " (modalità laterale — NO-GO disattivate)" : " (verifica collisione col cane)") +
          ":</div>";
        if (mg.depth_m != null || mg.reach_m != null) {
          html +=
            '<div style="font-size:0.72rem;color:var(--muted);">depth=' +
            (mg.depth_m != null ? Number(mg.depth_m).toFixed(3) + "m" : "—") +
            " · reach=" +
            (mg.reach_m != null ? Number(mg.reach_m).toFixed(3) + "m" : "—") +
            (mg.reachable === false ? " (FUORI REACH)" : "") +
            "</div>";
        }
        mg.ik_stages.forEach(function (s) {
          var col = s.safe ? "#30d070" : "#d05050";
          var t = s.target_xyz_m || [];
          var det =
            t.length >= 3
              ? "x=" + t[0].toFixed(2) + " y=" + t[1].toFixed(2) + " z=" + t[2].toFixed(2)
              : "";
          var why = !s.ik_ok ? " IK no" : s.collision_dog ? " ✖ " + s.collision_dog : " ok";
          html +=
            '<div style="font-size:0.72rem;color:' +
            col +
            ';">• ' +
            (s.stage || "?") +
            ": " +
            det +
            why +
            "</div>";
        });
        stagesEl.innerHTML = html;
        stagesEl.hidden = false;
      } else if (mg && mg.reason) {
        stagesEl.innerHTML =
          '<div style="font-size:0.72rem;color:#d0a000;">Grounding metrico non disponibile: ' +
          mg.reason +
          " (uso stima del modello)</div>";
        stagesEl.hidden = false;
      } else {
        stagesEl.hidden = true;
      }
    }
  }

  function _graspCoachStopChain(j) {
    // Ferma l'auto-chain se il coach chiude la pinza (presa fatta), non sa dove andare (target nullo),
    // non vede l'oggetto, o la mossa è stata bloccata da una NO-GO zone.
    var interp = j && j.interpreted;
    if (!interp) {
      return false;
    }
    if (String(interp.gripper_command || "") === "close") {
      return true;
    }
    if (interp.target_xyz_base_link_m == null) {
      return true;
    }
    if (interp.nogo_blocked) {
      return true;
    }
    if (interp.object_visible === false || j.object_visible === false) {
      return true;
    }
    return false;
  }

  window.operatorsGraspCoachStep = function () {
    if (window.__graspCoachStepBusy) {
      return;
    }
    var pre = document.getElementById("graspCoachPre");
    var stepBtn = document.getElementById("graspCoachStepBtn");
    var chainEl = document.getElementById("graspCoachChain");
    if (!_graspCoachBuildBody()) {
      if (pre) {
        pre.textContent = "Missing instruction text.";
      }
      return;
    }
    var chain = chainEl ? parseInt(chainEl.value, 10) : 1;
    if (isNaN(chain) || chain < 1) {
      chain = 1;
    }
    window.__graspCoachStepBusy = true;
    if (stepBtn) {
      stepBtn.disabled = true;
    }
    var total = chain;
    var done = 0;

    function runOne() {
      var body = _graspCoachBuildBody();
      if (!body) {
        return Promise.resolve();
      }
      window.__graspCoachStepIndex += 1;
      graspCoachProgressStart();
      if (pre) {
        pre.textContent = "… step " + (done + 1) + "/" + total;
      }
      var tStep0 = typeof performance !== "undefined" ? performance.now() : 0;
      return fetch(window.operatorsApi("/api/grasp_coach/step"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      })
        .then(function (r) {
          return r.json().then(function (j) {
            return { httpOk: r.ok, httpStatus: r.status, json: j, resp: r };
          });
        })
        .then(function (pack) {
          var j = pack.json;
          window.__graspCoachLastResponse = j;
          var foot =
            typeof window.operatorsHttpTimingFooterLines === "function"
              ? window.operatorsHttpTimingFooterLines(pack.resp, tStep0)
              : "";
          if (pre) {
            pre.textContent = JSON.stringify(j, null, 2) + foot;
          }
          try {
            _graspCoachRenderMetricPanel(j);
            _graspCoachDrawViz(j);
            _graspCoachRenderReplyIt(j);
          } catch (eViz) {
            /* viz best-effort */
          }
          var ok = !!(j && j.ok);
          var hint =
            j && j.reason
              ? String(j.reason)
              : !pack.httpOk
                ? "HTTP " + String(pack.httpStatus)
                : "";
          graspCoachProgressDone(ok, j && j.timings_ms, hint, j);
          done += 1;
          if (!ok || _graspCoachStopChain(j) || done >= total) {
            return;
          }
          // breve pausa: la posa tiene via coupling, la camera/feedback si assestano.
          return new Promise(function (res) {
            window.setTimeout(res, 800);
          }).then(runOne);
        });
    }

    runOne()
      .catch(function (e) {
        window.__graspCoachLastResponse = null;
        if (pre) {
          pre.textContent = String(e);
        }
        graspCoachProgressDone(false, null, String(e), null);
      })
      .then(function () {
        window.__graspCoachStepBusy = false;
        if (stepBtn) {
          stepBtn.disabled = false;
        }
      });
  };

  function _graspCoachInstructionText() {
    var hero = document.getElementById("graspTeachInstruction");
    var instrEl = document.getElementById("graspCoachInstruction");
    var t =
      (hero && hero.value && String(hero.value).trim()) ||
      (instrEl && instrEl.value && String(instrEl.value).trim()) ||
      "";
    if (instrEl && hero && instrEl.value !== hero.value) {
      instrEl.value = hero.value || "";
    }
    return t || "prendi l'oggetto";
  }

  function _graspCoachRenderReplyIt(j) {
    var el = document.getElementById("graspCoachReplyIt");
    if (!el) {
      return;
    }
    var txt = j && j.assistant_reply_it ? String(j.assistant_reply_it).trim() : "";
    if (!txt) {
      el.style.display = "none";
      el.textContent = "";
      return;
    }
    var mode = j && j.coach_mode ? String(j.coach_mode) : "";
    var prefix =
      mode === "lateral_metric_only"
        ? "Coach metrico · "
        : mode === "llm_vision"
          ? "Coach GPT · "
          : "";
    el.textContent = prefix + txt;
    el.style.display = "block";
  }

  function _graspCoachRunAcquire(btnIds) {
    var pre = document.getElementById("graspCoachPre");
    var btns = (btnIds || ["graspCoachAcquireBtn", "graspCoachPreviewBtn"]).map(function (id) {
      return document.getElementById(id);
    });
    btns.forEach(function (b) {
      if (b) {
        b.disabled = true;
      }
    });
    if (pre) {
      pre.textContent =
        "POST /api/grasp_coach/preview … Orbbec SDK (RGB+depth, ~3–8s, nessun movimento)";
    }
    var body = { instruction: _graspCoachInstructionText() };
    if (typeof window.operatorsStartVariantPayload === "function") {
      Object.keys(window.operatorsStartVariantPayload()).forEach(function (k) {
        body[k] = window.operatorsStartVariantPayload()[k];
      });
    }
    var t0 = typeof performance !== "undefined" ? performance.now() : 0;
    return fetch(window.operatorsApi("/api/grasp_coach/preview"), {
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
        var j = pack.j;
        window.__graspCoachLastResponse = j;
        var foot =
          typeof window.operatorsHttpTimingFooterLines === "function"
            ? window.operatorsHttpTimingFooterLines(pack.r, t0)
            : "";
        if (pre) {
          pre.textContent = JSON.stringify(j, null, 2) + foot;
        }
        _graspCoachRenderMetricPanel(j);
        _graspCoachDrawViz(j);
      })
      .catch(function (e) {
        if (pre) {
          pre.textContent = String(e);
        }
      })
      .finally(function () {
        btns.forEach(function (b) {
          if (b) {
            b.disabled = false;
          }
        });
      });
  }

  window.operatorsGraspCoachAcquire = function () {
    _graspCoachRunAcquire(["graspCoachAcquireBtn"]);
  };

  window.operatorsGraspCoachPreview = function () {
    _graspCoachRunAcquire(["graspCoachAcquireBtn", "graspCoachPreviewBtn"]);
  };

  window.operatorsGraspCoachGrasp = function () {
    var instrEl = document.getElementById("graspCoachInstruction");
    var execEl = document.getElementById("graspCoachExecute");
    if (instrEl && !String(instrEl.value || "").trim()) {
      instrEl.value = "prendi l'oggetto";
    }
    if (execEl) {
      execEl.checked = true;
    }
    operatorsGraspCoachStep();
  };

  window.operatorsGraspCoachGotoStart = function () {
    var pre = document.getElementById("graspCoachPre");
    var btn = document.getElementById("graspCoachStartBtn");
    if (btn) {
      btn.disabled = true;
    }
    if (pre) {
      pre.textContent = "POST /api/arm/goto_saved_start … (porto il braccio in START)";
    }
    var t0 = typeof performance !== "undefined" ? performance.now() : 0;
    var startBody = { confirm: "ARM_GOTO_SAVED_START" };
    if (typeof window.operatorsStartVariantPayload === "function") {
      Object.keys(window.operatorsStartVariantPayload()).forEach(function (k) {
        startBody[k] = window.operatorsStartVariantPayload()[k];
      });
    }
    fetch(window.operatorsApi("/api/arm/goto_saved_start"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(startBody),
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
        if (pre) {
          pre.textContent = JSON.stringify(pack.j, null, 2) + foot;
        }
      })
      .catch(function (e) {
        if (pre) {
          pre.textContent = String(e);
        }
      })
      .finally(function () {
        clearTimeout(scanTimer);
        _graspTeachRestoreMjpegAfterFetch(pausedMjpeg);
        if (btn) {
          btn.disabled = false;
        }
      });
  };

  window.operatorsGraspCoachFeedback = function () {
    var pre = document.getElementById("graspCoachPre");
    var fbTa = document.getElementById("graspCoachFeedbackAfter");
    var codeTa = document.getElementById("graspCoachCodeCorrection");
    var btn = document.getElementById("graspCoachFeedbackBtn");
    var txt = fbTa && fbTa.value ? String(fbTa.value).trim() : "";
    if (!txt) {
      if (pre) {
        pre.textContent = JSON.stringify(
          { ok: false, reason: "missing_feedback_text", hint_it: "Compila il campo feedback." },
          null,
          2
        );
      }
      return;
    }
    var last = window.__graspCoachLastResponse;
    var rsi =
      last && typeof last.step_index === "number" && !isNaN(last.step_index)
        ? last.step_index
        : null;
    var rrep =
      last && last.assistant_reply_it ? String(last.assistant_reply_it).trim().slice(0, 500) : "";
    var body = {
      feedback_text: txt,
      code_correction_note:
        codeTa && codeTa.value && String(codeTa.value).trim()
          ? String(codeTa.value).trim()
          : undefined,
      related_step_index: rsi,
      related_assistant_reply_it: rrep || undefined,
    };
    if (btn) {
      btn.disabled = true;
    }
    var tFb0 = typeof performance !== "undefined" ? performance.now() : 0;
    fetch(window.operatorsApi("/api/grasp_coach/feedback"), {
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
        var j = pack.j;
        var foot =
          typeof window.operatorsHttpTimingFooterLines === "function"
            ? window.operatorsHttpTimingFooterLines(pack.r, tFb0)
            : "";
        if (pre) {
          pre.textContent = JSON.stringify(j, null, 2) + foot;
        }
        if (j && j.ok && fbTa) {
          fbTa.value = "";
        }
        if (j && j.ok && codeTa) {
          codeTa.value = "";
        }
      })
      .catch(function (e) {
        if (pre) {
          pre.textContent = String(e);
        }
      })
      .finally(function () {
        if (btn) {
          btn.disabled = false;
        }
      });
  };

  window.operatorsGraspSideSetup = function () {
    var pre = document.getElementById("graspCoachPre");
    var btn = document.getElementById("graspSideSetupBtn");
    var postureEl = document.getElementById("graspSidePosture");
    var instrEl = document.getElementById("graspCoachInstruction");
    if (
      !window.confirm(
        "Setup PRESA DI LATO: il cane si alza/fa 2 passi, gira 90° a destra e il braccio va a START ruotato. Procedere?"
      )
    ) {
      return;
    }
    var body = {
      confirm: "RUN_SIDE_GRASP_SETUP",
      posture: postureEl ? postureEl.value : "auto",
      instruction: instrEl && instrEl.value ? String(instrEl.value).trim() : "",
      front_camera: 6,
    };
    if (btn) {
      btn.disabled = true;
    }
    if (pre) {
      pre.textContent = "POST /api/grasp/side_approach_setup … (avvio in background)";
    }
    if (window.__graspSidePoll) {
      clearInterval(window.__graspSidePoll);
      window.__graspSidePoll = null;
    }
    fetch(window.operatorsApi("/api/grasp/side_approach_setup"), {
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
        var j = pack.j;
        if (pre) {
          pre.textContent = JSON.stringify(j, null, 2);
        }
        // Dry-run (200, started:false) o errore (409/…): niente polling.
        if (!j || j.started !== true) {
          if (btn) {
            btn.disabled = false;
          }
          return;
        }
        // Job avviato: polla lo stato finché running:false.
        window.__graspSidePoll = setInterval(function () {
          fetch(window.operatorsApi("/api/grasp/side_approach_status"))
            .then(function (r) {
              return r.json();
            })
            .then(function (st) {
              if (pre) {
                pre.textContent = JSON.stringify(st, null, 2);
              }
              if (st && st.running === false) {
                clearInterval(window.__graspSidePoll);
                window.__graspSidePoll = null;
                if (btn) {
                  btn.disabled = false;
                }
              }
            })
            .catch(function () {
              /* timeout transitorio LAN: riprova al prossimo tick */
            });
        }, 1200);
      })
      .catch(function (e) {
        if (pre) {
          pre.textContent = String(e);
        }
        if (btn) {
          btn.disabled = false;
        }
      });
  };

  var __graspTeachCalibPoll = null;

  function _graspTeachCalibSetUi(active) {
    ["graspTeachCalibBtn", "graspTeachVisionDiagBtn", "graspTeachGotoVisionBtn", "graspTeachScanJ90Btn", "graspTeachGripOpenBtn", "graspTeachGripCloseBtn"].forEach(function (id) {
      var btn = document.getElementById(id);
      if (btn) {
        btn.disabled = !!active;
      }
    });
    var cancelBtn = document.getElementById("graspTeachCalibCancelBtn");
    if (cancelBtn) {
      cancelBtn.disabled = !active;
    }
  }

  function _graspTeachCalibStatusEls() {
    return document.querySelectorAll("#graspTeachCalibStatus");
  }

  function _graspTeachCalibRenderStatus(st) {
    if (st && st.samples_count != null && typeof _graspUpdateSamplesBadge === "function") {
      _graspUpdateSamplesBadge(st.samples_count);
    }
    var els = _graspTeachCalibStatusEls();
    if (!els.length || !st) {
      return;
    }
    var parts = [];
    if (st.phase && st.phase !== "idle") {
      parts.push(st.phase_label_it || st.phase);
      if (st.remaining_s > 0) {
        parts.push(Math.ceil(st.remaining_s) + "s");
      }
    } else if (st.samples_count != null) {
      parts.push("Campioni teach salvati: " + st.samples_count);
    }
    if (st.error) {
      parts.push("ERRORE: " + st.error);
    }
    if (st.phase_label_it && (st.phase === "error" || st.error)) {
      parts.push(st.phase_label_it);
    }
    if (st.last_sample && st.phase === "done") {
      var d = st.last_sample.delta && st.last_sample.delta.servo_deg;
      if (d && d.length) {
        parts.push("Δ giunti grasp: " + d.slice(0, 6).map(function (x) { return Number(x).toFixed(1); }).join("° "));
      }
    }
    var text = parts.length ? parts.join(" · ") : "Pronto.";
    var color = st.error ? "#d05050" : st.active ? "#e0c040" : st.phase === "done" ? "#30d070" : "var(--muted)";
    els.forEach(function (el) {
      el.textContent = text;
      el.style.color = color;
    });
  }

  function _graspTeachCalibPollOnce() {
    fetch(window.operatorsApi("/api/grasp_coach/teach_calib/status?_=" + Date.now()), { cache: "no-store" })
      .then(function (r) {
        return r.json();
      })
      .then(function (st) {
        _graspTeachCalibRenderStatus(st);
        _graspTeachCalibSetUi(!!st.active);
        if (st.phase === "releasing") {
          _graspTeachActionBanner(false, "Teach posa presa", "Rilascio giunti (funcode 5 mode 0)…", "");
        } else if (st.phase === "teach_manual") {
          _graspTeachActionBanner(
            true,
            "Giunti liberi",
            st.phase_label_it || "Posa manualmente il braccio",
            "Quando finisce il countdown la posa viene memorizzata."
          );
        } else if (st.phase === "error") {
          _graspTeachActionBanner(
            false,
            "Teach fallito",
            st.error || "teach_error",
            st.phase_label_it || _graspTeachFailureHint(String(st.error || ""))
          );
        } else if (st.phase === "done") {
          _graspTeachActionBanner(true, "Teach salvato", st.phase_label_it || "Offset memorizzato.", "");
        }
        if (!st.active) {
          if (__graspTeachCalibPoll) {
            clearInterval(__graspTeachCalibPoll);
            __graspTeachCalibPoll = null;
          }
          _graspTeachUpdateWizardState();
          if (st.phase === "done") {
            operatorsGraspCoachAcquire();
          }
        }
      })
      .catch(function () {});
  }

  window.operatorsGraspTeachCalibRefresh = function () {
    fetch(window.operatorsApi("/api/grasp_coach/teach_calib/samples"))
      .then(function (r) {
        return r.json();
      })
      .then(function (j) {
        var pre = document.getElementById("graspCoachPre");
        if (pre) {
          pre.textContent = JSON.stringify(j, null, 2);
        }
        _graspTeachCalibRenderStatus({ samples_count: j.count, phase: "idle" });
        _graspUpdateSamplesBadge(j.count);
      });
  };

  function _graspUpdateSamplesBadge(count) {
    var el = document.getElementById("graspTeachSamplesBadge");
    if (!el) {
      return;
    }
    var n = Number(count || 0);
    window.__graspTeachSamplesCount = n;
    el.textContent = "Calibrazioni salvate: " + n;
    el.className = "op-grasp-front-badge " + (n > 0 ? "is-ok" : "is-idle");
  }

  // Pulsante "Cancella tutti i teaching": svuota i campioni di calibrazione salvati.
  window.operatorsGraspTeachCalibClear = function () {
    if (!window.confirm("Cancellare TUTTE le calibrazioni salvate e ripartire da zero?")) {
      return;
    }
    var btn = document.getElementById("graspTeachCalibClearBtn");
    if (btn) {
      btn.disabled = true;
    }
    fetch(window.operatorsApi("/api/grasp_coach/teach_calib/samples"), { method: "DELETE" })
      .then(function (r) {
        return r.json();
      })
      .then(function (j) {
        _graspUpdateSamplesBadge((j && j.samples) || 0);
        _graspTeachCalibRenderStatus({ samples_count: (j && j.samples) || 0, phase: "idle" });
        _graspTeachActionBanner(
          true,
          "Teaching cancellati",
          "Tutte le calibrazioni salvate sono state eliminate. Riparti dalla procedura.",
          ""
        );
      })
      .catch(function (e) {
        _graspTeachActionBanner(false, "Errore", String(e), "Verifica la rete verso la NX.");
      })
      .finally(function () {
        if (btn) {
          btn.disabled = false;
        }
      });
  };

  // Badge verde/rosso: la camera depth polso è vista e dà profondità reale?
  window.operatorsGraspWristCameraCheck = function () {
    var el = document.getElementById("graspWristCamHealth");
    var btn = document.getElementById("graspWristCamCheckBtn");
    // #region agent log
    var mjpgCount = 0;
    try {
      document.querySelectorAll('img[src*=".mjpg"]').forEach(function () {
        mjpgCount += 1;
      });
    } catch (eCnt) {
      /* ignore */
    }
    var dbgT0 = Date.now();
    fetch("http://127.0.0.1:7648/ingest/1f6b2724-6bbf-4c6c-a795-45910cf4b1c4", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Debug-Session-Id": "149a4f" },
      body: JSON.stringify({
        sessionId: "149a4f",
        location: "operators_grasp_coach.js:operatorsGraspWristCameraCheck",
        message: "check_start",
        data: { mjpgCount: mjpgCount },
        hypothesisId: "H1",
        timestamp: dbgT0,
      }),
    }).catch(function () {});
    // #endregion
    if (el) {
      el.textContent = "Camera polso: controllo in corso…";
      el.className = "op-grasp-front-badge is-running";
    }
    if (btn) {
      btn.disabled = true;
    }
    // Timeout client: il badge non resta MAI bloccato su "controllo in corso…".
    var ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
    var timedOut = false;
    var tmr = setTimeout(function () {
      timedOut = true;
      // #region agent log
      fetch("http://127.0.0.1:7648/ingest/1f6b2724-6bbf-4c6c-a795-45910cf4b1c4", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Debug-Session-Id": "149a4f" },
        body: JSON.stringify({
          sessionId: "149a4f",
          location: "operators_grasp_coach.js:operatorsGraspWristCameraCheck",
          message: "client_timeout_15s",
          data: { elapsed_ms: Date.now() - dbgT0, mjpgCount: mjpgCount },
          hypothesisId: "H1,H3,H5",
          timestamp: Date.now(),
        }),
      }).catch(function () {});
      // #endregion
      if (ctrl) {
        try { ctrl.abort(); } catch (e) {}
      }
      if (el) {
        el.textContent = "⛔ Camera polso: nessuna risposta (timeout). Riprova; se persiste riavvia la dashboard.";
        el.className = "op-grasp-front-badge is-fail";
      }
      if (btn) {
        btn.disabled = false;
      }
    }, 15000);
    var opts = { cache: "no-store" };
    if (ctrl) {
      opts.signal = ctrl.signal;
    }
    fetch(window.operatorsApi("/api/grasp/wrist_camera_health?_=" + Date.now()), opts)
      .then(function (r) {
        return r.json();
      })
      .then(function (j) {
        // #region agent log
        fetch("http://127.0.0.1:7648/ingest/1f6b2724-6bbf-4c6c-a795-45910cf4b1c4", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Debug-Session-Id": "149a4f" },
          body: JSON.stringify({
            sessionId: "149a4f",
            location: "operators_grasp_coach.js:operatorsGraspWristCameraCheck",
            message: "fetch_ok",
            data: {
              elapsed_ms: Date.now() - dbgT0,
              timedOut: timedOut,
              ok: !!(j && j.ok),
              reason: j && j.reason,
              server_total_ms: j && j._http_timing_ms && j._http_timing_ms.server_total_ms,
            },
            hypothesisId: "H1,H2,H3,H4",
            timestamp: Date.now(),
          }),
        }).catch(function () {});
        // #endregion
        if (timedOut || !el) {
          return;
        }
        if (j && j.ok) {
          var md = j.depth_center_median_m != null ? " · centro ~" + Number(j.depth_center_median_m).toFixed(2) + " m" : "";
          el.textContent = "✅ Camera polso OK (depth reale" + md + ")";
          el.className = "op-grasp-front-badge is-ok";
        } else {
          el.textContent = "⛔ Camera polso: " + (j && (j.hint_it || j.reason) ? j.hint_it || j.reason : "non disponibile");
          el.className = "op-grasp-front-badge is-fail";
        }
      })
      .catch(function (e) {
        // #region agent log
        fetch("http://127.0.0.1:7648/ingest/1f6b2724-6bbf-4c6c-a795-45910cf4b1c4", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Debug-Session-Id": "149a4f" },
          body: JSON.stringify({
            sessionId: "149a4f",
            location: "operators_grasp_coach.js:operatorsGraspWristCameraCheck",
            message: "fetch_error",
            data: { elapsed_ms: Date.now() - dbgT0, timedOut: timedOut, error: String(e) },
            hypothesisId: "H1,H4,H5",
            timestamp: Date.now(),
          }),
        }).catch(function () {});
        // #endregion
        if (timedOut) {
          return;
        }
        if (el) {
          el.textContent = "⛔ Camera polso: errore (" + String(e) + ")";
          el.className = "op-grasp-front-badge is-fail";
        }
      })
      .finally(function () {
        clearTimeout(tmr);
        if (!timedOut && btn) {
          btn.disabled = false;
        }
      });
  };

  window.operatorsGraspWristCameraCheck = function () {
    var el = document.getElementById("graspWristCamHealth");
    var btn = document.getElementById("graspWristCamCheckBtn");
    if (el) {
      el.textContent = "Camera polso: controllo in corso... non blocca il teach.";
      el.className = "op-grasp-front-badge is-run";
    }
    if (btn) btn.disabled = true;
    var ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
    var done = false;
    var tmr = setTimeout(function () {
      done = true;
      if (ctrl) {
        try { ctrl.abort(); } catch (e) {}
      }
      if (el) {
        el.textContent = "Camera polso: check lento. Vai avanti con START +90 e 1.";
        el.className = "op-grasp-front-badge is-idle";
      }
      if (btn) btn.disabled = false;
    }, 6000);
    var opts = { cache: "no-store" };
    if (ctrl) opts.signal = ctrl.signal;
    fetch(window.operatorsApi("/api/grasp/wrist_camera_health?_=" + Date.now()), opts)
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (done || !el) return;
        if (j && j.ok) {
          var md = j.depth_center_median_m != null ? " - depth centro " + Number(j.depth_center_median_m).toFixed(2) + " m" : "";
          el.textContent = "Camera polso OK" + md;
          el.className = "op-grasp-front-badge is-ok";
        } else {
          el.textContent = "Camera polso: " + (j && (j.hint_it || j.reason) ? j.hint_it || j.reason : "non disponibile");
          el.className = "op-grasp-front-badge is-fail";
        }
      })
      .catch(function (e) {
        if (done || !el) return;
        el.textContent = "Camera polso: errore " + String(e);
        el.className = "op-grasp-front-badge is-fail";
      })
      .finally(function () {
        clearTimeout(tmr);
        if (btn) btn.disabled = false;
      });
  };

  window.operatorsGraspTeachCalibCancel = function () {
    fetch(window.operatorsApi("/api/grasp_coach/teach_calib/cancel"), { method: "POST" })
      .then(function (r) {
        return r.json();
      })
      .then(function (j) {
        _graspTeachCalibRenderStatus(j.status || j);
        _graspTeachCalibSetUi(false);
      });
  };

  function _graspTeachActionBanner(ok, title, detail, hint) {
    var el = document.getElementById("graspTeachErrorBanner");
    if (!el) {
      return;
    }
    if (!detail && !hint) {
      el.hidden = true;
      el.innerHTML = "";
      return;
    }
    el.hidden = false;
    if (ok) {
      el.style.borderColor = "rgba(52, 211, 153, 0.55)";
      el.style.background = "rgba(6, 78, 59, 0.35)";
      el.style.color = "#a7f3d0";
    } else {
      el.style.borderColor = "rgba(248, 113, 113, 0.55)";
      el.style.background = "rgba(127, 29, 29, 0.35)";
      el.style.color = "#fecaca";
    }
    el.innerHTML =
      "<strong>" +
      String(title || (ok ? "OK" : "Errore")).replace(/</g, "&lt;") +
      "</strong>" +
      (detail ? " · " + String(detail).replace(/</g, "&lt;") : "") +
      (hint
        ? '<br><span class="grasp-teach-hint">' + String(hint).replace(/</g, "&lt;") + "</span>"
        : "");
  }

  window.operatorsGraspTeachGotoScanJ90 = function () {
    var btn = document.getElementById("graspTeachScanJ90Btn");
    var pre = document.getElementById("graspCoachPre");
    var badge = document.getElementById("graspTeachGoBadge");
    _dbgAgentLog("operators_grasp_coach.js:scan_j90", "click_start", {}, "H-JS-SCAN");
    if (btn) {
      btn.disabled = true;
    }
    _graspTeachActionBanner(false, "START +90°", "Movimento in corso verso Scansione +90°…", "");
    _graspTeachCalibRenderStatus({
      phase: "hold",
      phase_label_it: "Movimento verso START +90° (Scansione)…",
      active: false,
    });
    if (badge) {
      badge.textContent = "START +90°: movimento…";
      badge.className = "op-grasp-front-badge is-running";
    }
    if (pre) {
      pre.textContent = "POST /api/presets/scan/goto { variant: j90 } …";
    }
    _graspTeachReleaseMjpegIfPaused();
    var pausedMjpeg = _graspTeachPauseMjpegForFetch();
    var scanCtrl = typeof AbortController !== "undefined" ? new AbortController() : null;
    var scanTimer = setTimeout(function () {
      if (scanCtrl) {
        try { scanCtrl.abort(); } catch (eAbort) {}
      }
    }, 25000);
    var scanOpts = {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ variant: "j90" }),
    };
    if (scanCtrl) {
      scanOpts.signal = scanCtrl.signal;
    }
    fetch(window.operatorsApi("/api/presets/scan/goto"), scanOpts)
      .then(function (r) {
        return r.json().then(function (j) {
          return { j: j, r: r };
        });
      })
      .then(function (pack) {
        _dbgAgentLog(
          "operators_grasp_coach.js:scan_j90",
          "scan_goto_response",
          {
            http_status: pack.r && pack.r.status,
            ok: pack.j && pack.j.ok,
            reason: pack.j && pack.j.reason,
            waypoint_name: pack.j && pack.j.waypoint_name,
            max_error_deg: pack.j && pack.j.wait_at_target && pack.j.wait_at_target.max_error_deg,
          },
          "H-JS-SCAN"
        );
        if (pre) {
          pre.textContent = JSON.stringify(pack.j, null, 2);
        }
        var j = pack.j || {};
        var wait = j.wait_at_target || {};
        var maxErr = wait.max_error_deg;
        var alreadyThere =
          pack.r.ok &&
          j.ok &&
          wait.reached &&
          maxErr != null &&
          !isNaN(Number(maxErr)) &&
          Number(maxErr) < 2.5;
        if (pack.r.ok && j.ok) {
          var detail = j.waypoint_name || "Scansione +90°";
          if (alreadyThere) {
            detail += " — già in posa (errore max " + Number(maxErr).toFixed(1) + "°)";
          }
          _graspTeachActionBanner(
            true,
            alreadyThere ? "START +90° · già in posa" : "START +90° OK",
            detail,
            alreadyThere
              ? "Il braccio era già al waypoint — nessun movimento visibile. Se serve un altro angolo, spostalo prima."
              : "Pinze in posa Scansione +90°. Il badge «START salvata» in tab Presa può ancora dire NON in posa: è normale (pose diverse)."
          );
          _graspTeachCalibRenderStatus({
            phase: "done",
            phase_label_it: "START +90° raggiunta — pronto per teach o «Prendi».",
          });
          if (badge) {
            badge.textContent = "START +90°: OK";
            badge.className = "op-grasp-front-badge is-ok";
          }
        } else {
          var reason = j.reason || j.error || "scan_j90_failed";
          var hint = j.hint_it || j.hint || _graspTeachFailureHint(String(reason));
          _graspTeachActionBanner(false, "START +90° fallito", reason, hint);
          _graspTeachCalibRenderStatus({
            phase: "error",
            error: reason,
            phase_label_it: hint || "Waypoint SCANSIONE 90 non trovato — salva il punto nel programma D1.",
          });
          if (badge) {
            badge.textContent = "START +90°: ERRORE";
            badge.className = "op-grasp-front-badge is-fail";
          }
        }
      })
      .catch(function (e) {
        _dbgAgentLog("operators_grasp_coach.js:scan_j90", "scan_goto_network_error", { error: String(e) }, "H-JS-SCAN");
        if (pre) {
          pre.textContent = String(e);
        }
        _graspTeachActionBanner(false, "START +90°", String(e), "Verifica rete verso la NX.");
        _graspTeachCalibRenderStatus({ phase: "error", error: String(e) });
        if (badge) {
          badge.textContent = "START +90°: ERRORE rete";
          badge.className = "op-grasp-front-badge is-fail";
        }
      })
      .finally(function () {
        if (btn) {
          btn.disabled = false;
        }
      });
  };

  window.operatorsGraspTeachGripper = function (open) {
    var path = open ? "/api/pick/gripper/open" : "/api/pick/gripper/close";
    var label = open ? "Apertura pinza…" : "Chiusura pinza…";
    _graspTeachCalibRenderStatus({ phase: "idle", phase_label_it: label, active: false });
    fetch(window.operatorsApi(path), { method: "POST" })
      .then(function (r) {
        return r.json().then(function (j) {
          return { j: j, ok: r.ok };
        });
      })
      .then(function (pack) {
        if (!pack.ok || !pack.j || !pack.j.ok) {
          _graspTeachCalibRenderStatus({
            phase: "error",
            error: (pack.j && (pack.j.reason || pack.j.error)) || "gripper_failed",
          });
          return;
        }
        _graspTeachCalibPollOnce();
      })
      .catch(function (e) {
        _graspTeachCalibRenderStatus({ phase: "error", error: String(e) });
      });
  };

  function _graspTeachFmtXyz(arr) {
    if (!arr || arr.length < 3) {
      return "—";
    }
    return (
      "x=" +
      Number(arr[0]).toFixed(3) +
      " y=" +
      Number(arr[1]).toFixed(3) +
      " z=" +
      Number(arr[2]).toFixed(3)
    );
  }

  function _graspTeachVisionDiagPanelSet(html, isError) {
    var el = document.getElementById("graspTeachVisionDiagPanel");
    if (!el) {
      return;
    }
    el.innerHTML = html;
    el.style.color = isError ? "#fbbf24" : "#e2e8f0";
    el.style.minHeight = "72px";
    el.style.border = isError
      ? "1px solid rgba(251,191,36,0.5)"
      : "1px solid rgba(56,189,248,0.35)";
    try {
      el.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (eScroll) {
      /* ignore */
    }
  }

  function _graspTeachRenderVisionDiag(d) {
    if (!d) {
      _graspTeachVisionDiagPanelSet("<strong>Errore:</strong> risposta vuota dalla NX.", true);
      return;
    }
    window.__graspTeachVisionDiag = d;
    var lines = [];
    var obj = _graspTeachObjectLabel(_graspTeachInstructionText());
    lines.push("<strong>Target visione (" + obj + "):</strong> " + _graspTeachFmtXyz(d.vision_target_base_link_m));
    lines.push("<strong>Punta utensile ora:</strong> " + _graspTeachFmtXyz(d.tool_tip_base_link_m_now));
    if (d.error_vision_minus_tcp_m && d.error_vision_minus_tcp_m.length >= 3) {
      lines.push(
        "<strong>Δ visione−punta:</strong> dx=" +
          Number(d.error_vision_minus_tcp_m[0]).toFixed(3) +
          " dy=" +
          Number(d.error_vision_minus_tcp_m[1]).toFixed(3) +
          " dz=" +
          Number(d.error_vision_minus_tcp_m[2]).toFixed(3) +
          " m" +
          (d.error_vision_tcp_distance_m != null
            ? " · dist=" + Number(d.error_vision_tcp_distance_m).toFixed(3) + " m"
            : "")
      );
    }
    if (d.depth_m != null) {
      lines.push(
        "<strong>Depth:</strong> " +
          Number(d.depth_m).toFixed(3) +
          " m" +
          (d.depth_source ? " (" + d.depth_source + ")" : "") +
          (d.rgb_depth_fallback ? " <span style='color:#e0a040'>RGB stimata</span>" : "")
      );
    }
    if (d.camera_xyz_m && d.camera_xyz_m.length >= 3) {
      lines.push("<strong>Camera frame:</strong> " + _graspTeachFmtXyz(d.camera_xyz_m));
    }
    if (d.reachable === false) {
      lines.push("<span style='color:#f87171'>Reach: FUORI (" + (d.reach_m != null ? Number(d.reach_m).toFixed(2) + " m" : "?") + ")</span>");
    }
    if (d.teach_calib_applied && d.teach_calib_delta_tcp_m) {
      lines.push(
        "<strong>Offset teach attivo:</strong> Δtcp [" +
          d.teach_calib_delta_tcp_m
            .slice(0, 3)
            .map(function (x) {
              return Number(x).toFixed(3);
            })
            .join(", ") +
          "] m"
      );
    } else if (d.last_teach_sample && d.last_teach_sample.delta_tcp_m) {
      lines.push(
        "<strong>Ultimo teach salvato Δtcp:</strong> [" +
          d.last_teach_sample.delta_tcp_m
            .slice(0, 3)
            .map(function (x) {
              return Number(x).toFixed(3);
            })
            .join(", ") +
          "] m (non applicato a questo bbox)"
      );
    }
    if (d.hints_it && d.hints_it.length) {
      lines.push("<strong>Cosa controllare:</strong><ul style='margin:4px 0 0 16px;padding:0;'>");
      d.hints_it.forEach(function (h) {
        lines.push("<li>" + String(h).replace(/</g, "&lt;") + "</li>");
      });
      lines.push("</ul>");
    }
    if (d.issues && d.issues.length) {
      lines.push("<span class='muted'>issues: " + d.issues.join(", ") + "</span>");
    }
    _graspTeachVisionDiagPanelSet(lines.join("<br>"), !d.ok);
  }

  window.operatorsGraspTeachVisionDiag = function () {
    var btn = document.getElementById("graspTeachVisionDiagBtn");
    var panel = document.getElementById("graspTeachVisionDiagPanel");
    var instrEl = document.getElementById("graspTeachInstruction");
    var instruction = instrEl && instrEl.value ? String(instrEl.value).trim() : "";
    if (typeof window.operatorsGraspSetFlow === "function") {
      window.operatorsGraspSetFlow("teach");
    }
    if (btn) {
      btn.disabled = true;
    }
    if (panel) {
      panel.innerHTML =
        "<strong style='color:#e0c040'>Acquisizione in corso…</strong> (~2–8 s) — attendi, non ricliccare.";
      panel.style.color = "#e0c040";
    }
    _graspTeachReleaseMjpegIfPaused();
    var pausedMjpeg = _graspTeachPauseMjpegForFetch();
    _graspTeachCalibRenderStatus({
      phase: "hold",
      phase_label_it: "Acquisizione metrica polso…",
      active: false,
    });
    var t0 = typeof performance !== "undefined" ? performance.now() : Date.now();
    fetch(window.operatorsApi("/api/grasp_coach/vision_diagnostic"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ instruction: instruction }),
    })
      .then(function (r) {
        return r.json();
      })
      .then(function (d) {
        var ms = Math.round((typeof performance !== "undefined" ? performance.now() : Date.now()) - t0);
        _graspTeachRenderVisionDiag(d);
        _graspTeachUpdateWizardState();
        _graspTeachActionBanner(
          !!d.ok,
          d.ok ? "Stima visione OK" : "Stima fallita",
          (d.label_it || d.reason || "") + " (" + ms + " ms)",
          d.ok ? "Vedi pannello giallo sotto i pulsanti calibrazione." : (d.hints_it && d.hints_it[0]) || ""
        );
        _graspTeachCalibRenderStatus({
          phase: d.ok ? "done" : "error",
          phase_label_it: d.label_it || (d.ok ? "Stima OK" : d.reason || "stima fallita"),
          error: d.ok ? null : d.reason,
        });
        var sdkImg = document.getElementById("graspTeachCamSdk");
        if (sdkImg) {
          sdkImg.src =
            window.operatorsApi("/api/grasp/detection_debug/wrist_realsense.jpg") + "?_=" + Date.now();
        }
        if (panel) {
          try {
            panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
          } catch (eSc) {
            /* ignore */
          }
        }
      })
      .catch(function (e) {
        if (panel) {
          panel.innerHTML = "<strong style='color:#f87171'>Errore rete/timeout:</strong> " + String(e);
        }
        _graspTeachActionBanner(false, "Stima visione", String(e), "Riprova — stream MJPEG in pausa.");
        _graspTeachCalibRenderStatus({ phase: "error", error: String(e) });
      })
      .finally(function () {
        _graspTeachRestoreMjpegAfterFetch(pausedMjpeg);
        if (btn) {
          btn.disabled = false;
        }
      });
  };

  window.operatorsGraspTeachGotoVision = function () {
    var d = window.__graspTeachVisionDiag;
    var tgt = d && d.vision_target_base_link_m;
    if (!tgt || tgt.length < 3) {
      window.alert("Prima premi «① Dove pensa la scatola».");
      return;
    }
    if (
      !window.confirm(
        "MUOVERE il braccio verso dove la VISIONE pensa sia la scatola?\n\n" +
          _graspTeachFmtXyz(tgt) +
          "\n\nArea libera? Se finisci lontano dalla scatola → «Teach posa presa»."
      )
    ) {
      return;
    }
    var btn = document.getElementById("graspTeachGotoVisionBtn");
    var instrEl = document.getElementById("graspTeachInstruction");
    var instruction = instrEl && instrEl.value ? String(instrEl.value).trim() : "";
    if (btn) {
      btn.disabled = true;
    }
    fetch(window.operatorsApi("/api/grasp_coach/goto_vision_target"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: "MOVE_VISION_TARGET", instruction: instruction, fresh: false }),
    })
      .then(function (r) {
        return r.json().then(function (j) {
          return { j: j, ok: r.ok };
        });
      })
      .then(function (pack) {
        if (pack.j && pack.j.diagnostic) {
          _graspTeachRenderVisionDiag(pack.j.diagnostic);
        }
        _graspTeachActionBanner(
          !!(pack.j && pack.j.ok),
          pack.j && pack.j.ok ? "Movimento test" : "Movimento fallito",
          pack.j && (pack.j.hint_it || pack.j.reason || (pack.j.motion && pack.j.motion.reason)),
          pack.j && pack.j.ok
            ? "Confronta pinze e scatola. Se sbagliato → «③ Calibra (posa reale)»."
            : ""
        );
      })
      .catch(function (e) {
        _graspTeachActionBanner(false, "Rete", String(e), "");
      })
      .finally(function () {
        if (btn) {
          btn.disabled = false;
        }
      });
  };

  window.operatorsGraspTeachGotoVision = function () {
    var d = window.__graspTeachVisionDiag;
    var tgt = d && d.vision_target_base_link_m;
    if (!tgt || tgt.length < 3) {
      window.alert("Prima premi 1 - Leggi target.");
      return;
    }
    var obj = _graspTeachObjectLabel(_graspTeachInstructionText());
    if (
      !window.confirm(
        "Muovere il braccio verso il target visione del " + obj + "?\n\n" +
          _graspTeachFmtXyz(tgt) +
          "\n\nArea libera? Se finisce lontano dall'oggetto, premi 3 - Salva presa vera."
      )
    ) {
      return;
    }
    var btn = document.getElementById("graspTeachGotoVisionBtn");
    var instruction = _graspTeachInstructionText();
    if (btn) {
      btn.disabled = true;
    }
    _graspTeachSetNextStep("Movimento test in corso verso il target visione...", "is-run");
    fetch(window.operatorsApi("/api/grasp_coach/goto_vision_target"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: "MOVE_VISION_TARGET", instruction: instruction, fresh: false }),
    })
      .then(function (r) {
        return r.json().then(function (j) {
          return { j: j, ok: r.ok };
        });
      })
      .then(function (pack) {
        if (pack.j && pack.j.diagnostic) {
          _graspTeachRenderVisionDiag(pack.j.diagnostic);
        }
        _graspTeachActionBanner(
          !!(pack.j && pack.j.ok),
          pack.j && pack.j.ok ? "Movimento test inviato" : "Movimento fallito",
          pack.j && (pack.j.hint_it || pack.j.reason || (pack.j.motion && pack.j.motion.reason)),
          pack.j && pack.j.ok ? "Se le pinze non sono sull'oggetto, premi 3 - Salva presa vera." : ""
        );
        _graspTeachSetNextStep(
          pack.j && pack.j.ok ? "Ora verifica fisicamente: se e' fuori, premi 3 - Salva presa vera." : "Movimento fallito: leggi il messaggio e riprova 1.",
          pack.j && pack.j.ok ? "is-ok" : "is-fail"
        );
      })
      .catch(function (e) {
        _graspTeachActionBanner(false, "Rete", String(e), "");
        _graspTeachSetNextStep("Errore rete sul movimento test.", "is-fail");
      })
      .finally(function () {
        clearTimeout(scanTimer);
        _graspTeachRestoreMjpegAfterFetch(pausedMjpeg);
        if (btn) {
          btn.disabled = false;
        }
        _graspTeachUpdateWizardState();
      });
  };

  window.operatorsGraspTeachCalibStart = function () {
    if (!_graspTeachHasValidVision()) {
      window.alert("Prima premi 1 - Leggi target. Serve una stima valida prima di salvare la presa vera.");
      _graspTeachSetNextStep("Bloccato: manca 1 - Leggi target.", "is-fail");
      return;
    }
    var msg =
      "③ CALIBRA (posa reale)\n\n" +
      "1) Prima fai ① e ② per vedere l'errore della visione.\n" +
      "2) Pinza si apre · 5 s attesa (coppia attiva).\n" +
      "3) Giunti liberi 15 s: metti le pinze ESATTAMENTE sulla presa reale dell'oggetto.\n" +
      "4) Offset salvato sulla NX e riusato alle prossime stime.\n\n" +
      "Avviare?";
    if (!window.confirm(msg)) {
      return;
    }
    var pre = document.getElementById("graspCoachPre");
    if (pre) {
      pre.textContent = "POST /api/grasp_coach/teach_calib/start …";
    }
    _graspTeachCalibSetUi(true);
    var instrEl = document.getElementById("graspTeachInstruction");
    var instruction = instrEl && instrEl.value ? String(instrEl.value).trim() : "";
    fetch(window.operatorsApi("/api/grasp_coach/teach_calib/start"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        require_detection: true,
        hold_s: 5,
        manual_s: 15,
        instruction: instruction,
      }),
    })
      .then(function (r) {
        return r.json().then(function (j) {
          return { j: j, r: r };
        });
      })
      .then(function (pack) {
        if (pre) {
          pre.textContent = JSON.stringify(pack.j, null, 2);
        }
        if (!pack.j || !pack.j.ok) {
          _graspTeachCalibSetUi(false);
          _graspTeachCalibRenderStatus({
            phase: "error",
            error: (pack.j && (pack.j.reason || pack.j.hint_it)) || "start_failed",
          });
          return;
        }
        if (!__graspTeachCalibPoll) {
          __graspTeachCalibPoll = setInterval(_graspTeachCalibPollOnce, 450);
        }
        _graspTeachCalibPollOnce();
      })
      .catch(function (e) {
        _graspTeachCalibSetUi(false);
        if (pre) {
          pre.textContent = String(e);
        }
      });
  };

  window.__graspTeachOrbbecStolen = false;
  window.__graspTeachOrbbecLiveOn = false;

  function _graspTeachWristBackend() {
    var st = window.operatorsLastCameraStatus || {};
    return String(st.wrist_depth_backend || "realsense").toLowerCase();
  }

  function _graspTeachSdkUsesRealsense() {
    return _graspTeachWristBackend() !== "orbbec";
  }

  function _graspTeachSdkJpgUrl() {
    return window.operatorsApi("/api/grasp/detection_debug/wrist_realsense.jpg");
  }

  function _graspTeachSdkTag() {
    return _graspTeachSdkUsesRealsense() ? "wrist_realsense" : "wrist_orbbec";
  }

  function _graspTeachSdkSnapshotUrl() {
    return window.operatorsApi("/api/grasp/detection_debug/" + _graspTeachSdkTag() + ".jpg");
  }

  function _graspTeachOrbbecBarSync() {
    var bar = document.querySelector(".op-teach-orbbec-bar");
    var rs = _graspTeachSdkUsesRealsense();
    if (bar) {
      bar.querySelectorAll("#graspTeachStealOrbbecBtn,#graspTeachOrbbecLiveBtn").forEach(function (el) {
        el.style.display = rs ? "none" : "";
      });
    }
    var capBtn = document.getElementById("graspTeachOrbbecCaptureBtn");
    if (capBtn && rs) {
      capBtn.textContent = "Foto SDK (metrica)";
      capBtn.title = "Nuova acquisizione pyrealsense2 polso (RGB+depth) — ~8–12 s";
    }
  }

  function _graspTeachInstructionForSdk() {
    return _graspTeachInstructionText();
  }

  function _graspTeachOrbbecMeta(msg) {
    var el = document.getElementById("graspTeachOrbbecMeta");
    if (el && msg) {
      el.textContent = msg;
    }
  }

  function _graspTeachOrbbecUpdateStealBtn() {
    var btn = document.getElementById("graspTeachStealOrbbecBtn");
    if (!btn) {
      return;
    }
    if (window.__graspTeachOrbbecStolen) {
      btn.textContent = "Cedi Orbbec";
      btn.classList.add("active");
      btn.title = "Rilascia lock SDK — Scene può riprendere log.0";
    } else {
      btn.textContent = "Ruba Orbbec";
      btn.classList.remove("active");
      btn.title = "Lock esclusivo Orbbec SDK per acquisizione metrica / Live SDK";
    }
  }

  function _graspTeachOrbbecUpdateLiveBtn() {
    var btn = document.getElementById("graspTeachOrbbecLiveBtn");
    if (!btn) {
      return;
    }
    btn.classList.toggle("ok", !!window.__graspTeachOrbbecLiveOn);
    btn.textContent = window.__graspTeachOrbbecLiveOn ? "Stop live SDK" : "Live SDK RGB";
  }

  window.operatorsGraspTeachOrbbecRefreshLast = function () {
    var img = document.getElementById("graspTeachCamSdk");
    var badge = document.getElementById("graspTeachSdkBadge");
    if (!img) {
      return;
    }
    _graspTeachOrbbecBarSync();
    window.__graspTeachOrbbecLiveOn = false;
    _graspTeachOrbbecUpdateLiveBtn();
    img.classList.remove("op-teach-sdk-empty");
    var applyUrl = function (snap) {
      var url = _graspTeachSdkSnapshotUrl() + "?_=" + Date.now();
      img.onerror = function () {
        if (badge) {
          badge.textContent = "Nessuna foto SDK — premi «Foto SDK (metrica)» (~10 s)";
          badge.style.color = "#fecaca";
        }
        img.classList.add("op-teach-sdk-empty");
      };
      img.onload = function () {
        if (!badge) {
          return;
        }
        var when = snap && snap.saved_at ? String(snap.saved_at).replace("T", " ").replace("+00:00", " UTC") : "";
        if (when) {
          badge.textContent = "Ultima acquisizione SDK: " + when + " (non è live MJPEG)";
        } else {
          badge.textContent = "Ultima acquisizione SDK (non live — premi «Foto SDK» per aggiornare)";
        }
        badge.style.color = "#86efac";
      };
      img.src = url;
    };
    fetch(window.operatorsApi("/api/grasp/detection_debug?_=" + Date.now()), { cache: "no-store" })
      .then(function (r) {
        return r.json();
      })
      .then(function (m) {
        var snap = m && m.snapshots ? m.snapshots[_graspTeachSdkTag()] : null;
        if (!snap || !snap.image_url) {
          if (badge) {
            badge.textContent = "Nessuna acquisizione SDK ancora — premi «Foto SDK (metrica)»";
            badge.style.color = "#fde68a";
          }
          img.classList.add("op-teach-sdk-empty");
          img.removeAttribute("src");
          return;
        }
        applyUrl(snap);
      })
      .catch(function () {
        applyUrl(null);
      });
  };

  window.operatorsGraspTeachOrbbecRefreshLock = function () {
    return fetch(window.operatorsApi("/api/orbbec/lock"))
      .then(function (r) {
        return r.json();
      })
      .then(function (data) {
        window.__graspTeachOrbbecStolen = !!data.we_hold;
        _graspTeachOrbbecUpdateStealBtn();
        if (data.we_hold) {
          _graspTeachOrbbecMeta(
            "Orbbec rubata — UVC log.0 può bloccarsi. Per la metrica usa «Foto SDK (metrica)» o «Prendi»."
          );
        } else if (data.holder) {
          _graspTeachOrbbecMeta("Orbbec occupata da: " + data.holder + " — prova «Ruba Orbbec».");
        }
        return data;
      })
      .catch(function () {
        return null;
      });
  };

  window.operatorsGraspTeachOrbbecStealToggle = function () {
    var btn = document.getElementById("graspTeachStealOrbbecBtn");
    if (btn) {
      btn.disabled = true;
    }
    var url = window.__graspTeachOrbbecStolen ? "/api/orbbec/release" : "/api/orbbec/steal";
    if (window.__graspTeachOrbbecStolen) {
      window.__graspTeachOrbbecLiveOn = false;
      var liveImg = document.getElementById("graspTeachCamSdk");
      if (liveImg) {
        liveImg.src = "";
        liveImg.classList.add("op-teach-sdk-empty");
      }
      _graspTeachOrbbecUpdateLiveBtn();
    }
    fetch(window.operatorsApi(url), { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
      .then(function (r) {
        return r.json().then(function (j) {
          return { j: j, r: r };
        });
      })
      .then(function (pack) {
        var j = pack.j || {};
        if (!j.ok && !window.__graspTeachOrbbecStolen) {
          _graspTeachOrbbecMeta(j.hint || j.reason || "Impossibile rubare Orbbec" + (j.holder ? ": " + j.holder : ""));
          return;
        }
        window.__graspTeachOrbbecStolen = url.indexOf("steal") >= 0 && !!j.ok;
        if (url.indexOf("release") >= 0) {
          window.__graspTeachOrbbecStolen = false;
        }
        _graspTeachOrbbecUpdateStealBtn();
        _graspTeachOrbbecMeta(j.hint || (window.__graspTeachOrbbecStolen ? "Orbbec rubata — usa Live SDK o «Prendi»" : "Orbbec ceduta"));
        if (window.__graspTeachOrbbecStolen) {
          window.operatorsGraspTeachOrbbecRefreshLast();
        }
      })
      .catch(function (e) {
        _graspTeachOrbbecMeta(String(e));
      })
      .finally(function () {
        if (btn) {
          btn.disabled = false;
        }
      });
  };

  window.operatorsGraspTeachOrbbecCapture = function () {
    var capBtn = document.getElementById("graspTeachOrbbecCaptureBtn");
    if (capBtn) {
      capBtn.disabled = true;
    }
    if (typeof _graspTeachOrbbecMeta === "function") {
      _graspTeachOrbbecMeta("Acquisizione SDK in corso (~3–5 s) — log.0 UVC in pausa…");
    }
    fetch(window.operatorsApi("/api/grasp_coach/preview"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ instruction: _graspTeachInstructionForSdk(), light: true }),
    })
      .then(function (r) {
        return r.json().then(function (j) {
          return { j: j, r: r };
        });
      })
      .then(function (pack) {
        var j = pack.j || {};
        window.operatorsGraspTeachOrbbecRefreshLast();
        if (typeof _graspCoachDrawViz === "function") {
          try {
            _graspCoachDrawViz(j);
          } catch (eV) {
            /* optional */
          }
        }
        if (typeof _graspCoachRenderMetricPanel === "function") {
          try {
            _graspCoachRenderMetricPanel(j);
          } catch (eM) {
            /* optional */
          }
        }
        if (j.ok) {
          _graspTeachOrbbecMeta("SDK metrico OK — bbox e depth acquisiti.");
        } else {
          var hint = j.label_it || j.hint_it || j.reason || "SDK senza detection/depth";
          _graspTeachOrbbecMeta(
            hint + " — immagine SDK aggiornata (non è lo stream UVC live)."
          );
        }
      })
      .catch(function (e) {
        _graspTeachOrbbecMeta(String(e));
      })
      .finally(function () {
        if (capBtn) {
          capBtn.disabled = false;
        }
      });
  };

  window.operatorsGraspTeachOrbbecLiveToggle = function () {
    if (_graspTeachSdkUsesRealsense()) {
      _graspTeachOrbbecMeta(
        "Con RealSense polso non c'è live SDK MJPEG: usa UVC log.0 (sinistra) o «Foto SDK (metrica)»."
      );
      return;
    }
    var img = document.getElementById("graspTeachCamSdk");
    if (!img) {
      return;
    }
    if (window.__graspTeachOrbbecLiveOn) {
      window.__graspTeachOrbbecLiveOn = false;
      img.src = "";
      _graspTeachOrbbecUpdateLiveBtn();
      window.operatorsGraspTeachOrbbecRefreshLast();
      return;
    }
    var startLive = function () {
      return fetch(window.operatorsApi("/api/orbbec/probe"))
        .then(function (r) {
          return r.json();
        })
        .then(function (probe) {
          if (!probe.ok || probe.chosen_v4l_index == null) {
            _graspTeachOrbbecMeta("Live SDK RGB non disponibile — nodo Orbbec IR/depth o camera occupata.");
            return;
          }
          window.__graspTeachOrbbecLiveOn = true;
          img.classList.remove("op-teach-sdk-empty");
          img.src = window.operatorsApi("/api/orbbec/live.mjpg") + "?t=" + Date.now();
          _graspTeachOrbbecUpdateLiveBtn();
          var badge = document.getElementById("graspTeachSdkBadge");
          if (badge) {
            badge.textContent = "Live SDK RGB /dev/video" + probe.chosen_v4l_index;
            badge.style.color = "#86efac";
          }
        });
    };
    if (!window.__graspTeachOrbbecStolen) {
      fetch(window.operatorsApi("/api/orbbec/steal"), { method: "POST", body: "{}" })
        .then(function (r) {
          return r.json();
        })
        .then(function (j) {
          if (!j.ok) {
            _graspTeachOrbbecMeta(j.hint || "Prima «Ruba Orbbec» — camera occupata");
            return;
          }
          window.__graspTeachOrbbecStolen = true;
          _graspTeachOrbbecUpdateStealBtn();
          return startLive();
        })
        .catch(function (e) {
          _graspTeachOrbbecMeta(String(e));
        });
    } else {
      startLive();
    }
  };

  window.operatorsGraspTeachOrbbecInit = function () {
    _graspTeachBindInstructionSync();
    _graspTeachSyncCoachInstructionFromHero();
    _graspTeachRefreshCoachBadge();
    if (typeof window.operatorsGraspTeachSyncStatus === "function") {
      window.operatorsGraspTeachSyncStatus();
    }
    _graspTeachOrbbecBarSync();
    _graspTeachOrbbecUpdateStealBtn();
    _graspTeachOrbbecUpdateLiveBtn();
    window.operatorsGraspTeachOrbbecRefreshLock().then(function () {
      window.operatorsGraspTeachOrbbecRefreshLast();
    });
    fetch(window.operatorsApi("/api/cameras/status?_=" + Date.now()), { cache: "no-store" })
      .then(function (r) {
        return r.json();
      })
      .then(function (j) {
        window.operatorsLastCameraStatus = j;
        _graspTeachOrbbecBarSync();
        if (typeof window.operatorsUpdateCamLiveBadges === "function") {
          window.operatorsUpdateCamLiveBadges(j);
        }
        var s0 = j && j.camera_summary && j.camera_summary["0"];
        var s6 = j && j.camera_summary && j.camera_summary["6"];
        if (s0 && s6 && s0.device_path && s0.device_path === s6.device_path) {
          _graspTeachOrbbecMeta(
            "ATTENZIONE: log.0 e log.6 puntano allo stesso " +
              (s0.device_path || "?") +
              " — in tab Scene: log.0 = D456 polso, log.6 = D435i frontale."
          );
        } else if (
          s0 &&
          /realsense/i.test(String(s0.sysfs_name || s0.card_name || s0.name || s0.label || "")) &&
          j &&
          j.wrist_depth_backend === "orbbec"
        ) {
          _graspTeachOrbbecMeta(
            "log.0 è RealSense (" +
              (s0.device_path || "?") +
              ") ma il backend polso è Orbbec — imposta GO2_WRIST_DEPTH_BACKEND=realsense sulla NX."
          );
        } else if (s0 && s0.color_ok === false) {
          _graspTeachOrbbecMeta(
            "log.0 NON è RGB (prob. IR/depth su " +
              (s0.device_path || "?") +
              ") — «Prossimo video polso» o tab Scene."
          );
        } else if (s0 && s0.error) {
          _graspTeachOrbbecMeta(String(s0.error));
        }
      })
      .catch(function () {
        /* ignore */
      });
    if (typeof window.operatorsRefreshCamerasStatus === "function") {
      window.operatorsRefreshCamerasStatus();
    }
  };

  var _GRASP_TEACH_STEP_ORDER = ["couple", "scan_j90", "gates", "grasp", "verify", "done"];
  window.__graspTeachLogCount = 0;
  window.__graspTeachPoll = null;
  window.__graspTeachVisionDiag = null;
  window.__graspTeachSamplesCount = 0;

  function _graspTeachObjectLabel(instruction) {
    var s = String(instruction || "").toLowerCase();
    if (s.indexOf("cilind") >= 0) return "cilindro";
    if (s.indexOf("sigaro") >= 0) return "sigaro";
    if (s.indexOf("scatol") >= 0 || s.indexOf("box") >= 0) return "scatola";
    return "oggetto";
  }

  function _graspTeachHasValidVision() {
    var d = window.__graspTeachVisionDiag;
    return !!(d && d.ok && d.vision_target_base_link_m && d.vision_target_base_link_m.length >= 3);
  }

  function _graspTeachSetNextStep(text, cls) {
    var el = document.getElementById("graspTeachNextStep");
    if (!el) return;
    el.textContent = text;
    el.className = "op-grasp-front-badge " + (cls || "is-run");
    el.style.display = "block";
    el.style.marginBottom = "10px";
    el.style.fontWeight = "800";
  }

  function _graspTeachUpdateWizardState() {
    var instruction = _graspTeachInstructionText();
    var obj = _graspTeachObjectLabel(instruction);
    var hint = document.getElementById("graspTeachObjectHint");
    if (hint) {
      hint.textContent = "Oggetto selezionato: " + obj + ". Fai START +90°, poi ① → ② → ③.";
    }
    var visionBtn = document.getElementById("graspTeachVisionDiagBtn");
    if (visionBtn) {
      visionBtn.textContent = "① Leggi target " + obj;
    }
    var gotoBtn = document.getElementById("graspTeachGotoVisionBtn");
    if (gotoBtn) {
      gotoBtn.textContent = "② Muovi al target";
      gotoBtn.disabled = !_graspTeachHasValidVision();
      gotoBtn.title = gotoBtn.disabled ? "Prima premi ① Leggi target." : "Muove il braccio verso il target 3D appena letto.";
    }
    var calibBtn = document.getElementById("graspTeachCalibBtn");
    if (calibBtn) {
      calibBtn.textContent = "③ Salva presa vera";
      calibBtn.disabled = !_graspTeachHasValidVision();
      calibBtn.title = calibBtn.disabled ? "Prima premi ① Leggi target." : "Libera i giunti: metti le pinze nella presa reale.";
    }
    if (_graspTeachHasValidVision()) {
      _graspTeachSetNextStep("Prossimo step: ② Muovi al target. Se non cade sull'oggetto, fai ③ Salva presa vera.", "is-ok");
    } else {
      _graspTeachSetNextStep("Prossimo step: START +90°, poi ① Leggi target " + obj, "is-run");
    }
  }

  window.operatorsGraspTeachSetInstruction = function (txt) {
    var el = document.getElementById("graspTeachInstruction");
    if (el) el.value = txt;
    window.__graspTeachVisionDiag = null;
    _graspTeachSyncCoachInstructionFromHero();
    _graspTeachVisionDiagPanelSet("Oggetto impostato. Premi START +90°, poi ① Leggi target.", false);
    _graspTeachUpdateWizardState();
  };

  window.operatorsGraspTeachInstructionChanged = function () {
    window.__graspTeachVisionDiag = null;
    _graspTeachSyncCoachInstructionFromHero();
    _graspTeachUpdateWizardState();
  };

  function _graspTeachInstructionText() {
    var el = document.getElementById("graspTeachInstruction");
    var coach = document.getElementById("graspCoachInstruction");
    var txt =
      (el && el.value && String(el.value).trim()) ||
      (coach && coach.value && String(coach.value).trim()) ||
      "prendi la scatola blu";
    if (coach && el && coach.value !== el.value) {
      coach.value = txt;
    }
    return txt;
  }

  function _graspTeachSyncCoachInstructionFromHero() {
    var hero = document.getElementById("graspTeachInstruction");
    var coach = document.getElementById("graspCoachInstruction");
    if (hero && coach) {
      coach.value = hero.value || "";
    }
    var modeBadge = document.getElementById("graspTeachModeBadge");
    if (modeBadge) {
      modeBadge.textContent = "modalità: " + _graspTeachModeLabel(hero && hero.value ? hero.value : "");
    }
  }

  function _graspTeachSetCoachBadge(text, cls) {
    var badge = document.getElementById("graspTeachCoachBadge");
    if (!badge) {
      return;
    }
    badge.textContent = text;
    badge.classList.remove("is-idle", "is-ok", "is-fail", "is-run");
    if (cls) {
      badge.classList.add(cls);
    }
  }

  function _graspTeachApplyCoachBadge(st, startVar) {
    st = st || {};
    startVar = startVar === "frontal" ? "frontal" : "lateral";
    if (!st.enabled) {
      _graspTeachSetCoachBadge("Coach API disabilitato (GO2_ENABLE_GRASP_COACH=0)", "is-fail");
      return;
    }
    var parts = [];
    parts.push("▸ Prendi = RealSense metrico + IK");
    if (startVar === "lateral" && st.lateral_metric_only !== false) {
      parts.push("step coach laterale = NO GPT");
    } else if (startVar === "frontal") {
      parts.push(
        st.openai_configured
          ? "step coach frontale = GPT vision"
          : "step frontale richiede OPENAI_API_KEY"
      );
    } else {
      parts.push("step coach = GPT vision");
    }
    if (st.supervisor_enabled) {
      parts.push("supervisor OpenAI attivo nel loop");
    }
    var cls = st.openai_configured || startVar === "lateral" ? "is-ok" : "is-idle";
    if (startVar === "frontal" && !st.openai_configured) {
      cls = "is-fail";
    }
    _graspTeachSetCoachBadge(parts.join(" · "), cls);
  }

  function _graspTeachRefreshCoachBadge() {
    var startVar =
      typeof window.operatorsStartVariant === "function" ? window.operatorsStartVariant() : "lateral";
    fetch(window.operatorsApi("/api/grasp_coach/status"), { cache: "no-store" })
      .then(function (r) {
        return r.json();
      })
      .then(function (st) {
        window.__graspCoachStatus = st;
        _graspTeachApplyCoachBadge(st, startVar);
      })
      .catch(function () {
        _graspTeachSetCoachBadge("Coach API: non raggiungibile", "is-fail");
      });
  }
  window.operatorsGraspTeachRefreshCoachBadge = _graspTeachRefreshCoachBadge;

  var __graspTeachInstrBound = false;
  function _graspTeachBindInstructionSync() {
    if (__graspTeachInstrBound) {
      return;
    }
    var hero = document.getElementById("graspTeachInstruction");
    if (!hero) {
      return;
    }
    __graspTeachInstrBound = true;
    hero.addEventListener("input", _graspTeachSyncCoachInstructionFromHero);
    hero.addEventListener("change", _graspTeachSyncCoachInstructionFromHero);
  }

  function _graspTeachModeLabel(instr) {
    return /\b(raccogli(?:ere)?|collect|pick\s+up\s+all|gather)\b/i.test(instr || "")
      ? "raccolta"
      : "singola";
  }

  function graspTeachSetStepClass(stepName, cls) {
    var li = document.querySelector('#graspTeachSteps [data-step="' + stepName + '"]');
    if (!li) {
      return;
    }
    li.classList.remove("is-idle", "is-run", "is-ok", "is-fail", "is-skip");
    li.classList.add(cls);
  }

  function _graspTeachFailureDetail(st) {
    if (!st || st.ok !== false) {
      return "";
    }
    var steps = st.steps || [];
    var failed = st.failed_step;
    var i;
    for (i = 0; i < steps.length; i++) {
      if (steps[i].id === failed && steps[i].detail) {
        return steps[i].detail;
      }
    }
    var lines = st.log_lines || [];
    for (i = lines.length - 1; i >= 0; i--) {
      if (lines[i].level === "error") {
        return lines[i].msg_it || "";
      }
    }
    return st.label_it || failed || "errore sconosciuto";
  }

  function _graspTeachFailureHint(detail) {
    var d = String(detail || "").toLowerCase();
    if (d.indexOf("plane_busy") >= 0) {
      return "Chiudi la sessione live nel tab «Braccio D1 · giunti» (Fine controllo), poi riprova «Prendi».";
    }
    if (d.indexOf("orbbec_busy") >= 0) {
      return "Premi «Ruba Orbbec» o chiudi altre acquisizioni camera, poi riprova.";
    }
    if (d.indexOf("not_coupled") >= 0 || d.indexOf("coppia") >= 0) {
      return "Verifica coppia DDS nel tab «Braccio D1 · giunti» e che il daemon d1_sdk_command sia attivo.";
    }
    return "";
  }

  function _graspTeachRenderErrorBanner(st) {
    var el = document.getElementById("graspTeachErrorBanner");
    if (!el) {
      return;
    }
    if (!st || st.running || st.ok !== false) {
      el.hidden = true;
      el.innerHTML = "";
      return;
    }
    var detail = _graspTeachFailureDetail(st);
    var hint = _graspTeachFailureHint(detail);
    el.hidden = false;
    el.innerHTML =
      "<strong>Errore flusso</strong> · step «" +
      String(st.failed_step || "?").replace(/</g, "&lt;") +
      "» — " +
      String(detail).replace(/</g, "&lt;") +
      (hint
        ? '<br><span class="grasp-teach-hint">' + String(hint).replace(/</g, "&lt;") + "</span>"
        : "");
  }

  function _graspTeachSetGoBadge(st) {
    var el = document.getElementById("graspTeachGoBadge");
    if (!el) {
      return;
    }
    st = st || {};
    if (st.running) {
      var step = st.current_step ? " · " + st.current_step : "";
      el.textContent = "Presa: IN CORSO — " + (st.label_it || "flusso attivo") + step;
      el.className = "op-grasp-front-badge is-run";
      return;
    }
    if (st.ok === true) {
      el.textContent = "Presa: OK — flusso completato";
      el.className = "op-grasp-front-badge is-ok";
      return;
    }
    if (st.ok === false) {
      var tail = "";
      var lines = st.log_lines || [];
      if (lines.length) {
        tail = " · " + (lines[lines.length - 1].msg_it || "");
      }
      el.textContent =
        "Presa: FALLITA — " + (st.label_it || st.failed_step || "errore") + tail;
      el.className = "op-grasp-front-badge is-fail";
      return;
    }
    el.textContent = "Presa: pronta — premi «Prendi»";
    el.className = "op-grasp-front-badge is-idle";
  }

  function graspTeachResetStoryboard(runningFirst) {
    var i;
    for (i = 0; i < _GRASP_TEACH_STEP_ORDER.length; i++) {
      graspTeachSetStepClass(_GRASP_TEACH_STEP_ORDER[i], "is-idle");
    }
    if (runningFirst) {
      graspTeachSetStepClass(_GRASP_TEACH_STEP_ORDER[0], "is-run");
    }
  }

  function graspTeachRenderSteps(st) {
    var steps = (st && st.steps) || [];
    var i;
    for (i = 0; i < steps.length; i++) {
      var s = steps[i];
      var name = s.id || s.step;
      if (!name) {
        continue;
      }
      var status = s.status || "idle";
      if (status === "running") {
        graspTeachSetStepClass(name, "is-run");
      } else if (status === "ok") {
        graspTeachSetStepClass(name, "is-ok");
      } else if (status === "fail") {
        graspTeachSetStepClass(name, "is-fail");
      } else if (status === "skip") {
        graspTeachSetStepClass(name, "is-skip");
      } else {
        graspTeachSetStepClass(name, "is-idle");
      }
      var txt = document.querySelector('#graspTeachSteps [data-step="' + name + '"] .op-grasp-story-txt');
      if (txt && s.detail) {
        var base = txt.textContent.split(" — ")[0];
        txt.textContent = base + " — " + s.detail;
      }
    }
    if (st && st.failed_step) {
      graspTeachSetStepClass(st.failed_step, "is-fail");
    }
  }

  function graspTeachRenderProgress(st) {
    var fill = document.getElementById("graspTeachProgressFill");
    var lab = document.getElementById("graspTeachProgressLabel");
    var pct = st && typeof st.progress_pct === "number" ? st.progress_pct : 0;
    if (fill) {
      fill.style.width = Math.max(0, Math.min(100, pct)) + "%";
      fill.classList.toggle("err", !!(st && st.running === false && st.ok === false));
    }
    if (lab) {
      lab.textContent =
        (st && st.label_it) ||
        (st && st.running ? "Flusso in corso…" : "Pronto — premi «Prendi».");
    }
  }

  function graspTeachRenderLog(st) {
    var logEl = document.getElementById("graspTeachLog");
    if (!logEl || !st) {
      return;
    }
    var lines = st.log_lines || [];
    if (!lines.length && st.label_it && logEl.innerHTML === "— log flusso —") {
      logEl.innerHTML =
        '<div class="log-info">· ' + String(st.label_it).replace(/</g, "&lt;") + "</div>";
    }
    var start = window.__graspTeachLogCount || 0;
    if (start >= lines.length) {
      return;
    }
    var html = logEl.innerHTML === "— log flusso —" ? "" : logEl.innerHTML;
    var j;
    for (j = start; j < lines.length; j++) {
      var ln = lines[j];
      var lvl = ln.level || "info";
      var cls = lvl === "error" ? "log-error" : lvl === "warn" ? "log-warn" : "log-info";
      var prefix = lvl === "error" ? "✗ " : lvl === "warn" ? "⚠ " : "· ";
      html +=
        '<div class="' +
        cls +
        '">' +
        prefix +
        (ln.msg_it || "") +
        "</div>";
    }
    logEl.innerHTML = html;
    logEl.scrollTop = logEl.scrollHeight;
    window.__graspTeachLogCount = lines.length;
  }

  function _graspTeachFmtDepthLive(lw) {
    if (!lw || lw.depth_m == null) {
      return "depth: — (in attesa D456)";
    }
    var dm = Number(lw.depth_m).toFixed(3) + " m";
    var src = lw.depth_source_it || lw.depth_source || "?";
    var tag = lw.rgb_depth_fallback ? "RGB stimata" : lw.depth_ok ? "D456 OK" : "D456 NO";
    var extra = "";
    if (lw.depth_support != null) {
      extra += " · px=" + lw.depth_support;
    }
    if (lw.depth_diag_reason) {
      extra += " · " + lw.depth_diag_reason;
    }
    if (lw.depth_nonzero_px != null) {
      extra += " · nz=" + lw.depth_nonzero_px;
    }
    return "depth: " + dm + " (" + src + ", " + tag + ")" + extra;
  }

  function _graspTeachUpdateMetricLive(st) {
    var lw = (st && st.live_wrist) || null;
    var mp = (st && st.metric_plan) || {};
    var g = (st && st.gates) || {};
    var depthVal = lw && lw.depth_m != null ? lw.depth_m : mp.depth_m != null ? mp.depth_m : g.depth_m;
    var elDepth = document.getElementById("graspCoachMetDepth");
    var elReach = document.getElementById("graspCoachMetReach");
    var elSt = document.getElementById("graspCoachMetStatus");
    var elObj = document.getElementById("graspCoachMetObj");
    var elConf = document.getElementById("graspCoachMetConf");
    var meta = document.getElementById("graspTeachOrbbecMeta");
    var sdkBadge = document.getElementById("graspTeachSdkBadge");
    var det = (lw && st.metric_plan && st.metric_plan.object_detection) || mp.object_detection || {};
    if (elDepth) {
      if (depthVal != null) {
        var rgbFb = lw ? lw.rgb_depth_fallback : mp.rgb_depth_fallback || g.rgb_depth_fallback;
        elDepth.textContent = Number(depthVal).toFixed(3) + " m" + (rgbFb ? " (RGB)" : " (D456)");
        elDepth.style.color = rgbFb ? "#e0a040" : "#30d070";
      } else {
        elDepth.textContent = "—";
        elDepth.style.color = "";
      }
    }
    if (elReach && (lw || mp).reach_m != null) {
      var rm = lw && lw.reach_m != null ? lw.reach_m : mp.reach_m;
      var reachOk = lw && lw.reachable != null ? lw.reachable : mp.reachable;
      elReach.textContent = Number(rm).toFixed(3) + " m" + (reachOk === false ? " FUORI" : reachOk ? " OK" : "");
      elReach.style.color = reachOk === false ? "#d05050" : "#30d070";
    }
    if (elSt && (st.running || lw)) {
      elSt.textContent = _graspTeachFmtDepthLive(lw || {
        depth_m: depthVal,
        depth_source_it: g.depth_source_it,
        rgb_depth_fallback: g.rgb_depth_fallback,
        depth_ok: g.depth_ok,
        depth_support: g.depth_support,
        depth_diag_reason: mp.depth_diag && mp.depth_diag.reason,
      });
      elSt.style.color = (lw && lw.depth_ok) || g.depth_ok ? "#30d070" : "#e0a040";
    }
    if (elObj && det && det.ok) {
      elObj.textContent = (det.color_hint || "oggetto") + " conf=" + (det.confidence != null ? Number(det.confidence).toFixed(2) : "?");
      elObj.style.color = "#30d070";
    }
    if (elConf && det && det.confidence != null) {
      elConf.textContent = Number(det.confidence).toFixed(2);
    }
    if (meta && (st.running || lw)) {
      var cycle = lw && lw.cycle ? " · " + lw.cycle : "";
      meta.innerHTML =
        "<strong>RealSense polso (live)</strong>" +
        cycle +
        ": " +
        _graspTeachFmtDepthLive(lw).replace(/</g, "&lt;") +
        ". Destra = ultima foto SDK con bbox (si aggiorna a ogni acquisizione).";
    }
    if (sdkBadge && (st.running || lw)) {
      sdkBadge.textContent = _graspTeachFmtDepthLive(lw);
      sdkBadge.style.color = lw && lw.depth_ok ? "#86efac" : "#fcd34d";
    }
    if (st && st.running && typeof window.operatorsGraspTeachOrbbecRefreshLast === "function") {
      var stamp = lw && lw.updated_at ? lw.updated_at : String(Date.now());
      if (window.__graspTeachLastSdkStamp !== stamp) {
        window.__graspTeachLastSdkStamp = stamp;
        window.operatorsGraspTeachOrbbecRefreshLast();
      }
    }
  }

  function _graspTeachUpdateGatesFromStatus(st) {
    var g = st && st.gates;
    if (!g) {
      return;
    }
    var lw = st && st.live_wrist;
    var elD = document.getElementById("graspCoachGateDepth");
    var elDet = document.getElementById("graspCoachGateDetect");
    var elR = document.getElementById("graspCoachGateReach");
    var elC = document.getElementById("graspCoachGateCalib");
    if (elD) {
      if (g.depth_m != null) {
        elD.textContent =
          "depth: " +
          Number(g.depth_m).toFixed(3) +
          "m " +
          (g.rgb_depth_fallback ? "(RGB)" : g.depth_ok ? "(D456)" : "(NO)") +
          (g.depth_support != null ? " s=" + g.depth_support : "");
      } else if (lw && lw.depth_m != null) {
        elD.textContent = _graspTeachFmtDepthLive(lw);
      } else {
        elD.textContent = "depth: " + (g.depth_ok ? "OK" : "NO");
      }
    }
    if (elDet) {
      elDet.textContent = "detect: " + (g.detect_ok ? "OK" : g.reason || "NO");
    }
    if (elR) {
      elR.textContent = "reach: " + (g.reach_ok ? "OK" : "NO");
    }
    if (elC) {
      elC.textContent = "calib: " + (g.calib_ok ? "OK" : "—");
    }
  }

  function graspTeachUpdateFromStatus(st) {
    if (!st) {
      return;
    }
    graspTeachRenderSteps(st);
    graspTeachRenderProgress(st);
    graspTeachRenderLog(st);
    _graspTeachSetGoBadge(st);
    _graspTeachRenderErrorBanner(st);
    _graspTeachUpdateGatesFromStatus(st);
    _graspTeachUpdateMetricLive(st);
    var badge = document.getElementById("graspTeachModeBadge");
    if (badge && st.mode) {
      badge.textContent = "modalità: " + (st.mode === "collect" ? "raccolta" : "singola");
      badge.className =
        "op-grasp-front-badge " +
        (st.running ? "is-run" : st.ok ? "is-ok" : st.ok === false ? "is-fail" : "is-idle");
    }
    var summary = document.getElementById("graspTeachSummary");
    if (summary) {
      if (st.running) {
        summary.textContent =
          (st.label_it || "Flusso in corso…") +
          (st.current_step ? " · step: " + st.current_step : "");
      } else if (st.running === false) {
        summary.textContent = st.ok
          ? "Flusso completato con successo."
          : "Flusso interrotto" + (st.failed_step ? " su «" + st.failed_step + "»." : ".");
      }
    }
    var cancelBtn = document.getElementById("graspTeachCancelBtn");
    if (cancelBtn) {
      cancelBtn.disabled = !st.running;
    }
    var runBtn = document.getElementById("graspTeachRunBtn");
    if (runBtn && st.running === false && window.__graspTeachPollMode !== "until_running") {
      runBtn.disabled = false;
    }
    if (st.metric_plan && typeof _graspCoachDrawViz === "function") {
      try {
        _graspCoachDrawViz({ ok: st.metric_plan.ok, metric_grounding: st.metric_plan, object_detection: st.metric_plan.object_detection });
      } catch (eV) {
        /* optional viz */
      }
    }
    if (st.current_step === "gates" || st.current_step === "grasp") {
      if (typeof window.operatorsGraspTeachOrbbecRefreshLast === "function") {
        window.operatorsGraspTeachOrbbecRefreshLast();
      }
    }
    if (st.running === false && window.__graspTeachMjpegPaused) {
      _graspTeachReleaseMjpegIfPaused();
    }
  }

  function _graspTeachPollOnce(onDone) {
    fetch(window.operatorsApi("/api/grasp/teach_status?_=" + Date.now()), { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) {
          throw new Error("HTTP " + r.status);
        }
        return r.json();
      })
      .then(function (st) {
        window.__graspTeachPollErrors = 0;
        graspTeachUpdateFromStatus(st);
        if (typeof onDone === "function") {
          onDone(st);
        }
      })
      .catch(function (e) {
        window.__graspTeachPollErrors = (window.__graspTeachPollErrors || 0) + 1;
        var mjpegActive = 0;
        try {
          mjpegActive = document.querySelectorAll('img[src*=".mjpg"]').length;
        } catch (eMj) {
          mjpegActive = -1;
        }
        // #region agent log
        fetch("http://127.0.0.1:7648/ingest/1f6b2724-6bbf-4c6c-a795-45910cf4b1c4", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Debug-Session-Id": "16a61f" },
          body: JSON.stringify({
            sessionId: "16a61f",
            runId: "post-fix-v2",
            hypothesisId: "H6",
            location: "operators_grasp_coach.js:teach_status_poll_fail",
            message: "teach_status poll fail",
            data: {
              poll_errors: window.__graspTeachPollErrors,
              poll_mode: window.__graspTeachPollMode,
              mjpeg_active: mjpegActive,
              mjpeg_paused: !!(window.__graspTeachMjpegPaused && window.__graspTeachMjpegPaused.length),
              error: String(e),
            },
            timestamp: Date.now(),
          }),
        }).catch(function () {});
        // #endregion
        var logEl = document.getElementById("graspTeachLog");
        if (logEl && window.__graspTeachPollErrors >= 3 && !logEl.querySelector(".grasp-teach-poll-warn")) {
          var pollWarn = document.createElement("div");
          pollWarn.className = "log-warn grasp-teach-poll-warn";
          pollWarn.textContent =
            "⚠ Aggiornamento log lento (rete o stream MJPEG) — i messaggi sotto restano validi.";
          logEl.insertBefore(pollWarn, logEl.firstChild);
        }
      });
  }

  function _graspTeachStartPolling(st0, opts) {
    opts = opts || {};
    var btn = document.getElementById("graspTeachRunBtn");
    window.__graspTeachPollMode = opts.untilRunning ? "until_running" : "normal";
    if (st0) {
      graspTeachUpdateFromStatus(st0);
    }
    if (window.__graspTeachPoll) {
      clearInterval(window.__graspTeachPoll);
      window.__graspTeachPoll = null;
    }
    window.__graspTeachPollErrors = 0;
    window.__graspTeachAwaitStartSince = opts.untilRunning ? Date.now() : 0;
    function _graspTeachBailAwaitStart(reasonIt) {
      window.__graspTeachPollMode = "normal";
      window.__graspTeachAwaitStartSince = 0;
      _graspTeachReleaseMjpegIfPaused();
      if (window.__graspTeachPoll) {
        clearInterval(window.__graspTeachPoll);
        window.__graspTeachPoll = null;
      }
      if (btn) {
        btn.disabled = false;
      }
      _graspTeachSetGoBadge({ running: false, ok: false, label_it: reasonIt });
      graspTeachRenderProgress({
        running: false,
        ok: false,
        progress_pct: 0,
        label_it: reasonIt,
      });
      _graspTeachRenderErrorBanner({
        running: false,
        ok: false,
        failed_step: "start",
        label_it: reasonIt,
        log_lines: [{ level: "error", msg_it: reasonIt }],
      });
    }
    function _pollDone(st) {
      if (window.__graspTeachPollMode === "until_running") {
        if (st && st.running) {
          window.__graspTeachPollMode = "normal";
          window.__graspTeachAwaitStartSince = 0;
        } else {
          var waited = Date.now() - (window.__graspTeachAwaitStartSince || Date.now());
          if (waited > 12000) {
            _graspTeachBailAwaitStart(
              "Avvio non confermato dalla NX — riprova «Prendi» (rete o tab bloccato)."
            );
            var logEl = document.getElementById("graspTeachLog");
            if (logEl) {
              logEl.innerHTML +=
                '<div class="log-error">✗ Nessun job teach avviato dopo 12s di polling.</div>';
            }
          }
          return;
        }
      }
      if (st && st.running === false && btn) {
        btn.disabled = false;
      }
    }
    _graspTeachPollOnce(_pollDone);
    window.__graspTeachPoll = setInterval(function () {
      _graspTeachPollOnce(function (st) {
        _pollDone(st);
        if (window.__graspTeachPollMode === "until_running") {
          return;
        }
        if (st && st.running === false) {
          clearInterval(window.__graspTeachPoll);
          window.__graspTeachPoll = null;
        }
      });
    }, 700);
  }

  window.operatorsGraspTeachSyncStatus = function () {
    fetch(window.operatorsApi("/api/grasp/teach_status?_=" + Date.now()), { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) {
          throw new Error("HTTP " + r.status);
        }
        return r.json();
      })
      .then(function (st) {
        if (!st) {
          return;
        }
        var hasJob =
          !!st.running ||
          st.ok === true ||
          (st.ok === false && (st.failed_step || (st.log_lines && st.log_lines.length)));
        window.__graspTeachLogCount = 0;
        if (hasJob) {
          graspTeachUpdateFromStatus(st);
        } else {
          window.__graspTeachPollMode = "normal";
          window.__graspTeachAwaitStartSince = 0;
          if (window.__graspTeachPoll) {
            clearInterval(window.__graspTeachPoll);
            window.__graspTeachPoll = null;
          }
          graspTeachUpdateFromStatus(st);
          _graspTeachRenderErrorBanner(st);
        }
        var btn = document.getElementById("graspTeachRunBtn");
        if (btn) {
          btn.disabled = !!st.running;
        }
        if (st.running) {
          _graspTeachReleaseMjpegIfPaused();
          var pausedMjpeg = _graspTeachPauseMjpegForFetch();
          window.__graspTeachMjpegPaused = pausedMjpeg;
          _graspTeachStartPolling(st);
        }
      })
      .catch(function () {
        /* ignore */
      });
  };

  window.operatorsGraspTeachResumeIfRunning = window.operatorsGraspTeachSyncStatus;

  window.operatorsGraspTeachCancel = function () {
    var btn = document.getElementById("graspTeachCancelBtn");
    var logEl = document.getElementById("graspTeachLog");
    if (btn) {
      btn.disabled = true;
    }
    fetch(window.operatorsApi("/api/grasp/teach_cancel"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason_it: "Annullato dall'operatore in dashboard." }),
    })
      .then(function (r) {
        return r.json();
      })
      .then(function (j) {
        var st = (j && j.status) || j;
        if (st && typeof st === "object" && st.log_lines) {
          graspTeachUpdateFromStatus(st);
        } else if (j && j.status) {
          graspTeachUpdateFromStatus(j.status);
        }
        if (window.__graspTeachPoll) {
          clearInterval(window.__graspTeachPoll);
          window.__graspTeachPoll = null;
        }
        var runBtn = document.getElementById("graspTeachRunBtn");
        if (runBtn) {
          runBtn.disabled = false;
        }
        _graspTeachReleaseMjpegIfPaused();
        if (logEl && j && !j.was_running) {
          logEl.innerHTML = '<div class="log-warn">⚠ Nessun flusso teach attivo da annullare.</div>';
        }
      })
      .catch(function (e) {
        if (logEl) {
          logEl.innerHTML = '<div class="log-error">✗ Annullamento fallito: ' + String(e) + "</div>";
        }
        if (btn) {
          btn.disabled = false;
        }
      });
  };

  function _graspTeachPauseMjpegForFetch() {
    var paused = [];
    try {
      document.querySelectorAll('img[src*=".mjpg"]').forEach(function (img) {
        if (!img || !img.src) {
          return;
        }
        paused.push({ img: img, src: img.src });
        img.removeAttribute("src");
      });
    } catch (e) {
      /* ignore */
    }
    return paused;
  }

  function _graspTeachRestoreMjpegAfterFetch(paused) {
    if (!paused || !paused.length) {
      return;
    }
    paused.forEach(function (item) {
      try {
        if (item.img && item.src) {
          item.img.src = item.src;
        }
      } catch (e) {
        /* ignore */
      }
    });
  }

  function _graspTeachReleaseMjpegIfPaused() {
    if (!window.__graspTeachMjpegPaused) {
      return;
    }
    _graspTeachRestoreMjpegAfterFetch(window.__graspTeachMjpegPaused);
    window.__graspTeachMjpegPaused = null;
  }

  window.operatorsGraspTeachGotoScanJ90 = function () {
    var btn = document.getElementById("graspTeachScanJ90Btn");
    var pre = document.getElementById("graspCoachPre");
    var badge = document.getElementById("graspTeachGoBadge");
    if (btn) btn.disabled = true;
    _graspTeachSetNextStep("START +90 in corso: sposto il braccio in scansione laterale.", "is-run");
    _graspTeachActionBanner(false, "START +90", "Movimento verso Scansione +90 in corso...", "");
    if (badge) {
      badge.textContent = "START +90: movimento...";
      badge.className = "op-grasp-front-badge is-run";
    }
    if (pre) pre.textContent = "POST /api/presets/scan/goto { variant: j90 }";

    _graspTeachReleaseMjpegIfPaused();
    var pausedMjpeg = _graspTeachPauseMjpegForFetch();
    var ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
    var timer = setTimeout(function () {
      if (ctrl) {
        try { ctrl.abort(); } catch (e) {}
      }
    }, 25000);
    var opts = {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ variant: "j90" }),
    };
    if (ctrl) opts.signal = ctrl.signal;
    fetch(window.operatorsApi("/api/presets/scan/goto"), opts)
      .then(function (r) {
        return r.json().then(function (j) { return { r: r, j: j }; });
      })
      .then(function (pack) {
        var j = pack.j || {};
        if (pre) pre.textContent = JSON.stringify(j, null, 2);
        if (pack.r.ok && j.ok) {
          var wait = j.wait_at_target || {};
          var detail = j.waypoint_name || "Punto SCANSIONE 90";
          if (wait.max_error_deg != null) detail += " - errore max " + Number(wait.max_error_deg).toFixed(1) + " deg";
          _graspTeachActionBanner(true, "START +90 OK", detail, "Ora premi 1 - Leggi target.");
          _graspTeachSetNextStep("Prossimo step: 1 - Leggi target.", "is-ok");
          if (badge) {
            badge.textContent = "START +90: OK";
            badge.className = "op-grasp-front-badge is-ok";
          }
          _graspTeachCalibRenderStatus({ phase: "done", phase_label_it: "START +90 raggiunta. Premi 1 - Leggi target." });
        } else {
          var reason = j.reason || j.error || "scan_j90_failed";
          _graspTeachActionBanner(false, "START +90 fallito", reason, j.hint_it || j.hint || "");
          _graspTeachSetNextStep("START +90 fallito: leggi il messaggio e riprova.", "is-fail");
          if (badge) {
            badge.textContent = "START +90: ERRORE";
            badge.className = "op-grasp-front-badge is-fail";
          }
          _graspTeachCalibRenderStatus({ phase: "error", error: reason });
        }
      })
      .catch(function (e) {
        var msg = String(e);
        if (pre) pre.textContent = msg;
        _graspTeachActionBanner(false, "START +90 rete", msg, "Ho verificato che il backend risponde. Fai Ctrl+F5 e riprova: la UI ora pausa gli stream prima del POST.");
        _graspTeachSetNextStep("Errore rete START +90. Fai Ctrl+F5 e riprova.", "is-fail");
        if (badge) {
          badge.textContent = "START +90: ERRORE rete";
          badge.className = "op-grasp-front-badge is-fail";
        }
      })
      .finally(function () {
        clearTimeout(timer);
        _graspTeachRestoreMjpegAfterFetch(pausedMjpeg);
        if (btn) btn.disabled = false;
      });
  };

  window.operatorsGraspTeachRun = function () {
    var btn = document.getElementById("graspTeachRunBtn");
    var instruction = _graspTeachInstructionText();
    var modeLbl = _graspTeachModeLabel(instruction);
    if (Number(window.__graspTeachSamplesCount || 0) <= 0) {
      _graspTeachActionBanner(
        false,
        "Nessun teaching salvato",
        "Puoi avviare Prendi, ma il braccio usera' solo depth/geometria.",
        "Per calibrare: START +90, 1 Leggi target, 2 Muovi al target, 3 Salva presa vera."
      );
    }
    if (
      !window.confirm(
        "PRENDI · flusso unificato\n\n" +
          "1) Scansione +90° automatica\n" +
          "2) Gate metrici polso\n" +
          "3) " +
          (modeLbl === "raccolta" ? "Raccolta scatole colorate" : "Presa autonoma stream") +
          "\n\nIstruzione: «" +
          instruction +
          "»\n\nArea libera e braccio accoppiato? Continuare?"
      )
    ) {
      return;
    }
    if (window.__graspTeachPoll) {
      clearInterval(window.__graspTeachPoll);
      window.__graspTeachPoll = null;
    }
    window.__graspTeachLogCount = 0;
    window.__graspTeachPollErrors = 0;
    var logEl = document.getElementById("graspTeachLog");
    if (logEl) {
      logEl.innerHTML = "— log flusso —";
    }
    graspTeachResetStoryboard(false);
    _graspTeachSetGoBadge({ running: true, label_it: "Invio comando…" });
    graspTeachRenderProgress({ progress_pct: 2, label_it: "Invio comando alla NX…", running: true });
    var badge = document.getElementById("graspTeachModeBadge");
    if (badge) {
      badge.textContent = "modalità: " + modeLbl;
      badge.className = "op-grasp-front-badge is-run";
    }
    if (btn) {
      btn.disabled = true;
    }
    // Libera slot HTTP (browser max ~6/host) prima di polling e teach_run.
    _graspTeachReleaseMjpegIfPaused();
    var pausedMjpeg = _graspTeachPauseMjpegForFetch();
    window.__graspTeachMjpegPaused = pausedMjpeg;
    _graspTeachStartPolling(
      { running: true, label_it: "Invio comando…", progress_pct: 2, log_lines: [] },
      { untilRunning: true }
    );
    var teachRunCtrl = typeof AbortController !== "undefined" ? new AbortController() : null;
    var teachRunT0 = typeof performance !== "undefined" ? performance.now() : Date.now();
    var teachRunMjpegCount = 0;
    try {
      teachRunMjpegCount = document.querySelectorAll('img[src*=".mjpg"]').length;
    } catch (eMj) {
      teachRunMjpegCount = -1;
    }
    // #region agent log
    fetch("http://127.0.0.1:7648/ingest/1f6b2724-6bbf-4c6c-a795-45910cf4b1c4", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Debug-Session-Id": "16a61f" },
      body: JSON.stringify({
        sessionId: "16a61f",
        runId: "post-fix",
        hypothesisId: "H1",
        location: "operators_grasp_coach.js:teach_run_fetch_start",
        message: "teach_run fetch start",
        data: { mjpeg_img_count: teachRunMjpegCount, paused_mjpeg: pausedMjpeg.length },
        timestamp: Date.now(),
      }),
    }).catch(function () {});
    // #endregion
    var teachRunTimer = setTimeout(function () {
      if (teachRunCtrl) {
        teachRunCtrl.abort();
      }
    }, 12000);
    fetch(window.operatorsApi("/api/grasp/teach_run"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: "RUN_TEACH_GRASP", instruction: instruction }),
      signal: teachRunCtrl ? teachRunCtrl.signal : undefined,
    })
      .then(function (r) {
        return r.json().then(function (j) {
          return { j: j, r: r, httpOk: r.ok, httpStatus: r.status };
        });
      })
      .then(function (pack) {
        clearTimeout(teachRunTimer);
        // #region agent log
        fetch("http://127.0.0.1:7648/ingest/1f6b2724-6bbf-4c6c-a795-45910cf4b1c4", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Debug-Session-Id": "16a61f" },
          body: JSON.stringify({
            sessionId: "16a61f",
            runId: "post-fix",
            hypothesisId: "H3",
            location: "operators_grasp_coach.js:teach_run_fetch_ok",
            message: "teach_run fetch ok",
            data: {
              client_ms: Math.round((typeof performance !== "undefined" ? performance.now() : Date.now()) - teachRunT0),
              http_status: pack.httpStatus,
              started: !!(pack.j && pack.j.started),
              reason: pack.j && pack.j.reason,
            },
            timestamp: Date.now(),
          }),
        }).catch(function () {});
        // #endregion
        var j = pack.j || {};
        if (!j.started) {
          if (j.reason === "job_already_running" && j.status) {
            var stAttach = j.status;
            window.__graspTeachLogCount = 0;
            graspTeachUpdateFromStatus(stAttach);
            if (logEl) {
              var attachBanner = document.createElement("div");
              attachBanner.className = "log-warn";
              attachBanner.textContent =
                "⚠ Flusso già in corso sulla NX — aggancio live (step: " +
                (stAttach.current_step || "?") +
                (stAttach.label_it ? " · " + stAttach.label_it : "") +
                ").";
              logEl.insertBefore(attachBanner, logEl.firstChild);
            }
            _graspTeachStartPolling(stAttach);
            return;
          }
          _graspTeachReleaseMjpegIfPaused();
          graspTeachResetStoryboard(false);
          if (btn) {
            btn.disabled = false;
          }
          var failMsg =
            j.hint_it ||
            j.reason ||
            (pack.httpOk ? "Avvio fallito." : "HTTP " + pack.httpStatus);
          graspTeachRenderProgress({
            progress_pct: 0,
            label_it: failMsg,
            running: false,
            ok: false,
          });
          if (logEl) {
            logEl.innerHTML = '<div class="log-error">✗ ' + failMsg + "</div>";
          }
          if (j.status) {
            graspTeachUpdateFromStatus(j.status);
          }
          return;
        }
        if (j.status) {
          graspTeachUpdateFromStatus(j.status);
        }
      })
      .catch(function (e) {
        clearTimeout(teachRunTimer);
        // #region agent log
        fetch("http://127.0.0.1:7648/ingest/1f6b2724-6bbf-4c6c-a795-45910cf4b1c4", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Debug-Session-Id": "16a61f" },
          body: JSON.stringify({
            sessionId: "16a61f",
            runId: "post-fix",
            hypothesisId: String(e && e.name) === "AbortError" ? "H1" : "H3",
            location: "operators_grasp_coach.js:teach_run_fetch_fail",
            message: "teach_run fetch fail",
            data: {
              client_ms: Math.round((typeof performance !== "undefined" ? performance.now() : Date.now()) - teachRunT0),
              error_name: e && e.name,
              error_msg: String(e),
              mjpeg_img_count: teachRunMjpegCount,
            },
            timestamp: Date.now(),
          }),
        }).catch(function () {});
        // #endregion
        if (String(e && e.name) === "AbortError") {
          window.__graspTeachAwaitStartSince = Date.now();
          if (logEl) {
            logEl.innerHTML =
              '<div class="log-warn">⚠ teach_run in attesa rete — aggancio al polling (stream MJPEG in pausa)…</div>';
          }
          return;
        }
        _graspTeachReleaseMjpegIfPaused();
        window.__graspTeachPollMode = "normal";
        graspTeachResetStoryboard(false);
        if (btn) {
          btn.disabled = false;
        }
        if (logEl) {
          logEl.innerHTML = '<div class="log-error">✗ ' + String(e) + "</div>";
        }
      });
  };

  function _graspCoachUpdateGateFromMetric(j) {
    var mg = j && j.metric_grounding;
    var elD = document.getElementById("graspCoachGateDepth");
    var elDet = document.getElementById("graspCoachGateDetect");
    var elR = document.getElementById("graspCoachGateReach");
    var elC = document.getElementById("graspCoachGateCalib");
    if (elD) {
      elD.textContent =
        "depth: " + (mg && mg.depth_m != null ? Number(mg.depth_m).toFixed(3) + " m" : "—");
    }
    if (elDet) {
      elDet.textContent =
        "detect: " + (mg && mg.ok ? (mg.label || "OK") : j && j.reason ? String(j.reason) : "—");
    }
    if (elR) {
      elR.textContent =
        "reach: " +
        (mg && mg.reachable != null ? (mg.reachable ? "OK" : "NO") : "—");
    }
    if (elC) {
      var parts = [];
      if (j && j.teach_calib_applied) {
        parts.push("teach");
      }
      if (j && j.online_calib_applied) {
        parts.push("online");
      }
      elC.textContent = "calib: " + (parts.length ? parts.join("+") : "—");
    }
  }

  window.operatorsGraspCoachScanJ90 = function () {
    var pre = document.getElementById("graspCoachPre");
    var btn = document.getElementById("graspCoachScanJ90Btn");
    if (
      !window.confirm(
        "Andare a «Scansione +90°» (waypoint dal programma salvato)?"
      )
    ) {
      return;
    }
    if (btn) {
      btn.disabled = true;
    }
    if (pre) {
      pre.textContent = "POST /api/presets/scan/goto { variant: j90 } …";
    }
    fetch(window.operatorsApi("/api/presets/scan/goto"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ variant: "j90" }),
    })
      .then(function (r) {
        return r.json().then(function (j) {
          return { j: j, r: r };
        });
      })
      .then(function (pack) {
        if (pre) {
          pre.textContent = JSON.stringify(pack.j, null, 2);
        }
      })
      .catch(function (e) {
        if (pre) {
          pre.textContent = String(e);
        }
      })
      .finally(function () {
        if (btn) {
          btn.disabled = false;
        }
      });
  };

  function _graspPollJob(url, pre, btn, label) {
    var poll = setInterval(function () {
      fetch(window.operatorsApi(url))
        .then(function (r) {
          return r.json();
        })
        .then(function (st) {
          if (pre) {
            pre.textContent = JSON.stringify(st, null, 2);
          }
          if (st && st.running === false) {
            clearInterval(poll);
            if (btn) {
              btn.disabled = false;
            }
          }
        })
        .catch(function () {
          /* ignore transient */
        });
    }, 700);
    if (pre) {
      pre.textContent = label + " (polling…) ";
    }
  }

  window.operatorsGraspAutonomousRun = function () {
    var pre = document.getElementById("graspCoachPre");
    var btn = document.getElementById("graspCoachAutonomousBtn");
    var instrEl = document.getElementById("graspCoachInstruction");
    if (
      !window.confirm(
        "PRESA AUTONOMA (stream): loop Orbbec + IK + verify pinza. Procedere?"
      )
    ) {
      return;
    }
    var body = {
      confirm: "RUN_AUTONOMOUS_GRASP",
      instruction:
        instrEl && instrEl.value
          ? String(instrEl.value).trim()
          : "prendi la scatola",
    };
    if (btn) {
      btn.disabled = true;
    }
    fetch(window.operatorsApi("/api/grasp/autonomous_run"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(function (r) {
        return r.json().then(function (j) {
          return { j: j };
        });
      })
      .then(function (pack) {
        if (pre) {
          pre.textContent = JSON.stringify(pack.j, null, 2);
        }
        if (pack.j && pack.j.started === true) {
          _graspPollJob("/api/grasp/autonomous_status", pre, btn, "Presa autonoma");
        } else if (btn) {
          btn.disabled = false;
        }
      })
      .catch(function (e) {
        if (pre) {
          pre.textContent = String(e);
        }
        if (btn) {
          btn.disabled = false;
        }
      });
  };

  window.operatorsGraspCollectRun = function () {
    var pre = document.getElementById("graspCoachPre");
    var btn = document.getElementById("graspCollectBtn");
    var instrEl = document.getElementById("graspCoachInstruction");
    if (
      !window.confirm(
        "RACCOLTA AUTONOMA: cerca scatole colorate, prende e deposita. Procedere?"
      )
    ) {
      return;
    }
    var body = {
      confirm: "RUN_COLLECT_MISSION",
      instruction:
        instrEl && instrEl.value
          ? String(instrEl.value).trim()
          : "raccogli le scatole blu",
      max_picks: 3,
    };
    if (btn) {
      btn.disabled = true;
    }
    fetch(window.operatorsApi("/api/grasp/collect"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(function (r) {
        return r.json().then(function (j) {
          return { j: j };
        });
      })
      .then(function (pack) {
        if (pre) {
          pre.textContent = JSON.stringify(pack.j, null, 2);
        }
        if (pack.j && pack.j.started === true) {
          _graspPollJob("/api/grasp/collect_status", pre, btn, "Raccolta");
        } else if (btn) {
          btn.disabled = false;
        }
      })
      .catch(function (e) {
        if (pre) {
          pre.textContent = String(e);
        }
        if (btn) {
          btn.disabled = false;
        }
      });
  };

  var _origRenderMetric = _graspCoachRenderMetricPanel;
  _graspCoachRenderMetricPanel = function (j) {
    _origRenderMetric(j);
    try {
      _graspCoachUpdateGateFromMetric(j);
    } catch (eG) {
      /* best-effort */
    }
  };

  _graspTeachCalibPollOnce();
  _graspTeachUpdateWizardState();
})();
