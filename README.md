# Vœrynth Sentinel

Vœrynth Sentinel is a Raspberry Pi-hosted **independent recovery guardian** for Home Assistant. It is designed for the situation where the automation host cannot be trusted to recover itself reliably once Home Assistant, Supervisor, or the host OS enters a broken state.

Instead of running inside the failing environment, Sentinel lives on a **separate Raspberry Pi control node**. From there it can observe Home Assistant from the outside, distinguish between normal maintenance and real failure, power-cycle the protected host through a Tuya smart plug, and finally initiate an encrypted backup restore over SSH when repeated reboot attempts still do not bring the system back.

This is not just a ping checker. It is a layered recovery system with state awareness, guarded escalation, backup intelligence, and an operator-facing dashboard.

## Repository status

This repository is intended for **public GitHub hosting** with runtime secrets kept local-only.

- Public source repository
- Keep `config.env` and live secrets out of Git
- Attribution to `Kiran Karthikeyan Achari` must be preserved when redistributing substantial portions
- Linked to the wider **Vœrynth OS** project
- Sanitized for source control without checked-in secrets

See `LICENSE` for the governing terms.

## Quickstart: make it work end-to-end

If you want the shortest path from clone to a working watchdog, follow this order:

1. Put the **Home Assistant host on a Tuya smart plug**, but keep the **Raspberry Pi on separate power** so Sentinel stays alive when it power-cycles HA.
2. In your router, give the **Home Assistant host** and the **Tuya plug** stable IP addresses or DHCP reservations.
3. Pair the smart plug in the **Smart Life** or **Tuya Smart** mobile app.
4. Get the Tuya values you need: `TUYA_DEVICE_ID`, `TUYA_DEVICE_IP`, `TUYA_LOCAL_KEY`, and usually `TUYA_VERSION`.
5. Make sure the Home Assistant SSH add-on is reachable and capture its host key for `HA_SSH_HOST_KEY`.
6. Copy `config.env.example` to `config.env` and fill in the real values.
7. Deploy to the Pi with `./deploy_to_pi.sh <pi-host> [pi-user]`.
8. Open the dashboard at `http://<pi-host>:8080` and verify both services stay healthy.

### Minimum real-world prerequisites

Before expecting automatic recovery to work, make sure all of these are true:

- the Pi can stay powered while the HA host loses power
- the HA machine is configured to **boot automatically after AC/power loss**
- the Pi can reach Home Assistant on ports `8123` and `4357`
- the Pi can reach the Tuya plug on the local network
- SSH access to the HA add-on works non-interactively
- you have at least one restoreable HA backup if you plan to use restore escalation

## Why this project is special

Most watchdogs are simple: if a port stops responding, they reboot something.

Vœrynth Sentinel is built to be more intelligent than that.

### What makes it different

1. **Out-of-band recovery**  
   The watchdog runs on a separate Raspberry Pi, so it can still act when Home Assistant or the host OS is unhealthy.

2. **Multi-signal diagnosis**  
   It does not trust a single HTTP check. It correlates Home Assistant Core, Home Assistant Observer, SSH/Supervisor state, and smart-plug control.

3. **Maintenance-aware behavior**  
   It tries to recognize valid startup, update, rebuild, and intentional reboot states before escalating.

4. **Two-strike recovery policy**  
   It does not jump straight from outage to restore. It tries hard reboot recovery first, then escalates to restore only after repeated failure.

5. **Backup-location intelligence**  
   It understands real Home Assistant backup metadata, including multi-location backups, and prefers `Local_NAS` when selecting a restore candidate.

6. **Operator visibility**  
   It includes a local dashboard and status API so the recovery node is observable, not a black box.

7. **Air-gapped UI assets**  
   Dashboard fonts and branding assets are served locally from `assets/` with no dependency on external CDNs.

## High-level architecture

| Component | Runs where | Role | Why it matters |
|---|---|---|---|
| `ha_watchdog.py` | Raspberry Pi | Main monitoring and recovery loop | Makes recovery decisions and executes them |
| `ha_watchdog_status_server.py` | Raspberry Pi | Dashboard + JSON status API | Gives operators live visibility into health and recent actions |
| Home Assistant Core (`8123`) | Protected HA host | Primary service being protected | Main indicator of whether HA is usable |
| Home Assistant Observer (`4357`) | Protected HA host | Secondary liveness signal | Helps distinguish “Core crashed” from “whole host is unhealthy” |
| HA SSH add-on | Protected HA host | Deeper investigation + restore channel | Exposes Supervisor state, jobs, and restore execution |
| Tuya smart plug | Between power and HA host | Hard power control | Allows out-of-band host power cycling |
| `Local_NAS` backup location | HA backup storage | Preferred restore source | Favors restore media likely to survive local-storage failure |

## Core mission

Sentinel exists to answer one question safely:

**“Is Home Assistant briefly busy, temporarily rebooting, partially unhealthy, completely dead, or beyond self-recovery?”**

Everything in the system is organized around answering that question conservatively before touching power or initiating restore.

## How Sentinel decides what is happening

### Health signals used by the watchdog

| Signal | Source | Meaning | Important nuance |
|---|---|---|---|
| Core HTTP | `http://<HA_HOST>:8123/api/` | Primary Home Assistant health signal | Any HTTP response counts as alive; the watchdog treats network reachability as the key fact |
| Observer HTTP | `http://<HA_HOST>:4357` | Host/Supervisor-side liveness signal | Must return HTTP `200` to count as alive |
| SSH Supervisor state | `ha info --raw-json` | Detects startup/setup states | Used to avoid rebooting during legitimate HA lifecycle work |
| SSH active jobs | `ha jobs info --raw-json` | Detects restore, rebuild, update, start/restart activity | Nested jobs are inspected recursively |
| Tuya relay control | Smart plug via `tinytuya` | Executes host power cycle | One automatic retry is used when powering back on |
| Optional remote probe | Dashboard only | External reachability visibility | Disabled safely when not configured |

### Internal classification states

The SSH investigation classifies the system into coarse states that drive watchdog behavior:

| Classified state | Meaning | Watchdog reaction |
|---|---|---|
| `starting` | Supervisor/setup or Core start/restart activity is in progress | Pause failure counters and wait |
| `rebuilding` | Restore or rebuild work is active | Pause failure counters and wait |
| `updating` | Core update-related work is active | Pause failure counters and wait |
| `stopped` | Supervisor is running but Core is not serving and no active job explains it | Start intentional reboot/shutdown grace handling |
| `unknown` | SSH succeeded but could not classify safely | Fall back to Observer-based logic |
| `dead` | SSH path failed / host appears unreachable | Fast-track hard-recovery path |

## End-to-end recovery model

### Normal startup phase

On process start, Sentinel deliberately waits for `STARTUP_GRACE_PERIOD` seconds before beginning normal enforcement. This exists because Observer often appears before Core during a clean boot, and reacting too early would create false hard-failure detections.

### Main monitoring loop

On each cycle, Sentinel roughly follows this order:

1. Check Home Assistant Core HTTP health.
2. If Core is healthy, reset counters and continue.
3. If Core is unhealthy, run **Deep SSH Investigation**.
4. Use SSH state to decide whether to wait, enter reboot grace, fall back to Observer logic, or fast-track hard recovery.
5. If needed, consult Observer to classify the outage as soft or hard.
6. If thresholds are crossed and policy allows, power-cycle the host.
7. After reboot, monitor for recovery during boot grace.
8. If repeated reboots fail, trigger restore from the preferred eligible backup.
9. After restore, wait through a monitored post-restore grace period.

## Failure classes and escalation logic

### Soft failure

A **soft failure** means:

- Core is offline, **but**
- Observer is still alive.

Interpretation: the machine is probably alive, but Home Assistant Core is stuck, crashed, or taking too long to restart.

Behavior:

- The watchdog starts a soft-failure timer.
- The first few checks are treated as a grace window.
- If Core stays offline beyond `SOFT_FAILURE_TIMEOUT`, Sentinel escalates to recovery.

### Hard failure

A **hard failure** means:

- Core is offline, and
- Observer is offline too.

Interpretation: the host may be frozen, networking may be broken, or the OS may be down hard.

Behavior:

- The watchdog increments a hard-failure counter.
- If `NETWORK_SANITY_CHECK_HOST` is configured and the Pi cannot ping it, Sentinel pauses hard-recovery enforcement instead of treating the outage as proof the HA host is frozen.
- Once `HARD_FAILURE_THRESHOLD` consecutive hard failures are seen, Sentinel attempts a power cycle.

### Intentional reboot/shutdown grace

If SSH indicates the system is `stopped`, or if Core goes offline in a way that looks consistent with a deliberate reboot/shutdown rather than a crash, Sentinel opens an intentional reboot/shutdown grace window.

While that window is active:

- if Observer is still alive, Sentinel waits
- if Observer drops offline during the window, Sentinel treats that as confirmation that the system is really going down and triggers recovery immediately
- if the grace expires without recovery, normal watchdog enforcement resumes

This protects manual or legitimate HA restarts from being mistaken for failure, but it does not wait forever.

## Two-strike recovery policy

Sentinel uses a **two-strike rule** before restore.

### Strike 1

If the watchdog crosses the failure threshold, it power-cycles the host and then monitors recovery during `BOOT_GRACE_PERIOD`.

If Core comes back during that window, the system is considered recovered and counters are reset.

### Strike 2

If Core still does not recover after the first reboot cycle:

- Sentinel does **not** immediately restore.
- It waits through the normal cooldown path.
- It allows a second reboot attempt under the normal policy.

Only after **two failed hard reboot cycles** does Sentinel escalate to SSH restore.

This makes restore a controlled last resort instead of an early destructive action.

## Restore behavior

### Restore trigger

An SSH restore is attempted only when:

- the host has already been power-cycled twice without recovery, and
- a suitable Home Assistant backup is available, and
- `BACKUP_PASS` is configured.

If `BACKUP_PASS` is missing, the restore path aborts cleanly and logs the reason.

### Restore candidate rules

Sentinel filters backups to those that:

1. have a valid `slug`
2. contain Home Assistant data (`content.homeassistant == true`)
3. are reported by Supervisor as restore-available

Among those candidates, it:

1. prefers backups available on `PREFERRED_RESTORE_LOCATION` (default: `Local_NAS`)
2. falls back to other restore-available candidates if needed
3. chooses the newest candidate by parsed backup timestamp

### Why `Local_NAS` is preferred

The preferred restore source is `Local_NAS` because a failure severe enough to require recovery may also make the HA host’s local storage the least trustworthy location.

That means Sentinel is biased toward a backup copy stored on the Pi-backed or external NAS path when available.

## Real-world backup metadata edge case

A key design detail is that Home Assistant backup metadata is not always simple.

Some valid restoreable backups may appear like this conceptually:

| Field | Example shape | Why it matters |
|---|---|---|
| `location` | `null` | A backup can still be valid even when this field is null |
| `locations` | `[null, "Local_NAS"]` | Multi-location truth may live here instead |
| `location_attributes` | `{ ".local": {...}, "Local_NAS": {...} }` | This can be the most reliable source of restore-location truth |

Sentinel explicitly handles this shape so it does not incorrectly reject mirrored or NAS-backed restore candidates.

## Edge-case handling matrix

### Watchdog behavior edge cases

| Edge case | What Sentinel does | Why this matters |
|---|---|---|
| Core is down but SSH reports `starting` | Pauses failure counters | Avoids rebooting during legitimate startup |
| Core is down but SSH reports `rebuilding` | Pauses failure counters | Avoids killing an in-progress restore/rebuild |
| Core is down but SSH reports `updating` | Pauses failure counters | Avoids corrupting update flow |
| SSH reports `stopped` | Starts intentional reboot/shutdown grace | Protects planned HA restarts/shutdowns |
| Intentional reboot grace active and Observer stays alive | Waits | Distinguishes controlled restart from crash |
| Intentional reboot grace active and Observer goes offline | Fast recovery path | Treats it as a genuine outage rather than waiting blindly |
| SSH is unreachable (`dead`) | Fast-tracks hard recovery logic | Speeds recovery when the host looks truly gone |
| Core is down, Observer alive for only a short time | Treats as grace | Avoids noisy or destructive action on short restarts |
| Core is still down after soft-failure timeout | Escalates to recovery | Handles hung-Core situations |
| Reboot count exceeds hourly cap | Refuses further reboots until window clears | Prevents infinite relay thrashing |
| Cooldown after reboot still active | Skips immediate further power cycle | Prevents rapid repeat intervention |
| Restore started | Enters monitored post-restore grace | Allows rebuild/recovery time without instant re-escalation |
| Post-restore grace expires and Core is still absent | Resumes normal enforcement | Prevents the system from waiting forever |

### Backup and restore edge cases

| Edge case | What Sentinel does | Why this matters |
|---|---|---|
| Backup contains add-ons only | Rejects it | Add-on-only backup is not a valid HA Core recovery source |
| Backup has no valid slug | Rejects it | Cannot restore something that cannot be referenced |
| Backup metadata is malformed | Falls back safely / ignores unusable record | Prevents crashes during candidate selection |
| Backup exists in both local and `Local_NAS` storage | Prefers `Local_NAS` | Matches the intended resilience policy |
| Newer backup is local-only but older backup is on `Local_NAS` | Picks `Local_NAS` backup | Storage trust is prioritized over raw recency |
| No `Local_NAS` backup exists | Falls back to newest restoreable HA backup anywhere | Still recovers instead of dead-ending |
| `location` is null but `locations` / `location_attributes` prove eligibility | Accepts the backup | Handles real Supervisor output |
| `BACKUP_PASS` missing | Aborts restore cleanly and logs error | Fails safe instead of half-running a destructive command |

### Dashboard and observability edge cases

| Edge case | What Sentinel does | Why this matters |
|---|---|---|
| Remote probe disabled | Returns a structured “disabled” status | The dashboard stays useful offline |
| Remote probe enabled but URL missing | Safely disables the remote probe in payload | Avoids misleading failures |
| External internet unavailable | Local dashboard still works | Operator visibility does not depend on WAN reachability |
| Asset CDNs unavailable | No impact | Fonts/images are served from local `assets/` |

## Dashboard and status API

The dashboard server runs separately from the watchdog and exposes:

- `/` or `/index.html` for the operator UI
- `/api/status` for a structured JSON payload
- `/assets/...` for local fonts and images

### Dashboard goals

The dashboard is meant to answer, at a glance:

- Is Core up?
- Is Observer up?
- Is the optional remote path reachable?
- What does the plug appear to be doing?
- How many failures has the watchdog seen recently?
- How many reboots happened in the last hour?
- Is the system in cooldown or boot grace?
- What is the latest watchdog log summary?

### Dashboard implementation notes

- Network checks are run in parallel so a slow remote path does not stall every status refresh.
- Recent watchdog logs are read and summarized for operator context.
- The UI uses locally vendored fonts and image assets only.
- The dashboard can remain useful even if the optional remote probe is disabled.

## Configuration model

The repository stores **no live secrets** in source files. Runtime values come from environment variables or a private `config.env` file loaded by systemd or directly by the Python runtime loader.

### Configuration file workflow

1. Copy `config.env.example` to `config.env`
2. Fill in the real values
3. Keep `config.env` private
4. Never commit it to Git

### How to get the Tuya device info

Sentinel needs these Tuya values:

- `TUYA_DEVICE_ID`
- `TUYA_DEVICE_IP`
- `TUYA_LOCAL_KEY`
- `TUYA_VERSION` (usually `3.4`, but confirm it)

The most reliable path is to use **TinyTuya** on your laptop/desktop first, then copy the discovered values into `config.env`.

#### Step 1: pair the plug in the Tuya/Smart Life app

- Factory-reset the smart plug if needed
- Pair it in **Smart Life** or **Tuya Smart**
- Confirm you can turn the plug on/off from the app
- If possible, create a DHCP reservation so the plug keeps the same IP

#### Step 2: install TinyTuya on your computer

- Run `python -m pip install tinytuya`

#### Step 3: scan your LAN for the plug IP and device ID

- Run `python -m tinytuya scan`
- Look for your plug in the results
- Note the discovered IP address and device ID
- Also note the reported protocol version if it is shown

#### Step 4: get the local key with the TinyTuya wizard

TinyTuya's setup wizard pulls device metadata from the Tuya IoT Cloud and is the easiest standard way to obtain the **local key**.

1. Create a developer account at `https://iot.tuya.com/`
2. Create a cloud project in the correct Tuya data center/region
3. In that project, enable the required APIs, especially **IoT Core** and **Authorization**
4. Link your Smart Life / Tuya app account to the project so your paired devices appear there
5. Run `python -m tinytuya wizard`
6. When prompted, enter the Tuya API key, API secret, region, and a sample device ID
7. Copy the matching device's `id` and `key` from the wizard output or generated `devices.json`

For Sentinel, map them like this:

- TinyTuya `id` -> `TUYA_DEVICE_ID`
- TinyTuya `key` -> `TUYA_LOCAL_KEY`
- scanned IP -> `TUYA_DEVICE_IP`
- scanned/version result -> `TUYA_VERSION`

#### Step 5: verify the plug details before using Sentinel

If the plug was re-paired or reset, the local key may change. Re-run the TinyTuya wizard whenever the old key stops working.

### How to get the Home Assistant SSH host key

Sentinel uses SSH for investigation and restore logic, so pin the host key instead of trusting any SSH server blindly.

1. Make sure the Home Assistant SSH add-on is installed and reachable
2. From a trusted machine, run `ssh-keyscan -p 22 <HA_HOST>`
3. Copy the resulting `ssh-ed25519 ...` or similar line into `HA_SSH_HOST_KEY`
4. Test SSH manually once before deploying Sentinel

### Configuration reference

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `HA_HOST` | Yes | `homeassistant.local` | Hostname/IP of the protected Home Assistant machine |
| `NETWORK_SANITY_CHECK_HOST` | No | empty | Optional pingable router/gateway IP used to detect whether the Pi itself is network-partitioned before hard-recovery escalation |
| `NETWORK_SANITY_CHECK_TIMEOUT` | No | `1` | Ping timeout in seconds for `NETWORK_SANITY_CHECK_HOST` |
| `HA_SSH_USER` | No | `ha` | SSH username for the Home Assistant SSH add-on |
| `HA_SSH_PORT` | No | `22` | SSH port used for deep investigation and restore |
| `HA_SSH_HOST_KEY` | Yes for SSH features | — | Pinned Home Assistant SSH server host key; accepts a full `known_hosts` line or `<key-type> <base64-key>` |
| `TUYA_DEVICE_ID` | Yes | — | Tuya device identifier for the smart plug |
| `TUYA_DEVICE_IP` | Yes | — | Smart plug IP address |
| `TUYA_LOCAL_KEY` | Yes | — | Tuya local API key |
| `TUYA_VERSION` | No | `3.4` | Tuya device protocol version |
| `BACKUP_PASS` | Yes for restore | — | Password used for encrypted HA backup restore |
| `PREFERRED_RESTORE_LOCATION` | No | `Local_NAS` | Preferred restore source when multiple candidates exist |
| `NABU_CASA_URL` | No | empty | Optional external URL for dashboard-only remote reachability visibility |
| `ENABLE_REMOTE_CHECK` | No | derived from `NABU_CASA_URL` | Enables remote probe in dashboard payload |
| `NABU_CASA_TIMEOUT` | No | `8` | Timeout for the optional remote probe |
| `BIND_HOST` | No | `0.0.0.0` | Dashboard server bind address |
| `DASHBOARD_HOST` | No | `127.0.0.1` | Dashboard display/reference host |
| `PORT` | No | `8080` | Dashboard port |
| `CHECK_INTERVAL` | No | `10` | Main watchdog poll interval |
| `REQUEST_TIMEOUT` | No | `4` | HTTP request timeout for health checks |
| `SSH_INVESTIGATION_TIMEOUT` | No | `5` | SSH timeout for deep investigation |
| `HARD_FAILURE_THRESHOLD` | No | `3` | Consecutive hard-failure checks required before power-cycle logic |
| `SOFT_FAILURE_GRACE` | No | `2` | Initial soft-failure checks treated as grace |
| `SOFT_FAILURE_TIMEOUT` | No | `120` | Maximum time Core may stay offline while Observer remains alive |
| `BOOT_GRACE_PERIOD` | No | `180` | Recovery watch period after a power cycle |
| `STARTUP_GRACE_PERIOD` | No | `120` | Initial delay before first enforcement after watchdog start |
| `INTENTIONAL_REBOOT_GRACE_PERIOD` | No | `300` | Window used to avoid interrupting legitimate reboot/shutdown flows |
| `POST_RESTORE_BOOT_GRACE_PERIOD` | No | `400` | Monitored grace after starting a restore |
| `POWER_OFF_SECONDS` | No | `12` | Duration to hold the relay off during power cycle |
| `MAX_REBOOTS_PER_HOUR` | No | `3` | Reboot cap inside the rolling reboot window |
| `REBOOT_WINDOW_SECONDS` | No | `3600` | Window used for reboot-rate limiting |
| `COOLDOWN_AFTER_REBOOT` | No | `300` | Minimum wait before allowing another power cycle |
| `DRY_RUN` | No | `false` | Simulates relay action without switching power |

## Deployment

The checked-in systemd service files are templates. The deployment script rewrites the install path and runtime user when deploying to the target Pi.

### Full setup flow

Use this checklist if you are setting up Sentinel from scratch.

1. Prepare hardware
   - Raspberry Pi stays powered independently
   - Home Assistant host is plugged into the Tuya plug
   - HA host is configured to boot after power loss
2. Prepare network
   - Pi, HA host, and Tuya plug are on the same reachable network
   - HA host and plug preferably have reserved IPs
   - optionally set `NETWORK_SANITY_CHECK_HOST` to your router/gateway IP so Sentinel can pause enforcement during a Pi-side network outage
3. Prepare Tuya access
   - pair the plug in Smart Life / Tuya Smart
   - collect `TUYA_DEVICE_ID`, `TUYA_DEVICE_IP`, `TUYA_LOCAL_KEY`, `TUYA_VERSION`
4. Prepare Home Assistant access
   - confirm the SSH add-on works
   - set `HA_HOST`, `HA_SSH_USER`, `HA_SSH_PORT`, and `HA_SSH_HOST_KEY`
5. Prepare restore inputs
   - set `BACKUP_PASS`
   - confirm your preferred backup location name if using `PREFERRED_RESTORE_LOCATION`
6. Create local config
   - copy `config.env.example` to `config.env`
   - fill in all required values
7. Deploy
   - run `./deploy_to_pi.sh <pi-host> [pi-user]`
   - default install path is `/home/<pi-user>/Documents/ha-watchdog`
8. Verify first boot
   - check `sudo systemctl status ha-watchdog.service ha-watchdog-status.service --no-pager`
   - open `http://<pi-host>:8080`
   - confirm the dashboard shows your real HA host / dashboard values instead of defaults

### Prerequisites

Before deployment, make sure:

- the Raspberry Pi can reach the Home Assistant host over the network
- the Pi can reach the Tuya smart plug locally
- SSH key auth to the Home Assistant SSH add-on works
- the HA SSH server host key has been pinned in `HA_SSH_HOST_KEY`
- the HA add-on accepts `bash -l -c` based command execution
- the target host is configured to power back on after AC loss
- `config.env` has been filled with real values locally and can be synced to the Pi during deploy, or will be seeded and edited there

### Deploy script usage

- `./deploy_to_pi.sh <pi-host> [pi-user]`

Examples:

- `./deploy_to_pi.sh 10.0.0.25 hawatchdog`
- `PI_HOST=watchdog-pi.local PI_USER=hawatchdog ./deploy_to_pi.sh`

### What `deploy_to_pi.sh` does

1. Creates the install directory and log directory on the Pi
2. Copies application code, assets, `README.md`, `LICENSE`, and `config.env.example`
3. Also copies local `config.env` when it exists, while still excluding `logs/`, `__pycache__/`, and `*.pyc`
4. Templates the systemd units with the chosen user and install path
5. Seeds `config.env` from `config.env.example` if no config file exists yet
6. Installs Python dependencies (`requests`, `tinytuya`, `paramiko`)
7. Reloads systemd, enables both services, restarts them, and prints service status

### Service files

| File | Purpose |
|---|---|
| `ha-watchdog.service` | Runs the main watchdog loop |
| `ha-watchdog-status.service` | Runs the dashboard/status server |

Both services load runtime configuration using:

- `EnvironmentFile=-<install-dir>/config.env`

This allows a private local config file while still permitting direct environment overrides.

## Operational runbook

### Healthy steady state

In normal operation you should expect:

- Core to respond on port `8123`
- Observer to respond on port `4357`
- watchdog counters to remain near zero
- few or no reboot actions in the logs
- dashboard to show healthy summaries

### If you are investigating a problem

Useful checks after deployment:

- `sudo systemctl status ha-watchdog.service ha-watchdog-status.service --no-pager`
- `journalctl -u ha-watchdog.service -u ha-watchdog-status.service -n 100 --no-pager`
- `tail -f <install-dir>/logs/watchdog.log`
- `http://<pi-host>:8080`

### What successful recovery looks like

Common healthy recovery paths are:

- brief Core outage with Observer alive, then Core returns before timeout
- intentional reboot/shutdown grace opens, Observer stays alive long enough, Core comes back
- hard reboot occurs, then Core returns during boot grace
- after restore, Core returns during post-restore grace and normal monitoring resumes

## Test-backed behavior

The repository includes unit coverage for key logic, including:

- waiting on `starting`, `rebuilding`, and `updating` states
- intentional reboot grace handling
- reboot-window decisions tied to Observer presence
- SSH classification for Supervisor startup/setup and nested jobs
- SSH failure mapping to `dead`
- restore candidate selection with real multi-location backup metadata
- preference for `Local_NAS` over newer local-only backups
- fallback to newest valid HA backup when preferred storage is absent
- clean restore abort when `BACKUP_PASS` is missing
- offline dashboard assets and safe remote-probe disable behavior

This matters because Sentinel’s most important behavior lives in policy and edge-case handling, not just in happy-path connectivity checks.

## Repository layout

| Path | Purpose |
|---|---|
| `ha_watchdog.py` | Main recovery loop and decision logic |
| `ha_watchdog_status_server.py` | Dashboard server and status payload builder |
| `runtime_config.py` | Centralized environment/config loader |
| `deploy_to_pi.sh` | Pi deployment helper |
| `ha-watchdog.service` | systemd template for the watchdog |
| `ha-watchdog-status.service` | systemd template for the dashboard |
| `config.env.example` | Sanitized config template |
| `tests/test_ha_watchdog.py` | Watchdog logic/unit tests |
| `tests/test_ha_watchdog_status_server.py` | Dashboard/status-server tests |
| `assets/` | Vendored local UI fonts and branding assets |
| `logs/` | Runtime log directory created on the Pi |

## Safety and privacy posture

This repository has been prepared for public hosting with runtime secrets removed from tracked source files.

### Git hygiene

The repository is configured to ignore:

- `config.env`
- `logs/`
- `__pycache__/`
- `*.pyc`

### What should never be committed

- real Home Assistant IPs/hostnames if you consider them sensitive
- Tuya local keys
- backup passwords
- SSH private keys
- runtime-generated logs containing internal network details

## Assumptions and limitations

Sentinel is powerful, but it depends on several environmental truths:

- the Pi itself must remain healthy and powered
- network reachability between Pi, plug, and HA host must exist at least enough for diagnosis or action
- the smart plug must still be controllable for out-of-band power recovery
- SSH access to the HA add-on must be working for deep investigation and restore
- the selected backup must actually be restorable by Supervisor in the target HA environment

Sentinel reduces operational risk; it does not eliminate every possible infrastructure failure.

## Validation commands

Useful local checks:

- `python -m py_compile runtime_config.py ha_watchdog.py ha_watchdog_status_server.py`
- `python -m unittest tests.test_ha_watchdog tests.test_ha_watchdog_status_server`

If `bash` is available locally, you can also syntax-check the deploy script:

- `bash -n deploy_to_pi.sh`

## License

This project is published with the MIT License. If you reuse substantial portions of the code, preserve attribution to `Kiran Karthikeyan Achari` by keeping the copyright and license notice.