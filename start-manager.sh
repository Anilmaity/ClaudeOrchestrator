#!/usr/bin/env bash
# Launch the MANAGER Claude session.
#
# The manager runs here, in the orchestrator project, so it picks up CLAUDE.md
# (which teaches it how to drive ./orch) and the project settings that
# pre-approve the orchestrator commands.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux is not installed. Run:  sudo apt install -y tmux" >&2
  exit 1
fi

exec claude --dangerously-skip-permissions \
  "You are the orchestration manager. Read CLAUDE.md, then ask me what projects and tasks I want the worker Claudes to start on."
