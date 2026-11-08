#!/usr/bin/env python3
"""Read-only Linux monitoring; adapted from a private homelab monitor."""
import argparse
import contextlib
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

FAILURES = 3
RECOVERIES = 2
ID_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")


def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError("Configuration must be an object.")
    seen = set()
    for kind in ("containers", "disks", "http", "tls"):
        entries = config.get(kind, [])
        if not isinstance(entries, list) or len(entries) > 20:
            raise ValueError("Each check group must be a list with at most 20 checks.")
        for item in entries:
            if not isinstance(item, dict):
                raise ValueError("Check entries must be objects.")
            identifier = item.get("id", "")
            if not isinstance(identifier, str) or not ID_PATTERN.fullmatch(identifier) or identifier in seen:
                raise ValueError("Check IDs must be unique lowercase aliases.")
            seen.add(identifier)
            if kind == "containers":
                name = item.get("name", "")
                if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", name):
                    raise ValueError("Invalid container name.")
            elif kind == "disks":
                if not isinstance(item.get("path"), str) or not os.path.isabs(item["path"]):
                    raise ValueError("Disk paths must be absolute.")
                if not isinstance(item.get("required_mount", False), bool):
                    raise ValueError("required_mount must be a boolean.")
            elif kind == "http":
                validate_http_url(item.get("url", ""))
                status = item.get("expected_status", 200)
                if type(status) is not int or not 100 <= status <= 599:
                    raise ValueError("Invalid expected HTTP status.")
            else:
                host = item.get("host", "")
                if not isinstance(host, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}", host):
                    raise ValueError("TLS host must be a DNS hostname or IPv4 address.")
                if type(item.get("port", 443)) is not int or not 1 <= item.get("port", 443) <= 65535:
                    raise ValueError("Invalid TLS port.")
    if len(seen) > 16:
        raise ValueError("At most 16 checks can run within one timer window.")
    warning = config.get("disk_warning_percent", 80)
    critical = config.get("disk_critical_percent", 90)
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in (warning, critical)):
        raise ValueError("Disk thresholds must be finite numbers.")
    if not 0 < warning < critical <= 100:
        raise ValueError("Disk thresholds must be ordered percentages.")
    days = config.get("certificate_warning_days", 21)
    if type(days) is not int or not 1 <= days <= 365:
        raise ValueError("Certificate warning must be 1 to 365 days.")
    if not isinstance(config.get("review_package_updates", False), bool):
        raise ValueError("review_package_updates must be a boolean.")
    return config


def validate_http_url(url):
    if not isinstance(url, str):
        raise ValueError("HTTP URL must be text.")
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or any(ord(c) < 32 for c in url)):
        raise ValueError("Use HTTP(S) URLs without credentials, queries or fragments.")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Invalid HTTP port.")
    return url


def run_readonly(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=20, check=False)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def probe_http(url, expected):
    request = urllib.request.Request(url, headers={"User-Agent": "Homelab-Monitor/1.0"})
    try:
        with http_opener().open(request, timeout=10) as response:
            return response.status == expected
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code == expected


def certificate_days(host, port):
    with socket.create_connection((host, port), timeout=10) as raw:
        with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as connection:
            expiry = ssl.cert_time_to_seconds(connection.getpeercert()["notAfter"])
    return math.floor((expiry - time.time()) / 86400)


def check_result(identifier, kind, status, message, **metrics):
    return dict(id=identifier, kind=kind, status=status, message=message, **metrics)


def collect(config):
    checks = []
    entries = config.get("containers", [])
    if entries:
        try:
            command = run_readonly(["docker", "ps", "-a", "--format", "{{json .}}"])
            if command.returncode:
                raise ValueError("Docker status unavailable")
            containers = {row["Names"]: row for row in
                          (json.loads(line) for line in command.stdout.splitlines() if line)}
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
            containers = None
        for item in entries:
            if containers is None:
                status, message = "unknown", "Docker could not be checked; service failure is not confirmed."
            else:
                row = containers.get(item["name"], {})
                if row.get("State") != "running" or "(unhealthy)" in row.get("Status", ""):
                    status, message = "critical", "Container is missing, stopped or unhealthy."
                elif "health: starting" in row.get("Status", ""):
                    status, message = "unknown", "Container health check is still starting."
                else:
                    status, message = "ok", "Container is running; this does not prove application readiness."
            checks.append(check_result(item["id"], "container", status, message))
    for item in config.get("disks", []):
        try:
            if item.get("required_mount", False) and not os.path.ismount(item["path"]):
                checks.append(check_result(item["id"], "disk", "critical", "Required filesystem is not mounted."))
                continue
            usage = shutil.disk_usage(item["path"])
            percentage = round(100 * usage.used / usage.total, 1)
            if percentage >= config.get("disk_critical_percent", 90):
                status, message = "critical", "Disk capacity critical."
            elif percentage >= config.get("disk_warning_percent", 80):
                status, message = "warning", "Disk capacity warning."
            else:
                status, message = "ok", "Disk capacity within thresholds."
            checks.append(check_result(item["id"], "disk", status, message, used_percent=percentage))
        except (OSError, ValueError, ZeroDivisionError):
            checks.append(check_result(item["id"], "disk", "unknown", "Filesystem usage could not be checked."))
    for item in config.get("http", []):
        try:
            ok = probe_http(item["url"], item.get("expected_status", 200))
        except (OSError, ValueError, urllib.error.URLError):
            ok = False
        checks.append(check_result(item["id"], "http", "ok" if ok else "critical",
                                   "Expected HTTP status received." if ok else "Endpoint check failed from this host."))
    for item in config.get("tls", []):
        try:
            days = certificate_days(item["host"], item.get("port", 443))
            status = "warning" if days < config.get("certificate_warning_days", 21) else "ok"
            checks.append(check_result(item["id"], "tls", status, "Certificate validation passed.", certificate_days=days))
        except (OSError, ValueError, KeyError):
            checks.append(check_result(item["id"], "tls", "critical", "TLS connection or certificate validation failed."))
    updates = {"status": "disabled", "reboot_required": Path("/run/reboot-required").exists()}
    if config.get("review_package_updates", False):
        try:
            result = run_readonly(["apt", "list", "--upgradable"])
            if result.returncode:
                raise ValueError("Package query failed")
            updates.update(status="ok", candidate_count=sum("[upgradable from:" in line for line in result.stdout.splitlines()))
        except (OSError, ValueError, subprocess.TimeoutExpired):
            updates["status"] = "unknown"
    return {"mode": "live", "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "checks": checks, "updates": updates}


def plan_alerts(state, checks):
    """Advance counters without acknowledging delivery. Unknown checks are distinct."""
    counters = state.setdefault("counters", {})
    announced = state.setdefault("announced", {})
    present = {check["id"] for check in checks}
    for old in list(counters):
        if old not in present:
            counters.pop(old, None)
            announced.pop(old, None)
    events = []
    for check in checks:
        key = check["id"]
        failing = check["status"] != "ok"
        previous = counters.get(key, 0)
        counters[key] = min(FAILURES, max(0, previous) + 1) if failing else max(-RECOVERIES, min(0, previous) - 1)
        if failing and counters[key] >= FAILURES and announced.get(key) != check["status"]:
            events.append({"id": key, "status": check["status"], "message": check["message"]})
        elif not failing and counters[key] <= -RECOVERIES and key in announced:
            events.append({"id": key, "status": "recovered", "message": "Check recovered."})
    return events


def acknowledge(state, events):
    for event in events:
        if event["status"] == "recovered":
            state["announced"].pop(event["id"], None)
        else:
            state["announced"][event["id"]] = event["status"]


def notification_payload(events):
    lines = ["Homelab status"]
    # Preserve every event's identity when a full set of checks changes at once.
    budget = (1800 - len(lines[0])) // max(1, len(events)) - 1
    for event in events:
        prefix = f"{event['id']} [{event['status']}]: "
        lines.append(prefix + event["message"][:max(0, budget - len(prefix))])
    return {"content": "\n".join(lines), "allowed_mentions": {"parse": []}}


def send_notification(webhook, events):
    parsed = urllib.parse.urlsplit(webhook)
    if (parsed.scheme != "https" or parsed.netloc != "discord.com" or parsed.query or parsed.fragment
            or not re.fullmatch(r"/api/webhooks/[0-9]+/[A-Za-z0-9_-]+", parsed.path)):
        raise ValueError("Invalid notification destination.")
    request = urllib.request.Request(webhook, data=json.dumps(notification_payload(events)).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": "Homelab-Monitor/1.0"})
    with http_opener().open(request, timeout=15) as response:
        if response.status not in (200, 204):
            raise ValueError("Delivery failed.")


def deliver(state, events, sender):
    try:
        sender(events)
    except Exception:
        # Never serialize exceptions: they may contain a credential-bearing URL.
        return False
    acknowledge(state, events)
    return True


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        path.parent.chmod(0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".monitor-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def locked(directory):
    import fcntl  # Linux live mode only; demo and unit tests also work on Windows.
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    with (directory / "monitor.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def load_state(path):
    if not path.exists():
        return {"counters": {}, "announced": {}}
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or not isinstance(state.get("counters"), dict) or not isinstance(state.get("announced"), dict):
        raise ValueError("Invalid state.")
    if any(type(v) is not int for v in state["counters"].values()):
        raise ValueError("Invalid state counters.")
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", action="store_true")
    mode.add_argument("--config", type=Path)
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".local/state/linux-homelab-automation")
    parser.add_argument("--notify", action="store_true", help="Enable Discord delivery using HOMELAB_DISCORD_WEBHOOK.")
    args = parser.parse_args(argv)
    if args.demo:
        if args.notify:
            parser.error("The demo cannot send notifications.")
        demo = Path(__file__).resolve().parents[1] / "examples/demo.json"
        print(demo.read_text(encoding="utf-8"))
        return 0
    if not sys.platform.startswith("linux"):
        parser.error("Live checks require Linux. Use --demo on other platforms.")
    try:
        config = validate_config(json.loads(args.config.read_text(encoding="utf-8")))
        with locked(args.state_dir):
            state_path = args.state_dir / "state.json"
            state = load_state(state_path)
            report = collect(config)
            events = plan_alerts(state, report["checks"])
            report["notifications"] = "disabled"
            if args.notify:
                webhook = os.environ.get("HOMELAB_DISCORD_WEBHOOK", "")
                if not webhook:
                    report["notifications"] = "missing runtime credential"
                elif events:
                    sent = deliver(state, events, lambda batch: send_notification(webhook, batch))
                    report["notifications"] = "sent" if sent else "delivery failed; will retry"
                else:
                    report["notifications"] = "enabled; no new events"
            atomic_json(state_path, state)
            atomic_json(args.state_dir / "report.json", report)
            print(json.dumps(report, indent=2))
        return 0
    except BlockingIOError:
        print("Another monitor run is active.", file=sys.stderr)
        return 0
    except (OSError, ValueError, TypeError):
        print("Monitor could not complete. Check local configuration, state and permissions.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
