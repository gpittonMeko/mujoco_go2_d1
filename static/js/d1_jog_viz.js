/**
 * Viewer 3D braccio D1 per dashboard jog (5053).
 * GET /api/viz/arm — catena FK in frame arm_base.
 */
(function (global) {
  "use strict";

  var state = {
    renderer: null,
    scene: null,
    camera: null,
    controls: null,
    markers: null,
    raf: 0,
    pollTimer: 0,
  };

  function api(path) {
    return path.charAt(0) === "/" ? path : "/" + path;
  }

  function armToThree(p) {
    if (!p || p.length < 3) return null;
    return new THREE.Vector3(Number(p[0]), Number(p[2]), -Number(p[1]));
  }

  function dispose() {
    if (state.raf) cancelAnimationFrame(state.raf);
    state.raf = 0;
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = 0;
    if (state.controls && state.controls.dispose) state.controls.dispose();
    if (state.renderer) state.renderer.dispose();
    state.renderer = null;
    state.scene = null;
    state.camera = null;
    state.controls = null;
    state.markers = null;
  }

  function ensureInit() {
    if (typeof THREE === "undefined") return false;
    var canvas = document.getElementById("d1JogVizCanvas");
    var wrap = document.getElementById("d1JogVizWrap");
    if (!canvas || !wrap) return false;
    if (state.renderer) return true;

    var scene = new THREE.Scene();
    scene.background = new THREE.Color(0x0b0f14);
    var cam = new THREE.PerspectiveCamera(48, 1, 0.02, 6);
    cam.position.set(0.55, 0.42, 0.5);
    var renderer = new THREE.WebGLRenderer({ canvas: canvas, antialias: true });
    renderer.setPixelRatio(Math.min(global.devicePixelRatio || 1, 2));
    scene.add(new THREE.AmbientLight(0xffffff, 0.55));
    var dir = new THREE.DirectionalLight(0xffffff, 0.9);
    dir.position.set(0.5, 1, 0.4);
    scene.add(dir);
    var root = new THREE.Group();
    scene.add(root);
    var axes = new THREE.AxesHelper(0.25);
    root.add(axes);
    var grid = new THREE.GridHelper(0.8, 12, 0x334155, 0x1e293b);
    grid.rotation.x = Math.PI / 2;
    root.add(grid);
    var markers = new THREE.Group();
    root.add(markers);
    var ctrl = new THREE.OrbitControls(cam, renderer.domElement);
    ctrl.enableDamping = true;
    ctrl.target.set(0.28, 0.12, 0);
    state.scene = scene;
    state.camera = cam;
    state.renderer = renderer;
    state.controls = ctrl;
    state.markers = markers;
    state._wrap = wrap;
    state._onResize = function () {
      var w = wrap.clientWidth || 400;
      var h = wrap.clientHeight || 360;
      renderer.setSize(w, h, false);
      cam.aspect = w / Math.max(h, 1);
      cam.updateProjectionMatrix();
    };
    global.addEventListener("resize", state._onResize);
    state._onResize();
    return true;
  }

  function clearMarkers() {
    if (!state.markers) return;
    while (state.markers.children.length) {
      var ch = state.markers.children[0];
      state.markers.remove(ch);
      if (ch.geometry) ch.geometry.dispose();
      if (ch.material) {
        (Array.isArray(ch.material) ? ch.material : [ch.material]).forEach(function (m) {
          m.dispose();
        });
      }
    }
  }

  function addSphere(p, color, r) {
    var g = new THREE.SphereGeometry(r || 0.014, 16, 12);
    var m = new THREE.MeshStandardMaterial({ color: color, metalness: 0.25, roughness: 0.4 });
    var mesh = new THREE.Mesh(g, m);
    mesh.position.copy(p);
    state.markers.add(mesh);
  }

  function addLine(points, color) {
    if (points.length < 2) return;
    var geom = new THREE.BufferGeometry().setFromPoints(points);
    state.markers.add(new THREE.Line(geom, new THREE.LineBasicMaterial({ color: color })));
  }

  function rebuild(d) {
    if (!state.markers || !d) return;
    clearMarkers();
    var chain = d.chain_arm_m || [];
    var pts = [];
    for (var i = 0; i < chain.length; i++) {
      var p = armToThree(chain[i]);
      if (!p) continue;
      var col = i === 0 ? 0x64748b : 0x38bdf8;
      var rad = i === chain.length - 1 ? 0.018 : 0.011;
      addSphere(p, i === chain.length - 1 ? 0xf472b6 : col, rad);
      pts.push(p);
    }
    addLine(pts, 0x10b981);
    var hint = document.getElementById("d1JogVizHint");
    if (hint) {
      hint.textContent = d.pose_is_feedback !== false
        ? "FK da angoli correnti · " + (chain.length ? chain.length + " punti" : "—")
        : "Nessun feedback";
    }
  }

  function tickRender() {
    if (!state.renderer) return;
    state.raf = requestAnimationFrame(tickRender);
    if (state.controls) state.controls.update();
    state.renderer.render(state.scene, state.camera);
  }

  function fetchViz(servoDeg) {
    var opts = { method: "GET", cache: "no-store" };
    if (servoDeg && servoDeg.length >= 6) {
      opts.method = "POST";
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify({ servo_deg: servoDeg });
    }
    return fetch(api("/api/viz/arm"), opts).then(function (r) {
      return r.json();
    });
  }

  function refresh(servoDeg) {
    return fetchViz(servoDeg).then(function (d) {
      if (d && d.ok) {
        state._lastViz = d;
        rebuild(d);
      }
      return d;
    });
  }

  function startPolling(getServoFn, hz) {
    stopPolling();
    var period = Math.max(80, Math.round(1000 / (hz || 8)));
    state.pollTimer = setInterval(function () {
      var sd = typeof getServoFn === "function" ? getServoFn() : null;
      refresh(sd);
    }, period);
  }

  function stopPolling() {
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = 0;
  }

  function init() {
    if (!ensureInit()) {
      var h = document.getElementById("d1JogVizHint");
      if (h) h.textContent = "Three.js non disponibile (CDN).";
      return false;
    }
    tickRender();
    return true;
  }

  global.D1JogViz = {
    init: init,
    dispose: dispose,
    refresh: refresh,
    rebuild: rebuild,
    startPolling: startPolling,
    stopPolling: stopPolling,
    /** Ultimo payload FK (per debug). */
    get lastViz() {
      return state._lastViz;
    },
  };
})(window);
