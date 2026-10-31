"""Persistent freeze ledger: which apps are frozen, and how to resume them.

Safety properties (A owns the "never leave apps frozen" invariant):
- Atomic writes: tmp file + os.replace, so a crash leaves old or new, never half.
- Locked: a thread lock for threads plus a lockfile for processes, so the
  consent card and the pressure guardian can use the ledger at the same time.
- Signed: HMAC-SHA256 over the entries, key in DPAPI (T3). A bad HMAC puts the
  ledger in read-only mode: new freezes are refused, but thawing still works
  because thawing is always safe. Entries with a live PID whose create time no
  longer matches are NEVER dropped (PID reuse guard); only confirmed-dead PIDs
  are pruned, and only on the thaw path.

Layout (FF_HOME, same convention as freezer.cost/sessions):
    ledger.json  {"v": 1, "entries": [...], "hmac": "<hex>"}
    ledger.lock  lockfile (content meaningless)
    ledger.key   DPAPI blob wrapping the 32-byte HMAC key
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Iterator

from freezer.contracts import LedgerEntry, ProcRef
from freezer.cost import home

log = logging.getLogger(__name__)

LEDGER_VERSION = 1
LOCK_TIMEOUT_S = 10.0
_KEY_BYTES = 32

_threads = threading.RLock()


class LedgerTampered(Exception):
    """Raised when a freeze is attempted while the ledger HMAC does not verify."""


def ledger_path() -> Path:
    return home() / "ledger.json"


def lock_path() -> Path:
    return home() / "ledger.lock"


def key_path() -> Path:
    return home() / "ledger.key"


# ---- cross-process file lock -------------------------------------------------

@contextlib.contextmanager
def _file_lock(timeout_s: float = LOCK_TIMEOUT_S) -> Iterator[None]:
    """Hold an exclusive lock on the ledger lockfile (cross-process)."""
    home().mkdir(parents=True, exist_ok=True)
    p = lock_path()
    fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\x00")
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            deadline = time.monotonic() + timeout_s
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"timed out waiting for {p}")
                    time.sleep(0.02)
        else:
            import fcntl

            deadline = time.monotonic() + timeout_s
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"timed out waiting for {p}")
                    time.sleep(0.02)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            os.close(fd)


@contextlib.contextmanager
def locked(timeout_s: float = LOCK_TIMEOUT_S) -> Iterator[None]:
    """Hold the ledger lock against both threads and other processes."""
    with _threads:
        with _file_lock(timeout_s):
            yield


# ---- HMAC key in DPAPI ---------------------------------------------------------

def _dpapi_protect(data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.c_void_p)]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buf = ctypes.create_string_buffer(data)
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.c_void_p))
    blob_out = DATA_BLOB()
    if not crypt32.CryptProtectData(ctypes.byref(blob_in), None, None, None, None, 0,
                                    ctypes.byref(blob_out)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(blob: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.c_void_p)]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buf = ctypes.create_string_buffer(blob)
    blob_in = DATA_BLOB(len(blob), ctypes.cast(buf, ctypes.c_void_p))
    blob_out = DATA_BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0,
                                      ctypes.byref(blob_out)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def load_key() -> bytes:
    """Load the HMAC key, generating and DPAPI-protecting it on first run."""
    p = key_path()
    if p.exists():
        blob = p.read_bytes()
        if os.name == "nt":
            return _dpapi_unprotect(blob)
        return blob  # non-Windows dev fallback: raw key file, mode 0600
    key = os.urandom(_KEY_BYTES)
    p.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        p.write_bytes(_dpapi_protect(key))
    else:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
    return key


# ---- (de)serialization ---------------------------------------------------------

def _encode(entries: dict[str, LedgerEntry]) -> bytes:
    body = {"v": LEDGER_VERSION,
            "entries": [asdict(e) for e in entries.values()]}
    raw = json.dumps(body, sort_keys=True).encode("utf-8")
    tag = hmac.new(load_key(), raw, hashlib.sha256).hexdigest()
    return json.dumps({**body, "hmac": tag}, indent=1).encode("utf-8")


def _decode(data: bytes) -> tuple[dict[str, LedgerEntry], bool]:
    """Return (entries, ok). ok=False means the HMAC did not verify."""
    try:
        doc = json.loads(data.decode("utf-8"))
        tag = doc.pop("hmac")
        raw = json.dumps(doc, sort_keys=True).encode("utf-8")
    except (ValueError, KeyError, AttributeError):
        return {}, False
    if not hmac.compare_digest(tag, hmac.new(load_key(), raw, hashlib.sha256).hexdigest()):
        log.error("ledger_tampered: HMAC mismatch on %s", ledger_path())
        entries: dict[str, LedgerEntry] = {}
        try:
            for e in doc.get("entries", []):
                entries[e["app_id"]] = LedgerEntry(
                    e["app_id"], [ProcRef(**p) for p in e["procs"]],
                    e["frozen_at"], e["freed_mb"], e["session"])
        except (KeyError, TypeError):
            pass
        return entries, False
    out: dict[str, LedgerEntry] = {}
    for e in doc.get("entries", []):
        out[e["app_id"]] = LedgerEntry(
            e["app_id"], [ProcRef(**p) for p in e["procs"]],
            e["frozen_at"], e["freed_mb"], e["session"])
    return out, True


# ---- the ledger ------------------------------------------------------------------

class Ledger:
    """Thread- and process-safe signed store of frozen apps."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or ledger_path()
        self._tampered = False

    @property
    def tampered(self) -> bool:
        return self._tampered

    def _read_unlocked(self) -> dict[str, LedgerEntry]:
        if not self._path.exists():
            return {}
        entries, ok = _decode(self._path.read_bytes())
        if not ok:
            self._tampered = True
        return entries

    def _write_unlocked(self, entries: dict[str, LedgerEntry]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_bytes(_encode(entries))
        os.replace(tmp, self._path)

    def load(self) -> dict[str, LedgerEntry]:
        """Read all entries. Works in read-only (tampered) mode too."""
        with locked():
            return self._read_unlocked()

    def _mutate(self, fn: Callable[[dict[str, LedgerEntry]], None],
                *, thaw_path: bool = False) -> dict[str, LedgerEntry]:
        with locked():
            entries = self._read_unlocked()
            if self._tampered and not thaw_path:
                raise LedgerTampered(
                    "ledger HMAC mismatch: refusing new freezes, thaw_all only")
            fn(entries)
            self._write_unlocked(entries)
            if thaw_path:
                self._tampered = False  # thaw rewrote a freshly signed ledger
            return entries

    def add(self, entry: LedgerEntry) -> None:
        def fn(entries: dict[str, LedgerEntry]) -> None:
            entries[entry.app_id] = entry
        self._mutate(fn)

    def update(self, entry: LedgerEntry) -> None:
        def fn(entries: dict[str, LedgerEntry]) -> None:
            entries[entry.app_id] = entry
        self._mutate(fn)

    def remove(self, app_id: str) -> None:
        def fn(entries: dict[str, LedgerEntry]) -> None:
            entries.pop(app_id, None)
        self._mutate(fn)

    def save_thawed(self, entries: dict[str, LedgerEntry]) -> None:
        """Write back the entries left over by thaw_all. Allowed in tamper mode:
        thawing is always safe, and entries are re-verified (PID+ctime) on
        every thaw, so a re-signed leftover cannot trick a future resume."""
        was_tampered = self._tampered
        self._mutate(lambda cur: (cur.clear(), cur.update(entries)), thaw_path=True)
        if was_tampered:
            log.warning("ledger_tampered: thaw rewrote the ledger, clearing tamper flag")

    def clear(self) -> None:
        self.save_thawed({})
