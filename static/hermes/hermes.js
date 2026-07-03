(function () {
  "use strict";

  var history = [];

  function addMsg(role, text) {
    var log = document.getElementById("chatLog");
    if (!log) return;
    var p = document.createElement("p");
    p.className = "hermes-msg " + (role === "user" ? "hermes-msg-user" : "hermes-msg-bot");
    var who = document.createElement("b");
    who.textContent = role === "user" ? "Tu scrivi:" : "Hermes scrive:";
    p.appendChild(who);
    p.appendChild(document.createTextNode(text));
    log.appendChild(p);
    log.scrollTop = log.scrollHeight;
  }

  function setStatus(text) {
    var el = document.getElementById("hermesStatus");
    if (el) el.textContent = text;
  }

  function send() {
    var input = document.getElementById("chatInput");
    if (!input) return;
    var msg = (input.value || "").trim();
    if (!msg) return;
    input.value = "";
    addMsg("user", msg);
    history.push({ role: "user", content: msg });
    var pending = document.createElement("p");
    pending.className = "hermes-msg hermes-msg-bot";
    pending.textContent = "…";
    setStatus("elaboro…");
    document.getElementById("chatLog").appendChild(pending);

    fetch("/api/hermes/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: msg, history: history, speak: true }),
    })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        pending.remove();
        var reply = j.reply || j.reason || "Errore";
        addMsg("bot", reply);
        history.push({ role: "assistant", content: reply });
        var lat = j.latency_s != null ? " · " + j.latency_s + "s" : "";
        var backend = j.backend ? " · " + j.backend : "";
        var act = j.action ? " · " + j.action : "";
        var sport = j.meta && j.meta.sport_ok === false ? " · sport fallito" : "";
        var timing = j.timing ? " · cam " + j.timing.camera_s + "s vis " + j.timing.vision_api_s + "s" : "";
        if (j.speech && j.speech.interaction) {
          setStatus("risposta" + lat + act + timing + backend + sport + " · ack + azione");
        } else if (j.speech && j.speech.async) {
          setStatus("risposta" + lat + act + timing + backend + sport + " · voce in coda");
        } else if (j.speech && j.speech.ok) {
          setStatus("ok" + lat + act + timing + backend + sport + " · voce OK");
        } else if (j.speech && !j.speech.skipped) {
          setStatus("ok" + lat + act + timing + backend + sport + " · voce fallita");
        } else {
          setStatus("ok" + lat + act + timing + backend + sport);
        }
      })
      .catch(function (e) {
        pending.textContent = "Errore: " + e;
      });
  }

  document.addEventListener("DOMContentLoaded", function () {
    fetch("/api/hermes/health")
      .then(function (r) { return r.json(); })
      .then(function (j) {
        var tts = j.tts || {};
        var active = tts.active_engine || tts.last_engine;
        var eng = tts.configured_engine || "?";
        var voice = "TTS " + eng + (active ? " · ultimo " + active : "");
        var integrated = j.integrated === true || j.standalone === false;
        var cam = integrated
          ? " · integrato nella dashboard 5056"
          : (j.operator_reachable ? " · servizi camera online" : " · servizi camera non raggiungibili");
        setStatus("Hermes · " + voice + cam);
      })
      .catch(function () { setStatus("offline"); });

    var form = document.getElementById("chatForm");
    if (form) {
      form.addEventListener("submit", function (ev) {
        ev.preventDefault();
        send();
      });
    }
  });
})();
