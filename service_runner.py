"""One foreground supervisor for Django and the single Huey worker (Linux)."""
import argparse
import base64
import fcntl
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parent
DEFAULT_CONFIG = Path.home() / ".config" / "cubarimoe" / "chapter-config.json"


def create_config(path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (path.parent.is_symlink() or path.parent.stat().st_mode & 0o077
            or path.parent.stat().st_uid != os.getuid()):
        raise ValueError("Private configuration directory must be owner-only (0700).")
    values = json.loads((REPO / "chapter-config.example.json").read_text())
    values.update(
        SECRET_KEY=secrets.token_urlsafe(48),
        CHAPTER_MASTER_PASSWORD=secrets.token_urlsafe(32),
        CHAPTER_KDF_SALT=base64.b64encode(secrets.token_bytes(32)).decode(),
        CHAPTER_MAX_ARCHIVE_BYTES=67108864,
        CHAPTER_MAX_IMAGE_BYTES=8388608,
        CHAPTER_CACHE_MAX_BYTES=268435456,
    )
    # O_EXCL refuses existing files/symlinks. Never print or log generated values.
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(values, stream, indent=2)
        stream.write("\n")


def _signal_children(children, signum):
    for child in children:
        if child.poll() is None:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass


def stop_children(children, grace=30):
    _signal_children(children, signal.SIGINT)
    deadline = time.monotonic() + grace
    while any(child.poll() is None for child in children) and time.monotonic() < deadline:
        time.sleep(0.1)
    _signal_children(children, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while any(child.poll() is None for child in children) and time.monotonic() < deadline:
        time.sleep(0.1)
    _signal_children(children, signal.SIGKILL)
    for child in children:
        child.wait()


def supervise(commands, environment, grace=30):
    children = []
    stopping = False
    previous = {}
    def request_stop(signum, frame):
        nonlocal stopping
        stopping = True
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, request_stop)
        for command in commands:
            if stopping:
                return 0
            children.append(subprocess.Popen(command, cwd=str(REPO), env=environment, start_new_session=True))
        print("Web and download worker started. Ctrl+C stops both.", flush=True)
        while not stopping:
            if any(child.poll() is not None for child in children):
                print("A service process exited; stopping the other process.", file=sys.stderr, flush=True)
                return 1
            time.sleep(0.2)
        return 0
    finally:
        stop_children(children, grace)
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def run_once(command, environment):
    """Keep interactive setup attached to the terminal, and reap it on interruption."""
    previous = {}
    child = None
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, signal.default_int_handler)
        child = subprocess.Popen(command, cwd=str(REPO), env=environment)
        result = child.wait()
        if result:
            raise subprocess.CalledProcessError(result, command)
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=("start", "setup", "init-config"), default="start")
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("CHAPTER_CONFIG_FILE", str(DEFAULT_CONFIG))))
    parser.add_argument("--bind", default="0.0.0.0:8010")
    parser.add_argument("--allowed-host", action="append", help="Allowed HTTP hostname/IP; repeat for multiple hosts.")
    args = parser.parse_args(argv)
    if args.action == "init-config":
        try:
            create_config(args.config.absolute())
        except FileExistsError:
            parser.exit(1, "Private configuration already exists; refusing to overwrite it.\n")
        except (OSError, ValueError):
            parser.exit(1, "Private configuration could not be created. Check directory permissions.\n")
        print("Private configuration created; fill in the S3 settings using your editor.")
        return 0
    environment = dict(os.environ)
    environment["CHAPTER_CONFIG_FILE"] = str(args.config.absolute())
    if args.allowed_host:
        environment["ALLOWED_HOSTS"] = ",".join(args.allowed_host)
    manage = [sys.executable, str(REPO / "manage.py")]
    try:
        from downloads.config import load_configuration, prepare_directories
        from django.core.exceptions import ImproperlyConfigured
        config = load_configuration(REPO, environment)
        prepare_directories(config)
    except ImproperlyConfigured as error:
        print(str(error), file=sys.stderr)
        return 1
    with open(str(config.runtime_dir / "service.lock"), "a+b") as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("A combined service is already running.", file=sys.stderr)
            return 1
        try:
            # Idempotent startup preflight; account creation is only the explicit setup action.
            commands = ("check", "migrate") + (("check_chapter_storage",) if args.action == "setup" else ())
            for command in commands:
                extra = ["--run-syncdb", "--noinput"] if command == "migrate" else []
                run_once(manage + [command] + extra, environment)
            if args.action == "setup":
                run_once(manage + ["createsuperuser"], environment)
                return 0
            return supervise([
                manage + ["run_huey"],
                manage + ["runserver", args.bind, "--noreload", "--insecure"],
            ], environment)
        except KeyboardInterrupt:
            return 0
        except (subprocess.CalledProcessError, OSError):
            print("Service startup failed; no processes will be left running.", file=sys.stderr)
            return 1


if __name__ == "__main__":
    sys.exit(main())
