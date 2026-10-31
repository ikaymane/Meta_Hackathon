"""Persistent freeze ledger: which apps are frozen, and how to resume them.

Safety properties (A owns the "never leave apps frozen" invariant):
- Atomic writes: tmp file + os.replace, so a crash leaves old or new, never half.
- Locked: a thread lock for threads plus a lockfile for processes, so the
  consent card and the pressure guardian can use the ledger at the same time.
- Signed: HMAC-SHA256 over the entries, key in DPAPI (T3). A bad HMAC puts the
  ledger in read-only mode: new freezes are refused, but thaw_all still works
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
def _file_lock(path: Path | None = None, timeout_s: float = LOCK_TIMEOUT_S) -> Iterator[None]:
    """Hold an exclusive lock on a lockfile (cross-process)."""
    home().mkdir(parents=True, exist_ok=True)
    p = path or lock_path()
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
        with _file_lock(None, timeout_s):
            yield


def fmutex_path() -> Path:
    return home() / "ledger.fmutex"


_FTHREADS = threading.RLock()


@contextlib.contextmanager
def freeze_locked(timeout_s: float = 60.0) -> Iterator[None]:
    """Serialize whole freeze/thaw operations across threads and processes.

    The per-op `locked()` guard is too short on its own: freeze adds the
    entry, releases the lock, then suspends, so a concurrent thaw_all could
    resume + remove in between and strand a suspend with no ledger record
    (and two concurrent freezers could double-suspend one app). Holding this
    mutex across the entire freeze — and the entire thaw/thaw_all — closes
    both races. Reads (frozen()/list) deliberately do NOT take it, so the UI
    stays snappy while a big app freezes.
    """
    with _FTHREADS:
        with _file_lock(fmutex_path(), timeout_s):
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


def _parse_entries(doc: dict) -> dict[str, LedgerEntry]:
    raw_entries = doc.get("entries", [])
    if not isinstance(raw_entries, list):
        raise TypeError("entries is not a list")
    out: dict[str, LedgerEntry] = {}
    for e in raw_entries:
        procs = [ProcRef(**p) for p in e["procs"]]
        out[e["app_id"]] = LedgerEntry(
            e["app_id"], procs, e["frozen_at"], e["freed_mb"], e["session"])
    return out


def _decode(data: bytes) -> tuple[dict[str, LedgerEntry], bool]:
    """Return (entries, ok). ok=False means: enter tamper (read-only) mode.

    Any wrong shape — non-dict JSON, non-string hmac, non-list entries —
    lands in tamper mode too: a ledger we cannot parse must never be
    silently treated as empty while apps may be frozen.
    """
    try:
        doc = json.loads(data.decode("utf-8"))
        if not isinstance(doc, dict):
            return {}, False
        tag = doc.get("hmac")
        if not isinstance(tag, str):
            return {}, False
        body = {k: v for k, v in doc.items() if k != "hmac"}
        raw = json.dumps(body, sort_keys=True).encode("utf-8")
        if not hmac.compare_digest(tag, hmac.new(load_key(), raw, hashlib.sha256).hexdigest()):
            log.error("ledger_tampered: HMAC mismatch on %s", ledger_path())
            try:
                return _parse_entries(doc), False
            except (KeyError, TypeError):
                return {}, False
        return _parse_entries(doc), True
    except (ValueError, KeyError, TypeError, AttributeError):
        return {}, False


# ---- the ledger ------------------------------------------------------------------

class Ledger:
    """Thread- and process-safe signed store of frozen apps."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or ledger_path()
        self._tampered = False

    @property
    def tampered(self) -> bool:
        return self._tampered

    def _backup_path(self) -> Path:
        return self._path.with_name(self._path.name + ".bak")

    def _read_unlocked(self) -> dict[str, LedgerEntry]:
        if self._path.exists():
            entries, ok = _decode(self._path.read_bytes())
            if not ok:
                self._tampered = True
            return entries
        bak = self._backup_path()
        if not bak.exists():
            return {}  # first run: nothing frozen, nothing deleted
        # The primary is gone but a backup exists: deleting the ledger would
        # otherwise strand frozen apps and bypass tamper mode for new freezes.
        # Deleting BOTH files is still a residual risk (documented): nothing
        # can distinguish that from a first run.
        log.error("ledger_deleted: %s missing, fell back to backup", self._path)
        entries, _ = _decode(bak.read_bytes())
        self._tampered = True  # deletion itself is tamper evidence: read-only
        return entries

    def _write_unlocked(self, entries: dict[str, LedgerEntry]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = _encode(entries)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, self._path)
        btmp = self._path.with_name(self._path.name + ".bak.tmp")
        btmp.write_bytes(data)
        os.replace(btmp, self._backup_path())

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

    def add_if_absent(self, entry: LedgerEntry) -> LedgerEntry | None:
        """Add under a single lock; return the existing entry if one is present.

        This closes the check-then-add race: two concurrent freezers of one
        app cannot both pass and double-suspend it.
        """
        result: dict[str, LedgerEntry] = {}

        def fn(entries: dict[str, LedgerEntry]) -> None:
            if entry.app_id in entries:
                result["existing"] = entries[entry.app_id]
            else:
                entries[entry.app_id] = entry

        self._mutate(fn)
        return result.get("existing")

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
