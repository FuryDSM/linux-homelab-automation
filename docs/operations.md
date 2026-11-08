# Installation and maintenance

## First run

Use an existing Linux account called `user` in the examples below. Replace `/home/user` only in your local installation if necessary. This repository does not create accounts, grant privileges, configure a firewall or alter remote access.

Place the checkout at `/home/user/linux-homelab-automation`. Create `config/monitor.local.json` from the example, then edit it for services you own. Local configuration is ignored by Git. Start with a single HTTP check and add the other checks after confirming the output.

```bash
cd /home/user/linux-homelab-automation
cp config/monitor.example.json config/monitor.local.json
python3 src/homelab_monitor.py --config config/monitor.local.json
```

Optional TLS entries have the form `{"id":"application-tls","host":"service.example.com","port":443}`. The hostname must match the certificate. The example hostname is not a service supplied by this project.

Set `required_mount` on a data-disk check when an absent mount must be detected. Otherwise a path on the root filesystem could look healthy even though the intended data disk is unavailable. Keep the total at or below 16 checks; each network check has a timeout and the user service has a four-minute limit.

Package review reads existing package indexes with `apt list --upgradable`. It does not refresh indexes or install packages. Stale indexes can therefore omit available updates.

## Optional recurring checks

After the manual check works:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/homelab-monitor.service systemd/homelab-monitor.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now homelab-monitor.timer
systemctl --user start homelab-monitor.service
systemctl --user status homelab-monitor.service
```

The supplied units use `%h`, so systemd resolves the current account's home directory. To continue after logout and start without an interactive login, an administrator may enable lingering for the intended account after reviewing that behavior. The repository does not enable lingering automatically.

To stop recurring checks:

```bash
systemctl --user disable --now homelab-monitor.timer
```

An already running check may finish. It only reads service status and writes its own state and report.

## Optional notifications

Notifications are disabled by default. To enable them, supply `HOMELAB_DISCORD_WEBHOOK` through your service manager or a private environment file outside this repository, and add `--notify` to the service's ExecStart using a local systemd override. Restrict the environment file to its owner. Do not paste webhook credentials into tracked files or shell commands retained in history.

Only Discord's HTTPS webhook endpoint is accepted. Redirects are not followed, all mentions are disabled, and errors are reported without printing the credential-bearing URL. Sending still shares your chosen aliases and check statuses with Discord. Use an appropriate private channel.

State files and JSON reports are created with private permissions in a dedicated state directory. Do not serve that directory publicly. The default is `~/.local/state/linux-homelab-automation`; use `--state-dir` only for another dedicated directory, not a shared system folder.

## Backup procedure

`scripts/backup.sh` is an opt-in maintenance tool. It temporarily stops all containers that were running on the selected Docker daemon. It must not be run merely to test the repository. Its exit trap attempts to restart those containers after success, failure or a catchable interruption. A power loss or forced process kill can prevent cleanup.

1. Select an off-server Restic repository and initialize it separately. Keep its credentials and recovery password outside Git.
2. Copy `config/backup-paths.example.txt` to `config/backup-paths.local.txt`. Review every source path, filesystem mount and application consistency requirement. The script rejects missing paths and an empty list.
3. Set `RESTIC_REPOSITORY` and `RESTIC_PASSWORD_FILE` through a protected environment. Confirm the intended Docker context and ensure the backup destination is reachable without the containers that will be stopped.
4. In an attended maintenance window, invoke `scripts/backup.sh --allow-downtime` as root with those variables available. The command prints operational output locally; do not publish it unreviewed.
5. Check the snapshot, container recovery and actual application usage. Rehearse restoring into a separate location. Repository integrity checks alone do not prove an application restore works.

Backup destinations and restore validation are installation-specific. The included tests do not perform a real backup or stop running containers.

## Maintenance schedule

| Interval | Work |
| --- | --- |
| Each check | Review service availability, disk capacity and certificate lifetime. |
| Weekly | Review pending updates, recent errors and backup status. Choose one related service group for an attended update. |
| Monthly | Review disk health and storage growth, retained rollback images and log rotation. Remove only items verified to be unused. |
| Quarterly | Restore a representative backup sample on an isolated machine and check application data. |
| After a planned reboot | Verify remote access, mounts, containers, application login and representative user operations. |

For an update, record current versions, take a consistent backup, review compatibility and migration notes, apply the approved group, and verify the actual user flows. Retain the matching previous image/configuration and data backup for rollback. A newer container image is not necessarily compatible with an older database.

Do not schedule automatic reboots, blanket volume pruning or unreviewed whole-host upgrades as part of this monitor.
