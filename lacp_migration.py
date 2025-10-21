# lacp_migration.py — EXOS Python (on-box), robust & ASCII-safe

import time
import re

try:
    import exsh  # EXOS shell API
except Exception:
    raise SystemExit("Run this on an EXOS switch (exsh required).")

########################
# Config (adjust)
########################

PRIMARY_PORT      = "1:60"          # LAG key-port
GROUPING_PORTS    = "1:60,2:60"     # full member list (VPEX example)
ALGORITHM_CLI     = "address-based L2"
REACHABILITY_IP   = "8.8.8.8"

OVERALL_TIMEOUT_S = 120   # total wait window
STABLE_REQUIRED_S = 60    # stable reachability before commit
PING_INTERVAL_S   = 2

# save names without dots/slashes to avoid prompts
BACKUP_NAME_PRE   = "lacp_prechange"
BACKUP_NAME_POST  = "primary"

# ping command template gets detected at runtime, e.g. "ping count 1 {}"
PING_CMD_TEMPLATE = None

########################
# Helpers
########################
def sanitize(s):
    if isinstance(s, bytes):
        s = s.decode('utf-8', 'ignore')
    return s.encode('ascii', 'ignore').decode('ascii')

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print("[{}] {}".format(ts, sanitize(str(msg))))

def cli(cmd, capture=True, ignore_error=False):
    """
    Execute CLI and never raise to Python; return (ok, output).
    When ignore_error=True, we swallow non-zero CLI return codes.
    """
    try:
        out = exsh.clicmd(cmd, capture)
        return True, out
    except Exception as e:
        # show trimmed error; many EXOS errors are benign for idempotent steps
        log("CLI error on '{}': {}".format(cmd, e))
        if ignore_error:
            return False, ""
        else:
            return False, ""

def try_save_named(name):
    """
    Best-effort save that avoids '.' and '/'.
    EXOS will store it in switch memory as a named config.
    """
    ok, _ = cli("save configuration {}".format(name), capture=True, ignore_error=True)
    if not ok:
        # Fallback: plain save (active partition). Also ignore errors.
        cli("save configuration", capture=True, ignore_error=True)
    return True

def sharing_present_on_primary():
    ok, out = cli("show ports sharing", capture=True, ignore_error=True)
    if not ok:
        return False
    for line in out.splitlines():
        # Lines usually start with the master port
        if re.search(r"^{}\b".format(re.escape(PRIMARY_PORT)), line):
            return True
    return False

def reset_sharing():
    # Always try both; ignore errors so it becomes idempotent.
    log("Reset sharing on {} (disable + unconfigure) ...".format(PRIMARY_PORT))
    cli("disable sharing {}".format(PRIMARY_PORT), capture=True, ignore_error=True)
    time.sleep(0.5)
    cli("unconfigure sharing {}".format(PRIMARY_PORT), capture=True, ignore_error=True)
    time.sleep(0.5)

def enable_sharing_lacp():
    log("Enable sharing LACP on {} (group {}, algo {})".format(PRIMARY_PORT, GROUPING_PORTS, ALGORITHM_CLI))
    cli("enable sharing {} grouping {} algorithm {} lacp"
        .format(PRIMARY_PORT, GROUPING_PORTS, ALGORITHM_CLI), capture=True, ignore_error=False)
    time.sleep(0.5)
    cli("configure sharing {} lacp activity active".format(PRIMARY_PORT), capture=True, ignore_error=True)

def try_ping_with_template(ip, template):
    ok, out = cli(template.format(ip), capture=True, ignore_error=True)
    if not ok:
        return False
    o = out.lower()
    # Accept success patterns from different EXOS versions
    return ("bytes from" in o) or (" 1 received" in o) or ("1 packets received" in o) or ("1 packet received" in o)

def detect_ping_template():
    """
    Try several EXOS ping syntaxes and cache the first that succeeds.
    Order is chosen based on common firmware variants.
    """
    candidates = [
        "ping count 1 {}",       # <-- your box accepts this
        "ping {}",               # one-shot default (some builds send 5; we still parse 'bytes from')
        "ping ipv4 count 1 {}",  # older/newer variants
    ]
    for tmpl in candidates:
        if try_ping_with_template(REACHABILITY_IP, tmpl):
            return tmpl
    return None

def ping_ok():
    global PING_CMD_TEMPLATE
    if PING_CMD_TEMPLATE is None:
        PING_CMD_TEMPLATE = detect_ping_template()
        if PING_CMD_TEMPLATE is None:
            log("Could not find a working ping syntax on this EXOS image.")
            return False
        else:
            log("Detected ping syntax: '{}'".format(PING_CMD_TEMPLATE))
    return try_ping_with_template(REACHABILITY_IP, PING_CMD_TEMPLATE)


def reachability_monitor():
    log("Start reachability monitor to {} ...".format(REACHABILITY_IP))
    start = time.time()
    stable_since = None
    while True:
        ok = ping_ok()
        now = time.time()
        if ok:
            if stable_since is None:
                stable_since = now
                log("Reachability OK - starting stability window ...")
            else:
                stable = int(now - stable_since)
                log("Reachability still OK ({}s)".format(stable))
                if stable >= STABLE_REQUIRED_S:
                    log("Stability target reached (>= {}s)".format(STABLE_REQUIRED_S))
                    return True
        else:
            if stable_since is not None:
                log("Reachability lost - resetting stability window.")
            else:
                log("No reachability yet - waiting ...")
            stable_since = None
        if (now - start) > OVERALL_TIMEOUT_S:
            log("Timeout ({}s) without stable reachability.".format(OVERALL_TIMEOUT_S))
            return False
        time.sleep(PING_INTERVAL_S)

def rollback_to_static_sharing():
    # Roll back to pre-change state: disable/unconfigure and re-enable sharing WITHOUT LACP
    log("Rollback: restoring static sharing (no LACP) on {} with group {} ...".format(PRIMARY_PORT, GROUPING_PORTS))
    cli("disable sharing {}".format(PRIMARY_PORT), capture=True, ignore_error=True)
    time.sleep(0.5)
    cli("unconfigure sharing {}".format(PRIMARY_PORT), capture=True, ignore_error=True)
    time.sleep(0.5)
    cli("enable sharing {} grouping {} algorithm {}"
        .format(PRIMARY_PORT, GROUPING_PORTS, ALGORITHM_CLI), capture=True, ignore_error=True)
    time.sleep(0.5)
    # Bounce master once to flush state
    log("Rollback: cycling master port {} to clear LAG state ...".format(PRIMARY_PORT))
    cli("disable ports {}".format(PRIMARY_PORT), capture=True, ignore_error=True)
    time.sleep(1.0)
    cli("enable ports {}".format(PRIMARY_PORT), capture=True, ignore_error=True)


########################
# Main
########################

def main():
    log("=== LACP migration (EXOS access -> VOSS SMLT) ===")
    log("Params: PRIMARY_PORT={}, GROUP={}, TARGET={}"
        .format(PRIMARY_PORT, GROUPING_PORTS, REACHABILITY_IP))

    # Detect working ping syntax up front
    _ = ping_ok()  # will trigger detection + log the chosen template


    # 1) Best-effort backup (avoid interactivity)
    log("Saving pre-change config name '{}' ...".format(BACKUP_NAME_PRE))
    try_save_named(BACKUP_NAME_PRE)

    # 2) Idempotent reset then enable LACP
    if sharing_present_on_primary():
        log("Existing sharing detected on {}.".format(PRIMARY_PORT))
    reset_sharing()
    enable_sharing_lacp()

    log("EXOS side switched to LACP. >>> Now configure SMLT/LAG on the two VOSS cores. <<<")

    # 3) Reachability loop
    success = reachability_monitor()

    # 4) Commit or rollback
    if success:
        log("Link up and stable - saving running config as '{}'.".format(BACKUP_NAME_POST))
        try_save_named(BACKUP_NAME_POST)
        log("Saved. Migration complete.")
    else:
        log("Link not stable - performing SOFT ROLLBACK (no reboot, no save).")
        rollback_to_static_sharing()
        log("Soft rollback done. Configuration NOT saved.")
        log("If you still want to reboot without saving, run manually at the CLI:")
        log("  use configuration {}".format(BACKUP_NAME_PRE))
        log("  reboot all   (then answer 'n' to the save prompt)")


if __name__ == "__main__":
    main()
