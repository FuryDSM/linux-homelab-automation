#!/usr/bin/env python3
"""Check repository text for common credential and private-identifier patterns."""
import ipaddress
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "private key": re.compile(r"-{5}BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY"),
    "GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    "webhook credential": re.compile(r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]{12,}"),
    "cloud access key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "personal network domain": re.compile(r"\b[a-zA-Z0-9.-]+\.(?:duckdns\.org|airdns\.org|ts\.net)\b"),
    "MAC address": re.compile(r"\b(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}\b"),
    "machine UUID": re.compile(r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b"),
    "credential assignment": re.compile(r'''(?i)(?:api[_-]?key|password|webhook|access[_-]?token)["']?\s*[:=]\s*["'][A-Za-z0-9_./+:-]{16,}["']'''),
}
IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")


def inspect_text(text):
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        for kind, pattern in PATTERNS.items():
            if pattern.search(line):
                findings.append((number, kind))
        for match in IP.finditer(line):
            try:
                address = ipaddress.ip_address(match.group())
            except ValueError:
                continue
            allowed = address.is_loopback or any(address in ipaddress.ip_network(net) for net in
                      ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"))
            if not allowed:
                findings.append((number, "non-example IP address"))
        for match in EMAIL.finditer(line):
            if match.group(1).lower() not in {"example.com", "example.org", "example.net"}:
                findings.append((number, "non-example email address"))
    return findings


def publication_files(root):
    if (root / ".git").exists():
        output = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                                cwd=root, capture_output=True, check=True).stdout
        return [root / name.decode("utf-8") for name in output.split(b"\0") if name]
    return [p for p in root.rglob("*") if p.is_file() and
            not set(p.relative_to(root).parts) & {".git", "__pycache__", ".venv"}]


def main():
    findings = []
    files = publication_files(ROOT)
    for path in files:
        relative = path.relative_to(ROOT).as_posix()
        if path.is_symlink():
            findings.append((relative, 0, "symlink"))
            continue
        if path.suffix.lower() in {".db", ".sql", ".log", ".zip", ".bin", ".pem", ".key", ".pyc"} or path.name.startswith(".env") or ".local." in path.name:
            findings.append((relative, 0, "private/runtime file"))
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append((relative, 0, "unreviewed binary file"))
            continue
        findings.extend((relative, line, kind) for line, kind in inspect_text(text))
    for path, line, kind in findings:
        print(f"{path}:{line}: {kind}")
    print(f"Checked {len(files)} files; {len(findings)} potential findings. Pattern checks are not a security guarantee.")
    return bool(findings)


if __name__ == "__main__":
    sys.exit(main())
