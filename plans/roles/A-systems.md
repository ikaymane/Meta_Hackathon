# Role A: Systems (freeze engine + pressure guardian)

**Mission:** freeze and restore apps safely, and **never leave an app frozen**. You own the most critical part of the product: if your code fails, users lose trust immediately.

**Best fit:** the strongest Windows / C / `ctypes` person on the team.
**Owns:** `freezer/actuator.py`, `freezer/ledger.py`, `freezer/watchdog.py`, `freezer/guardian.py`, `freezer/cli.py`
**Plan steps:** 1, 8, smart restore (feature 4). Full plan: [../ram-freeze-agent.md](../ram-freeze-agent.md)
**Interfaces you implement:** `Actuator`, `LedgerEntry`, `PressureEvent` in [`freezer/contracts.py`](../../freezer/contracts.py)
**You review:** every PR that touches freeze code or security.

---

## Step 0: Setup (Oct 28)
1. Clone the team repo, create your branch `a/actuator`.
2. Python 3.12 venv, `pip install psutil pytest`.
3. Run the proven prototype (800 MB → 0 MB in 90 ms) to see the mechanism work:
   ```powershell
   powershell -File tools/freeze_test.ps1
   ```
4. Read `contracts.py`. Raise any interface change **today** in the team call, not later.

## Step 1: Freeze/thaw one process (Oct 29 morning)
1. In `actuator.py`, wrap the Win32 calls with `ctypes`:
   ```python
   import ctypes
   from ctypes import wintypes
   ntdll, psapi, k32 = ctypes.WinDLL("ntdll"), ctypes.WinDLL("psapi"), ctypes.WinDLL("kernel32")
   PROCESS_SUSPEND_RESUME = 0x0800
   PROCESS_SET_QUOTA = 0x0100
   PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

   def _open(pid):
       h = k32.OpenProcess(PROCESS_SUSPEND_RESUME | PROCESS_SET_QUOTA
                           | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
       if not h:
           raise ctypes.WinError()
       return h
   # ntdll.NtSuspendProcess(h), ntdll.NtResumeProcess(h), psapi.EmptyWorkingSet(h), k32.CloseHandle(h)
   ```
2. Always close handles (`try/finally`).
3. **Verify:** freeze `notepad.exe`. Its working set drops to about 0 in Task Manager. Thaw it and type: it still works.

## Step 2: Freeze a whole app (Oct 29 afternoon)
1. `freeze(app: AppSnapshot, session)` loops over **every** `ProcRef` in `app.procs` (Chrome has 10–40 processes).
2. Before suspending, check each PID's `create_time` still matches (PID reuse guard). Skip mismatches.
3. Minimize the app's top-level windows first (`ShowWindow(hwnd, SW_MINIMIZE)`), so no "Not Responding" ghost appears.
4. Order: **write ledger → minimize → suspend all → EmptyWorkingSet all**.
5. Measure freed MB (working set before minus after) into `LedgerEntry.freed_mb`.
6. **Verify:** freeze Chrome with 10 tabs → all its processes suspended. Thaw → tabs intact.

## Step 3: Ledger + crash recovery (Oct 30)
1. `ledger.py` stores `~/.focusfreeze/ledger.json`: a list of `LedgerEntry`.
2. Write atomically: write to `ledger.json.tmp`, then `os.replace`.
3. On agent startup: **resume everything in the ledger**, then clear it.
4. Panic hotkey (`Ctrl+Alt+Shift+F`) runs `thaw_all()` from anywhere.
5. `watchdog.py`: a tiny separate process that checks the agent's heartbeat file every 5 s. If the agent has been dead for 15 s, it runs `thaw_all()`.
6. **Chaos test:** freeze 3 apps → `Stop-Process -Force` the agent → restart → all 3 resumed. Run it **10 times. Must pass 10/10.**

## Step 4: CLI (Oct 30)
1. `ff freeze <exe>`, `ff thaw <exe>`, `ff thaw --all`, `ff list` (shows app, MB freed, time frozen).
2. Use `argparse`, no extra dependencies.
3. **Verify:** B, C and D can use the CLI to test their own parts.

## Step 5: Signed ledger, tamper-proof (Oct 31)
1. Generate a random 32-byte key on first run and protect it with Windows DPAPI (`CryptProtectData` via ctypes, or `win32crypt` from pywin32).
2. Each write stores `hmac_sha256(key, ledger_bytes)` next to the ledger.
3. On load, a bad HMAC → **read-only mode**: refuse new freezes, allow only `thaw_all` (thawing is always safe).
4. **Tamper test:** flip one byte in `ledger.json` → agent detects it and logs `ledger_tampered`.

## Step 6: Pressure guardian (Oct 30–31)
1. `guardian.py` polls `GlobalMemoryStatusEx` every 5 s (`dwMemoryLoad` = RAM %).
2. Fire only if RAM is above 85% **for 30 s straight**. After firing, wait until RAM drops below 75% before it can fire again (hysteresis).
3. After the user dismisses a card: 10-min cooldown.
4. On fire, emit `PressureEvent(ram_pct, sustained_s, candidates)`. Candidates = apps sorted by `last_focus_s` × MB. The planner (C) refines them, the UI (D) shows the card.
5. Never freeze without consent unless the active `FocusSession.auto_freeze` is true.
6. **Verify on the 4 GB VM:** a memory-hog script → exactly **one** event, not a storm.

## Step 7: Integration day (Nov 1, whole team)
1. Replace the mock `Actuator` in D's UI and C's loop with yours.
2. Pair with B: every freeze goes through `policy.filter()` first. No bypass path.
3. Full loop: voice → plan → consent → freeze → "done" → thaw.

## Step 8: Smart restore + soak (Nov 2)
1. `thaw_all()` order: foreground-at-freeze-time first, then most recently used.
2. After resuming, restore minimized windows (`SW_RESTORE`) so they're ready.
3. Report "restored X GB in Y s" for the UI.
4. **Soak test:** guardian runs 1 hour on the VM with normal use → no false triggers, at most 1 LLM call per trigger.

## Step 9: Bug fixes + video support (Nov 3–4)
- Fix bugs found in rehearsal. Be on call while D records.
- Your demo moment: **gauge drops ~4 GB in 1 s**, then "done" → apps come back with an **unsaved document intact**.

---

## Done when
- [ ] Chaos test 10/10: killed agent → 0 apps left frozen
- [ ] Tampered ledger detected, read-only mode works
- [ ] Guardian: one event per pressure episode, 1-hour soak clean
- [ ] CLI works for the whole team
- [ ] Restore order + time reported

## Biggest risk
Apps left frozen forever. Every code path that freezes must be undoable by `thaw_all()`, the panic hotkey, the watchdog, and startup recovery.
