"""Batch rigoroso: 4 acquisizioni sinistra + 4 destra (grasp_coach/preview su NX)."""
import json
import sys
import time

import paramiko

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HOST = "192.168.123.18"
USER = "unitree"
PASS = "123"
BASE = "/home/unitree/go2_visual_dashboard"
PREVIEW_URL = "http://127.0.0.1:5052/api/grasp_coach/preview"
HEALTH_URL = "http://127.0.0.1:5052/api/health"
JOG_HEALTH = "http://127.0.0.1:5053/"


def connect():
    for attempt in range(1, 16):
        try:
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c.connect(HOST, username=USER, password=PASS, timeout=12, banner_timeout=15, auth_timeout=15)
            return c
        except Exception as exc:
            print(f"[ssh] tentativo {attempt}: {exc}")
            time.sleep(2)
    return None


def run(c, cmd, timeout=80):
    _stdin, stdout, stderr = c.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    return out, err


def health_ok(c):
    out, _ = run(c, f"curl -s -m 6 -o /dev/null -w '%{{http_code}}' {HEALTH_URL}", timeout=12)
    return out.strip().endswith("200")


def wait_health(c, label, max_wait_s=90):
    for i in range(max_wait_s // 3):
        if health_ok(c):
            print(f"[health] {label}: 5052 OK dopo {i * 3}s")
            return True
        time.sleep(3)
    print(f"[health] {label}: 5052 NON raggiungibile")
    return False


def preview_once(c, trial_id, side_label):
    py = r"""
import json, sys, urllib.request
url = sys.argv[1]
req = urllib.request.Request(url, data=b'{}', method='POST', headers={'Content-Type': 'application/json'})
try:
    with urllib.request.urlopen(req, timeout=58) as resp:
        body = resp.read().decode('utf-8', errors='replace')
        http = resp.status
except Exception as e:
    print(json.dumps({'http': 0, 'error': str(e), 'ok': False, 'reason': 'http_error'}))
    sys.exit(0)
try:
    d = json.loads(body)
except Exception as e:
    print(json.dumps({'http': http, 'ok': False, 'reason': 'bad_json', 'error': str(e), 'raw_head': body[:300]}))
    sys.exit(0)
mg = d.get('metric_grounding') or {}
det = mg.get('detection') or {}
out = {
    'http': http,
    'ok': bool(d.get('ok')),
    'reason': d.get('reason') or mg.get('reason') or det.get('reason') or '',
    'label_it': (d.get('label_it') or '')[:120],
    'conf': mg.get('confidence'),
    'bbox': mg.get('bbox_xyxy'),
    'ang': mg.get('orientation_deg'),
    'depth_m': mg.get('depth_m'),
    'reach_m': mg.get('reach_m'),
    'reachable': mg.get('reachable'),
    'target': mg.get('target_base_link_m') or mg.get('target_base_link'),
    'object_pixel_norm': mg.get('object_pixel_norm'),
    'detection_ok': mg.get('detection_ok'),
}
print(json.dumps(out))
"""
    cmd = (
        f"python3 -c {json.dumps(py)} {json.dumps(PREVIEW_URL)}"
    )
    out, err = run(c, cmd, timeout=75)
    line = ""
    for raw in out.splitlines():
        raw = raw.strip()
        if raw.startswith("{"):
            line = raw
            break
    if not line:
        result = {
            "trial": trial_id,
            "side": side_label,
            "pass": False,
            "reason": "no_output",
            "stderr": err[:200],
            "stdout": out[:300],
        }
        return result
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return {
            "trial": trial_id,
            "side": side_label,
            "pass": False,
            "reason": "json_parse_error",
            "raw": line[:300],
        }

    http = int(data.get("http") or 0)
    ok = bool(data.get("ok"))
    reason = str(data.get("reason") or "")
    conf = data.get("conf")
    reachable = data.get("reachable")
    depth = data.get("depth_m")
    target = data.get("target")
    norm = data.get("object_pixel_norm")
    u_norm = norm[0] if isinstance(norm, (list, tuple)) and len(norm) >= 1 else None
    y_bl = None
    if isinstance(target, (list, tuple)) and len(target) >= 2:
        y_bl = target[1]
    elif isinstance(target, dict):
        y_bl = target.get("y")

    passed = http == 200 and ok and reachable is not False and conf is not None
    if passed and isinstance(conf, (int, float)) and conf < 0.22:
        passed = False
        reason = reason or "confidence_too_low"

    return {
        "trial": trial_id,
        "side": side_label,
        "pass": passed,
        "http": http,
        "ok": ok,
        "reason": reason,
        "conf": conf,
        "depth_m": depth,
        "reach_m": data.get("reach_m"),
        "reachable": reachable,
        "ang": data.get("ang"),
        "bbox": data.get("bbox"),
        "u_norm": u_norm,
        "target_y_base_link": y_bl,
        "label_it": data.get("label_it"),
    }


def main():
    c = connect()
    if not c:
        print("SSH_UNREACHABLE")
        sys.exit(2)

    # Stato iniziale
    out, _ = run(c, "pgrep -af 'serve_dashboard_lite|serve_d1_jog' | grep -v pgrep || true")
    print("=== processi ===")
    print(out.strip())

    if not wait_health(c, "iniziale"):
        # un solo restart 5052, mai 5053
        run(c, "pkill -f nx_dashboard_supervise.sh; pkill -f serve_dashboard_lite.py; sleep 2; echo killed_5052_only")
        run(c, f"cd {BASE} && nohup bash scripts/nx_dashboard_supervise.sh >> dashboard_supervise.log 2>&1 </dev/null &")
        time.sleep(18)
        if not wait_health(c, "post-restart"):
            c.close()
            sys.exit(3)

    print("attendo 12s stabilizzazione Orbbec...")
    time.sleep(12)

    batches = [
        ("sinistra", 4),
        ("destra", 4),
    ]
    all_results = []

    for side_label, count in batches:
        print(f"\n===== BATCH {side_label.upper()} ({count} prove) =====")
        print(f"(posiziona la scatola a {side_label} se non già fatto — parto tra 8s)")
        time.sleep(8)
        for i in range(1, count + 1):
            trial_id = f"{side_label[0]}{i}"
            if i > 1:
                time.sleep(4)
            if not health_ok(c):
                print(f"[{trial_id}] 5052 down — attendo ripresa...")
                wait_health(c, f"mid-{trial_id}", max_wait_s=60)
                time.sleep(8)
            r = preview_once(c, trial_id, side_label)
            all_results.append(r)
            status = "PASS" if r["pass"] else "FAIL"
            print(
                f"[{trial_id}] {status} http={r.get('http')} ok={r.get('ok')} "
                f"conf={r.get('conf')} depth={r.get('depth_m')} reach={r.get('reach_m')} "
                f"u={r.get('u_norm')} y_bl={r.get('target_y_base_link')} reason={r.get('reason')}"
            )

    # Riepilogo
    print("\n===== RIEPILOGO =====")
    for side in ("sinistra", "destra"):
        subset = [r for r in all_results if r["side"] == side]
        n_pass = sum(1 for r in subset if r["pass"])
        n_fail = len(subset) - n_pass
        print(f"{side}: {n_pass}/4 PASS, {n_fail}/4 FAIL")
        for r in subset:
            print(f"  {r['trial']}: {'OK' if r['pass'] else 'KO'} conf={r.get('conf')} reason={r.get('reason') or '-'}")

    total_pass = sum(1 for r in all_results if r["pass"])
    total_fail = len(all_results) - total_pass
    print(f"\nTOTALE: {total_pass}/8 PASS, {total_fail}/8 FAIL")

    out, _ = run(c, f"curl -s -m 5 -o /dev/null -w '5052=%{{http_code}} 5053=%{{http_code}}\\n' {HEALTH_URL} {JOG_HEALTH}")
    print(out.strip())
    c.close()


if __name__ == "__main__":
    main()
