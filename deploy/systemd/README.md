# Sentinel systemd supervision (user units)

Runs the alerter (+ correlation detector), Truth Social, Kalshi, and futures
collectors under `systemd --user`, with automatic restart on crash. No root
required. Polymarket is intentionally excluded — it's ACMA-blocked in
Australia and replaced by Kalshi (see repo `CLAUDE.md`).

## Prereqs

- venv built at `~/dev/newdev/Sentinel/venv` with `requirements.txt` installed
- `playwright install chromium` run (Truth Social collector)
- `sentinel.db` initialised (`python sentinel/scripts/init_db.py`)
- `config.yaml` and `.env` (Truth Social credentials) present in the repo root

These units assume the repo lives at `~/dev/newdev/Sentinel`. If yours is
elsewhere, edit `WorkingDirectory=` and the `Environment=`/`ExecStart=` paths
in each `.service` file (or set up a symlink).

## Install

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/*.service deploy/systemd/*.target ~/.config/systemd/user/
systemctl --user daemon-reload
```

## Enable on boot + start now

```bash
systemctl --user enable --now sentinel.target
```

`sentinel.target` pulls in all four services (`Wants=`). Each service also
has its own `[Install]` block, so `systemctl --user enable sentinel-kalshi`
etc. works individually if you only want a subset running.

So the services keep running after you log out / across reboots (not just
while a graphical/SSH session is open):

```bash
loginctl enable-linger $USER
```

## Operate

```bash
systemctl --user status sentinel.target
systemctl --user status sentinel-kalshi
systemctl --user restart sentinel-truth-social
systemctl --user stop sentinel.target        # stop everything
journalctl --user -u sentinel-kalshi -f      # tail logs for one unit
journalctl --user -u 'sentinel-*' -f         # tail all four
```

## Healthcheck

```bash
python sentinel/scripts/healthcheck.py
```

Checks that each monitored source has written a signal recently; exits
non-zero if any collector looks stale. Good candidate for a `systemd --user`
timer or cron entry once the services are confirmed stable.

## ntfy notifications

Live alerting is controlled by `alerts.enabled` in `config.yaml`, independent
of these units. `false` (the current setting) is data-collection-only mode —
collectors and the correlation detector still write to `signals`, but no ntfy
push goes out. Flip to `true` and restart `sentinel-alerter` to resume live
alerting.

## Changing the install path

Edit `WorkingDirectory=` and every `%h/dev/newdev/Sentinel/...` path in the
four `.service` files, then `systemctl --user daemon-reload`.
