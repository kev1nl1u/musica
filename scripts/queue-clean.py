#!/usr/bin/env python3
"""Remove a stuck job from the deemix queue.

deemix keeps each queued item as a JSON file under config/queue/, and reloads
them all on start. A job can drop out of order.json -- so it vanishes from the
UI -- while its file stays behind and gets reloaded every restart. One such
orphan looped on an unavailable track for two days (see the README), holding a
concurrency slot and leaking memory.

Removing the file is not enough on its own: the running server still holds the
job in memory. So the container is stopped, the file (and any order.json entry)
removed, and the container started again on a clean queue.

  queue-clean.py                 list the queue
  queue-clean.py UUID [UUID...]  remove these
  queue-clean.py --orphans       remove everything not in order.json
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from stackconfig import ROOT
QUEUE = ROOT / "deemix/config/queue"
ORDER = QUEUE / "order.json"


def log(msg):
    print(msg, flush=True)


def read_order():
    if not ORDER.exists():
        return []
    try:
        return json.loads(ORDER.read_text())
    except Exception:
        return []


def jobs():
    order = read_order()
    out = []
    for f in sorted(QUEUE.glob("*.json")):
        if f.name == "order.json":
            continue
        try:
            d = json.loads(f.read_text())
        except Exception:
            d = {}
        uuid = d.get("uuid", f.stem)
        out.append(
            {
                "uuid": uuid,
                "file": f,
                "title": d.get("title", "?"),
                "orphan": uuid not in order,
            }
        )
    return out


def docker(action):
    subprocess.run(["docker", action, "deemix"], check=True, capture_output=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("uuids", nargs="*")
    ap.add_argument("--orphans", action="store_true",
                    help="remove every job missing from order.json")
    args = ap.parse_args()

    current = jobs()
    if not args.uuids and not args.orphans:
        if not current:
            log("coda vuota")
            return 0
        for j in current:
            log(f"{'[orfano] ' if j['orphan'] else '          '}{j['uuid']}  {j['title']}")
        log("\nPassa un UUID per rimuoverlo, o --orphans per tutti gli orfani.")
        return 0

    # Match only against real queue files, so a bad UUID can never point the
    # removal at an arbitrary path.
    by_uuid = {j["uuid"]: j for j in current}
    if args.orphans:
        targets = [j for j in current if j["orphan"]]
    else:
        targets = []
        for u in args.uuids:
            if u in by_uuid:
                targets.append(by_uuid[u])
            else:
                log(f"nessun job con uuid {u!r}, salto")

    if not targets:
        log("niente da rimuovere")
        return 0

    for j in targets:
        log(f"rimuovo {j['uuid']}  {j['title']}")

    docker("stop")
    try:
        order = read_order()
        remove = {j["uuid"] for j in targets}
        for j in targets:
            j["file"].unlink(missing_ok=True)
        kept = [u for u in order if u not in remove]
        if kept != order:
            ORDER.write_text(json.dumps(kept))
    finally:
        docker("start")
        time.sleep(4)

    log(f"\nrimossi {len(targets)} job, deemix riavviato")
    remaining = jobs()
    log(f"in coda ora: {len(remaining)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
