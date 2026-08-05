#!/usr/bin/env python3
"""Extract repos and their members from one or more SCMs.

Writes each SCM's result into a directory named for its mnemonic, and makes
NO ArmorCode calls at all — this phase only reads source control.

    python extract.py --scm all
    python extract.py --scm gh-main --limit 10
    python extract.py --scm gitlab-prod --repo mygroup/juice-shop

What's collected per repo: its web URL (the key later matched against an
ArmorCode sub-product's repoLink) and its members, split into those with a
resolvable email and those without.

Nothing here decides team names. That comes later, from the ArmorCode Group
of the sub-product whose repoLink matches the repo URL.

--limit, --repo and --changed-since all mark the extract PARTIAL. Provision
and reconcile refuse a partial extract by default, because under the
strict-mirror reconciliation rule a repo that was never read is
indistinguishable from a repo with no members — and acting on that
difference removes people's access.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

import config
import extract_store
from extract_store import ExtractStore, RepoRecord
from scm_readers import build_reader

# How often to print the progress heartbeat, in repos.
PROGRESS_EVERY = 25


def _fmt_duration(seconds: float) -> str:
    """Human-readable duration for ETAs: 45s, 12m, 3h20m."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    return f"{hours}h{minutes:02d}m" if minutes else f"{hours}h"


def extract_one(scm_cfg, base_dir, limit, repo, changed_since) -> bool:
    """Extract one SCM. Returns True on a clean, complete run."""
    store = ExtractStore(base_dir, scm_cfg.mnemonic)

    # Any of these means the extract does not represent the whole SCM.
    partial_reason = None
    if limit is not None:
        partial_reason = f"--limit {limit}"
    elif repo is not None:
        partial_reason = f"--repo {repo}"
    elif changed_since is not None:
        partial_reason = f"--changed-since {changed_since.date()}"

    print(f"\n{'='*70}")
    print(f"  {scm_cfg.mnemonic}  ({scm_cfg.type} @ {scm_cfg.url})")
    print(f"{'='*70}")
    if partial_reason:
        print(f"[partial] {partial_reason} — this extract will be marked PARTIAL "
              f"and refused by provision/reconcile without --force")

    meta = store.begin(scm_cfg.type, scm_cfg.url, limit, partial_reason)

    try:
        reader = build_reader(scm_cfg)
    except Exception as e:
        print(f"[error] could not connect: {e}")
        store.fail(meta, f"connect: {e}")
        return False

    try:
        print("[read] listing repos (sorted by id for a stable order)...")
        all_repos = reader.load_repos(repo=repo, changed_since=changed_since)
        meta.repos_listed = len(all_repos)
        print(f"[read] {len(all_repos)} repo(s) visible to this token")

        to_process = len(all_repos) if limit is None else min(len(all_repos), limit)
        started = time.monotonic()
        records: list[RepoRecord] = []
        seen = 0

        for scm_repo in reader.iter_repos(all_repos, limit=limit):
            seen += 1
            full_name = reader.repo_full_name(scm_repo)
            url = reader.repo_url(scm_repo)

            members = reader.get_members(scm_repo)
            with_email = [m for m in members if m.get("email")]
            without = [m for m in members if not m.get("email")]

            records.append(RepoRecord(
                scm=scm_cfg.mnemonic,
                repo_id=reader.repo_id(scm_repo),
                full_name=full_name,
                name=reader.repo_name(scm_repo),
                url=url,
                members=with_email,
                members_missing_email=without,
            ))

            if not url:
                print(f"  [warn] {full_name}: no web URL — cannot be matched to a "
                      f"sub-product")

            if seen % PROGRESS_EVERY == 0 or seen == to_process:
                elapsed = time.monotonic() - started
                rate = seen / elapsed if elapsed > 0 else 0
                pct = (seen / to_process * 100) if to_process else 100.0
                msg = (f"[progress] {seen}/{to_process} repos ({pct:.0f}%), "
                       f"{rate:.1f} repo/s")
                if rate > 0 and seen < to_process:
                    msg += f", ~{_fmt_duration((to_process - seen) / rate)} remaining"
                print(msg)

        store.write(meta, records)

    except KeyboardInterrupt:
        # Explicitly marked failed rather than left 'running': an operator who
        # hits Ctrl-C knows why, but a downstream run a week later would not.
        print("\n[abort] interrupted")
        store.fail(meta, "interrupted by user")
        return False
    except Exception as e:
        print(f"[error] extract failed: {e}")
        store.fail(meta, str(e))
        return False

    print(f"\n[done] {meta.repos_written} repo(s), {meta.members_total} member(s) "
          f"({meta.members_with_email} with email)")
    print(f"       status: {meta.status.upper()}  ->  {store.dir}/")
    return meta.status == extract_store.STATUS_COMPLETE


def main():
    parser = argparse.ArgumentParser(
        description="Extract repos and members from configured SCMs "
                    "(no ArmorCode calls)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python extract.py --scm all\n"
            "  python extract.py --scm gh-main --limit 10\n"
            "  python extract.py --scm gitlab-prod --repo mygroup/juice-shop\n"
        ),
    )
    parser.add_argument("--scm", default="all",
                        help="Which SCM to extract: a mnemonic from the config, "
                             "or 'all' (default) for every configured SCM.")
    parser.add_argument("--env", default="envfile",
                        help="Path to the ini config file (default: envfile).")
    parser.add_argument("--out-dir", default=".",
                        help="Parent directory for the per-SCM output "
                             "directories (default: current directory).")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process at most N repos per SCM. Marks the extract "
                             "PARTIAL, which provision/reconcile refuse by "
                             "default — this is a testing knob, not a way to "
                             "shorten a real run.")
    parser.add_argument("--repo", default=None,
                        help="Restrict to a single repo. GitHub: 'owner/name'. "
                             "GitLab: short path or 'namespace/path'. Marks the "
                             "extract PARTIAL.")
    parser.add_argument("--changed-since", default=None, metavar="YYYY-MM-DD",
                        help="Only repos updated at or after this date. Marks the "
                             "extract PARTIAL, because a member publishing an "
                             "email or gaining access via a group does not change "
                             "the repo's timestamp.")

    args = parser.parse_args()

    try:
        cfg = config.load_config(args.env)
    except config.ConfigError as e:
        print(f"[error] {e}")
        sys.exit(1)

    changed_since = None
    if args.changed_since:
        try:
            changed_since = datetime.fromisoformat(args.changed_since).replace(
                tzinfo=timezone.utc)
        except ValueError:
            print(f"[error] --changed-since {args.changed_since!r} is not a valid "
                  f"date (expected YYYY-MM-DD)")
            sys.exit(1)

    try:
        scms = cfg.select_scms(args.scm)
    except config.ConfigError as e:
        print(f"[error] {e}")
        sys.exit(1)

    if args.repo and len(scms) > 1:
        print("[error] --repo needs a single --scm (it names one repo in one SCM)")
        sys.exit(1)

    print(f"[config] {len(scms)} SCM(s): {', '.join(s.mnemonic for s in scms)}")

    results = {}
    for scm_cfg in scms:
        results[scm_cfg.mnemonic] = extract_one(
            scm_cfg, args.out_dir, args.limit, args.repo, changed_since)

    print(f"\n{'='*70}")
    complete = sum(1 for ok in results.values() if ok)
    print(f"  {complete}/{len(results)} SCM(s) extracted cleanly")
    for mnemonic, ok in sorted(results.items()):
        store = ExtractStore(args.out_dir, mnemonic)
        meta = store.load_meta()
        status = meta.status if meta else "unknown"
        print(f"    {'ok  ' if ok else 'WARN'}  {mnemonic:<20} {status}")
    print(f"{'='*70}\n")

    # Non-zero if any SCM did not complete, so a cron pipeline
    # (extract && reconcile) stops before anything can be removed.
    sys.exit(0 if complete == len(results) else 1)


if __name__ == "__main__":
    main()
