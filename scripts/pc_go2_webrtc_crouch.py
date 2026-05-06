#!/usr/bin/env python3
"""
Crouch (Sport StandDown) sul Go2 via **WebRTC** (stesso canale dell’app Unitree),
dal PC sulla **stessa LAN** del robot (STA locale) o in AP.

Dipendenza: ``pip install unitree-webrtc-connect`` (PyPI).

Uso (PowerShell, dalla root del repo):
  python scripts/pc_go2_webrtc_crouch.py --ip 192.168.123.1
  # da PowerShell, connesso al Wi‑Fi del cane (gateway automatico sulla scheda 802.11):
  python scripts/pc_go2_webrtc_crouch.py --ip auto
  # ``auto`` = (1) discovery multicast Unitree, (2) gateway Wi‑Fi, (3) scan /24 su host tipici
  # oppure AP del cane:
  python scripts/pc_go2_webrtc_crouch.py --ap
  # se il gateway AP non è 192.168.12.1 (vedi ``ipconfig``):
  python scripts/pc_go2_webrtc_crouch.py --ap --ap-ip 192.168.54.1
  # Windows: probe su .161 / .1 / .18 / .20 (LAN 123 tipica Unitree)
  powershell -ExecutionPolicy Bypass -File scripts/go2_lab_connect.ps1

Env:
  GO2_WEBRTC_IP    IP del **Go2** in STA locale (signaling :9991 / legacy :8081).
  GO2_WEBRTC_AP_IP  Gateway in modalità AP (default 192.168.12.1 se usi ``--ap``).

Riferimenti: https://github.com/legion1581/unitree_webrtc_connect
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import socket
import subprocess
import sys

# Windows cp1252: la libreria stampa emoji in print_status()
if sys.platform == "win32":
    for _name in ("stdout", "stderr"):
        _s = getattr(sys, _name, None)
        if _s is not None and hasattr(_s, "buffer"):
            setattr(
                sys,
                _name,
                io.TextIOWrapper(_s.buffer, encoding="utf-8", errors="replace"),
            )


async def _sport_step(pub_sub, topic: str, api_id: int) -> dict:
    return await pub_sub.publish_request_new(
        topic,
        options={"api_id": int(api_id), "parameter": ""},
    )


def _default_ap_ip() -> str:
    return (os.environ.get("GO2_WEBRTC_AP_IP") or "192.168.12.1").strip()


def _windows_wifi_adapter_json() -> dict[str, str]:
    """Ritorna gateway IPv4 e indirizzo locale sulla scheda 'tipo Wi‑Fi' (score), come JSON una riga."""
    ps = r"""
$ErrorActionPreference = 'SilentlyContinue'
$best = $null
foreach ($c in Get-NetIPConfiguration) {
    $ad = Get-NetAdapter -InterfaceIndex $c.InterfaceIndex -ErrorAction SilentlyContinue
    if (-not $ad -or $ad.Status -ne 'Up') { continue }
    $alias = $c.InterfaceAlias
    $mt = "$($ad.MediaType) $($ad.PhysicalMediaType)"
    if ($alias -match '(?i)vEthernet|VMware|Virtual|Hyper-V|Loopback|Bluetooth|Teredo') { continue }
    $score = 0
    if ($alias -match '(?i)wi-?fi|wlan|wireless') { $score += 4 }
    if ($mt -match '802\.11') { $score += 3 }
    if ($alias -match '(?i)\bethernet\b') { $score -= 2 }
    $gw = $null
    if ($c.IPv4DefaultGateway) {
        $g0 = @($c.IPv4DefaultGateway)[0]
        if ($g0.NextHop) { $gw = $g0.NextHop.ToString().Trim() }
    }
    $ipv4 = $null
    if ($c.IPv4Address) {
        $a0 = @($c.IPv4Address)[0]
        if ($a0.IPAddress) { $ipv4 = $a0.IPAddress.ToString().Trim() }
    }
    if (-not $ipv4) { continue }
    $o = [PSCustomObject]@{ Score = $score; Gw = $gw; Ipv4 = $ipv4; Alias = $alias }
    if (-not $best -or $o.Score -gt $best.Score) { $best = $o }
}
if ($best) {
    $h = @{ ipv4 = $best.Ipv4; alias = $best.Alias; score = $best.Score }
    if ($best.Gw) { $h['gateway'] = $best.Gw }
    ($h | ConvertTo-Json -Compress)
}
"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        raw = (r.stdout or "").strip()
        if not raw:
            return {}
        return json.loads(raw.splitlines()[-1])
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return {}


def _windows_wifi_default_gateway() -> str | None:
    j = _windows_wifi_adapter_json()
    g = (j.get("gateway") or "").strip()
    return g or None


def _discover_via_multicast(timeout: float = 2.5) -> str | None:
    """IP annunciato dal robot (multicast Unitree); spesso coincide col signaling."""
    try:
        from unitree_webrtc_connect.multicast_scanner import discover_ip_sn
    except ImportError:
        return None
    m = discover_ip_sn(timeout=timeout)
    if not m:
        return None
    ips = [str(x).strip() for x in m.values() if x]
    return ips[0] if ips else None


def _find_signaling_on_subnet(local_ipv4: str, timeout: float = 0.45) -> str | None:
    """Cerca TCP 9991/8081 su host comuni nella stessa /24 del PC (il gateway spesso non è il MCU)."""
    parts = local_ipv4.strip().split(".")
    if len(parts) != 4:
        return None
    try:
        a, b, c, my_last = (int(x) for x in parts)
    except ValueError:
        return None
    base = f"{a}.{b}.{c}."
    last_octets: list[int] = []
    # 161 = computer di bordo Go2 (doc Unitree SDK); 18 spesso Jetson add-on; .1 spesso gateway.
    for lo in (161, 18, 20, 1, 50, 100, 200, 222):
        if lo not in last_octets:
            last_octets.append(lo)
    for delta in (-2, -1, 1, 2):
        lo = my_last + delta
        if 1 <= lo <= 254 and lo not in last_octets:
            last_octets.append(lo)
    for lo in last_octets:
        ip = f"{base}{lo}"
        if _probe_tcp(ip, 9991, timeout=timeout)[0]:
            return ip
        if _probe_tcp(ip, 8081, timeout=timeout)[0]:
            return ip
    return None


def _resolve_ip_auto() -> str | None:
    """Multicast -> gateway/scheda Wi‑Fi -> scan /24 da IPv4 locale."""
    print("--ip auto: provo discovery multicast (2.5s)...")
    ip = _discover_via_multicast(timeout=2.5)
    if ip:
        print(f"--ip auto -> multicast: {ip}")
        return ip

    j = _windows_wifi_adapter_json()
    gw = (j.get("gateway") or "").strip()
    loc = (j.get("ipv4") or "").strip()
    alias = (j.get("alias") or "").strip()
    score = j.get("score")
    if gw:
        print(f"--ip auto -> gateway ({alias!r}, score={score}): {gw}")
        if _probe_tcp(gw, 9991, timeout=0.6)[0] or _probe_tcp(gw, 8081, timeout=0.6)[0]:
            return gw
        print(
            f"--ip auto: il gateway {gw} non ha 9991/8081; "
            "probabile signaling su altro host nella LAN del cane.",
        )
        if gw.startswith("192.168.123."):
            onboard = "192.168.123.161"
            print(f"--ip auto: provo onboard Go2 (doc Unitree): {onboard} …")
            if _probe_tcp(onboard, 9991, timeout=0.6)[0] or _probe_tcp(
                onboard, 8081, timeout=0.6
            )[0]:
                print(f"--ip auto -> {onboard}")
                return onboard
    if loc:
        print(f"--ip auto: scan subnet da PC {loc!r}...")
        found = _find_signaling_on_subnet(loc)
        if found:
            print(f"--ip auto -> trovato signaling: {found}")
            return found
    if gw and not (
        _probe_tcp(gw, 9991, timeout=0.35)[0] or _probe_tcp(gw, 8081, timeout=0.35)[0]
    ):
        print(
            f"Il gateway {gw} non espone signaling. "
            "Prova (doc Unitree LAN 123):  python scripts/pc_go2_webrtc_crouch.py --probe --ip 192.168.123.161 "
            "poi --ip 192.168.123.18 se hai Jetson sulla stessa rete.",
            file=sys.stderr,
        )
    return None


def _resolve_ip_arg(raw: str) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    if s.lower() != "auto":
        return s
    if sys.platform == "win32":
        g = _resolve_ip_auto()
        if g:
            return g
        print(
            "Impossibile trovare host con signaling (9991/8081). "
            "Esegui:  python scripts/pc_go2_webrtc_crouch.py --probe --ip <IP_GO2_o_Jetson>",
            file=sys.stderr,
        )
        return None
    print(
        "--ip auto su questo OS non è implementato; usa --ip <indirizzo>.",
        file=sys.stderr,
    )
    return None


def _probe_tcp(host: str, port: int, timeout: float = 2.0) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "open (TCP accept)"
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def probe_signaling_hosts(hosts: list[str]) -> int:
    print("Probe signaling (come unitree_webrtc_connect: prima 9991, poi 8081)")
    for h in hosts:
        if not h:
            continue
        print(f"  host {h!r}")
        for port in (9991, 8081):
            ok, msg = _probe_tcp(h, port)
            print(f"    {port}: {'OK ' if ok else 'FAIL'} {msg}")
    print(
        "\nSe entrambe FAIL: non sei sul Wi‑Fi giusto, il gateway non è quell’IP, "
        "VPN/firewall Windows blocca, oppure il firmware non espone signaling in AP.\n"
        "Verifica in PowerShell: ipconfig  (Default gateway quando sei sull’AP del cane)\n"
        "Poi: Test-NetConnection <gateway> -Port 9991\n"
        "Su subnet 192.168.123.x prova anche l’onboard Go2: Test-NetConnection 192.168.123.161 -Port 9991"
    )
    return 0


async def _run(
    *,
    use_ap: bool,
    ip: str | None,
    ap_ip: str,
) -> int:
    try:
        from unitree_webrtc_connect import (
            RTC_TOPIC,
            SPORT_CMD,
            UnitreeWebRTCConnection,
            WebRTCConnectionMethod,
        )
    except ImportError:
        print(
            "Manca il pacchetto:  python -m pip install unitree-webrtc-connect",
            file=sys.stderr,
        )
        return 2

    # LocalAP nella libreria forza solo 192.168.12.1; LocalSTA+ip è equivalente e consente override.
    if use_ap:
        sta_ip = ap_ip.strip()
        conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, ip=sta_ip)
        target = f"{sta_ip} (AP / gateway)"
    else:
        if not ip:
            print("Serve --ip <IP_GO2> oppure --ap", file=sys.stderr)
            return 2
        conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, ip=ip.strip())
        target = ip.strip()

    print(f"WebRTC Local{'AP' if use_ap else 'STA'} -> {target}")
    await conn.connect()
    ps = conn.datachannel.pub_sub
    topic = RTC_TOPIC["SPORT_MOD"]

    r_stop = await _sport_step(ps, topic, SPORT_CMD["StopMove"])
    print("StopMove:", json.dumps(r_stop, default=str)[:800])
    r_down = await _sport_step(ps, topic, SPORT_CMD["StandDown"])
    print("StandDown:", json.dumps(r_down, default=str)[:800])

    await conn.disconnect()
    ok = True
    if isinstance(r_down, dict):
        data = r_down.get("data") or {}
        info = data.get("info") if isinstance(data, dict) else {}
        if isinstance(info, dict) and info.get("execution") not in (None, "ok", "OK"):
            ok = False
    print("OK" if ok else "CHECK_RESPONSE")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Go2 crouch via WebRTC (Local STA/AP)")
    ap.add_argument(
        "--ip",
        default=(os.environ.get("GO2_WEBRTC_IP") or "").strip(),
        metavar="ADDR|auto",
        help="IP signaling del Go2, oppure 'auto' (Windows: gateway sulla scheda 802.11). Default: env GO2_WEBRTC_IP",
    )
    ap.add_argument(
        "--ap",
        action="store_true",
        help="Usa gateway AP del cane (default IP: 192.168.12.1 o GO2_WEBRTC_AP_IP / --ap-ip)",
    )
    ap.add_argument(
        "--ap-ip",
        default=_default_ap_ip(),
        metavar="ADDR",
        help="Gateway in modalità AP (default: env GO2_WEBRTC_AP_IP o 192.168.12.1). Usa con --ap.",
    )
    ap.add_argument(
        "--probe",
        action="store_true",
        help="Solo test TCP 9991 e 8081 sugli IP scelti; non esegue crouch",
    )
    args = ap.parse_args()

    resolved_ip = _resolve_ip_arg(args.ip) if (args.ip or "").strip() else None

    if (
        not args.ap
        and (args.ip or "").strip().lower() == "auto"
        and not resolved_ip
    ):
        return 2

    if args.probe:
        hosts: list[str] = []
        if resolved_ip:
            hosts.append(resolved_ip)
        elif (args.ip or "").strip() and (args.ip or "").strip().lower() != "auto":
            hosts.append((args.ip or "").strip())
        if args.ap:
            h = (args.ap_ip or "").strip()
            if h not in hosts:
                hosts.append(h)
        if not hosts:
            hosts = [_default_ap_ip()]
        return probe_signaling_hosts(hosts)

    return asyncio.run(
        _run(
            use_ap=args.ap,
            ip=resolved_ip,
            ap_ip=args.ap_ip or _default_ap_ip(),
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
