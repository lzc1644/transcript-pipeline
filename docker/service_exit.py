"""Keep Supervisor's API and TLS programs in one container lifecycle."""
from __future__ import annotations

import os
import sys

from supervisor import childutils


def main():
    while True:
        _, payload = childutils.listener.wait(sys.stdin, sys.stdout)
        fields = dict(item.split(":", 1) for item in payload.splitlines()[0].split())
        childutils.listener.ok(sys.stdout)
        if fields.get("processname") in {"api", "https"}:
            print(f"{fields['processname']} exited; stopping the container services", file=sys.stderr, flush=True)
            childutils.getRPCInterface(os.environ).supervisor.shutdown()
            return


if __name__ == "__main__":
    main()
