(function () {
  "use strict";
  var api = window.operatorsApi;
  if (typeof api !== "function") {
    throw new Error('operators_core.js must load before this module');
  }

  var SR = window.__OPERATORS_SCRIPT_ROOT__ || "";

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
    if (window.operatorsStartVariantInit) {
      window.operatorsStartVariantInit();
    }
    if (window.operatorsFillStreamUrlCodes) {
      window.operatorsFillStreamUrlCodes();
    }
    var wire = window.operatorsWireMjpegStream;
    if (wire) {
      wire(document.getElementById("cam0Preview"));
      wire(document.getElementById("cam6Preview"));
      wire(document.getElementById("graspDockCam0"));
      wire(document.getElementById("graspDockCam6"));
    }
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
    if (window.operatorsGraspRefreshStartPoseBadge) {
      window.operatorsGraspRefreshStartPoseBadge();
    }
  });
})();
