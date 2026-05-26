/**
 * Viewer 3D minimale per tab operator: consuma GET /api/arm/scene_3d (fast|full).
 * Richiede THREE + OrbitControls + STLLoader (stessi CDN del monolite). Mesh D1 da ``/api/arm/scene_meshes/d1/*.STL``.
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
    meshAssembly: null,
    markers: null,
    d1JointGroups: null,
    d1StlBuildSignature: null,
    d1StlDoneCount: 0,
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
    clearD1MeshRig();
    if (state.renderer) {
      state.renderer.dispose();
    }
    state.renderer = null;
    state.scene = null;
    state.camera = null;
    state.root = null;
    state.meshAssembly = null;
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

    var axes = new THREE.AxesHelper(0.35);
    root.add(axes);

    var meshAssembly = new THREE.Group();
    root.add(meshAssembly);

    var markers = new THREE.Group();
    root.add(markers);

    var ctrl = new THREE.OrbitControls(cam, renderer.domElement);
    ctrl.enableDamping = true;
    ctrl.target.set(0.25, 0, 0.12);

    state.scene = scene;
    state.camera = cam;
    state.renderer = renderer;
    state.controls = ctrl;
    state.root = root;
    state.meshAssembly = meshAssembly;
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
    var dbgCb = $("scene3dShowDebug");
    if (dbgCb && !dbgCb._scene3dBound) {
      dbgCb._scene3dBound = true;
      dbgCb.addEventListener("change", function () {
        if (state.lastPayload && state.markers) {
          rebuildMarkers(state.lastPayload);
          if (state.renderer && state.scene && state.camera) {
            state.renderer.render(state.scene, state.camera);
          }
        }
      });
    }
    function bindCb(id) {
      var el = $(id);
      if (el && !el._scene3dBound) {
        el._scene3dBound = true;
        el.addEventListener("change", function () {
          if (id === "scene3dLoadStlArm") {
            clearD1MeshRig();
          }
          if (state.lastPayload && state.markers) {
            rebuildMarkers(state.lastPayload);
            if (state.renderer && state.scene && state.camera) {
              state.renderer.render(state.scene, state.camera);
            }
          }
        });
      }
    }
    bindCb("scene3dLoadStlArm");
    bindCb("scene3dShowFkBones");
    return true;
  }

  function clearD1MeshRig() {
    state.d1JointGroups = null;
    state.d1StlBuildSignature = null;
    state.d1StlDoneCount = 0;
    if (!state.meshAssembly) {
      return;
    }
    while (state.meshAssembly.children.length > 0) {
      var ch = state.meshAssembly.children[0];
      state.meshAssembly.remove(ch);
      ch.traverse(function (obj) {
        if (obj.geometry) {
          obj.geometry.dispose();
        }
        if (obj.material) {
          if (Array.isArray(obj.material)) {
            obj.material.forEach(function (m) {
              m.dispose();
            });
          } else {
            obj.material.dispose();
          }
        }
      });
    }
  }

  function scene3dStlArmEnabled() {
    var el = $("scene3dLoadStlArm");
    return !el || el.checked;
  }

  function scene3dFkBonesEnabled() {
    var el = $("scene3dShowFkBones");
    return !!(el && el.checked);
  }

  function prepareStlGeometry(geom) {
    if (!geom) {
      return geom;
    }
    if (geom.computeBoundingSphere) {
      geom.computeBoundingSphere();
    }
    if (geom.computeVertexNormals) {
      geom.computeVertexNormals();
    }
    return geom;
  }

  function d1StlMaterial(hex, em) {
    return new THREE.MeshStandardMaterial({
      color: hex,
      metalness: 0.22,
      roughness: 0.48,
      emissive: new THREE.Color(em != null ? em : hex),
      emissiveIntensity: 0.35,
      flatShading: true,
    });
  }

  function meshUrl(kind, fname) {
    return api("/api/arm/scene_meshes/" + kind + "/" + encodeURIComponent(fname));
  }

  function applyD1VisualOffset(mesh, vmOff, linkIdx) {
    if (!mesh || !vmOff || !vmOff[linkIdx]) {
      return;
    }
    var o = vmOff[linkIdx];
    var p = o.pos_m;
    var q = o.quat_xyzw;
    if (p && p.length >= 3) {
      mesh.position.set(Number(p[0]), Number(p[1]), Number(p[2]));
    }
    if (q && q.length >= 4) {
      mesh.quaternion.set(Number(q[0]), Number(q[1]), Number(q[2]), Number(q[3]));
    }
  }

  function applyJointLocals(locals) {
    if (!locals || !state.d1JointGroups) {
      return;
    }
    var n = Math.min(locals.length, state.d1JointGroups.length);
    for (var i = 0; i < n; i++) {
      var g = state.d1JointGroups[i];
      var L = locals[i];
      if (!L || !L.quaternion_xyzw) {
        continue;
      }
      var q = L.quaternion_xyzw;
      g.quaternion.set(Number(q[0]), Number(q[1]), Number(q[2]), Number(q[3]));
      if (L.translation_m && L.translation_m.length >= 3) {
        g.position.set(Number(L.translation_m[0]), Number(L.translation_m[1]), Number(L.translation_m[2]));
      }
    }
  }

  function ensureD1MeshRigFromPayload(d) {
    if (!state.meshAssembly) {
      return;
    }
    if (!scene3dStlArmEnabled()) {
      clearD1MeshRig();
      return;
    }
    var STLLoaderCls = typeof THREE !== "undefined" ? THREE.STLLoader : null;
    if (!STLLoaderCls) {
      return;
    }
    var sg = d.scene_graph || {};
    var locals = sg.d1_joint_locals_m;
    if (!locals || locals.length < 6) {
      return;
    }
    var mnt = sg.arm_mount_xyz_m || [0.15, 0, 0.06];
    var sig = [mnt[0], mnt[1], mnt[2], locals.length].join(",");
    if (state.d1StlBuildSignature === sig && state.d1JointGroups && state.d1JointGroups.length === 6) {
      applyJointLocals(locals);
      return;
    }
    clearD1MeshRig();
    state.d1StlBuildSignature = sig;
    state.d1StlDoneCount = 0;
    var vmOff = sg.d1_mesh_visual_offsets_m || [];
    var armMount = new THREE.Group();
    armMount.position.set(Number(mnt[0]), Number(mnt[1]), Number(mnt[2]));
    state.meshAssembly.add(armMount);
    var link00 = new THREE.Group();
    armMount.add(link00);
    var parent = link00;
    var d1Joints = [];
    for (var ji = 0; ji < 6; ji++) {
      var jg = new THREE.Group();
      var tr = locals[ji].translation_m;
      jg.position.set(Number(tr[0]), Number(tr[1]), Number(tr[2]));
      var qx = locals[ji].quaternion_xyzw;
      jg.quaternion.set(Number(qx[0]), Number(qx[1]), Number(qx[2]), Number(qx[3]));
      parent.add(jg);
      d1Joints.push(jg);
      parent = jg;
    }
    state.d1JointGroups = d1Joints;
    var stlLoader = new STLLoaderCls();
    function oneDone() {
      state.d1StlDoneCount++;
      if (state.renderer && state.scene && state.camera) {
        state.renderer.render(state.scene, state.camera);
      }
    }
    stlLoader.load(
      meshUrl("d1", "base_link.STL"),
      function (geom) {
        prepareStlGeometry(geom);
        var mesh = new THREE.Mesh(geom, d1StlMaterial(0x9aacbd, 0x334155));
        applyD1VisualOffset(mesh, vmOff, 0);
        link00.add(mesh);
        oneDone();
      },
      undefined,
      function () {
        oneDone();
      }
    );
    var linkStl = [
      "Empty_Link1.STL",
      "Empty_Link2.STL",
      "Empty_Link3.STL",
      "Empty_Link4.STL",
      "Empty_Link5.STL",
      "Empty_Link6.STL",
    ];
    for (var li = 0; li < 6; li++) {
      (function (idx, fname) {
        stlLoader.load(
          meshUrl("d1", fname),
          function (geom) {
            prepareStlGeometry(geom);
            var mesh = new THREE.Mesh(geom, d1StlMaterial(0x22c3e6, 0x0e7490));
            applyD1VisualOffset(mesh, vmOff, idx + 1);
            d1Joints[idx].add(mesh);
            oneDone();
          },
          undefined,
          function () {
            oneDone();
          }
        );
      })(li, linkStl[li]);
    }
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

  function addBoneLink(parent, A, B, color, radius) {
    if (!A || !B || typeof THREE === "undefined") {
      return;
    }
    var dir = new THREE.Vector3().subVectors(B, A);
    var len = dir.length();
    if (len < 0.0004) {
      return;
    }
    var r = radius != null ? radius : 0.011;
    var geo = new THREE.CylinderGeometry(r, r, len, 12, 1, false);
    var mat = new THREE.MeshStandardMaterial({
      color: color != null ? color : 0x94a3b8,
      metalness: 0.2,
      roughness: 0.5,
    });
    var mesh = new THREE.Mesh(geo, mat);
    var mid = new THREE.Vector3().addVectors(A, B).multiplyScalar(0.5);
    mesh.position.copy(mid);
    var up = new THREE.Vector3(0, 1, 0);
    var ax = dir.clone().normalize();
    var q = new THREE.Quaternion().setFromUnitVectors(up, ax);
    mesh.quaternion.copy(q);
    parent.add(mesh);
  }

  function scene3dDebugOn() {
    var el = $("scene3dShowDebug");
    return !!(el && el.checked);
  }

  function rebuildMarkers(d) {
    if (!state.markers) {
      return;
    }
    ensureD1MeshRigFromPayload(d);

    var canStl = typeof THREE !== "undefined" && THREE.STLLoader;
    var stlOn = scene3dStlArmEnabled() && !!canStl;
    var stlComplete = stlOn && state.d1StlDoneCount >= 7;
    var showBones = scene3dFkBonesEnabled() || !stlOn || !stlComplete;

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
    var dbg = scene3dDebugOn();

    if (dbg) {
      addSphere(state.markers, v3(lm.xt16_tag_m), 0xff5555, 0.018);
      addSphere(state.markers, v3(lm.front_camera_display_base_link_m), 0xa855f7, 0.014);
      addSphere(state.markers, v3(lm.wrist_camera_display_base_link_m), 0xeab308, 0.014);
      var objTdbg = lm.object_target_display_base_link_m || lm.object_target_base_link_m;
      if (objTdbg) {
        addSphere(state.markers, v3(objTdbg), 0x22d3ee, 0.016);
      }
      var cyl = lm.xt16_lidar_cylinder_base_link_m;
      if (cyl && cyl.center_m) {
        addCylinder(state.markers, cyl, 0x64748b);
      }
    }
    var primCenter = prim.center_base_link_m;
    if ((!primCenter || primCenter.length < 3) && lm.object_nominal_20cm_base_link_m) {
      primCenter = lm.object_nominal_20cm_base_link_m;
    }
    if (primCenter && primCenter.length >= 3 && prim.size_m) {
      addBox(state.markers, v3(primCenter), prim.size_m, 0x4ade80);
    }

    var dog = d.dog_occupancy_base_link;
    if (dog && dog.enabled && dog.center_base_link_m && dog.size_m && dog.center_base_link_m.length >= 3 && dog.size_m.length >= 3) {
      addBox(state.markers, v3(dog.center_base_link_m), dog.size_m, 0xea580c);
    }

    if (showBones) {
      var mount = v3(sg.arm_mount_xyz_m);
      var joints = sg.d1_joint_centers_base_link_m;
      var tip = v3(lm.tool_tip_base_link_m);
      var armChainOk = false;
      if (mount && Array.isArray(joints) && joints.length) {
        var chain = [mount];
        for (var ji = 0; ji < joints.length; ji++) {
          var jp = v3(joints[ji]);
          if (jp) {
            chain.push(jp);
          }
        }
        if (tip) {
          chain.push(tip);
        }
        var boneCol = 0x38bdf8;
        for (var bi = 0; bi + 1 < chain.length; bi++) {
          var rBone = bi === 0 ? 0.014 : 0.011;
          addBoneLink(state.markers, chain[bi], chain[bi + 1], boneCol, rBone);
        }
        armChainOk = true;
      } else if (Array.isArray(joints) && joints.length) {
        var pts = [];
        for (var i = 0; i < joints.length; i++) {
          var p = v3(joints[i]);
          if (p) {
            pts.push(p);
          }
        }
        for (var bj = 0; bj + 1 < pts.length; bj++) {
          addBoneLink(state.markers, pts[bj], pts[bj + 1], 0x38bdf8, 0.011);
        }
        if (tip && pts.length) {
          addBoneLink(state.markers, pts[pts.length - 1], tip, 0xff00cc, 0.012);
        }
        armChainOk = !!tip || pts.length > 1;
      }
      if (!armChainOk) {
        addSphere(state.markers, v3(lm.tool_tip_base_link_m), 0xff00cc, 0.013);
      }
    }

    var tags = d.tags_for_viewer || [];
    for (var t = 0; t < tags.length; t++) {
      var row = tags[t];
      var bid = row && row.base_xyz_base_link_m;
      var id = parseInt(row && row.id, 10);
      addSphere(state.markers, v3(bid), tagColor(isNaN(id) ? -1 : id), 0.013);
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

    var ov = d.operator_vla_display || {};
    var vlaPath = ov.openvla_approach_tool_path_base_link_m;
    if (Array.isArray(vlaPath) && vlaPath.length > 1) {
      var vp = [];
      for (var vi = 0; vi < vlaPath.length; vi++) {
        var pth = v3(vlaPath[vi]);
        if (pth) {
          vp.push(pth);
        }
      }
      if (vp.length > 1) {
        addLineStrip(state.markers, vp, 0x22c55e);
      }
    }

    if (global.__operatorsGraspMarkerBaseLink_m) {
      addSphere(state.markers, v3(global.__operatorsGraspMarkerBaseLink_m), 0xffffff, 0.02);
    }

    var wpg = lm.worker_plan_grasp_base_link_m;
    if (wpg && wpg.length >= 3) {
      var wpp = v3(wpg);
      if (wpp) {
        addSphere(state.markers, wpp, 0xff8800, 0.018);
        var curTip = v3(lm.tool_tip_base_link_m);
        if (curTip && wpp.distanceTo(curTip) > 0.022) {
          addLineStrip(state.markers, [curTip, wpp], 0xf59e0b);
        }
      }
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
      lines.push("pose_is_feedback=" + String(!!(sg && sg.pose_is_feedback)));
      lines.push("stl_arm=" + String(stlOn) + " loaded=" + String(state.d1StlDoneCount) + "/7");
      lines.push("fk_bones=" + String(showBones));
      if (!canStl) {
        lines.push("STLLoader.js mancante (CDN)");
      }
      lines.push("debug_landmarks=" + String(dbg));
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
      var ovd = d.operator_vla_display || {};
      if (ovd.marker_source) {
        lines.push("vla_marker_source=" + String(ovd.marker_source));
      }
      if (ovd.distance_tip_to_marker_m != null && ovd.distance_tip_to_marker_m !== "") {
        lines.push("dist_tip_to_vla_marker_m=" + String(ovd.distance_tip_to_marker_m));
      }
      if (lm.worker_plan_grasp_source) {
        lines.push("worker_plan_grasp_source=" + String(lm.worker_plan_grasp_source));
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

  global.operatorsScene3dReloadSceneOnce = function () {
    if (!ensureInit()) {
      return Promise.resolve();
    }
    return fetchScene().then(function (o) {
      if (o.j && o.j.ok !== false && state.markers) {
        rebuildMarkers(o.j);
      }
      if (state.renderer && state.scene && state.camera) {
        state.renderer.render(state.scene, state.camera);
      }
    });
  };

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
