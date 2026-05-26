/**
 * Console Agent (Hermes): chat, contesto missione, permessi, TTS.
 * Dipende da ``operators.js`` (``window.operatorsApi``). Caricare dopo ``operators.js``.
 */
(function () {
  "use strict";

  function hermesParseJsonResponse(txt) {
    try {
      return txt ? JSON.parse(txt) : {};
    } catch (e) {
      return { ok: false, reason: "non_json", detail: String(txt || "").slice(0, 500) };
    }
  }

  function hermesPickAssistantReply(j) {
    if (!j || typeof j !== "object") {
      return "";
    }
    var keys = ["assistant_reply_it", "assistant_reply_en", "assistant_reply", "message", "reply", "answer"];
    function innerPick(obj) {
      if (!obj || typeof obj !== "object") {
        return "";
      }
      var i;
      var k;
      var v;
      var sub;
      for (i = 0; i < keys.length; i++) {
        k = keys[i];
        v = obj[k];
        if (typeof v === "string" && v.trim()) {
          return v.trim();
        }
        if (v && typeof v === "object") {
          sub = v.text || v.message || v.content;
          if (typeof sub === "string" && sub.trim()) {
            return sub.trim();
          }
        }
      }
      return "";
    }
    var r = innerPick(j);
    return r || innerPick(j.intent) || innerPick(j.intent_llm);
  }

  function api(path) {
    if (typeof window.operatorsApi === "function") {
      return window.operatorsApi(path);
    }
    var SR = window.__OPERATORS_SCRIPT_ROOT__ || "";
    if (!path) {
      path = "/";
    }
    if (path.charAt(0) !== "/") {
      path = "/" + path;
    }
    return SR + path;
  }

  window.__operatorsHermesRec = null;
  window.__operatorsHermesPending = null;
  window.__operatorsHermesLastUserCommand = "";

  window.operatorsHermesClearChat = function () {
    var log = document.getElementById("hermesChatLog");
    if (log) {
      log.innerHTML = "";
    }
    window.__operatorsHermesPending = null;
    window.operatorsHermesHideApprovePanel();
  };

  window.operatorsHermesHideApprovePanel = function () {
    var panel = document.getElementById("hermesApprovePanel");
    if (panel) {
      panel.style.display = "none";
      panel.classList.remove("hermes-approve-panel--visible");
      panel.setAttribute("aria-hidden", "true");
    }
  };

  window.operatorsHermesShowApprovePanel = function (digestText) {
    var panel = document.getElementById("hermesApprovePanel");
    var digest = document.getElementById("hermesApproveDigest");
    if (digest) {
      digest.textContent = digestText || "—";
    }
    if (panel) {
      panel.style.display = "block";
      panel.classList.add("hermes-approve-panel--visible");
      panel.setAttribute("aria-hidden", "false");
    }
  };

  function hermesApplyHermesHttpResponse(o, ctx) {
    var j = o.j || {};
    var pre = ctx.pre;
    var ta = ctx.ta;
    var ttsOi = ctx.ttsOi;
    var tHermes0 = ctx.tHermes0;
    var clearTa = ctx.clearTa !== false;
    var rep = hermesPickAssistantReply(j);
    if (!rep && j.warnings_it && j.warnings_it.length) {
      rep = j.warnings_it
        .map(function (w) {
          return String(w);
        })
        .join(" ")
        .trim();
    }
    if (!rep && j.reason) {
      rep =
        "Error: " +
        String(j.reason) +
        (j.detail ? " — " + String(j.detail).slice(0, 400) : "") +
        (j.hint_it ? " · " + String(j.hint_it).slice(0, 220) : "");
    }
    if (!rep && !o.okHttp) {
      rep = "HTTP " + o.status;
    }
    if (!rep) {
      rep = "(No reply — check Technical JSON)";
    }

    var slim = {
      ok: j.ok,
      assistant_reply_it: j.assistant_reply_it,
      warnings_it: j.warnings_it,
      dry_run: j.dry_run,
      intent: j.intent,
      steps: j.steps,
      preview_only: j.preview_only,
      action_digest_it: j.action_digest_it,
      approved_execute: j.approved_execute,
    };
    var technical =
      !o.okHttp || j.ok === false || j.reason ? JSON.stringify(j, null, 2) : JSON.stringify(slim, null, 2);

    var timingFoot =
      typeof window.operatorsHttpTimingFooterLines === "function"
        ? window.operatorsHttpTimingFooterLines(o.resp, tHermes0)
        : "";

    if (pre) {
      pre.textContent = technical + "\nHTTP " + o.status + timingFoot;
    }

    var suppressAudio = !!(j && j.tts_suppress_client_audio);
    var hasCloudMp3 = !!(j && j.tts_audio_mp3_base64);

    var speak = "";
    try {
      if (j.warnings_it && j.warnings_it.length) {
        speak = j.warnings_it.join(". ") + ". ";
      }
      speak += hermesPickAssistantReply(j) || rep;
    } catch (e2) {
      speak = rep;
    }
    hermesAppendLine("bot", rep);

    if (!suppressAudio && !hasCloudMp3) {
      window.operatorsHermesSpeakBrowser(speak);
    }

    if (!suppressAudio && hasCloudMp3) {
      operatorsHermesPlayMp3Base64(j.tts_audio_mp3_base64);
    } else if (ttsOi && j && j.tts_openai_error && pre) {
      pre.textContent = (pre.textContent || "") + "\n[TTS cloud] " + j.tts_openai_error;
    }

    if (j && j.tts_playback_hint_it && pre) {
      pre.textContent = (pre.textContent || "") + "\n[TTS playback] " + String(j.tts_playback_hint_it);
    }

    if (clearTa && ta) {
      ta.value = "";
    }
    return j;
  }

  window.operatorsHermesClearMission = function () {
    var b = document.getElementById("hermesMissionBrief");
    if (b) {
      b.value = "";
    }
  };

  window.operatorsHermesOnPersonalityChange = function () {
    var p = document.getElementById("hermesPersonalitySelect");
    var v = document.getElementById("hermesOpenAiVoiceSelect");
    if (!p || !v) {
      return;
    }
    if (p.value === "bender_meeting" && (!v.value || v.value === "")) {
      v.value = "echo";
    }
  };

  function hermesAppendLine(role, text) {
    var log = document.getElementById("hermesChatLog");
    if (!log || text == null) {
      return;
    }
    var div = document.createElement("div");
    div.className = "agent-chat-msg agent-chat-msg--" + (role === "you" || role === "bot" || role === "sys" ? role : "sys");
    div.setAttribute("role", "article");
    if (role === "you") {
      div.innerHTML = '<span class="agent-chat-who">Tu</span><span class="agent-chat-body">' + escapeHtml(String(text)) + "</span>";
    } else if (role === "bot") {
      div.innerHTML =
        '<span class="agent-chat-who">Hermes</span><span class="agent-chat-body">' + escapeHtml(String(text)) + "</span>";
    } else {
      div.textContent = String(text);
    }
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  function escapeHtml(s) {
    return s
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function hermesSetLoading(on) {
    var w = document.getElementById("hermesLoadingWrap");
    var btn = document.querySelector(".agent-btn-send");
    var approveBtn = document.getElementById("hermesApproveBtn");
    if (w) {
      w.style.display = on ? "flex" : "none";
      w.setAttribute("aria-busy", on ? "true" : "false");
    }
    if (btn) {
      btn.disabled = !!on;
    }
    if (approveBtn) {
      approveBtn.disabled = !!on;
    }
  }

  window.operatorsHermesSaveMemoryNote = function () {
    var ta = document.getElementById("hermesMemorySticky");
    var note = ta && ta.value ? String(ta.value).trim() : "";
    if (!note) {
      hermesAppendLine("sys", "Memoria: testo vuoto.");
      return;
    }
    fetch(api("/api/operator_session/memory"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: "Hermes feedback",
        note: note,
        tags: ["hermes", "operator_feedback"],
      }),
    })
      .then(function (r) {
        return r.text().then(function (txt) {
          return { status: r.status, okHttp: r.ok, j: hermesParseJsonResponse(txt) };
        });
      })
      .then(function (o) {
        if (o.okHttp && o.j && o.j.ok) {
          hermesAppendLine(
            "sys",
            "Memoria salvata (tag hermes). Sarà inclusa nelle prossime richieste se «Includi memoria» è attivo."
          );
          if (ta) {
            ta.value = "";
          }
        } else {
          hermesAppendLine("sys", "Memoria: errore HTTP " + o.status + " — " + JSON.stringify(o.j || {}).slice(0, 400));
        }
      })
      .catch(function (e) {
        hermesAppendLine("sys", "Memoria: " + String(e));
      });
  };

  function operatorsHermesBuildCapabilities() {
    var ex = document.getElementById("hermesCapExplicit");
    if (!ex || !ex.checked) {
      return null;
    }
    return {
      lab_fragile_payload: !!(document.getElementById("hermesFragile") && document.getElementById("hermesFragile").checked),
      allow_base_motion: !!(document.getElementById("hermesAllowBase") && document.getElementById("hermesAllowBase").checked),
      allow_base_stand_crouch:
        !!(document.getElementById("hermesAllowStandCrouch") && document.getElementById("hermesAllowStandCrouch").checked),
      allow_base_joystick:
        !!(document.getElementById("hermesAllowJoystick") && document.getElementById("hermesAllowJoystick").checked),
      allow_base_velocity:
        !!(document.getElementById("hermesAllowVelocity") && document.getElementById("hermesAllowVelocity").checked),
      allow_base_damping:
        !!(document.getElementById("hermesAllowDamping") && document.getElementById("hermesAllowDamping").checked),
      allow_arm_presets: !!(document.getElementById("hermesAllowArmPre") && document.getElementById("hermesAllowArmPre").checked),
      allow_arm_joint_delta: !!(function () {
        var jog = document.getElementById("hermesAllowArmJog");
        if (jog) {
          return !!jog.checked;
        }
        return !!(document.getElementById("hermesAllowArmPre") && document.getElementById("hermesAllowArmPre").checked);
      })(),
      allow_arm_tool_target: !!(function () {
        var ik = document.getElementById("hermesAllowArmVisionIk");
        if (ik) {
          return !!ik.checked;
        }
        return !!(document.getElementById("hermesAllowArmPre") && document.getElementById("hermesAllowArmPre").checked);
      })(),
    };
  }

  window.operatorsHermesStopSpeech = function () {
    try {
      window.speechSynthesis.cancel();
    } catch (e) {}
    if (window.__operatorsHermesAudio) {
      try {
        window.__operatorsHermesAudio.pause();
        window.__operatorsHermesAudio.src = "";
      } catch (e2) {}
      window.__operatorsHermesAudio = null;
    }
  };

  window.operatorsHermesSpeakBrowser = function (text) {
    if (!text || typeof text !== "string") {
      return;
    }
    var chk = document.getElementById("hermesTtsBrowser");
    if (chk && !chk.checked) {
      return;
    }
    window.operatorsHermesStopSpeech();
    try {
      var u = new SpeechSynthesisUtterance(text);
      u.lang = "en-US";
      u.rate = 1;
      var persEl = document.getElementById("hermesPersonalitySelect");
      if (persEl && persEl.value === "bender_meeting") {
        u.rate = 0.93;
        try {
          u.pitch = 0.78;
        } catch (e1) {}
      }
      window.speechSynthesis.speak(u);
    } catch (e) {}
  };

  function operatorsHermesPlayMp3Base64(b64) {
    try {
      var binStr = atob(b64);
      var n = binStr.length;
      var bytes = new Uint8Array(n);
      for (var i = 0; i < n; i++) {
        bytes[i] = binStr.charCodeAt(i);
      }
      window.operatorsHermesStopSpeech();
      var blob = new Blob([bytes], { type: "audio/mpeg" });
      var url = URL.createObjectURL(blob);
      var a = new Audio(url);
      window.__operatorsHermesAudio = a;
      a.play().finally(function () {
        try {
          URL.revokeObjectURL(url);
        } catch (e) {}
      });
    } catch (e) {
      console.warn("Agent TTS MP3", e);
    }
  }

  function operatorsHermesRefreshIntegrationBadge() {
    var el = document.getElementById("hermesIntegrationBadge");
    if (!el) {
      return;
    }
    fetch(api("/api/hermes/status?_=" + Date.now()), { cache: "no-store" })
      .then(function (r) {
        return r.json().then(function (j) {
          return { okHttp: r.ok, j: j || {} };
        });
      })
      .then(function (o) {
        var j = o.j || {};
        var parts = [];
        if (j.GO2_ENABLE_HERMES_AGENT && j.has_openai_api_key) {
          parts.push("Hermes LLM");
        } else {
          parts.push("Hermes off");
        }
        if (j.go2_local) {
          parts.push("NX+DDS");
        }
        if ((j.go2_enable_base_motion_env || "").trim() === "1") {
          parts.push("Sport");
        }
        if (j.turn_memory_logging_default) {
          parts.push("mem turni");
        }
        var te = j.tts_env || {};
        if (te.GO2_HERMES_PLAY_ON_GO2_WEBRTC && !te.unitree_webrtc_connect_import_ok) {
          parts.push("⚠ webrtc pip?");
        } else if (te.GO2_HERMES_PLAY_ON_GO2_WEBRTC && !te.GO2_WEBRTC_IP_configured) {
          parts.push("⚠ GO2_WEBRTC_IP?");
        }
        el.textContent = parts.join(" · ");
      })
      .catch(function () {
        el.textContent = "stato ?";
      });
  }

  window.operatorsHermesRefreshIntegrationBadge = operatorsHermesRefreshIntegrationBadge;

  window.operatorsHermesMicToggle = function () {
    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    var hint = document.getElementById("hermesMicStatus");
    if (!SR) {
      if (hint) {
        hint.style.display = "block";
        hint.textContent =
          "Speech recognition not supported in this browser. Try Chrome on HTTPS or localhost.";
      }
      return;
    }
    if (window.__operatorsHermesRec) {
      try {
        window.__operatorsHermesRec.stop();
      } catch (e) {}
      window.__operatorsHermesRec = null;
      if (hint) {
        hint.style.display = "none";
      }
      return;
    }
    var rec = new SR();
    rec.lang = "en-US";
    rec.interimResults = false;
    rec.maxAlternatives = 1;
    if (hint) {
      hint.style.display = "block";
      hint.textContent = "Listening… speak now.";
    }
    rec.onerror = function (ev) {
      if (hint) {
        hint.textContent = "Mic error: " + String(ev.error || "?");
      }
    };
    rec.onend = function () {
      window.__operatorsHermesRec = null;
      if (hint) {
        hint.style.display = "none";
      }
    };
    rec.onresult = function (event) {
      var t = "";
      try {
        t = event.results[0][0].transcript;
      } catch (e) {}
      var ta = document.getElementById("hermesCommandInput");
      if (ta && t) {
        ta.value = (ta.value ? ta.value.trim() + " " : "") + t.trim();
      }
    };
    window.__operatorsHermesRec = rec;
    try {
      rec.start();
    } catch (e) {
      if (hint) {
        hint.textContent = "Mic: failed to start (" + String(e) + ").";
      }
    }
  };

  window.operatorsHermesStatus = function () {
    var pre = document.getElementById("hermesReplyPre");
    fetch(api("/api/hermes/status?_=" + Date.now()), { cache: "no-store" })
      .then(function (r) {
        return r.text().then(function (txt) {
          return { status: r.status, okHttp: r.ok, j: hermesParseJsonResponse(txt) };
        });
      })
      .then(function (o) {
        var txt = JSON.stringify(o.j, null, 2) + "\nHTTP " + o.status;
        if (pre) {
          pre.textContent = txt;
        }
        hermesAppendLine("sys", "API status in Technical JSON.");
      })
      .catch(function (e) {
        if (pre) {
          pre.textContent = "Error: " + String(e);
        }
      });
  };

  window.operatorsHermesSend = function () {
    var ta = document.getElementById("hermesCommandInput");
    var dryEl = document.getElementById("hermesDryRun");
    var pre = document.getElementById("hermesReplyPre");
    var text = ta && ta.value ? String(ta.value).trim() : "";
    if (!text) {
      if (pre) {
        pre.textContent = "Hermes: empty message.";
      }
      return;
    }
    window.__operatorsHermesLastUserCommand = text;
    var caps = operatorsHermesBuildCapabilities();
    var dry = !!(dryEl && dryEl.checked);
    var ttsOi = !!(document.getElementById("hermesTtsOpenAi") && document.getElementById("hermesTtsOpenAi").checked);
    hermesAppendLine("you", text);
    if (pre) {
      pre.textContent = "POST /api/hermes/command…";
    }
    hermesSetLoading(true);

    var body = { text: text, dry_run: dry };
    var modeEl = document.querySelector('input[name="hermesExecMode"]:checked');
    body.execution_mode = modeEl && modeEl.value === "preview" ? "preview" : "run";

    var briefEl = document.getElementById("hermesMissionBrief");
    var brief = briefEl && briefEl.value ? String(briefEl.value).trim() : "";
    if (brief) {
      body.mission_context = brief;
    }
    var incMem = document.getElementById("hermesIncludeMemory");
    body.include_operator_memory = !!(incMem && incMem.checked);
    var logTurnEl = document.getElementById("hermesLogTurns");
    body.log_turn_to_memory = !!(logTurnEl && logTurnEl.checked);
    var persEl = document.getElementById("hermesPersonalitySelect");
    var pers = persEl && persEl.value ? String(persEl.value).trim() : "";
    if (pers) {
      body.personality = pers;
    }
    var voiceEl = document.getElementById("hermesOpenAiVoiceSelect");
    var voicePick = voiceEl && voiceEl.value ? String(voiceEl.value).trim() : "";
    if (voicePick && ttsOi) {
      body.tts_voice = voicePick;
    }
    if (caps) {
      body.capabilities = caps;
    }
    if (ttsOi) {
      body.tts_openai = true;
    }
    var attachEl = document.getElementById("hermesAttachCamera");
    if (attachEl && attachEl.checked) {
      body.attach_camera = true;
    }

    var tHermes0 = performance.now();
    var ctrl = new AbortController();
    var ms = 270000;
    var tid = setTimeout(function () {
      try {
        ctrl.abort();
      } catch (eA) {}
    }, ms);
    fetch(api("/api/hermes/command"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    })
      .then(function (r) {
        return r.text().then(function (txt) {
          return {
            status: r.status,
            okHttp: r.ok,
            j: hermesParseJsonResponse(txt),
            resp: r,
          };
        });
      })
      .then(function (o) {
        var j = hermesApplyHermesHttpResponse(o, {
          pre: pre,
          ta: ta,
          ttsOi: ttsOi,
          tHermes0: tHermes0,
          clearTa: true,
        });
        if (j.preview_only && j.intent) {
          window.__operatorsHermesPending = { intent: j.intent };
          window.operatorsHermesShowApprovePanel(j.action_digest_it || "");
          hermesAppendLine("sys", "Anteprima: motori fermi. Controlla il riquadro e approva.");
        } else {
          window.__operatorsHermesPending = null;
          window.operatorsHermesHideApprovePanel();
        }
      })
      .catch(function (e) {
        var msg = String(e);
        if (e && e.name === "AbortError") {
          msg = "Timeout dopo " + Math.round(ms / 1000) + "s (Hermes+TTS può essere lento). Ritenta o riduci attach_camera/TTS.";
        }
        if (pre) {
          pre.textContent = "Network error: " + msg;
        }
        hermesAppendLine("sys", "Network error: " + msg);
      })
      .finally(function () {
        clearTimeout(tid);
        hermesSetLoading(false);
        if (typeof window.operatorsHermesRefreshIntegrationBadge === "function") {
          window.operatorsHermesRefreshIntegrationBadge();
        }
      });
  };

  window.operatorsHermesApprovePending = function () {
    var pending = window.__operatorsHermesPending;
    var pre = document.getElementById("hermesReplyPre");
    var dryEl = document.getElementById("hermesDryRun");
    if (!pending || !pending.intent) {
      hermesAppendLine("sys", "Niente in attesa di approvazione.");
      return;
    }
    var caps = operatorsHermesBuildCapabilities();
    var body = {
      intent: pending.intent,
      dry_run: !!(dryEl && dryEl.checked),
      source_command: window.__operatorsHermesLastUserCommand || "",
      log_turn_to_memory: !!(document.getElementById("hermesLogTurns") && document.getElementById("hermesLogTurns").checked),
    };
    if (caps) {
      body.capabilities = caps;
    }
    if (pre) {
      pre.textContent = "POST /api/hermes/execute_intent…";
    }
    hermesSetLoading(true);
    var t0 = performance.now();
    var ctrl = new AbortController();
    var tid = setTimeout(function () {
      try {
        ctrl.abort();
      } catch (e1) {}
    }, 120000);
    fetch(api("/api/hermes/execute_intent"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    })
      .then(function (r) {
        return r.text().then(function (txt) {
          return { status: r.status, okHttp: r.ok, j: hermesParseJsonResponse(txt), resp: r };
        });
      })
      .then(function (o) {
        hermesApplyHermesHttpResponse(o, {
          pre: pre,
          ta: null,
          ttsOi: false,
          tHermes0: t0,
          clearTa: false,
        });
        window.__operatorsHermesPending = null;
        window.operatorsHermesHideApprovePanel();
        hermesAppendLine("sys", "Approvazione applicata sul robot (vedi Technical JSON).");
      })
      .catch(function (e) {
        var msg = String(e);
        if (pre) {
          pre.textContent = "execute_intent error: " + msg;
        }
        hermesAppendLine("sys", "Esecuzione approvata fallita: " + msg);
      })
      .finally(function () {
        clearTimeout(tid);
        hermesSetLoading(false);
      });
  };

  window.operatorsHermesDiscardPending = function () {
    window.__operatorsHermesPending = null;
    window.operatorsHermesHideApprovePanel();
    hermesAppendLine("sys", "Anteprima annullata.");
  };

  if (typeof document !== "undefined") {
    document.addEventListener("DOMContentLoaded", function () {
      try {
        var savedMode = localStorage.getItem("hermesExecMode");
        if (savedMode === "preview" || savedMode === "run") {
          var sel = document.querySelector('input[name="hermesExecMode"][value="' + savedMode + '"]');
          if (sel) {
            sel.checked = true;
          }
        }
      } catch (e0) {}
      var modeInputs = document.querySelectorAll('input[name="hermesExecMode"]');
      var mi;
      for (mi = 0; mi < modeInputs.length; mi++) {
        modeInputs[mi].addEventListener("change", function (ev) {
          try {
            var t = ev.target;
            if (t && t.value) {
              localStorage.setItem("hermesExecMode", t.value);
            }
          } catch (e1) {}
        });
      }
      if (typeof window.operatorsHermesOnPersonalityChange === "function") {
        window.operatorsHermesOnPersonalityChange();
      }
      if (typeof window.operatorsHermesRefreshIntegrationBadge === "function") {
        window.operatorsHermesRefreshIntegrationBadge();
      }
    });
  }
})();
