import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m = load("monitor", ROOT / "src/homelab_monitor.py")
scanner = load("publication_check", ROOT / "scripts/check_publication.py")


class ConfigurationTests(unittest.TestCase):
    def test_example_is_valid(self):
        m.validate_config(json.loads((ROOT / "config/monitor.example.json").read_text()))

    def test_duplicate_ids_rejected(self):
        with self.assertRaises(ValueError):
            m.validate_config({"disks": [{"id": "same", "path": "/"}, {"id": "same", "path": "/"}]})

    def test_bad_thresholds_rejected(self):
        for warning, critical in [(95, 80), (float("nan"), 90), (0, 90)]:
            with self.subTest(warning=warning), self.assertRaises(ValueError):
                m.validate_config({"disk_warning_percent": warning, "disk_critical_percent": critical})

    def test_url_credentials_and_queries_rejected(self):
        for url in ("http://user:example@localhost/", "https://example.com/?session=example", "file:///tmp/status", "https://example.com/#fragment"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                m.validate_http_url(url)

    def test_alias_cannot_include_mentions(self):
        with self.assertRaises(ValueError):
            m.validate_config({"containers": [{"id": "@everyone", "name": "demo"}]})

    def test_large_configuration_rejected(self):
        with self.assertRaises(ValueError):
            m.validate_config({"http": [{"id": f"check-{n}", "url": "http://localhost/"} for n in range(17)]})


class CollectionTests(unittest.TestCase):
    def test_docker_failure_is_unknown_not_outage(self):
        config = {"containers": [{"id": "service", "name": "private-container"}]}
        with patch.object(m, "run_readonly", return_value=SimpleNamespace(returncode=1, stdout="", stderr="private details")):
            report = m.collect(config)
        self.assertEqual(report["checks"][0]["status"], "unknown")
        self.assertNotIn("private", json.dumps(report))

    def test_container_status_does_not_copy_raw_output(self):
        rows = [{"Names": "private-container", "State": "running", "Status": "Up 1 hour (unhealthy)", "Ports": "private connection data"}]
        with patch.object(m, "run_readonly", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(rows[0]))):
            report = m.collect({"containers": [{"id": "service", "name": "private-container"}]})
        self.assertEqual(report["checks"][0]["status"], "critical")
        self.assertNotIn("private", json.dumps(report))

    def test_missing_mount_not_measured_as_parent_disk(self):
        with patch.object(m.os.path, "ismount", return_value=False), patch.object(m.shutil, "disk_usage") as usage:
            report = m.collect({"disks": [{"id": "data", "path": "/example-data", "required_mount": True}]})
        usage.assert_not_called()
        self.assertEqual(report["checks"][0]["status"], "critical")

    def test_disk_threshold_and_private_path(self):
        with patch.object(m.shutil, "disk_usage", return_value=SimpleNamespace(total=100, used=85)):
            report = m.collect({"disks": [{"id": "data", "path": "/example-private-directory"}]})
        self.assertEqual(report["checks"][0]["status"], "warning")
        self.assertNotIn("example-private-directory", json.dumps(report))

    def test_http_failure_does_not_publish_exception_url(self):
        with patch.object(m, "probe_http", side_effect=OSError("private endpoint information")):
            report = m.collect({"http": [{"id": "web", "url": "http://localhost/"}]})
        self.assertEqual(report["checks"][0]["status"], "critical")
        self.assertNotIn("private", json.dumps(report))

    def test_certificate_expiry_warning(self):
        with patch.object(m, "certificate_days", return_value=7):
            report = m.collect({"tls": [{"id": "tls", "host": "service.example.com"}]})
        self.assertEqual(report["checks"][0]["status"], "warning")
        self.assertNotIn("service.example.com", json.dumps(report))

    def test_tls_validation_failure(self):
        with patch.object(m, "certificate_days", side_effect=m.ssl.SSLError("certificate failed")):
            report = m.collect({"tls": [{"id": "tls", "host": "service.example.com"}]})
        self.assertEqual(report["checks"][0]["status"], "critical")

    def test_update_review_does_not_install(self):
        output = "Listing...\nexample/stable 2 [upgradable from: 1]\n"
        with patch.object(m, "run_readonly", return_value=SimpleNamespace(returncode=0, stdout=output)) as command:
            report = m.collect({"review_package_updates": True})
        command.assert_called_once_with(["apt", "list", "--upgradable"])
        self.assertEqual(report["updates"]["candidate_count"], 1)
        self.assertNotIn("example/stable", json.dumps(report))


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.state = {"counters": {}, "announced": {}}
        self.failure = [{"id": "web", "status": "critical", "message": "Endpoint check failed from this host."}]
        self.healthy = [{"id": "web", "status": "ok", "message": "OK"}]

    def test_three_failures_and_two_recoveries(self):
        self.assertEqual(m.plan_alerts(self.state, self.failure), [])
        self.assertEqual(m.plan_alerts(self.state, self.failure), [])
        events = m.plan_alerts(self.state, self.failure)
        self.assertEqual(len(events), 1)
        m.acknowledge(self.state, events)
        self.assertEqual(m.plan_alerts(self.state, self.failure), [])
        self.assertEqual(m.plan_alerts(self.state, self.healthy), [])
        recovery = m.plan_alerts(self.state, self.healthy)
        self.assertEqual(recovery[0]["status"], "recovered")
        m.acknowledge(self.state, recovery)
        self.assertEqual(m.plan_alerts(self.state, self.healthy), [])

    def test_failure_is_not_acknowledged_when_delivery_fails(self):
        for _ in range(3):
            events = m.plan_alerts(self.state, self.failure)
        def failing_sender(events):
            raise OSError("private delivery credential")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(m.deliver(self.state, events, failing_sender))
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(self.state["announced"], {})
        self.assertEqual(len(m.plan_alerts(self.state, self.failure)), 1)

    def test_all_mentions_disabled(self):
        payload = m.notification_payload(self.failure)
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertNotIn("@everyone", payload["content"])

    def test_large_alert_batch_preserves_every_check(self):
        events = [{"id": f"check-{i:02d}-" + "x" * 39, "status": "critical", "message": "Failure detail " * 20}
                  for i in range(16)]
        payload = m.notification_payload(events)
        self.assertLessEqual(len(payload["content"]), 1800)
        for event in events:
            self.assertIn(event["id"], payload["content"])

    def test_notification_destination_restricted(self):
        for url in ("http://discord.com/api/webhooks/1/example", "https://example.com/api/webhooks/1/example", "https://discord.com.evil.example/api/webhooks/1/example"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                m.send_notification(url, self.failure)

    def test_redirects_disabled(self):
        self.assertIsNone(m.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com/"))

    def test_flapping_check_does_not_alert(self):
        for _ in range(4):
            self.assertEqual(m.plan_alerts(self.state, self.failure), [])
            self.assertEqual(m.plan_alerts(self.state, self.healthy), [])


class StorageAndDemoTests(unittest.TestCase):
    def test_atomic_private_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "report.json"
            m.atomic_json(path, {"ok": True})
            self.assertEqual(json.loads(path.read_text()), {"ok": True})
            self.assertEqual([p.name for p in path.parent.iterdir()], ["report.json"])
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_demo_has_no_live_collection_or_delivery(self):
        with patch.object(m, "collect") as collect, patch.object(m, "send_notification") as send, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(m.main(["--demo"]), 0)
        collect.assert_not_called()
        send.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["mode"], "synthetic demo")

    def test_demo_cannot_notify(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            m.main(["--demo", "--notify"])

    def test_malformed_state_is_not_silently_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text('{"counters": [], "announced": {}}')
            with self.assertRaises(ValueError):
                m.load_state(path)


class PublicationTests(unittest.TestCase):
    def test_json_credential_assignment(self):
        sample = json.dumps({"password": "x" * 24})
        self.assertEqual(scanner.inspect_text(sample), [(1, "credential assignment")])

    def test_secret_detection_does_not_return_secret_value(self):
        sample = "gh" + "p_" + "x" * 32
        findings = scanner.inspect_text(sample)
        self.assertEqual(findings, [(1, "GitHub token")])
        self.assertNotIn(sample, repr(findings))

    def test_real_addresses_rejected_examples_allowed(self):
        address = ".".join(["10", "23", "45", "67"])
        self.assertTrue(scanner.inspect_text(address))
        self.assertEqual(scanner.inspect_text("127.0.0.1 192.0.2.10 user@example.com"), [])


if __name__ == "__main__":
    unittest.main()
