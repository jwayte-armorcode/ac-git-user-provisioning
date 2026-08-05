#!/usr/bin/env python3
"""Extract every SCM, then provision — one command, for interactive use.

    python run_all.py                # dry run end to end
    python run_all.py --apply

This is a convenience wrapper for working by hand. It deliberately does NOT
reconcile: removing access should be a decision someone makes explicitly,
after reading a report, not something that happens because they ran the
"do everything" command.

For the weekly job, call the steps separately so a failed extract stops the
pipeline before anything is removed:

    python extract.py --scm all && python reconcile.py --apply

The && matters. extract.py exits non-zero if any SCM did not complete, and
reconcile.py refuses an incomplete extract anyway — but chaining on success
means the second command never even starts, which is easier to see in a cron
log than a refusal buried in output.
"""

from __future__ import annotations

import argparse
import subprocess
import sys


def run(cmd: list[str]) -> int:
    """Run a step, streaming its output, and return its exit code."""
    print(f"\n$ {' '.join(cmd)}\n", flush=True)
    return subprocess.call(cmd)


def main():
    parser = argparse.ArgumentParser(
        description="Extract every SCM then provision (interactive convenience)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python run_all.py                 # dry run\n"
            "  python run_all.py --apply\n"
            "\n"
            "This never reconciles. For the weekly job:\n"
            "  python extract.py --scm all && python reconcile.py --apply\n"
        ),
    )
    parser.add_argument("--env", default="envfile",
                        help="Path to the ini config file (default: envfile).")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap repos per SCM during extract. Marks the extracts "
                             "PARTIAL, so the provision step will refuse them "
                             "without --force-extracts — useful only for a smoke "
                             "test of the extract itself.")
    parser.add_argument("--apply", action="store_true",
                        help="Write to ArmorCode. Without this the provision step "
                             "is a dry run (the extract always writes its own "
                             "files).")
    parser.add_argument("--force", action="store_true",
                        help="Passed through to provision.py, for a tenant where "
                             "most sub-products have no Repository URL set.")
    parser.add_argument("--dump-json", action="store_true",
                        help="Passed through to provision.py.")

    args, extra = parser.parse_known_args()
    py = sys.executable

    extract_cmd = [py, "extract.py", "--env", args.env, "--scm", "all"]
    if args.limit is not None:
        extract_cmd += ["--limit", str(args.limit)]

    code = run(extract_cmd)
    if code != 0:
        print(f"\n[abort] extract exited {code} — not provisioning from an "
              f"incomplete set of extracts.")
        print("        Fix the SCM(s) reported above and re-run.")
        sys.exit(code)

    provision_cmd = [py, "provision.py", "--env", args.env]
    if args.apply:
        provision_cmd.append("--apply")
    if args.force:
        provision_cmd.append("--force")
    if args.dump_json:
        provision_cmd.append("--dump-json")
    provision_cmd += extra          # anything else goes straight through

    code = run(provision_cmd)
    if code != 0:
        sys.exit(code)

    print("\n[run_all] done.")
    if not args.apply:
        print("[run_all] that was a dry run — re-run with --apply to write.")
    print("[run_all] this command does not reconcile. To remove stale "
          "memberships:")
    print("          python reconcile.py            # report first")
    print("          python reconcile.py --apply")


if __name__ == "__main__":
    main()
