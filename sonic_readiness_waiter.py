#!/usr/bin/env python3
"""sonic-readiness-waiter.

Runs on a booting SONiC switch and walks the bringup stages in order. Each
stage is polled until the redis keys it depends on are published (or the stage
times out). The first stage that does not turn green is reported as the point
where the switch is stuck.

Must run as root on the switch (reads redis, systemd and docker state).
"""

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

DB_CLI = "sonic-db-cli"
SYSTEMD_WANTS_DIRS = (
    "/etc/systemd/system/multi-user.target.wants",
    "/etc/systemd/system/sonic.target.wants",
)
# Mirrors the exclude list in files/scripts/system/sysmonitor.py.
SERVICE_EXCLUDES = {
    "aaastatsd", "rasdaemon", "ztp", "pde", "systemcoreready", "dynamic-licensing",
}
# Mirrors spl_srv_list / NON_BLOCKING_INACTIVE_REASONS in health_checker/sysmonitor.py.
INACTIVE_OK_SERVICES = {"database-chassis", "gbsyncd"}
NON_BLOCKING_INACTIVE_REASONS = {"exec-condition"}
CHECKED_UNIT_FILE_STATES = {"enabled", "enabled-runtime", "static", "generated"}
HEALTH_CONFIG = "/usr/share/sonic/device/{platform}/system_health_monitoring_config.json"
DOWN_LIST_LIMIT = 8
DISABLED_FEATURE_STATES = {"disabled", "always_disabled"}
NIL_VALUES = ("", "None", "(nil)")

Result = Tuple[bool, str]


def run(cmd: List[str], timeout: float = 30) -> Tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, ""
    except FileNotFoundError:
        return 127, ""
    return proc.returncode, proc.stdout.strip()


class Redis:
    """Read-only access to SONiC DBs via swsscommon, falling back to sonic-db-cli."""

    def __init__(self) -> None:
        self._conns: Dict[str, object] = {}
        try:
            from swsscommon import swsscommon  # type: ignore
            self._sw = swsscommon
        except ImportError:
            self._sw = None
        if self._sw is None and shutil.which(DB_CLI) is None:
            raise RuntimeError(f"neither python swsscommon nor {DB_CLI} is available")

    def _sw_call(self, db: str, fn: Callable):
        try:
            if db not in self._conns:
                self._conns[db] = self._sw.DBConnector(db, 0)
            return fn(self._conns[db])
        except Exception:
            self._conns.pop(db, None)
            raise

    def _cli(self, db: str, *args: str) -> str:
        rc, out = run([DB_CLI, db, *args])
        if rc != 0:
            raise RuntimeError(f"{DB_CLI} {db} {' '.join(args)} failed (rc={rc})")
        return out

    def ping(self, db: str) -> bool:
        if self._sw:
            self._sw_call(db, lambda c: c.exists("__readiness_probe__"))
            return True
        return self._cli(db, "PING") in ("PONG", "True")

    def get(self, db: str, key: str) -> Optional[str]:
        if self._sw:
            return self._sw_call(db, lambda c: c.get(key))
        out = self._cli(db, "GET", key)
        return None if out in NIL_VALUES else out

    def hget(self, db: str, key: str, fld: str) -> Optional[str]:
        if self._sw:
            return self._sw_call(db, lambda c: c.hget(key, fld))
        out = self._cli(db, "HGET", key, fld)
        return None if out in NIL_VALUES else out

    def exists(self, db: str, key: str) -> bool:
        if self._sw:
            return bool(self._sw_call(db, lambda c: c.exists(key)))
        return self._cli(db, "EXISTS", key) in ("1", "True")

    def keys(self, db: str, pattern: str) -> List[str]:
        if self._sw:
            return list(self._sw_call(db, lambda c: c.keys(pattern)))
        out = self._cli(db, "KEYS", pattern)
        if out.startswith("[") and out.endswith("]"):
            return list(ast.literal_eval(out))
        return [line.strip() for line in out.splitlines() if line.strip() not in NIL_VALUES]

    def hlen(self, db: str, key: str) -> int:
        if self._sw:
            return len(self._sw_call(db, lambda c: c.hgetall(key)))
        out = self._cli(db, "HLEN", key)
        return int(out) if out.isdigit() else 0


@dataclass
class Ctx:
    redis: Redis
    ports: List[str] = field(default_factory=list)
    port_count: int = 0
    down_services: List[str] = field(default_factory=list)


@dataclass
class Stage:
    name: str
    waits_on: str
    hint: str
    check: Callable[[Ctx], Result]
    timeout: float
    optional: bool = False


def summarize(total: int, missing: List[str], limit: int = 8) -> str:
    text = f"{total - len(missing)}/{total} ok"
    if missing:
        more = f" +{len(missing) - limit} more" if len(missing) > limit else ""
        text += f"; missing: {', '.join(missing[:limit])}{more}"
    return text


def chk_redis_up(ctx: Ctx) -> Result:
    ok = ctx.redis.ping("APPL_DB")
    return ok, "PONG" if ok else "no reply"


def chk_config_db_initialized(ctx: Ctx) -> Result:
    val = ctx.redis.get("CONFIG_DB", "CONFIG_DB_INITIALIZED")
    return val == "1", f"CONFIG_DB_INITIALIZED={val}"


def chk_syncd_switch_created(ctx: Ctx) -> Result:
    hidden = ctx.redis.exists("ASIC_DB", "HIDDEN")
    switches = ctx.redis.keys("ASIC_DB", "ASIC_STATE:SAI_OBJECT_TYPE_SWITCH:*")
    return hidden and bool(switches), f"HIDDEN={'yes' if hidden else 'no'} switch_objects={len(switches)}"


def chk_port_config_done(ctx: Ctx) -> Result:
    ctx.ports = sorted(k.split("|", 1)[1] for k in ctx.redis.keys("CONFIG_DB", "PORT|*"))
    count = ctx.redis.hget("APPL_DB", "PORT_TABLE:PortConfigDone", "count")
    if count is None:
        pending = ctx.redis.exists("APPL_DB", "_PORT_TABLE:PortConfigDone")
        note = "published by portsyncd but not consumed by orchagent" if pending else "not published"
        return False, f"PortConfigDone {note} (CONFIG_DB PORT entries={len(ctx.ports)})"
    ctx.port_count = int(count) if count.isdigit() else len(ctx.ports)
    return True, f"count={count} CONFIG_DB PORT entries={len(ctx.ports)}"


def chk_asic_ports_created(ctx: Ctx) -> Result:
    num = len(ctx.redis.keys("ASIC_DB", "ASIC_STATE:SAI_OBJECT_TYPE_PORT:oid:*"))
    return num > 0 and num >= ctx.port_count, f"ASIC port objects={num} expected>={ctx.port_count}"


def chk_host_ifs_created(ctx: Ctx) -> Result:
    missing = [p for p in ctx.ports
               if ctx.redis.hget("STATE_DB", f"PORT_TABLE|{p}", "state") != "ok"]
    return bool(ctx.ports) and not missing, summarize(len(ctx.ports), missing)


def chk_port_init_done(ctx: Ctx) -> Result:
    appl = ctx.redis.exists("APPL_DB", "PORT_TABLE:PortInitDone")
    state = ctx.redis.hget("STATE_DB", "PORT_INI_TABLE|PortInitDone", "status")
    return appl, f"APPL_DB PortInitDone={'yes' if appl else 'no'} STATE_DB PORT_INI_TABLE status={state}"


def chk_switch_capability(ctx: Ctx) -> Result:
    ok = ctx.redis.exists("STATE_DB", "SWITCH_CAPABILITY|switch")
    return ok, f"SWITCH_CAPABILITY|switch={'present' if ok else 'absent'}"


def chk_port_counters_mapped(ctx: Ctx) -> Result:
    num = ctx.redis.hlen("COUNTERS_DB", "COUNTERS_PORT_NAME_MAP")
    return num > 0 and num >= ctx.port_count, f"COUNTERS_PORT_NAME_MAP entries={num} expected>={ctx.port_count}"


def chk_xcvr_info(ctx: Ctx) -> Result:
    num = len(ctx.redis.keys("STATE_DB", "TRANSCEIVER_INFO|*"))
    return num > 0, f"TRANSCEIVER_INFO entries={num}"


def expected_services(redis: Redis) -> Tuple[List[str], Set[str], Set[str]]:
    """Return (services, docker features, services requiring UP_STATUS) like sysmonitor."""
    services: Set[str] = set()
    for wants in SYSTEMD_WANTS_DIRS:
        if os.path.isdir(wants):
            services.update(f[:-len(".service")] for f in os.listdir(wants) if f.endswith(".service"))
    features: Set[str] = set()
    up_check: Set[str] = set()
    for table in ("FEATURE", "HOST_FEATURE"):
        for key in redis.keys("CONFIG_DB", f"{table}|*"):
            name = key.split("|", 1)[1]
            if table == "FEATURE":
                if redis.hget("CONFIG_DB", key, "state") in DISABLED_FEATURE_STATES:
                    continue
                features.add(name)
                services.add(name)
            if redis.hget("CONFIG_DB", key, "check_up_status") == "True":
                up_check.add(name)
    excludes = SERVICE_EXCLUDES | platform_ignored_services(redis)
    services -= excludes
    return sorted(services), features - excludes, up_check


def platform_ignored_services(redis: Redis) -> Set[str]:
    platform = redis.hget("CONFIG_DB", "DEVICE_METADATA|localhost", "platform")
    if not platform:
        return set()
    try:
        with open(HEALTH_CONFIG.format(platform=platform)) as fh:
            ignored = json.load(fh).get("services_to_ignore") or []
    except (OSError, ValueError, AttributeError):
        return set()
    return {s[:-len(".service")] if s.endswith(".service") else s for s in ignored}


def unit_states(units: List[str]) -> Dict[str, Dict[str, str]]:
    if not units:
        return {}
    _, out = run(["systemctl", "show", "--no-pager", "-p",
                  "LoadState,UnitFileState,Type,ActiveState,SubState,Result,ConditionResult",
                  *[f"{u}.service" for u in units]])
    blocks = out.split("\n\n")
    return {unit: dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            for unit, block in zip(units, blocks)}


def docker_states(containers: List[str]) -> Dict[str, str]:
    if not containers:
        return {}
    _, out = run(["docker", "inspect", "-f", "{{.Name}} {{.State.Status}}", *containers])
    states = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            states[parts[0].lstrip("/")] = parts[1]
    return states


def chk_dockers_up(ctx: Ctx) -> Result:
    services, features, up_check = expected_services(ctx.redis)
    if not services:
        return False, "no expected services found (CONFIG_DB FEATURE empty and no systemd wants)"
    units = unit_states(services)
    dockers = docker_states(sorted(features))
    down = []
    skipped = 0
    for svc in services:
        unit = units.get(svc, {})
        if (unit.get("LoadState") != "loaded"
                or unit.get("UnitFileState") not in CHECKED_UNIT_FILE_STATES):
            skipped += 1
            continue
        state = unit.get("ActiveState")
        active = state == "active" or (state == "inactive" and (
            unit.get("Type") == "oneshot"
            or svc in INACTIVE_OK_SERVICES
            or unit.get("Result") in NON_BLOCKING_INACTIVE_REASONS
            or unit.get("ConditionResult") == "no"))
        if not active:
            down.append(f"{svc}(unit {state or '?'}/{unit.get('SubState', '?')})")
        elif svc in features and dockers.get(svc) != "running":
            down.append(f"{svc}(docker {dockers.get(svc, 'absent')})")
        elif svc in up_check and ctx.redis.hget(
                "STATE_DB", f"FEATURE|{svc}", "UP_STATUS") not in ("True", "TRUE"):
            reason = ctx.redis.hget("STATE_DB", f"FEATURE|{svc}", "FAIL_REASON") or "NA"
            down.append(f"{svc}(app not up: {reason})")
    ctx.down_services = down
    checked = len(services) - skipped
    detail = f"{checked - len(down)}/{checked} services up (skipped={skipped})"
    if down:
        more = f" (+{len(down) - DOWN_LIST_LIMIT} more)" if len(down) > DOWN_LIST_LIMIT else ""
        detail += f"; down: {', '.join(down[:DOWN_LIST_LIMIT])}{more}"
    return not down, detail


# sysmonitor starts DOWN and only writes on state change, so an absent key means DOWN.
def chk_system_ready(ctx: Ctx) -> Result:
    status = ctx.redis.hget("STATE_DB", "SYSTEM_READY|SYSTEM_STATE", "Status")
    if status is None:
        return False, "Status=DOWN (not yet posted; sysmonitor only writes on change)"
    return status == "UP", f"Status={status}"


def chk_warm_reconciled(ctx: Ctx) -> Result:
    state = ctx.redis.hget("STATE_DB", "WARM_RESTART_TABLE|orchagent", "state")
    return state == "reconciled", f"orchagent state={state}"


def build_stages(warm: bool) -> List[Stage]:
    stages = [
        Stage("REDIS_UP", "redis PING (database container)",
              "systemctl status database; docker logs database", chk_redis_up, 120),
        Stage("CONFIG_DB_INITIALIZED", "CONFIG_DB GET CONFIG_DB_INITIALIZED == 1",
              "journalctl -u config-setup; check /etc/sonic/config_db.json loads",
              chk_config_db_initialized, 300),
        Stage("SYNCD_SWITCH_CREATED", "ASIC_DB HIDDEN + ASIC_STATE:SAI_OBJECT_TYPE_SWITCH:*",
              "docker logs syncd; show logging | grep -i -e sai -e syncd",
              chk_syncd_switch_created, 300),
        Stage("PORT_CONFIG_DONE", "APPL_DB PORT_TABLE:PortConfigDone (count)",
              "CONFIG_DB PORT table; docker logs swss (portsyncd, orchagent)",
              chk_port_config_done, 300),
        Stage("ASIC_PORTS_CREATED", "ASIC_DB ASIC_STATE:SAI_OBJECT_TYPE_PORT:oid:* >= count",
              "docker logs swss (orchagent portsorch); docker logs syncd",
              chk_asic_ports_created, 300),
        Stage("HOST_IFS_CREATED", "STATE_DB PORT_TABLE|<port> state=ok for all CONFIG_DB ports",
              "ip link show <missing port>; docker logs swss (portsyncd)",
              chk_host_ifs_created, 300),
        Stage("PORT_INIT_DONE", "APPL_DB PORT_TABLE:PortInitDone",
              "docker exec swss supervisorctl status; docker logs swss (portsyncd)",
              chk_port_init_done, 300),
        Stage("SWITCH_CAPABILITY", "STATE_DB SWITCH_CAPABILITY|switch",
              "docker logs swss (orchagent init)", chk_switch_capability, 120),
        Stage("PORT_COUNTERS_MAPPED", "COUNTERS_DB COUNTERS_PORT_NAME_MAP >= count",
              "docker logs swss (portsorch); counterpoll show",
              chk_port_counters_mapped, 300),
        Stage("XCVR_INFO", "STATE_DB TRANSCEIVER_INFO|*",
              "docker logs pmon (xcvrd); show interfaces transceiver presence",
              chk_xcvr_info, 180, optional=True),
        Stage("DOCKERS_UP", "systemd wants + enabled CONFIG_DB FEATURE units/containers up",
              "systemctl status <svc>; docker logs <name>; show feature status",
              chk_dockers_up, 600),
        Stage("SYSTEM_READY", "STATE_DB SYSTEM_READY|SYSTEM_STATE Status=UP",
              "show system-health sysready-status", chk_system_ready, 900),
    ]
    if warm:
        stages.append(Stage("WARM_RECONCILED", "STATE_DB WARM_RESTART_TABLE|orchagent state=reconciled",
                            "show warm_restart state; docker logs swss",
                            chk_warm_reconciled, 600))
    return stages


def evaluate(stage: Stage, ctx: Ctx) -> Result:
    try:
        return stage.check(ctx)
    except Exception as exc:  # redis/systemd not reachable yet counts as "not ready"
        return False, f"error: {exc}"


def read_uptime() -> Optional[float]:
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def wait_for(stage: Stage, ctx: Ctx, interval: float, deadline: float,
             progress: Callable[[float, str], None]) -> Tuple[bool, str, float, int]:
    start = time.monotonic()
    end = min(start + stage.timeout, deadline)
    next_note = start + 10
    polls = 0
    while True:
        ok, detail = evaluate(stage, ctx)
        polls += 1
        now = time.monotonic()
        if ok or now >= end:
            return ok, detail, now - start, polls
        if now >= next_note:
            progress(now - start, detail)
            next_note = now + 10
        time.sleep(max(0.0, min(interval, end - now)))


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wait for SONiC bringup stages and report where it is stuck.")
    parser.add_argument("--interval", type=float, default=2.0, help="poll interval in seconds (default 2)")
    parser.add_argument("--total-timeout", type=float, default=0, help="cap on the whole run in seconds (0 = none)")
    parser.add_argument("--stage-timeout", action="append", default=[], metavar="NAME=SECONDS",
                        help="override a stage timeout; repeatable")
    parser.add_argument("--warm", action="store_true", help="also wait for warm-reboot reconciliation")
    parser.add_argument("--skip-optional", action="store_true", help="drop optional stages")
    parser.add_argument("--status", action="store_true", help="check each stage once without waiting")
    parser.add_argument("--one-shot", action="store_true",
                        help="check each stage once, in order; stop at the first failure")
    parser.add_argument("--list", action="store_true", help="list stages and the keys they wait on")
    parser.add_argument("--json", action="store_true", help="print a JSON summary instead of progress lines")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    stages = build_stages(args.warm)
    if args.skip_optional:
        stages = [s for s in stages if not s.optional]
    by_name = {s.name: s for s in stages}
    for item in args.stage_timeout:
        name, _, secs = item.partition("=")
        try:
            by_name[name].timeout = float(secs)
        except (KeyError, ValueError):
            print(f"error: invalid --stage-timeout '{item}' (stages: {', '.join(by_name)})", file=sys.stderr)
            return 2
    total = len(stages)
    if args.status and args.one_shot:
        print("error: --status and --one-shot are mutually exclusive", file=sys.stderr)
        return 2
    single = args.status or args.one_shot

    if args.list:
        for idx, stage in enumerate(stages, 1):
            opt = " (optional)" if stage.optional else ""
            print(f"{idx:>2}. {stage.name:<22} {stage.timeout:>5.0f}s  {stage.waits_on}{opt}")
        return 0

    try:
        ctx = Ctx(Redis())
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    def emit(line: str) -> None:
        if not args.json:
            print(line, flush=True)

    def row(tag: str, idx: int, stage: Stage, secs: float, detail: str) -> None:
        emit(f"[{tag}] {idx:>2}/{total} {stage.name:<22} {secs:7.1f}s  {detail}")

    if args.status:
        mode = "status check"
    elif args.one_shot:
        mode = "one-shot check"
    else:
        mode = f"waiting, poll every {args.interval:g}s"
    emit(f"sonic-readiness-waiter: {total} stages ({mode})")
    start_uptime = read_uptime()
    boot_epoch = time.time() - start_uptime if start_uptime is not None else None
    deadline = time.monotonic() + args.total_timeout if args.total_timeout > 0 else float("inf")
    results = []
    stuck: Optional[Tuple[int, Stage, str]] = None
    for idx, stage in enumerate(stages, 1):
        if stuck is not None and not args.status:
            emit(f"[----] {idx:>2}/{total} {stage.name:<22} {'':>8}  not reached")
            results.append({"stage": stage.name, "status": "NOT_REACHED"})
            continue
        polls = 1
        if single:
            start = time.monotonic()
            ok, detail = evaluate(stage, ctx)
            secs = time.monotonic() - start
        else:
            ok, detail, secs, polls = wait_for(stage, ctx, args.interval, deadline,
                                               lambda el, d, i=idx, s=stage: row("WAIT", i, s, el, d))
        status = "PASS" if ok else ("WARN" if stage.optional else "FAIL")
        row(status, idx, stage, secs, detail)
        entry = {"stage": stage.name, "status": status, "seconds": round(secs, 1), "detail": detail}
        if ok and not single:
            up = read_uptime()
            if up is not None:
                entry["uptime"] = round(up, 1)
                entry["at_first_check"] = polls == 1
        if stage.check is chk_dockers_up and ctx.down_services:
            entry["down"] = ctx.down_services
        results.append(entry)
        if status == "FAIL" and stuck is None:
            stuck = (idx, stage, detail)

    if stuck is not None:
        idx, stage, detail = stuck
        emit("")
        label = "CURRENT STAGE" if args.status else ("FAILED AT" if args.one_shot else "STUCK AT")
        emit(f"{label}: {stage.name} ({idx}/{total})")
        emit(f"  waiting on: {stage.waits_on}")
        emit(f"  last seen : {detail}")
        emit(f"  hint      : {stage.hint}")
    else:
        emit("")
        emit("SONiC bringup complete: all required stages PASS")

    uptimes = {e["stage"]: e["uptime"] for e in results if "uptime" in e}
    milestones = {"all_dockers_up": uptimes.get("DOCKERS_UP"),
                  "system_ready": uptimes.get("SYSTEM_READY")}
    boot_str = (time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(boot_epoch))
                if boot_epoch is not None else None)

    if not single and start_uptime is not None:
        emit("")
        emit(f"BOOT TIMELINE  (boot {boot_str}, waiter started at uptime {start_uptime:.1f}s)")
        prev = None
        for e in results:
            if "uptime" not in e:
                continue
            first = e["at_first_check"]
            gap = f"+{e['uptime'] - prev:.1f}s" if prev is not None and not first else ""
            emit(f"   {e['stage']:<22} {'<=' if first else '  '}{e['uptime']:7.1f}s  {gap:>7}")
            prev = e["uptime"]
        for name, val in (("all dockers up", milestones["all_dockers_up"]),
                          ("SYSTEM_READY", milestones["system_ready"])):
            emit(f" Boot -> {name:<15}: " + (f"{val:.1f}s" if val is not None else "not reached"))
        emit(" ('<=' already passed at first check; times accurate to about the poll interval)")

    if args.json:
        summary = {"ready": stuck is None,
                   "stuck_at": stuck[1].name if stuck else None,
                   "stages": results}
        if not single:
            summary["boot_time"] = boot_str
            summary["waiter_start_uptime"] = round(start_uptime, 1) if start_uptime is not None else None
            summary["milestones"] = milestones
        print(json.dumps(summary, indent=2))
    return 1 if stuck is not None else 0


if __name__ == "__main__":
    sys.exit(main())

