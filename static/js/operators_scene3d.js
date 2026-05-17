/**
 * Viewer 3D minimale per tab operator: consuma GET /api/arm/scene_3d (fast|full).
 * Richiede THREE + THREE.OrbitControls (stessi script CDN del dashboard monolite).
 */
(function (global) {
  "use strict";

  var SR = global.__OPERATORS_SCRIPT_ROOT__ || "";

  function api(path) {
    if (!path) {
      path = "/";
    }
    if (path.charAt(0) !== "/") {
      path = "/" + path;
    }
    return SR + path;
  }

  var state = {
    renderer: null,
    scene: null,
    camera: null,
    controls: null,
    root: null,
    markers: null,
    raf: 0,
    pollTimer: 0,
    polling: false,
    lastPayload: null,
  };

  function $(id) {
    return document.getElementById(id);
  }

  function disposeThree() {
    if (state.raf) {
      cancelAnimationFrame(state.raf);
      state.raf = 0;
    }
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = 0;
    }
    state.polling = false;
    if (state.controls && state.controls.dispose) {
      state.controls.dispose();
    }
    state.controls = null;
    if (state.renderer) {
      state.renderer.dispose();
    }
    state.renderer = null;
    state.scene = null;
    state.camera = null;
    state.root = null;
    state.markers = null;
  }

  function ensureInit() {
    if (typeof THREE === "undefined") {
      var h = $("operatorsScene3dHint");
      if (h) {
        h.textContent =
          "Three.js non caricato (CDN bloccata o script mancanti). Controlla rete verso cdn.jsdelivr.net.";
      }
      return false;
    }
    var canvas = $("operatorsScene3dCanvas");
    var wrap = $("operatorsScene3dCanvasWrap");
    if (!canvas || !wrap) {
      return false;
    }
    if (state.renderer) {
      return true;
    }
    var scene = new THREE.Scene();
    scene.background = new THREE.Color(0x0b0f14);

    var cam = new THREE.PerspectiveCamera(50, 1, 0.02, 8.0);
    cam.position.set(0.55, 0.35, 0.45);

    var renderer = new THREE.WebGLRenderer({ canvas: canvas, antialias: true, alpha: false });
    renderer.setPixelRatio(Math.min(global.devicePixelRatio || 1, 2));

    var amb = new THREE.AmbientLight(0xffffff, 0.55);
    scene.add(amb);
    var dir = new THREE.DirectionalLight(0xffffff, 0.85);
    dir.position.set(0.4, 1.2, 0.6);
    scene.add(dir);

    var root = new THREE.Group();
    scene.add(root);
    var markers = new THREE.Group();
    root.add(markers);

    var axes = new THREE.AxesHelper(0.35);
    root.add(axes);

    var ctrl = new THREE.OrbitControls(cam, renderer.domElement);
    ctrl.enableDamping = true;
    ctrl.target.set(0.25, 0, 0.12);

    state.scene = scene;
    state.camera = cam;
    state.renderer = renderer;
    state.controls = ctrl;
    state.root = root;
    state.markers = markers;

    function onResize() {
      if (!state.renderer || !wrap) {
        return;
      }
      var w = wrap.clientWidth || 320;
      var h = wrap.clientHeight || 280;
      state.renderer.setSize(w, h, false);
      state.camera.aspect = w / Math.max(h, 1);
      state.camera.updateProjectionMatrix();
    }
    state._onResize = onResize;
    global.addEventListener("resize", onResize);
    onResize();
    return true;
  }

  function v3(a) {
    if (!a || a.length < 3) {
      return null;
    }
    return new THREE.Vector3(Number(a[0]), Number(a[1]), Number(a[2]));
  }

  function addSphere(parent, p, color, radius) {
    if (!p) {
      return;
    }
    var g = new THREE.SphereGeometry(radius || 0.012, 18, 14);
    var m = new THREE.MeshStandardMaterial({ color: color, metalness: 0.2, roughness: 0.45 });
    var mesh = new THREE.Mesh(g, m);
    mesh.position.copy(p);
    parent.add(mesh);
  }

  function addLineStrip(parent, points, color) {
    if (!points || points.length < 2) {
      return;
    }
    var geom = new THREE.BufferGeometry().setFromPoints(points);
    var mat = new THREE.LineBasicMaterial({ color: color, linewidth: 1 });
    parent.add(new THREE.Line(geom, mat));
  }

  function addPointCloudBaseLink(parent, triplets, colorHex) {
    if (!triplets || !triplets.length || typeof THREE.Points === "undefined") {
      return;
    }
    var n = Math.min(triplets.length, 6000);
    var positions = new Float32Array(n * 3);
    for (var i = 0; i < n; i++) {
      var t = triplets[i];
      if (!t || t.length < 3) {
        continue;
      }
      positions[i * 3] = Number(t[0]);
      positions[i * 3 + 1] = Number(t[1]);
      positions[i * 3 + 2] = Number(t[2]);
    }
    var geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    var mat = new THREE.PointsMaterial({
      color: colorHex != null ? colorHex : 0x7dd3fc,
      size: 0.01,
      sizeAttenuation: true,
      transparent: true,
      opacity: 0.9,
      depthWrite: false,
    });
    parent.add(new THREE.Points(geo, mat));
  }

  function addBox(parent, center, size, color) {
    if (!center || !size || size.length < 3) {
      return;
    }
    var g = new THREE.BoxGeometry(Number(size[0]), Number(size[1]), Number(size[2]));
    var edges = new THREE.EdgesGeometry(g);
    var line = new THREE.LineSegments(
      edges,
      new THREE.LineBasicMaterial({ color: color })
    );
    line.position.copy(center);
    parent.add(line);
  }

  function addCylinder(parent, spec, color) {
    if (!spec || !spec.center_m || !spec.radius_m || !spec.height_m) {
      return;
    }
    var c = v3(spec.center_m);
    var ax = v3(spec.axis_unit_m || [0, 0, 1]);
    if (!c || !ax) {
      return;
    }
    var h = Number(spec.height_m);
    var r = Number(spec.radius_m);
    var geo = new THREE.CylinderGeometry(r, r, h, 20, 1, false);
    var mesh = new THREE.Mesh(
      geo,
      new THREE.MeshStandardMaterial({ color: color, metalness: 0.15, roughness: 0.55, transparent: true, opacity: 0.75 })
    );
    var up = new THREE.Vector3(0, 1, 0);
    var q = new THREE.Quaternion().setFromUnitVectors(up, ax.clone().normalize());
    mesh.quaternion.copy(q);
    mesh.position.copy(c.clone().add(ax.clone().normalize().multiplyScalar(0)));
    parent.add(mesh);
  }

  function tagColor(id) {
    var palette = { 0: 0x22c55e, 1: 0x38bdf8, 2: 0xf59e0b, 3: 0xf472b6, 5: 0xef4444 };
    return palette[id] != null ? palette[id] : 0x94a3b8;
  }

  function rebuildMarkers(d) {
    if (!state.markers) {
      return;
    }
    while (state.markers.children.length > 0) {
      var ch = state.markers.children[0];
      state.markers.remove(ch);
      if (ch.geometry) {
        ch.geometry.dispose();
      }
      if (ch.material) {
        if (Array.isArray(ch.material)) {
          ch.material.forEach(function (m) {
            m.dispose();
          });
        } else {
          ch.material.dispose();
        }
      }
    }

    var lm = d.viewer_landmarks_base_link_m || {};
    var prim = d.viewer_detected_object_primitive || {};
    var sg = d.scene_graph || {};
    var vis = d.vis_geometry_markers_arm_m || {};

    addSphere(state.markers, v3(lm.xt16_tag_m), 0xff5555, 0.018);
    addSphere(state.markers, v3(lm.front_camera_display_base_link_m), 0xa855f7, 0.014);
    addSphere(state.markers, v3(lm.wrist_camera_display_base_link_m), 0xeab308, 0.014);
    addSphere(state.markers, v3(lm.tool_tip_base_link_m), 0xff00cc, 0.014);
    addSphere(state.markers, v3(lm.object_target_display_base_link_m || lm.object_target_base_link_m), 0x22d3ee, 0.016);

    var cyl = lm.xt16_lidar_cylinder_base_link_m;
    if (cyl && cyl.center_m) {
      addCylinder(state.markers, cyl, 0x64748b);
    }

    if (prim.center_base_link_m && prim.size_m) {
      addBox(state.markers, v3(prim.center_base_link_m), prim.size_m, 0x4ade80);
    }

    var joints = sg.d1_joint_centers_base_link_m;
    if (Array.isArray(joints) && joints.length) {
      var pts = [];
      for (var i = 0; i < joints.length; i++) {
        var p = v3(joints[i]);
        if (p) {
          addSphere(state.markers, p, 0x60a5fa, 0.009);
          pts.push(p);
        }
      }
      addLineStrip(state.markers, pts, 0x38bdf8);
    }

    var tags = d.tags_for_viewer || [];
    for (var t = 0; t < tags.length; t++) {
      var row = tags[t];
      var bid = row && row.base_xyz_base_link_m;
      var id = parseInt(row && row.id, 10);
      addSphere(state.markers, v3(bid), tagColor(isNaN(id) ? -1 : id), 0.011);
    }

    var traj = d.ik_trajectory || {};
    var tips = traj.fk_tool_xyz_m;
    if (Array.isArray(tips) && tips.length > 1) {
      var tps = [];
      for (var j = 0; j < tips.length; j++) {
        var tp = v3(tips[j]);
        if (tp) {
          tps.push(tp);
        }
      }
      addLineStrip(state.markers, tps, 0xf97316);
    }

    if (global.__operatorsGraspMarkerBaseLink_m) {
      addSphere(state.markers, v3(global.__operatorsGraspMarkerBaseLink_m), 0xffffff, 0.02);
    }

    var agPts = global.__operatorsGraspPointsBaseLink_m;
    if (Array.isArray(agPts) && agPts.length) {
      addPointCloudBaseLink(state.markers, agPts, 0x38bdf8);
    }

    var hint = $("operatorsScene3dHint");
    if (hint) {
      var snap = d.vision_snapshot || {};
      var chain = d.vis_geometry_chain_mm || null;
      var lines = [];
      lines.push("servo_feedback_ok=" + String(d.servo_feedback_ok));
      lines.push("geometry_fast_preview=" + String(!!d.geometry_fast_preview));
      lines.push("planner_ok=" + String(!!snap.planner_ok));
      lines.push("target_ok=" + String(!!snap.target_ok));
      lines.push("preview_ik_ok=" + String(!!snap.preview_ik_ok));
      if (chain && typeof chain === "object") {
        lines.push("mjcf_depth_to_tag5_mm=" + String(chain.mjcf_depth_to_tag5_mm));
      }
      if (d.viewer_3d_warnings && d.viewer_3d_warnings.length) {
        lines.push("warnings: " + d.viewer_3d_warnings.length);
      }
      var npc = global.__operatorsGraspPointsBaseLink_m;
      if (npc && npc.length) {
        lines.push("anygrasp_cloud_pts(base_link)=" + npc.length);
      }
      if (global.__operatorsGraspMarkerBaseLink_m) {
        lines.push("anygrasp_marker_base_link=on");
      }
      hint.textContent = lines.join("\n");
    }

    state.lastPayload = d;
    if (global.__operatorsScene3dPayloadHook) {
      try {
        global.__operatorsScene3dPayloadHook(d);
      } catch (e) {}
    }
  }

  function fetchScene() {
    var fastEl = document.querySelector('input[name="scene3dMode"]:checked');
    var fast = !fastEl || fastEl.value !== "full";
    var q = fast ? "?fast=1&_=" : "?_=";
    return fetch(api("/api/arm/scene_3d" + q + Date.now()), { cache: "no-store" }).then(function (r) {
      return r.json().then(function (j) {
        return { ok: r.ok, j: j };
      });
    });
  }

  function tickLoop() {
    if (!state.renderer) {
      state.raf = 0;
      return;
    }
    if (state.controls) {
      state.controls.update();
    }
    state.renderer.render(state.scene, state.camera);
    if (state.polling) {
      state.raf = requestAnimationFrame(tickLoop);
    } else {
      state.raf = 0;
    }
  }

  function startPolling() {
    if (state.polling) {
      return;
    }
    if (!ensureInit()) {
      return;
    }
    state.polling = true;
    var ms = parseInt(($("scene3dIntervalMs") && $("scene3dIntervalMs").value) || "1500", 10);
    if (!isFinite(ms) || ms < 400) {
      ms = 1500;
    }

    function one() {
      fetchScene().then(
        function (o) {
          if (o.j && o.j.ok !== false) {
            rebuildMarkers(o.j);
          } else {
            var h = $("operatorsScene3dHint");
            if (h) {
              h.textContent = JSON.stringify(o.j, null, 2).slice(0, 1200);
            }
          }
        },
        function (e) {
          var h2 = $("operatorsScene3dHint");
          if (h2) {
            h2.textContent = String(e);
          }
        }
      );
    }
    one();
    state.pollTimer = global.setInterval(one, ms);
    tickLoop();
  }

  function stopPolling() {
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = 0;
    }
    state.polling = false;
    if (state.raf) {
      cancelAnimationFrame(state.raf);
      state.raf = 0;
    }
  }

  global.operatorsScene3dStart = function () {
    if (!ensureInit()) {
      return;
    }
    stopPolling();
    startPolling();
  };

  global.operatorsScene3dStop = function () {
    stopPolling();
  };

  global.operatorsScene3dOnTabShown = function () {
    if (state._onResize) {
      state._onResize();
    }
    if (state.renderer && state.scene && state.camera) {
      if (state.controls) {
        state.controls.update();
      }
      state.renderer.render(state.scene, state.camera);
    }
  };

  global.operatorsScene3dRefreshGraspLayer = function () {
    if (!ensureInit()) {
      return;
    }
    if (state.lastPayload && state.markers) {
      rebuildMarkers(state.lastPayload);
      if (state.renderer && state.scene && state.camera) {
        state.renderer.render(state.scene, state.camera);
      }
      return;
    }
    fetchScene().then(function (o) {
      if (o.j && o.j.ok !== false && state.markers) {
        rebuildMarkers(o.j);
        if (state.renderer && state.scene && state.camera) {
          state.renderer.render(state.scene, state.camera);
        }
      }
    });
  };

  global.operatorsScene3dDispose = function () {
    stopPolling();
    if (state._onResize) {
      global.removeEventListener("resize", state._onResize);
      state._onResize = null;
    }
    disposeThree();
  };
})(window);
