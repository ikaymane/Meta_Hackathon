"""Ledger: atomic signed writes, tamper read-only mode, concurrent access."""
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from freezer import ledger
from freezer.contracts import LedgerEntry, ProcRef
from freezer.ledger import Ledger, LedgerTampered

ROOT = Path(__file__).parent.parent


def entry(app_id: str, n: int = 2) -> LedgerEntry:
    return LedgerEntry(app_id, [ProcRef(1000 + i, 111.0 + i) for i in range(n)],
                       1234.0, 10.0 * n, "s")


def test_round_trip_preserves_entries():
    lg = Ledger()
    lg.add(entry("a.exe"))
    lg.add(entry("b.exe", 3))
    back = lg.load()
    assert set(back) == {"a.exe", "b.exe"}
    assert back["b.exe"].procs == [ProcRef(1000, 111.0), ProcRef(1001, 112.0), ProcRef(1002, 113.0)]
    assert back["b.exe"].freed_mb == 30.0
    assert not lg.tampered


def test_write_is_atomic_and_leaves_no_tmp():
    Ledger().add(entry("a.exe"))
    assert ledger.ledger_path().exists()
    assert not ledger.ledger_path().with_suffix(".tmp").exists()


def test_update_and_remove():
    lg = Ledger()
    lg.add(entry("a.exe"))
    e = entry("a.exe")
    e.freed_mb = 999.0
    lg.update(e)
    assert lg.load()["a.exe"].freed_mb == 999.0
    lg.remove("a.exe")
    assert lg.load() == {}


def _flip_ledger_byte() -> None:
    p = ledger.ledger_path()
    doc = json.loads(p.read_text(encoding="utf-8"))
    doc["entries"][0]["freed_mb"] += 1  # valid JSON, broken HMAC
    p.write_text(json.dumps(doc), encoding="utf-8")


def test_tampered_ledger_goes_read_only_but_thaw_still_works():
    lg = Ledger()
    lg.add(entry("a.exe"))
    _flip_ledger_byte()

    back = lg.load()
    assert lg.tampered
    assert set(back) == {"a.exe"}  # entries still readable for thaw_all

    with pytest.raises(LedgerTampered):
        lg.add(entry("b.exe"))
    with pytest.raises(LedgerTampered):
        lg.update(entry("a.exe"))
    with pytest.raises(LedgerTampered):
        lg.remove("a.exe")

    lg.save_thawed({})  # the thaw path is always allowed
    assert not lg.tampered
    assert Ledger().load() == {}  # re-signed ledger verifies on a fresh instance


def test_garbage_ledger_counts_as_tampered():
    lg = Ledger()
    ledger.ledger_path().parent.mkdir(parents=True, exist_ok=True)
    ledger.ledger_path().write_text("{not json", encoding="utf-8")
    assert lg.load() == {}
    assert lg.tampered


def test_concurrent_thread_writes_lose_nothing():
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            lg = Ledger()
            for i in range(25):
                lg.add(entry(f"w{n}-{i}.exe", 1))
        except BaseException as e:  # noqa: BLE001 - re-raised on the main thread
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
        assert not t.is_alive()
    assert errors == []
    back = Ledger().load()
    assert len(back) == 200  # every thread's entries survived


def test_lock_excludes_another_process():
    child = subprocess.Popen(
        [sys.executable, "-c",
         "from freezer.ledger import _file_lock; import time\n"
         "with _file_lock():\n"
         "    print('HELD', flush=True)\n"
         "    time.sleep(5)\n"],
        cwd=ROOT, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "HELD"
        with pytest.raises(TimeoutError):
            with ledger.locked(timeout_s=1):
                pass
    finally:
        child.kill()
        child.wait(timeout=10)
