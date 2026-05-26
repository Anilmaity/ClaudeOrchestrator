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
