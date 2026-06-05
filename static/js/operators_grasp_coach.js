(function () {
  "use strict";

  window.__graspCoachStepBusy = false;
  window.__graspCoachProgressTimer = null;
  window.__graspCoachLastResponse = null;

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

  function graspCoachProgressDone(ok, timingsMs, errHint) {
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
    var lab = "";
    if (ok) {
      lab =
        ms != null
          ? "Interiorizzato — risposta pronta (~" + ms + " ms rete OpenAI). Puoi dare feedback sotto."
          : "Interiorizzato — risposta pronta. Puoi dare feedback sotto.";
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
    var ta = document.getElementById("graspCoachInstruction");
    var sess = document.getElementById("graspCoachSession");
    var execEl = document.getElementById("graspCoachExecute");
    var depthEl = document.getElementById("graspCoachDepth");
    var camEl = document.getElementById("graspCoachCam");
    var blendEl = document.getElementById("graspCoachBlend");
    var instr = ta && ta.value ? String(ta.value).trim() : "";
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
    if (j && j.ok && mg.ok !== false) {
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
      elSt.textContent = j.label_it || "Acquisizione OK — target metrico pronto";
      elSt.style.color = "#30d070";
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
      var hint = mg.hint_it || "";
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
      parts.push(visible === false ? "Oggetto: NON visto" : "Oggetto: visto");
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
        drawMarker();
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
          graspCoachProgressDone(ok, j && j.timings_ms, hint);
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
        graspCoachProgressDone(false, null, String(e));
      })
      .then(function () {
        window.__graspCoachStepBusy = false;
        if (stepBtn) {
          stepBtn.disabled = false;
        }
      });
  };

  function _graspCoachInstructionText() {
    var instrEl = document.getElementById("graspCoachInstruction");
    var t = instrEl && instrEl.value ? String(instrEl.value).trim() : "";
    return t || "prendi l'oggetto";
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
      pre.textContent = "POST /api/grasp_coach/preview … Orbbec polso log.0 (RGB+depth, nessun movimento)";
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
})();
