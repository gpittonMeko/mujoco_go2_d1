#!/usr/bin/env python3
"""CLI unificata per smoke test di laboratorio (dashboard NX, arm, Hermes, worker VLA).

Sostituisce i singoli ``scripts/verify_*.py`` (restano wrapper di compatibilità).

Esempi:
  python scripts/verify_go2_lab.py dashboard http://192.168.123.18:5052
  python scripts/verify_go2_lab.py dashboard-nx http://192.168.123.18:5052
  python scripts/verify_go2_lab.py hermes --http --url http://192.168.123.18:5052
  python scripts/verify_go2_lab.py hermes --integration --url http://192.168.123.18:5052
  python scripts/verify_go2_lab.py arm move http://192.168.123.18:5052 --dry-run
  python scripts/verify_go2_lab.py arm scene3d --base http://192.168.123.18:5052
  python scripts/verify_go2_lab.py grasp-coach http://192.168.123.18:5052 --step
  python scripts/verify_go2_lab.py voice http://192.168.123.18:5052
  python scripts/verify_go2_lab.py worker http://192.168.123.3:8765
  python scripts/verify_go2_lab.py worker --aws https://host:8765 --token SECRET
  python scripts/verify_go2_lab.py openvla-env
  python scripts/verify_go2_lab.py quick http://192.168.123.18:5052
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_SCRIPTS = Path(__file__).resolve().parent
_DEFAULT_NX = "http://192.168.123.18:5052"


def _load(script: str) -> ModuleType:
    path = _SCRIPTS / script
    if not path.is_file():
        raise FileNotFoundError(path)
    name = script.replace(".py", "").replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(script: str, argv: list[str]) -> int:
    mod = _load(script)
    main = getattr(mod, "main", None)
    if not callable(main):
        print(f"{script}: no main()", file=sys.stderr)
        return 1
    old = sys.argv
    try:
        sys.argv = [script] + argv
        return int(main())
    finally:
        sys.argv = old


def _cmd_dashboard(args: argparse.Namespace) -> int:
    url = args.url or _DEFAULT_NX
    if args.nx_apis:
        return _run("verify_nx_dashboard_apis.py", [url])
    return _run("verify_dashboard_http.py", [url])


def _cmd_hermes(args: argparse.Namespace) -> int:
    argv: list[str] = []
    if args.url:
        argv.extend(["--url", args.url])
    if args.live_openai:
        argv.append("--live-openai")
    if args.probe_depth:
        argv.append("--probe-depth-previews")
    if args.integration:
        if not args.url:
            print("hermes --integration requires --url", file=sys.stderr)
            return 2
        return _run("verify_hermes_integration.py", argv)
    if args.http:
        return _run("verify_hermes_smoke.py", argv)
    if args.offline or not argv:
        return _run("verify_hermes_smoke.py", [])
    return _run("verify_hermes_smoke.py", argv)


def _cmd_arm(args: argparse.Namespace) -> int:
    if args.arm_action == "move":
        argv = list(args.rest)
        if args.dry_run:
            argv.append("--dry-run")
        return _run("verify_d1_arm_small_move_http.py", argv)
    if args.arm_action == "scene3d":
        argv = ["--base", args.base or _DEFAULT_NX]
        argv.extend(args.rest)
        return _run("verify_arm_scene3d_realtime.py", argv)
    print("arm: use 'move' or 'scene3d'", file=sys.stderr)
    return 2


def _cmd_grasp_coach(args: argparse.Namespace) -> int:
    argv = [args.url or _DEFAULT_NX]
    if args.step:
        argv.append("--step")
    if args.execute:
        argv.append("--execute")
    if args.feedback:
        argv.append("--feedback")
    return _run("verify_grasp_coach_http.py", argv)


def _cmd_voice(args: argparse.Namespace) -> int:
    argv = [args.url or _DEFAULT_NX]
    if args.mode:
        argv.extend(["--mode", args.mode])
    if args.text:
        argv.extend(["--text", args.text])
    return _run("verify_go2_voice_playback.py", argv)


def _cmd_worker(args: argparse.Namespace) -> int:
    if args.aws:
        argv = [args.url] if args.url else []
        if args.token:
            argv.extend(["--token", args.token])
        if args.dual_jpeg:
            argv.extend(["--dual-jpeg", args.dual_jpeg[0], args.dual_jpeg[1]])
        if args.instruction:
            argv.extend(["--instruction", args.instruction])
        if args.timeout:
            argv.extend(["--timeout", str(args.timeout)])
        return _run("verify_aws_vla_worker.py", argv)
    url = args.url or ""
    return _run("verify_anygrasp_worker_http.py", [url] if url else [])


def _cmd_quick(args: argparse.Namespace) -> int:
    url = args.url or _DEFAULT_NX
    rc = _run("verify_dashboard_http.py", [url])
    if rc != 0:
        return rc
    return _run("verify_hermes_smoke.py", ["--url", url])


def _cmd_nx(args: argparse.Namespace) -> int:
    url = args.url or _DEFAULT_NX
    rc = _run("verify_dashboard_http.py", [url])
    if rc != 0:
        return rc
    rc = _run("verify_nx_dashboard_apis.py", [url])
    if rc != 0:
        return rc
    return _run("verify_hermes_integration.py", ["--url", url])


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    parser = argparse.ArgumentParser(description="Go2 lab verification (unified)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_dash = sub.add_parser("dashboard", help="GET /api/health + home")
    p_dash.add_argument("url", nargs="?", default=None)
    p_dash.add_argument("--nx-apis", action="store_true", help="include sport/status APIs")
    p_dash.set_defaults(func=_cmd_dashboard)

    p_q = sub.add_parser("quick", help="dashboard + hermes HTTP dry")
    p_q.add_argument("url", nargs="?", default=None)
    p_q.set_defaults(func=_cmd_quick)

    p_nx = sub.add_parser("nx", help="dashboard + nx-apis + hermes integration")
    p_nx.add_argument("url", nargs="?", default=None)
    p_nx.set_defaults(func=_cmd_nx)

    p_h = sub.add_parser("hermes", help="Hermes smoke / integration")
    p_h.add_argument("--url", default=None)
    p_h.add_argument("--offline", action="store_true")
    p_h.add_argument("--http", action="store_true")
    p_h.add_argument("--integration", action="store_true")
    p_h.add_argument("--probe-depth", action="store_true")
    p_h.add_argument("--live-openai", action="store_true")
    p_h.set_defaults(func=_cmd_hermes)

    p_arm = sub.add_parser("arm", help="arm move or scene3d poll")
    p_arm_sub = p_arm.add_subparsers(dest="arm_action", required=True)
    p_move = p_arm_sub.add_parser("move", help="small joint move + servo watch")
    p_move.add_argument("rest", nargs=argparse.REMAINDER)
    p_move.add_argument("--dry-run", action="store_true")
    p_move.set_defaults(func=_cmd_arm, arm_action="move")
    p_s3 = p_arm_sub.add_parser("scene3d", help="poll scene_3d?fast=1")
    p_s3.add_argument("--base", default=None)
    p_s3.add_argument("rest", nargs=argparse.REMAINDER)
    p_s3.set_defaults(func=_cmd_arm, arm_action="scene3d")

    p_gc = sub.add_parser("grasp-coach", help="grasp coach HTTP")
    p_gc.add_argument("url", nargs="?", default=None)
    p_gc.add_argument("--step", action="store_true")
    p_gc.add_argument("--execute", action="store_true")
    p_gc.add_argument("--feedback", action="store_true")
    p_gc.set_defaults(func=_cmd_grasp_coach)

    p_v = sub.add_parser("voice", help="Go2 speaker test")
    p_v.add_argument("url", nargs="?", default=None)
    p_v.add_argument("--mode", choices=("pcm", "ttsmaker"), default=None)
    p_v.add_argument("--text", default=None)
    p_v.set_defaults(func=_cmd_voice)

    p_w = sub.add_parser("worker", help="VLA/AnyGrasp worker :8765")
    p_w.add_argument("url", nargs="?", default=None)
    p_w.add_argument("--aws", action="store_true")
    p_w.add_argument("--token", default=None)
    p_w.add_argument("--dual-jpeg", nargs=2, metavar=("CAM0", "CAM6"))
    p_w.add_argument("--instruction", default=None)
    p_w.add_argument("--timeout", type=float, default=None)
    p_w.set_defaults(func=_cmd_worker)

    p_env = sub.add_parser("openvla-env", help="RTX torch/OpenVLA env (local)")
    p_env.set_defaults(func=lambda _a: _run("verify_openvla_rtx_env.py", []))

    p_dnx = sub.add_parser("dashboard-nx", help="sport/status APIs (read-only)")
    p_dnx.add_argument("url", nargs="?", default=None)
    p_dnx.set_defaults(
        func=lambda a: _run("verify_nx_dashboard_apis.py", [a.url or _DEFAULT_NX])
    )

    if not argv:
        parser.print_help()
        return 2

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
