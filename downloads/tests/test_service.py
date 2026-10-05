import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from unittest import TestCase
from unittest.mock import patch
from dataclasses import replace
from django.conf import settings

import service_runner


class ServiceTests(TestCase):
    def test_private_generator_permissions_and_refusal_to_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "chapter-config.json"
            service_runner.create_config(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            original = path.read_bytes()
            values = json.loads(original)
            self.assertGreaterEqual(len(values["CHAPTER_MASTER_PASSWORD"]), 16)
            self.assertEqual(values["CHAPTER_S3_ENDPOINT_URL"], "")
            with self.assertRaises(FileExistsError):
                service_runner.create_config(path)
            self.assertEqual(path.read_bytes(), original)

    def test_missing_configuration_never_launches_services(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors), patch("service_runner.subprocess.Popen") as launch:
                result = service_runner.main(["--config", str(Path(directory) / "missing.json")])
            self.assertEqual(result, 1)
            launch.assert_not_called()
            self.assertIn("CHAPTER_CONFIG_FILE", errors.getvalue())

    def test_start_setup_preflight_order_hosts_and_launcher_lock(self):
        import fcntl
        from downloads.config import prepare_directories
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = replace(settings.CHAPTER_CONFIG, runtime_dir=root, cache_dir=root / "cache")
            prepare_directories(config)
            with patch("downloads.config.load_configuration", return_value=config), patch(
                "service_runner.run_once"
            ) as command, patch("service_runner.supervise", return_value=0) as start:
                self.assertEqual(service_runner.main(["--allowed-host", "10.121.15.2", "--allowed-host", "localhost"]), 0)
                self.assertEqual([call.args[0][2] for call in command.call_args_list], ["check", "migrate"])
                self.assertEqual(start.call_args.args[1]["ALLOWED_HOSTS"], "10.121.15.2,localhost")
                self.assertEqual([child[2] for child in start.call_args.args[0]], ["run_huey", "runserver"])
                command.reset_mock()
                start.reset_mock()
                self.assertEqual(service_runner.main(["setup"]), 0)
                self.assertEqual([call.args[0][2] for call in command.call_args_list],
                                 ["check", "migrate", "check_chapter_storage", "createsuperuser"])
                start.assert_not_called()
                with open(str(root / "service.lock"), "a+b") as lock, contextlib.redirect_stderr(io.StringIO()):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    command.reset_mock()
                    self.assertEqual(service_runner.main([]), 1)
                    command.assert_not_called()

    def test_child_exit_and_launch_failure_reap_remaining_process(self):
        live = [sys.executable, "-c", "import signal,sys,time; signal.signal(signal.SIGINT,lambda *_:sys.exit(0)); time.sleep(30)"]
        failed = [sys.executable, "-c", "raise SystemExit(7)"]
        actual_popen = subprocess.Popen
        for second in (failed, ["/nonexistent/cubari-test-command"]):
            children = []
            def start(*args, **kwargs):
                child = actual_popen(*args, **kwargs)
                children.append(child)
                return child
            with patch("service_runner.subprocess.Popen", side_effect=start), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                try:
                    result = service_runner.supervise([live, second], {}, grace=0.2)
                    self.assertEqual(result, 1)
                except OSError:
                    self.assertEqual(second[0], "/nonexistent/cubari-test-command")
            self.assertTrue(all(child.poll() is not None for child in children))

    def test_ctrl_c_and_service_stop_reap_both_children(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            with tempfile.TemporaryDirectory() as directory:
                child_script = "import signal,sys,time; signal.signal(signal.SIGINT,lambda *_:sys.exit(0)); time.sleep(30)"
                # Record PIDs before supervision begins polling, without invoking Django/Huey.
                script = '''
import json, os, sys
import service_runner
original = service_runner.subprocess.Popen
children = []
def start(*args, **kwargs):
    child = original(*args, **kwargs)
    children.append(child.pid)
    if len(children) == 2:
        with open(sys.argv[1], "w") as file: json.dump(children, file)
    return child
service_runner.subprocess.Popen = start
command = [sys.executable, "-c", sys.argv[2]]
sys.exit(service_runner.supervise([command, command], {}, grace=0.2))
'''
                path = Path(directory) / "pids.json"
                parent = subprocess.Popen([sys.executable, "-c", script, str(path), child_script],
                                          cwd=str(service_runner.REPO), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 5
                    while not path.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(path.exists())
                    while True:
                        try:
                            pids = json.loads(path.read_text())
                            break
                        except json.JSONDecodeError:
                            if time.monotonic() >= deadline:
                                raise
                            time.sleep(0.02)
                    parent.send_signal(signum)
                    parent.communicate(timeout=8)
                    self.assertEqual(parent.returncode, 0)
                    for pid in pids:
                        with self.assertRaises(ProcessLookupError):
                            os.kill(pid, 0)
                finally:
                    if parent.poll() is None:
                        parent.kill()
                        parent.communicate()
