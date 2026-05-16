# Go2 Dashboard Mission Control Cleanup Plan

> **For Hermes:** use this branch (`dashboard/mission-control-cleanup`) to implement the cleanup in small commits without mixing unrelated dashboard legacy work.

**Goal:** turn the current diagnostics dashboard into a cleaner mission-control UI that preserves the working camera/grasp/3D stack, adds a dedicated control page, and makes the current vision/grasp state readable at a glance.

**Architecture:** keep the existing Flask API surface and working planner/3D endpoints (`/api/cameras/status`, `/api/box/plan`, `/api/arm/grasp_pipeline`, `/api/arm/scene_3d`) and build a cleaner UI layer on top. Do **not** rework grasp logic and UI layout in the same commit. First reorganize views around existing data; then add any missing API fields only where the current endpoints are insufficient.

**Current verified state (2026-05-16):**
- Local repo is clean and now on branch `dashboard/mission-control-cleanup`.
- Local branch `ui/dashboard-overhaul` already contains:
  - `9ea780d fix(camera): auto-map Orbbec wrist camera on NX`
  - `9c0fd0f feat(vision): overlay grasp detection on camera streams`
- NX dashboard health is OK at `http://192.168.123.18:5050/api/health`.
- NX runtime is a copied bundle at `/home/unitree/go2_visual_dashboard`, **not** a git checkout.
- NX process is running via `scripts/nx_dashboard_supervise.sh`; user systemd unit exists but is inactive.
- Live endpoints currently show:
  - cam0 available = true
  - cam6 available = true
  - `/api/arm/grasp_pipeline` reports `grip_detection_any=true`, `fusion_ready_for_execute=true`
  - both cameras currently return `classic_contour_fallback` grip points with preview IK OK
  - `/api/arm/scene_3d?fast=1` returns servo feedback and full scene graph

**Important gaps to respect while designing UI:**
- There is **no exposed live depth-image endpoint** yet. Camera 6 exists as RealSense RGB plus 3D/mount metadata; true depth visualization is not currently surfaced as an image stream.
- Object detection is currently strongest for boxes / box-like blobs via `classic_contour_fallback`; AprilTag visibility is separate.
- The current template already has most functionality, but it is too cluttered and duplicated.

---

## Scope requested by user

1. Cameras shown small first, then expandable.
2. For each camera: normal image, detection overlay, and depth-related view if available.
3. Clear AprilTag/Aruco visibility status.
4. 3D object visualization, primarily boxes, ideally generalized.
5. Precise 3D arm visualization showing arm, lidar, cameras, and predicted grasp.
6. Separate control page for dog + arm with the practical controls already available.
7. Keep repo/NX deployment clean; avoid sloppy mixed commits.

---

## Implementation strategy

### Phase 1 — UI information architecture only

**Objective:** reduce clutter without changing the grasp/control logic.

**Files likely to change:**
- Modify: `templates/dashboard.html`
- Possibly modify: `diagnostics_dashboard.py` only if a new route/template is cleaner than conditional rendering

**Plan:**
1. Introduce top-level navigation with 3 clear areas:
   - `Overview / Vision`
   - `3D Scene`
   - `Control`
2. Keep the current root dashboard working; avoid deleting old logic in the first pass.
3. Prefer a **new clean landing/page structure** over trying to untangle every existing legacy card in place.
4. Preserve all existing JS ids/endpoints wherever possible to avoid breaking the working polls.

**Acceptance:**
- A user can immediately find camera views, 3D scene, and controls without scanning the whole page.

---

### Phase 2 — Clean Vision page

**Objective:** make camera perception readable.

**Files likely to change:**
- Modify: `templates/dashboard.html`
- Possibly modify: `diagnostics_dashboard.py`

**UI structure:**
1. **Compact camera strip** at top with 4 small tiles:
   - cam0 raw
   - cam0 detection overlay
   - cam6 raw
   - cam6 detection overlay
2. Each tile opens an expanded panel / modal / details area with:
   - larger MJPEG image
   - status badges (`available`, device path, source)
   - detection summary (object visible, tag ids, grip source, confidence)
3. Add a per-camera summary row fed from:
   - `/api/cameras/status`
   - `/api/box/plan`
   - `/api/arm/grasp_pipeline`
4. Explicitly split:
   - **AprilTag/Aruco seen?**
   - **Object/grip seen?**
   - **Which source is active?**
5. Add a truthful label for depth:
   - if no real depth image endpoint exists, label camera 6 as `RealSense RGB + depth model metadata only`
   - do **not** fake a depth panel with RGB data

**Acceptance:**
- User can tell for each camera: what it sees, whether tags are visible, whether an object/grip is visible, and whether that camera is currently trusted.

---

### Phase 3 — 3D object panel

**Objective:** show object(s) in 3D, not just text.

**Files likely to change:**
- Modify: `diagnostics_dashboard.py`
- Modify: `templates/dashboard.html`
- Possibly modify: `scripts/box_grasp_planner.py`

**Plan:**
1. Reuse `/api/arm/scene_3d` as the source of truth.
2. Add explicit `detected_objects` payload if current scene payload is too implicit.
3. Minimum supported object class in 3D:
   - `box`
4. Generalization path:
   - `label`
   - `pose/base_xyz_m`
   - `size_xyz_m` or estimated footprint
   - `source_camera`
   - `confidence`
   - `grasp_center`
   - `grasp_axis`
5. In the viewer, render:
   - oriented box mesh / wireframe for box detections
   - target point
   - optional grasp axis arrow

**Acceptance:**
- A box detection appears as a real 3D object primitive in the viewer, not only as a 2D overlay or text blob.

---

### Phase 4 — Precise 3D grasp scene cleanup

**Objective:** make the existing 3D scene actually operational for grasp review.

**Files likely to change:**
- Modify: `templates/dashboard.html`
- Possibly modify: `diagnostics_dashboard.py`

**Plan:**
1. Keep the existing scene graph / FK / tool-tip logic.
2. Reframe the 3D scene legend and toggles around user tasks:
   - arm links / joints
   - wrist camera
   - front camera
   - lidar XT-16
   - detected object(s)
   - target point
   - grasp trajectory / ghost arm states
3. Highlight the predicted grasp plan clearly:
   - pre-grasp
   - approach
   - grasp
   - lift
4. Add a status card next to the viewer showing:
   - selected camera
   - grip source
   - preview IK OK?
   - object target xyz
   - tool tip xyz
   - stage list
5. Keep calibration sliders hidden by default behind `Advanced alignment`, because they are useful but currently dominate the page.

**Acceptance:**
- From the 3D page the user can understand where arm/lidar/cameras are and what grasp the planner intends to execute.

---

### Phase 5 — Dedicated control page

**Objective:** provide a practical operator page for dog + arm control.

**Files likely to change:**
- Modify: `templates/dashboard.html`
- Possibly add route/template in: `diagnostics_dashboard.py`

**Plan:**
1. Create a dedicated `Control` page/section with only control-relevant actions.
2. Group by subsystem:
   - **Dog base:** crouch, stand, accompany mode, sport connectivity state
   - **Arm motion:** ZERO, START, live joints, hold, teach mode
   - **Manipulation:** start grasp, start after crouch, emergency hold
   - **Manual / drag:** drag-follow modes, diagnostics
3. Add a `WebRTC / robot app parity` card:
   - summarize what is already controllable from dashboard
   - include link/instructions around `scripts/pc_go2_webrtc_crouch.py`
   - if no embedded WebRTC control exists, say so explicitly instead of implying it does
4. Keep dangerous actions visually separated from read-only actions.

**Acceptance:**
- A human operator can use one page to drive the practical base/arm actions already implemented in the repo.

---

### Phase 6 — Clean commit sequence

**Objective:** avoid another mixed mega-commit.

**Commit plan:**
1. `feat(dashboard): add mission-control information architecture`
2. `feat(vision): add compact expandable camera perception panels`
3. `feat(scene): surface detected objects and grasp preview in 3d`
4. `feat(control): add dedicated dog and arm control page`
5. `chore(dashboard): hide advanced calibration behind expert panels`

**Rules:**
- Do not mix API payload changes with large CSS/HTML refactors unless the UI cannot ship without them.
- Run smoke tests after each meaningful step.
- Deploy to NX only after local template/API smoke passes.

---

## Validation

Run locally after each phase:

```bash
python3 scripts/test_dashboard_smoke.py
python3 scripts/test_grasp_overlay.py
python3 scripts/test_camera_usb_mapping.py
```

Run live against NX after deploy:

```bash
python3 scripts/verify_nx_dashboard_apis.py http://192.168.123.18:5050
```

Manual operator checks on NX:
1. Root page loads.
2. Cameras 0 and 6 are visible in compact strip.
3. Expanded panels show raw + overlay correctly.
4. AprilTag state is visible even when no object is detected.
5. 3D scene shows arm, cameras, lidar, and grasp preview.
6. Control page exposes crouch/stand + arm controls without the rest of the diagnostic clutter.

---

## Open questions to resolve during implementation

1. Should the new clean UI replace `/` immediately, or should it ship first as `/mission-control` while old `/` remains as fallback?
2. Do we want to add a real depth-image endpoint for RealSense now, or explicitly defer it and keep only RGB + 3D depth metadata?
3. For 3D object visualization, is `box` enough for the first cleanup pass, with later generalization to cylinders/packages?

## Recommended execution order right now

1. Implement a new clean page structure **without changing backend planner logic**.
2. Reuse existing endpoints first.
3. Only then add missing API fields for explicit 3D object rendering.
4. Deploy to NX and verify visually.
