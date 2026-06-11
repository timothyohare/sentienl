# Plan 04 — systemd Supervision + Deployment README

**Goal:** turn "restart" into a single command and auto-restart on crash —
closing the root-cause gap behind the "ran ~33h then died, never came back" state.

Use **systemd `--user`** units (no root required) under `deploy/systemd/`.

## Units (one per long-running process + a target)
Polymarket is intentionally **excluded** (ACMA-blocked; Kalshi replaces it). The
correlation detector runs inside the alerter, so no separate unit.

- `sentinel-alerter.service` — `python -m sentinel.dispatcher.alerter_runner`
- `sentinel-truth-social.service` — `python -m sentinel.collectors.truth_social_runner`
- `sentinel-kalshi.service` — `python -m sentinel.collectors.kalshi_runner`
- `sentinel-futures.service` — `python -m sentinel.collectors.futures_runner`
- `sentinel.target` — groups all four (`Wants=` each); `WantedBy` on the services

### Common service settings
- `WorkingDirectory=%h/dev/newdev/Sentinel` (documented as the install dir; README
  notes how to change)
- `ExecStart=%h/dev/newdev/Sentinel/venv/bin/python -m <module>`
- `Restart=always`, `RestartSec=10`, `StartLimitIntervalSec=300`,
  `StartLimitBurst=10` (crash-loop guard)
- `Environment=SENTINEL_CONFIG=%h/dev/newdev/Sentinel/config.yaml`
- `Environment=SENTINEL_DB=%h/dev/newdev/Sentinel/sentinel.db`
- truth-social also: `EnvironmentFile=%h/dev/newdev/Sentinel/.env` (TS creds)
- `PartOf=sentinel.target` + `WantedBy=sentinel.target` so the target controls all
- `StandardOutput=journal`, `StandardError=journal`

## README (`deploy/systemd/README.md`)
- Prereqs (venv built, `playwright install chromium`, DB initialised, config/.env).
- Install: copy units to `~/.config/systemd/user/`, `systemctl --user daemon-reload`.
- Enable on boot + start: `systemctl --user enable --now sentinel.target`.
  Note `loginctl enable-linger $USER` so user services survive logout/reboot.
- Operate: status/start/stop/restart per unit and via the target; `journalctl
  --user -u sentinel-kalshi -f` for logs.
- Healthcheck: `python sentinel/scripts/healthcheck.py`.
- How to change the install dir / paths (edit `WorkingDirectory` + `Environment`).

## "Testing" (no app code; validate config)
- `systemd-analyze --user verify deploy/systemd/*.service` if available; otherwise
  a syntax sanity check (each unit parses; ExecStart path resolvable). Document
  whichever was run.
- Confirm `ExecStart` python path and module names match the real runners.

## Done criteria
Five unit files + README present; unit syntax validated; README steps accurate
against the repo layout. Run code-quality agent on the diff.
