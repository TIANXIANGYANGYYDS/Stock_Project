"""Run a service with bounded stdout/stderr logs, forwarding stop signals.

Use: python -m app.manually_execute_script.run_logged --log PATH -- COMMAND...
The wrapper PID is the service-control PID. Child output, including print and
tracebacks, is rotated together; no copy/truncate race with a live writer.
"""
import argparse
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import signal
import subprocess


def run(command, path, *, max_bytes=20 * 1024**2, backups=5):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(message)s'))
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding='utf-8', errors='replace', bufsize=1)
    previous = {}
    def stop(signum, frame):
        if process.poll() is None:
            process.send_signal(signum)
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, stop)
        for line in process.stdout:
            handler.emit(logging.LogRecord('service', logging.INFO, '', 0, line.rstrip('\n'), (), None))
        return process.wait()
    finally:
        for sig, callback in previous.items():
            signal.signal(sig, callback)
        process.stdout.close()
        handler.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--log', required=True)
    parser.add_argument('--max-bytes', type=int, default=20 * 1024**2)
    parser.add_argument('--backups', type=int, default=5)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command or args.max_bytes <= 0 or args.backups < 1:
        parser.error('command, positive max-bytes and at least one backup required')
    raise SystemExit(run(command, args.log, max_bytes=args.max_bytes, backups=args.backups))


if __name__ == '__main__':
    main()
