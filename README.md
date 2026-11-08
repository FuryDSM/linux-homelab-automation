# Linux Homelab Automation

Linux server monitoring, alerting and backup tools built with Python, Bash and systemd.

The homelab runs containerized applications on Ubuntu, with private remote administration and HTTPS access to selected services. A separate Debian device provides DNS filtering and Wake-on-LAN. This repository presents a portable monitoring and backup toolkit based on that environment, using example configuration throughout.

## What is included

- Read-only Docker status checks for explicitly selected containers.
- Disk capacity and required-mount checks.
- HTTP availability and certificate-expiry checks with TLS validation enabled.
- Optional counts of pending Ubuntu/Debian package updates from existing package indexes.
- Alerts after three consecutive failures, recovery after two successful checks, and retry after notification delivery fails.
- Optional Discord notifications with all mentions disabled and credentials supplied at runtime.
- Atomic private state/report files, a systemd user timer, an offline demonstration, and automated tests.
- An attended Restic backup script that attempts to restore the original running-container set after the backup completes or fails.

The monitor does not install updates, restart services, reboot the machine, or delete files. Those actions belong to a separately reviewed maintenance window.

## Try the offline demonstration

Requires Python 3.10 or later. The monitor uses only the standard library.

```bash
python3 src/homelab_monitor.py --demo
python3 -m unittest discover -s tests -v
python3 scripts/check_publication.py
```

The demo prints clearly labeled synthetic results. It does not inspect the host, contact any service, save state, or send messages. Tests use temporary directories and mocked checks; they do not touch a live server.

## Configure a Linux installation

1. Copy `config/monitor.example.json` to `config/monitor.local.json`.
2. Replace the example container, disk and endpoint entries with services you administer. Remove unused checks. Use generic check IDs: they appear in reports and notifications.
3. Run one collection manually:

   ```bash
   python3 src/homelab_monitor.py --config config/monitor.local.json
   ```

4. Inspect the JSON report under `~/.local/state/linux-homelab-automation/`. Live reports and configuration stay outside version control.
5. Follow [installation and maintenance](docs/operations.md) before enabling the timer or notifications.

HTTP checks do not follow redirects. Set the exact final health URL and expected status code. Do not put credentials, query strings, or fragments in check URLs. HTTPS checks validate the server certificate. Checks observe connectivity from the monitoring host, not every external client's connection.

Docker access must already be configured for the operator. Membership in the Docker group is powerful; this project does not grant it or mount a Docker socket into a web application.

## Project guide

| File | Purpose |
| --- | --- |
| [Architecture](docs/architecture.md) | What the private server does and how the portable monitor fits into it |
| [Operations](docs/operations.md) | Installation, alert setup, maintenance schedule and recovery checklist |
| `src/homelab_monitor.py` | Configurable monitoring and alert state machine |
| `scripts/backup.sh` | Explicitly authorized cold backups with container recovery |
| `tests/test_monitor.py` | Failure, recovery, privacy and delivery regression tests |
| `systemd/` | Optional recurring execution without a web control endpoint |

## Scope

This edition uses example configuration and synthetic demo data. The portable monitor is an adaptation of the homelab implementation; it is tested separately from the running server. DNS, remote access and application services are described in the architecture guide and are not provisioned by these scripts.

The backup script requires an operator-configured Restic repository and an attended maintenance window. A successful integrity check does not replace an application restore test. Monitoring results describe observations from one host and do not guarantee external availability.
