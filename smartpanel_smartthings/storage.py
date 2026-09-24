"""File-backed key/value store shared by every process on the server.

Gunicorn runs two web workers and the background push worker is a third
process, so a threading.Lock is not enough: every operation takes an
exclusive fcntl lock on a sidecar ".lock" file, re-reads the JSON from
disk, applies the change and writes it back atomically (temp file +
os.replace). No process ever writes back a stale copy of the data.
"""

import contextlib
import fcntl
import json
import os
import tempfile


class TokenStore:
    def __init__(self, path: str):
        self.path = path
        self._lock_path = f"{path}.lock"

    @contextlib.contextmanager
    def _locked(self):
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _load(self) -> dict:
        try:
            with open(self.path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _save(self, data: dict):
        directory = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def set(self, key: str, value):
        with self._locked():
            data = self._load()
            data[key] = value
            self._save(data)

    def get(self, key: str, default=None):
        with self._locked():
            return self._load().get(key, default)

    def delete(self, *keys: str):
        with self._locked():
            data = self._load()
            if any(k in data for k in keys):
                for k in keys:
                    data.pop(k, None)
                self._save(data)

    def update(self, key: str, fn, default=None):
        """Atomically read-modify-write one entry.

        fn(old) returns the new value; returning None removes the key.
        """
        with self._locked():
            data = self._load()
            new = fn(data.get(key, default))
            if new is None:
                data.pop(key, None)
            else:
                data[key] = new
            self._save(data)
            return new

    def delete_where(self, predicate):
        """Delete every entry for which predicate(key, value) is true."""
        with self._locked():
            data = self._load()
            doomed = [k for k, v in data.items() if predicate(k, v)]
            for k in doomed:
                del data[k]
            if doomed:
                self._save(data)
            return doomed

    def all(self) -> dict:
        with self._locked():
            return self._load()
