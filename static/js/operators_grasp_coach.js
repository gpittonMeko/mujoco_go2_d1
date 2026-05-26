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

  window.operatorsGraspCoachStep = function () {
    var pre = document.getElementById("graspCoachPre");
    var ta = document.getElementById("graspCoachInstruction");
    var sess = document.getElementById("graspCoachSession");
    var execEl = document.getElementById("graspCoachExecute");
    var depthEl = document.getElementById("graspCoachDepth");
    var camEl = document.getElementById("graspCoachCam");
    var stepBtn = document.getElementById("graspCoachStepBtn");
    var instr = ta && ta.value ? String(ta.value).trim() : "";
    if (window.__graspCoachStepBusy) {
      return;
    }
    if (!instr) {
      if (pre) {
        pre.textContent = "Missing instruction text.";
      }
      return;
    }
    if (typeof window.__graspCoachStepIndex !== "number" || window.__graspCoachStepIndex < 0) {
      window.__graspCoachStepIndex = 0;
    }
    var body = {
      instruction: instr,
      execute: !!(execEl && execEl.checked),
      include_depth: !!(depthEl && depthEl.checked),
      logical_camera_rgb: camEl && camEl.value === "6" ? 6 : 0,
      step_index: window.__graspCoachStepIndex,
    };
    window.__graspCoachStepIndex += 1;
    if (sess && sess.value && String(sess.value).trim()) {
      body.session_note = String(sess.value).trim();
    }
    window.__graspCoachStepBusy = true;
    if (stepBtn) {
      stepBtn.disabled = true;
    }
    graspCoachProgressStart();
    if (pre) {
      pre.textContent = "…";
    }
    var tStep0 = typeof performance !== "undefined" ? performance.now() : 0;
    fetch(window.operatorsApi("/api/grasp_coach/step"), {
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
        var ok = !!(j && j.ok);
        var hint =
          j && j.reason
            ? String(j.reason)
            : !pack.httpOk
              ? "HTTP " + String(pack.httpStatus)
              : "";
        graspCoachProgressDone(ok, j && j.timings_ms, hint);
      })
      .catch(function (e) {
        window.__graspCoachLastResponse = null;
        if (pre) {
          pre.textContent = String(e);
        }
        graspCoachProgressDone(false, null, String(e));
      })
      .finally(function () {
        window.__graspCoachStepBusy = false;
        if (stepBtn) {
          stepBtn.disabled = false;
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
})();
