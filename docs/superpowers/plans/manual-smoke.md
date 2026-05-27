# Manual smoke test (real claude, Windows)

Run these from PowerShell in the project directory. Requires `claude` on PATH and
`python -m pip install -r requirements.txt` already done.

1. `mkdir $env:TEMP\orch-smoke` (ignore if it exists)
2. `.\orch.cmd spawn --name smoke --dir $env:TEMP\orch-smoke --task "Create hello.txt containing 'hi', then print a line starting WORKER-DONE:"`
   - A new console window titled `corch:smoke` opens; claude boots; the trust and
     Bypass-Permissions dialogs are auto-accepted.
3. `.\orch.cmd list`        -> `smoke` shows `busy`, then `done`
4. `.\orch.cmd peek smoke`  -> shows claude's output including the WORKER-DONE line
5. `Test-Path $env:TEMP\orch-smoke\hello.txt`  -> True
6. `.\orch.cmd send smoke "now create bye.txt with 'bye'"`  -> bye.txt appears
7. `.\orch.cmd stop smoke`  -> the console window closes; the status file is removed
8. `.\orch.cmd list`        -> no workers

## Fleet host-stability check (pyte crash regression)

Verifies the agent host no longer dies mid-task. Background (commit `bbdbeda`):
pyte's `Screen`/`ByteStream` are not thread-safe, so the host pump thread feeding
the screen must not race the control-socket `CAPTURE` reads the dispatcher and
dashboard make every ~2s. The old bug surfaced the resulting pyte exception on the
pump thread, ending the pump and killing the host (which closed the ConPTY and
took `claude` down) — every dispatched task then ended `failed` with
`[agent terminal closed]`.

1. `.\fleet.cmd up --host 127.0.0.1 --port 8790`  -> all agents reach `idle`
2. `.\fleet.cmd add researcher "Read fleet.py and summarize the dispatcher's robustness mechanisms with line citations. Do NOT modify any files."`
3. `.\fleet.cmd status`  -> task goes `pending` -> `running` -> `done`; `gone_seen` stays 0
4. Throughout, the `corch:researcher` window keeps showing the live TUI and never
   closes, and no `~/.claude-orch/win/<name>/crash.log` is written.
   - Regression signal (old bug): the host window vanishes ~40-60s in and the task
     ends `failed` with `[agent terminal closed]`.
5. Automated equivalent (no real claude needed):
   `py -3.14 -m pytest -q tests/test_screen_buffer_concurrency.py`
