# Go2 Dashboard Context

- Main dashboard: `scripts/serve_focus_dashboard.py`
- Main URL on the NX: `http://192.168.123.18:5056/`
- The five-tab UI is `templates/focus_dashboard.html`.
- Do not confuse the NX dashboard with the arm/controller host `192.168.123.161`.
- Legacy dashboard files are archived under `old/focus_dashboard_snapshot/`.
- If `serve_dashboard_lite.py` is running on 5056, that is the older/operator dashboard, not the main focus dashboard.

Keep this file as the first source of truth for LLMs working in this repo.

## D1 arm safety invariant — never regress

- The arm must use the process-independent `scripts/d1_hold_daemon.py` as the
  sole owner of `bin/d1_sdk_command`. Flask/dashboard processes are clients via
  `go2_dashboard/d1_hold_client.py`; they must never own or kill the DDS writer.
- On the NX, `D1_HOLD_DAEMON_EXTERNAL=1` and
  `D1_INFER_COUPLED_ON_FEEDBACK=0` are mandatory. Servo feedback proves only
  reachability, never torque/coupling.
- A safe coupled state requires all of: daemon reachable, publisher alive,
  funcode 5 mode 1 asserted, cached funcode 2 pose, and a fresh heartbeat.
  Use `hold_daemon_status()["hold_active"]`; never trust a standalone boolean.
- All funcodes, including 6, 5, 2, 7 and explicit release, must traverse the
  external daemon. Do not add one-shot DDS publishers or a second writer.
- Never call `pkill d1_sdk_command`, `stop_command_daemon()`, or restart the D1
  hold daemon while coupled. Dashboard restarts must leave the hold daemon PID
  and heartbeat unchanged.
- Funcode 5 mode 0 is allowed only after an explicit user release request and
  confirmation that the arm is physically supported. It must not be emitted by
  cleanup, exception, timeout, deploy, page change, or emergency-hold code.
- Before any physical movement or hold-daemon maintenance, verify the arm is
  supported and inspect `/api/arm/status` for `hold_active=true`.
- Required regression tests: `scripts/test_d1_hold_daemon.py`,
  `scripts/test_d1_hold_service_guards.py`, `scripts/test_d1_ensure_coupled.py`,
  and `scripts/test_d1_motion_path_guards.py`.
