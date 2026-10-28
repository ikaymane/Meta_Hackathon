# Role D: UI, voice, demo + Representative

**Mission:** make Focus Freeze usable hands-free and **win the video**. Demo is 25% of the score, and finalists are judged on the video. You are also the **Representative** who submits for the team.

**Best fit:** frontend, design, video editing, clear writing.
**Owns:** `ui/` (tray, gauge, cards), `freezer/voice.py`, `demo/`, `README.md`, the submission
**Plan steps:** 5, 6, stats card (feature 7). Full plan: [../ram-freeze-agent.md](../ram-freeze-agent.md)
**You use:** `Plan`, `Decision`, `PolicyResult`, `PressureEvent`, `FocusSession`, `LedgerEntry` from [`freezer/contracts.py`](../../freezer/contracts.py). Use mocks until Nov 1.
**C reviews the wording of reasons shown in your UI.**

---

## Step 0: Setup (Oct 28)
1. Branch `d/ui`. Pick the UI stack today: **PySide6** (one Python app, simplest) or `pystray` + a small local web overlay. Recommended: PySide6.
2. `pip install PySide6 sounddevice websockets`.
3. Write `ui/mocks.py`: a fake `Plan`, a fake `PolicyResult` with 2 blocked items, a fake `Actuator` that just sleeps. You build everything against these until Nov 1.
4. Claim your $150 Meta API credits and set a spend alert.

## Step 1: Tray + live RAM gauge (Oct 29)
1. Tray icon with a menu: Focus…, Done (thaw all), Sessions, Panic thaw, Quit.
2. Small always-on-top gauge: RAM %, GB used, GB freed this session. It updates every second (`psutil.virtual_memory()`).
3. The gauge is the hero of the video: **make the drop visible** (animated number, colour change).

## Step 2: Consent card (Oct 30)
1. Shows: task sentence, apps to freeze with checkboxes, MB per app, **one-line reason per app** (from `Decision.reason`), total GB to free.
2. **Blocked section:** items from `PolicyResult.blocked` with the rule in plain words ("Protected process", "Playing audio", "Injection attempt in title").
3. Warning icon on apps with `has_network` ("may disconnect, reconnects on restore").
4. Buttons: **Freeze**, Cancel. Unticking an app → ask "Never freeze this during <session>?" → if yes, call `sessions.add_rule()` (C).

## Step 3: Voice (Oct 30)
1. `voice.py`: push-to-talk hotkey (`Ctrl+Alt+Space`) streams the mic to `muse-voice-transcribe-1.0` (realtime WebSocket). See https://dev.meta.ai/docs for the endpoint. Cost: $0.003/min.
2. Commands, matched by simple keywords first (fast, free), then sent to C's planner as the task:
   - "focus on …" / "I'm working on …" → plan → consent card
   - "start <name> session" → load the session
   - "I'm done" / "bring them back" → `thaw_all()`
3. Meta has **no text-to-speech**. Speak replies with Windows SAPI (`pyttsx3`) or just show text.

## Step 4: Guardian card + session picker (Oct 31)
1. Guardian card, from A's `PressureEvent`: "RAM 91%. Freeze 4 idle apps, free ~3.2 GB?" A dismiss starts the 10-min cooldown (tell A's guardian).
2. Session picker: list of saved `FocusSession`s, last used, number of rules, an "auto-freeze" toggle with a clear warning.

## Step 5: Demo rig (by Oct 31)
1. **Hyper-V VM with 4 GB RAM**, Windows 11. Without memory pressure the demo shows no benefit.
2. `demo/setup.ps1` opens the 12 demo apps: VS Code with the project, Chrome with 15 tabs, Teams/Discord, Spotify, Steam, Word with an **unsaved document**, a PDF reader, etc.
3. `demo/hog.ps1`: allocates memory to push RAM over 85% (for the guardian scene).
4. `demo/injection.html`: a local page whose `<title>` is an injection attack (get the exact text from B).

## Step 6: Video script draft (by Oct 31)
3 minutes max. Draft `demo/script.md`, scene by scene:
| Time | Scene |
|---|---|
| 0:00–0:20 | Problem: 8 GB laptop, IDE build stutters, 12 apps open |
| 0:20–0:50 | Voice: "I'm fixing the login bug" → consent card with reasons → Freeze → **gauge drops ~4 GB** |
| 0:50–1:10 | Build is fast now. The PDF that stays open despite being idle 40 min ("a timer would have frozen this") |
| 1:10–1:30 | **Security:** injection tab → blocked card → `ff verify-log` passes |
| 1:30–1:50 | Guardian: RAM spikes → card appears unprompted |
| 1:50–2:20 | "I'm done" → all apps return, **unsaved Word doc intact** |
| 2:20–2:50 | How it works: Muse Spark planner, voice transcribe, safety layers, cost per call |
| 2:50–3:00 | Name, team, repo link |
Share it with the team on Oct 31 and collect "this looks weak" feedback.

## Step 7: Integration day (Nov 1, whole team)
- Swap every mock for the real part (A's actuator, B's policy + sensor, C's planner + sessions).
- Run the full voice loop with **no keyboard**, start to restore.

## Step 8: Stats card + rehearsal (Nov 2)
1. End-of-session card: GB freed, focus minutes, apps frozen, $ spent on AI.
2. Rehearse the script 3 times on the VM, timing each scene.

## Step 9: Record (Nov 3)
- 1080p screen capture (OBS), clear mic. **Two clean takes.** Edit to ≤ 3:00.
- Only show work built during the hackathon (a submission rule).

## Step 10: Submit (Nov 4, by noon ET, not 9 PM)
1. README: what it is, problem statement 1 ("Build for Your World"), Meta models used and how, setup steps, architecture diagram, link to `docs/threat-model.md`, eval numbers from C.
2. Repo is **public**, video uploaded, all 4 teammates listed.
3. Submit at https://cerebralvalley.ai/e/meta-ai-developer-hackathon/hackathon/submit

---

## Done when
- [ ] Full voice loop works with no keyboard
- [ ] Consent card shows reasons + blocked items with rules
- [ ] Guardian card + session picker work
- [ ] 4 GB demo VM reproduces the pressure every time
- [ ] Video ≤ 3:00, two clean takes, edited
- [ ] Submitted by noon ET Nov 4

## Biggest risk
No RAM pressure in the demo = no visible benefit. Always film on the 4 GB VM, never on a 16 GB laptop.
