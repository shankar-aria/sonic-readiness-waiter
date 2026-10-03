# sonic-readiness-waiter

**Stage-by-stage bringup readiness for SONiC switches.**

- **Project title:** sonic-readiness-waiter: Stage-by-Stage Bringup Readiness for SONiC Switches
- **Team:** Shankar Vaideeswaran, Saravanakumar Venkat, Shrey Shah, Krishnamurthy Mayya
- **Company:** Aria Networks · contact: shankar@arianetworks.com

Waits for a booting SONiC switch to pass each bringup stage in order, and
reports the first stage where it is stuck, what it was waiting on, and where
to look next.

SONiC bringup state is spread across five Redis DBs (`CONFIG_DB`, `APPL_DB`,
`ASIC_DB`, `STATE_DB`, `COUNTERS_DB`) plus systemd and docker. Each stage
depends on the one before it, so the first stage that does not pass is the
point to debug.

---

## Repository layout

```
sonic_readiness_waiter.py   the waiter: single file, runs on the switch
check.sh                    compile, Python 3.9 syntax and lint checks
requirements.txt            development tools for check.sh (ruff)
```

## Requirements

- **On the switch:** Python 3.9 or newer, standard library only. Uses
  `swsscommon` (shipped with the SONiC image) and falls back to `sonic-db-cli`.
  Must run as root (reads Redis, systemd and docker state).
- **On the developer machine:** only `ruff`, for `check.sh`.

## Quickstart

```bash
git clone https://github.com/shankar-aria/sonic-readiness-waiter.git
cd sonic-readiness-waiter
scp sonic_readiness_waiter.py admin@<switch>:/tmp/
ssh admin@<switch> sudo python3 /tmp/sonic_readiness_waiter.py
```

## Usage

```bash
sudo python3 sonic_readiness_waiter.py [options]
```

| Option | Meaning |
|---|---|
| `--interval SECONDS` | poll interval (default 2) |
| `--total-timeout SECONDS` | cap on the whole run (default 0 = none) |
| `--stage-timeout NAME=SECONDS` | override one stage's timeout; repeatable |
| `--warm` | also wait for warm-reboot reconciliation (`WARM_RECONCILED`) |
| `--skip-optional` | drop optional stages (`XCVR_INFO`) |
| `--status` | check every stage once without waiting |
| `--one-shot` | check stages once, in order; stop at the first failure |
| `--list` | list stages, timeouts and the keys they wait on |
| `--json` | print a JSON summary instead of progress lines |

`--status` and `--one-shot` cannot be used together.

## The 12 stages

| # | Stage | Waits on | Timeout | Hint on failure |
|---|---|---|---|---|
| 1 | `REDIS_UP` | Redis PING (database container) | 120 s | `systemctl status database`; `docker logs database` |
| 2 | `CONFIG_DB_INITIALIZED` | `CONFIG_DB` `CONFIG_DB_INITIALIZED == 1` | 300 s | `journalctl -u config-setup`; check `config_db.json` loads |
| 3 | `SYNCD_SWITCH_CREATED` | `ASIC_DB` `HIDDEN` + `ASIC_STATE:SAI_OBJECT_TYPE_SWITCH:*` | 300 s | `docker logs syncd` |
| 4 | `PORT_CONFIG_DONE` | `APPL_DB` `PORT_TABLE:PortConfigDone` (count) | 300 s | `CONFIG_DB` `PORT` table; `docker logs swss` |
| 5 | `ASIC_PORTS_CREATED` | `ASIC_DB` port objects >= count | 300 s | `docker logs swss` (portsorch); `docker logs syncd` |
| 6 | `HOST_IFS_CREATED` | `STATE_DB` `PORT_TABLE\|<port>` `state=ok` for every `CONFIG_DB` port | 300 s | `ip link show <port>`; `docker logs swss` (portsyncd) |
| 7 | `PORT_INIT_DONE` | `APPL_DB` `PORT_TABLE:PortInitDone` | 300 s | `docker exec swss supervisorctl status` |
| 8 | `SWITCH_CAPABILITY` | `STATE_DB` `SWITCH_CAPABILITY\|switch` | 120 s | `docker logs swss` (orchagent init) |
| 9 | `PORT_COUNTERS_MAPPED` | `COUNTERS_DB` `COUNTERS_PORT_NAME_MAP` >= count | 300 s | `docker logs swss`; `counterpoll show` |
| 10 | `XCVR_INFO` (optional) | `STATE_DB` `TRANSCEIVER_INFO\|*` | 180 s | `docker logs pmon` (xcvrd) |
| 11 | `DOCKERS_UP` | expected systemd units and containers up (see below) | 600 s | `systemctl status <svc>`; `show feature status` |
| 12 | `SYSTEM_READY` | `STATE_DB` `SYSTEM_READY\|SYSTEM_STATE` `Status=UP` | 900 s | `show system-health sysready-status` |

`--warm` adds stage 13, `WARM_RECONCILED` (`STATE_DB`
`WARM_RESTART_TABLE|orchagent` `state=reconciled`, 600 s). An optional stage
that times out is reported as `WARN` and does not block the result.

## Modes

| Mode | Behaviour | Result label |
|---|---|---|
| default (wait) | polls each stage until it passes or times out; later stages show `not reached` | `STUCK AT` |
| `--one-shot` | checks each stage once, in order; stops at the first failure | `FAILED AT` |
| `--status` | checks every stage once, even after a failure | `CURRENT STAGE` |

Row tags: `PASS`, `WAIT` (still polling, printed every 10 s), `WARN`
(optional stage failed), `FAIL`, `----` (not reached).

On failure the waiter prints the stage, what it waits on, the last value seen
and a hint, for example:

```
STUCK AT: HOST_IFS_CREATED (6/12)
  waiting on: STATE_DB PORT_TABLE|<port> state=ok for all CONFIG_DB ports
  last seen : 65/66 ok; missing: Ethernet57
  hint      : ip link show <missing port>; docker logs swss (portsyncd)
```

## Example: cold boot on th5-0025

Wait mode also prints a `BOOT TIMELINE`, with each stage's pass time as
seconds since kernel boot (from `/proc/uptime`). Abridged:

```
sonic-readiness-waiter: 12 stages (waiting, poll every 1s)
[PASS]  1/12 REDIS_UP                   0.0s  PONG
[PASS]  4/12 PORT_CONFIG_DONE           0.0s  count=66 CONFIG_DB PORT entries=66
[PASS]  6/12 HOST_IFS_CREATED           0.0s  66/66 ok
[PASS] 10/12 XCVR_INFO                 71.0s  TRANSCEIVER_INFO entries=8
[PASS] 11/12 DOCKERS_UP                 0.4s  45/45 services up (skipped=49)
[PASS] 12/12 SYSTEM_READY               0.0s  Status=UP

SONiC bringup complete: all required stages PASS

BOOT TIMELINE  (boot 2026-10-03 04:17:02 UTC, waiter started at uptime 49.3s)
   REDIS_UP               <=   49.3s
   ...
   PORT_COUNTERS_MAPPED   <=   49.3s
   XCVR_INFO                  120.3s   +71.0s
   DOCKERS_UP             <=  120.7s
   SYSTEM_READY           <=  120.7s
 Boot -> all dockers up : 120.7s
 Boot -> SYSTEM_READY   : 120.7s
```

`<=` means the stage had already passed at its first check, so the real time
is at or before the value shown. Times are accurate to about the poll
interval. Start the waiter as early as possible after SSH answers for exact
per-stage times.

## JSON output

`--json` prints one summary object instead of progress lines:

| Field | Meaning |
|---|---|
| `ready` | `true` if every required stage passed |
| `stuck_at` | name of the first failed stage, or `null` |
| `stages[]` | per stage: `stage`, `status` (`PASS`/`WARN`/`FAIL`/`NOT_REACHED`), `seconds`, `detail`; in wait mode also `uptime` and `at_first_check`; `DOCKERS_UP` adds `down` (services not up) |
| `boot_time` | kernel boot time in UTC (wait mode only) |
| `waiter_start_uptime` | uptime when the waiter started (wait mode only) |
| `milestones` | `all_dockers_up` and `system_ready` as seconds since boot (wait mode only) |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | all required stages passed |
| 1 | a required stage failed or timed out |
| 2 | usage or environment error (bad `--stage-timeout`, `--status` with `--one-shot`, neither `swsscommon` nor `sonic-db-cli` available) |

## How DOCKERS_UP decides

`DOCKERS_UP` mirrors SONiC's `sysmonitor`, so it agrees with
`show system-health`:

1. **Expected services:** units in `multi-user.target.wants` and
   `sonic.target.wants`, plus enabled `CONFIG_DB` `FEATURE` entries.
   Removed: the `sysmonitor` exclude list and the platform's
   `services_to_ignore` from `system_health_monitoring_config.json`.
2. **Skipped units:** not loaded, or a unit file state other than
   `enabled`, `enabled-runtime`, `static` or `generated` (for example masked
   or disabled).
3. **Up:** the unit is `active`, or `inactive` because it is a oneshot, is
   `database-chassis`/`gbsyncd`, stopped on an `exec-condition`, or its start
   condition was not met.
4. **Features** must also have a running container, and features with
   `check_up_status=True` need `STATE_DB` `FEATURE|<name>` `UP_STATUS=True`.

Services that are not up are listed by name with the reason, for example
`saphy(unit failed/failed)` or `swss(docker exited)`.

## Development checks

Run on the developer machine:

```bash
pip install -r requirements.txt   # once
bash check.sh
```

`check.sh` runs three checks and exits 0 only if all pass:

1. **Compile:** in memory, no `__pycache__` left behind.
2. **Python 3.9 syntax:** safe for older switch images.
3. **Lint:** `ruff check` with ruff's default rules.

## Scope & limitations

- Read-only: it never writes to Redis, systemd or docker.
- Stage keys come from community SONiC; vendor images may publish different
  keys or skip some.
- Tested on Broadcom TH5 switches only.
- Stage times are accurate to about the poll interval; stages that passed
  before the waiter started show as `<=`.
- If SSH drops during boot (the management network restarts shortly after
  SSH first answers), the waiter is killed with the session. Run it under
  `nohup` with output to a file to survive this.
- No unit tests yet.

## Status & provenance

**COMPLETED.**

- **Prior work:** none. The idea, the stage design, the waiter and the
  checks were all built during the hackathon.
- **Validated by hand on live switches:**
  - full cold boot on `th5-0025`: 12/12 stages pass, with stage times
    matching the switch's syslog
  - injected `HOST_IFS_CREATED` fault on `th5-0025` (one port's `state`
    removed from `STATE_DB`): caught by `--one-shot`, recovered in wait mode
  - real platform issue on `th5-0019`: `DOCKERS_UP` names the host services
    that exit at boot, which is why `SYSTEM_READY` is never posted
  - `check.sh`: compile, Python 3.9 syntax and ruff checks pass
- **Caveats:** one platform family (Broadcom TH5); no automated tests.
