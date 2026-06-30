(function () {
  "use strict";

  var SR = window.__FOCUS_SCRIPT_ROOT__ || "";

  function api(path) {
    if (!path) path = "/";
    if (path.charAt(0) !== "/") path = "/" + path;
    return SR + path;
  }

  function $(id) { return document.getElementById(id); }

  function setPill(id, text, kind) {
    var el = $(id);
    if (!el) return;
    el.textContent = text;
    el.className = "pill " + (kind || "");
  }

  function write(id, obj) {
    var el = $(id);
    if (!el) return;
    if (typeof obj === "string") el.textContent = obj;
    else el.textContent = JSON.stringify(obj, null, 2);
  }

  function jsonFetch(path, opts) {
    return fetch(api(path), Object.assign({ cache: "no-store", credentials: "same-origin" }, opts || {}))
      .then(function (r) {
        return r.text().then(function (t) {
          var j = {};
          try { j = t ? JSON.parse(t) : {}; } catch (e) { j = { ok: false, raw: t.slice(0, 500) }; }
          j._http_status = r.status;
          return j;
        });
      });
  }

  function activateTab(name) {
    document.querySelectorAll(".focus-tabs button").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-tab") === name);
    });
    document.querySelectorAll(".panel").forEach(function (p) {
      p.classList.toggle("active", p.id === "tab-" + name);
    });
    if (name === "hermes") {
      var fr = $("hermesFrame");
      if (fr && !fr.getAttribute("src")) fr.setAttribute("src", fr.getAttribute("data-src"));
    }
    if (name === "motors") {
      var mf = $("motorsFrame");
      if (mf && !mf.getAttribute("src")) mf.setAttribute("src", mf.getAttribute("data-src"));
    }
    if (name === "system") refreshAll();
    try { window.location.hash = "tab-" + name; } catch (e) {}
  }

  function initialTabFromHash() {
    var h = (window.location.hash || "").replace(/^#/, "").trim().toLowerCase();
    if (!h) return "teach";
    if (h.indexOf("tab-") === 0) h = h.slice(4);
    if (["teach", "motion", "motors", "hermes", "system"].indexOf(h) >= 0) return h;
    return "teach";
  }

  function refreshHealth() {
    return jsonFetch("/api/health")
      .then(function (j) {
        setPill("focusHealthPill", j.ok ? "online" : "health err", j.ok ? "ok" : "err");
        return j;
      })
      .catch(function (e) {
        setPill("focusHealthPill", "offline", "err");
        return { ok: false, error: String(e) };
      });
  }

  function refreshFocusStatus() {
    return jsonFetch("/api/focus/status?_=" + Date.now())
      .then(function (j) {
        var teach = j.teach || {};
        var arm = j.arm || {};
        var model = !!teach.has_active_model;
        var coupled = !!arm.arm_coupled;
        setPill("focusTeachPill", coupled ? (model ? "teach OK" : "teach?") : "arm free", coupled && model ? "ok" : "warn");
        return j;
      })
      .catch(function (e) {
        setPill("focusTeachPill", "focus err", "err");
        return { ok: false, error: String(e) };
      });
  }

  function ddsProbe() {
    write("motionOut", "Test DDS...");
    return jsonFetch("/api/base/sport_connectivity?_=" + Date.now())
      .then(function (j) {
        write("motionOut", j);
        return j;
      })
      .catch(function (e) {
        write("motionOut", String(e));
      });
  }

  function sportLast() {
    return jsonFetch("/api/base/sport_last?_=" + Date.now()).then(function (j) {
      write("motionOut", j);
      return j;
    });
  }

  function sport(mode) {
    write("motionOut", "Invio " + mode + "...");
    var pre = Promise.resolve({});
    if (mode === "crouch") {
      pre = jsonFetch("/api/arm/true_zero", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ op: "goto_zero" }),
      }).then(function (jZero) {
        return jsonFetch("/api/motor/thermal/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ recovery_stand_enabled: false }),
        }).then(function (jThermal) {
          return { arm_zero: jZero, thermal: jThermal };
        }).catch(function (e) {
          return { arm_zero: jZero, thermal: { ok: false, error: String(e) } };
        });
      }).catch(function (e) {
        return { arm_zero: { ok: false, error: String(e) } };
      });
    }

    return pre.then(function (preOut) {
      return jsonFetch("/api/base/accompany_mode", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: mode, enable: true, sync: true }),
      }).then(function (j) {
        write("motionOut", { pre: preOut, sport: j });
        sportLast();
      });
    }).catch(function (e) {
      write("motionOut", String(e));
    });
  }

  function refreshAll() {
    Promise.all([
      refreshHealth(),
      refreshFocusStatus(),
      jsonFetch("/api/base/sport_last?_=" + Date.now()).catch(function (e) { return { ok: false, error: String(e) }; }),
      jsonFetch("/api/hermes/health?_=" + Date.now()).catch(function (e) { return { ok: false, error: String(e) }; }),
      jsonFetch("/api/motor/state?_=" + Date.now()).catch(function (e) { return { ok: false, error: String(e) }; }),
      jsonFetch("/api/arm/status?_=" + Date.now()).catch(function (e) { return { ok: false, error: String(e) }; }),
      jsonFetch("/api/pick/teach/samples?_=" + Date.now()).catch(function (e) { return { ok: false, error: String(e) }; }),
    ]).then(function (rows) {
      write("systemOut", {
        health: rows[0],
        focus: rows[1],
        sport_last: rows[2],
        hermes: rows[3],
        motor: rows[4],
        arm: rows[5],
        teach_samples: rows[6],
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".focus-tabs button").forEach(function (b) {
      b.addEventListener("click", function () { activateTab(b.getAttribute("data-tab")); });
    });
    document.querySelectorAll("[data-sport]").forEach(function (b) {
      b.addEventListener("click", function () { sport(b.getAttribute("data-sport")); });
    });
    var btnDds = $("btnDds");
    if (btnDds) btnDds.addEventListener("click", ddsProbe);
    var btnSportLast = $("btnSportLast");
    if (btnSportLast) btnSportLast.addEventListener("click", sportLast);
    var btnRefreshAll = $("btnRefreshAll");
    if (btnRefreshAll) btnRefreshAll.addEventListener("click", refreshAll);
    activateTab(initialTabFromHash());
    refreshHealth();
    refreshFocusStatus();
  });
})();
