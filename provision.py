#!/usr/bin/env python3
"""Provision ArmorCode teams, users and scope from the SCM extracts.

    python provision.py                  # dry run (default)
    python provision.py --apply
    python provision.py --apply --limit 5
    python provision.py --dump-json

Reads EVERY configured SCM's extract, matches each repo to an ArmorCode
sub-product by URL, and provisions one team per ArmorCode Group.

There is deliberately no --scm flag. A Group can hold sub-products whose
repoLinks point at different SCMs, so a team's membership is the union
across all of them; provisioning from one SCM would compute a partial
member set and write it as though complete. For the same reason the run
refuses to start unless every configured SCM has a complete, non-partial
extract.

Additive only: this creates users, creates and scopes teams, and adds
members. It never removes anything — that is reconcile.py's job, and it is
a separate command precisely because it can take access away.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys

import config
import email_exceptions
import extract_store
import model
import snapshot
from armorcode import (
    ArmorCodeClient,
    ArmorCodeState,
    add_user_to_team,
    merge_scope_into_team,
    user_label,
)
from matching import SubProductIndex

# If more than this fraction of repos match no sub-product, stop. At that
# point the likely cause is a URL-format mismatch (normalisation missing a
# form the tenant uses), not thousands of genuine onboarding gaps — and
# provisioning from a broken match would create a handful of teams while
# silently ignoring most of the estate.
UNMATCHED_ABORT_RATE = 0.5

UNMATCHED_CSV_COLUMNS = ["scm", "repo", "url", "reason"]


def write_unmatched_csv(path: str, rows: list[dict]) -> None:
    """Write the unmatched-repo report, overwriting any previous one.

    Overwritten rather than appended: it reports THIS run, so a stale row
    for a repo since onboarded would be misleading. Written even when empty
    (header only), so a previous run's rows can't linger and misreport.
    """
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=UNMATCHED_CSV_COLUMNS)
        writer.writeheader()
        for r in rows:
            writer.writerow({c: r.get(c, "") for c in UNMATCHED_CSV_COLUMNS})


def provision_users(ac, state, agg, default_role, dry_run) -> dict:
    """Create any ArmorCode users that don't exist yet, once per email.

    Returns email -> user record for everyone that exists (or would).
    Existence is checked against the tenant user list already loaded at
    startup, so an unchanged user costs no API call.
    """
    print(f"\n{'-'*70}\n  Users: {len(agg.users)} distinct member(s) with an email\n{'-'*70}")
    records: dict[str, dict] = {}
    created = 0
    would_create: list[str] = []

    for email, info in sorted(agg.users.items()):
        existing = state.users_by_email.get(email)
        if existing:
            records[email] = existing
            continue
        if dry_run:
            would_create.append(f"{info['name']} <{info['email']}>")
            continue
        try:
            rec = ac.create_user(name=info["name"], email=info["email"],
                                 tenant_role=default_role)
            rec.setdefault("teamInfo", [])
            state.users_by_email[email] = rec
            records[email] = rec
            created += 1
        except Exception as e:
            print(f"    [error] failed to create user {info['email']}: {e}")

    if dry_run:
        print(f"  [dry_run] {len(records)} already exist, "
              f"{len(would_create)} would be created"
              f"{':' if would_create else ''}")
        for label in would_create:
            print(f"    - {label}")
    else:
        print(f"  {len(records) - created} already existed, {created} created")
    return records


def provision_teams(ac, state, agg, user_records, default_role, dry_run,
                    limit, exceptions_file, today) -> dict:
    """Create or update each team exactly once, then add its members.

    Teams are processed in sorted order so --limit takes the same slice on
    every run rather than a shifting subset.
    """
    names = sorted(agg.teams)
    total = len(names)
    if limit is not None:
        names = names[:limit]

    print(f"\n{'-'*70}\n  Teams: {len(names)}"
          f"{f' of {total} (--limit {limit})' if limit is not None else ''}"
          f"\n{'-'*70}")

    stats = {"created": 0, "scoped": 0, "members_added": 0, "errors": 0}

    for team_name in names:
        plan = agg.teams[team_name]
        scope_entries = plan.scope_entries

        label = f"[team] {team_name}"
        if plan.group_name != team_name:
            label += f"  (Group {plan.group_name!r})"
        print(f"\n{label}  ({len(plan.repos)} repo(s), {len(plan.members)} member(s), "
              f"{len(plan.sub_product_ids)} sub-product(s))")

        team = state.find_team(team_name)
        if team is None:
            print(f"    [create-team] {team_name}")
            if dry_run:
                team = {"id": None, "name": team_name}
                if scope_entries:
                    print(f"      [dry_run] would create with scope: {scope_entries}")
            else:
                try:
                    group_scopes = [(e["product"], e["subProduct"]) for e in scope_entries]
                    team = ac.create_team_scoped(
                        name=team_name, group_scopes=group_scopes,
                        business_unit_id=state.business_unit_id,
                        business_unit_name=state.business_unit_name,
                    )
                    state.register_team(team)
                    stats["created"] += 1
                except Exception as e:
                    print(f"      [error] failed to create team {team_name!r}: {e}")
                    stats["errors"] += 1
                    continue
        else:
            print(f"    [team] exists (id={team['id']})")
            if scope_entries:
                if dry_run:
                    print(f"      [dry_run] would merge scope: {scope_entries}")
                else:
                    try:
                        body, changed = merge_scope_into_team(
                            ac, team, scope_entries,
                            business_unit_id=state.business_unit_id,
                            business_unit_name=state.business_unit_name,
                        )
                        if changed:
                            ac.put_team(body)
                            print(f"      [update] scope merged: {scope_entries}")
                            stats["scoped"] += 1
                        else:
                            print("      [noop] scope already covers these sub-products")
                    except Exception as e:
                        print(f"      [error] failed to merge scope: {e}")
                        stats["errors"] += 1

        # Membership, once per (team, user).
        member_records = [user_records[e] for e in sorted(plan.members)
                          if e in user_records]
        pending = [f"{n} <{e}> (would be created)"
                   for e, n in sorted(plan.members.items()) if e not in user_records]

        if member_records or pending:
            if dry_run:
                labels = [user_label(r) for r in member_records] + pending
                print(f"      [dry_run] would ensure {len(labels)} member(s) on team:")
                for lab in labels:
                    print(f"        - {lab}")
            else:
                added, already = [], []
                for record in member_records:
                    try:
                        if add_user_to_team(ac, record, team["id"], default_role):
                            added.append(user_label(record))
                        else:
                            already.append(user_label(record))
                    except Exception as e:
                        uid = record.get("userId") or record.get("id")
                        print(f"      [error] failed to add user {uid}: {e}")
                        stats["errors"] += 1
                if added:
                    print(f"      [update] added {len(added)} member(s):")
                    for lab in added:
                        print(f"        - {lab}")
                    stats["members_added"] += len(added)
                if already:
                    print(f"      [noop] {len(already)} member(s) already on team")

        # Members with no resolvable email: logged per (repo, team) so the CSV
        # names where the person was found. Grouped by repo because
        # log_exceptions() rewrites the file on each call.
        if not dry_run and plan.members_missing_email:
            by_repo: dict[tuple, list] = {}
            for miss in plan.members_missing_email:
                by_repo.setdefault((miss["scm"], miss["repo"]), []).append(miss["member"])
            for (scm, repo_full), members in by_repo.items():
                email_exceptions.log_exceptions(
                    exceptions_file, scm, repo_full, team_name, members, today)

    if limit is not None and total > len(names):
        print(f"\n  [limit] processed {len(names)} of {total} team(s) "
              f"(--limit {limit}); {total - len(names)} left untouched")
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Provision ArmorCode teams/users/scope from the SCM extracts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python provision.py                 # dry run\n"
            "  python provision.py --dump-json     # dry run + inspect the plan\n"
            "  python provision.py --apply --limit 5\n"
            "  python provision.py --apply\n"
        ),
    )
    parser.add_argument("--env", default="envfile",
                        help="Path to the ini config file (default: envfile).")
    parser.add_argument("--extract-dir", default=".",
                        help="Parent directory holding the per-SCM extract "
                             "directories (default: current directory).")
    parser.add_argument("--limit", type=int, default=None,
                        help="Provision at most N teams (sorted order). Caps teams "
                             "WRITTEN, not repos read — each team still gets its "
                             "full membership, so a limited run is correct as far "
                             "as it goes. Good for a first --apply.")
    parser.add_argument("--default-role", default=None,
                        help="tenantRole for newly-created users. Overrides the "
                             "config file.")
    parser.add_argument("--exceptions-file", default="email_exceptions.csv",
                        help="CSV of members with no resolvable email "
                             "(default: email_exceptions.csv).")
    parser.add_argument("--unmatched-csv", nargs="?", const="unmatched_repos.csv",
                        default=None, metavar="PATH",
                        help="Write repos that matched no sub-product to CSV. "
                             "Overwritten each run; written in dry runs too.")
    parser.add_argument("--dump-json", action="store_true",
                        help="Write provision_plan.json — the full aggregated "
                             "picture (teams, members, scope) before anything is "
                             "written. The clearest way to review a run.")
    parser.add_argument("--force", action="store_true",
                        help="Proceed despite unusable extracts or an implausibly "
                             "high unmatched rate. Both guards exist to catch "
                             "broken input; override only when you know why.")
    parser.add_argument("--no-snapshot", action="store_true",
                        help="Skip the pre-write snapshot. Not recommended: the "
                             "snapshot is what restore.py rebuilds from if a run "
                             "turns out to be wrong.")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=None,
                      help="Print what would happen without writing (default)")
    mode.add_argument("--apply", action="store_true", default=None,
                      help="Write changes to ArmorCode")

    args = parser.parse_args()
    dry_run = not args.apply

    try:
        cfg = config.load_config(args.env)
    except config.ConfigError as e:
        print(f"[error] {e}")
        sys.exit(1)

    mnemonics = sorted(cfg.scms)
    print(f"\n{'='*70}")
    print(f"  ArmorCode provision ({'DRY RUN' if dry_run else 'APPLY'})")
    print(f"{'='*70}")
    print(f"[config] tenant {cfg.armorcode_url}")
    print(f"[config] SCMs: {', '.join(mnemonics)}")

    # Precondition: every SCM must have a usable extract. A missing or
    # truncated one silently under-reports membership.
    ok, problems = extract_store.check_all_usable(args.extract_dir, mnemonics)
    if not ok:
        print("\n[error] not every SCM has a usable extract:")
        for p in problems:
            print(f"    - {p}")
        if not args.force:
            print("\n  Provisioning from a partial view would write incomplete team "
                  "membership.\n  Re-run the extracts, or pass --force if you "
                  "understand the consequences.")
            sys.exit(1)
        print("\n  [force] continuing anyway — team membership may be incomplete")

    repos = extract_store.load_all_repos(args.extract_dir, mnemonics)
    print(f"[extract] {len(repos)} repo(s) across {len(mnemonics)} SCM(s)")
    if not repos:
        print("[error] no repos in the extracts — nothing to do")
        sys.exit(1)

    ac = ArmorCodeClient(tenant_url=cfg.armorcode_url, token=cfg.armorcode_token)

    default_role = args.default_role or cfg.default_role
    # Validated up front: an unknown role otherwise fails per-user deep into
    # a run with 400 "Provided Tenant Role Not Found", after teams and scope
    # have already been written.
    try:
        valid = ac.get_role_names()
    except Exception as e:
        print(f"[warn] could not fetch tenant roles to validate {default_role!r}: {e}")
    else:
        if default_role not in valid:
            print(f"[error] role {default_role!r} does not exist in this tenant.")
            print(f"        Valid roles: {', '.join(sorted(valid))}")
            sys.exit(1)
    print(f"[config] new users will be created with role: {default_role!r}")

    print("\n[armorcode] loading sub-products (1 call, includes repoLink + Group)...")
    index = SubProductIndex(ac.get_sub_products_full())
    print(f"[armorcode] {index.total} sub-product(s), {index.indexed} with a "
          f"matchable repoLink ({index.without_link} have none)")

    agg = model.aggregate(repos, index)
    print(f"[match] {agg.matched_repos}/{agg.total_repos} repo(s) matched "
          f"({agg.match_rate*100:.0f}%) -> {len(agg.teams)} team(s), "
          f"{len(agg.users)} user(s)")

    if agg.folded_matches:
        print(f"[match] {len(agg.folded_matches)} repo(s) matched only after "
              f"case-folding the URL:")
        for f in agg.folded_matches[:10]:
            print(f"    - {f['scm']}:{f['repo']}")

    if args.dump_json:
        with open("provision_plan.json", "w") as fh:
            json.dump(model.to_jsonable(agg), fh, indent=2, sort_keys=True)
        print("[dump] wrote provision_plan.json")

    if args.unmatched_csv:
        write_unmatched_csv(args.unmatched_csv, agg.unmatched)
        print(f"[report] wrote {args.unmatched_csv} ({len(agg.unmatched)} row(s))")

    # A high unmatched rate almost always means URL normalisation is missing
    # a form this tenant uses, not that the estate is unonboarded.
    if agg.total_repos and agg.unmatched_rate > UNMATCHED_ABORT_RATE:
        print(f"\n[error] {len(agg.unmatched)} of {agg.total_repos} repo(s) "
              f"({agg.unmatched_rate*100:.0f}%) matched no sub-product.")
        print("        That usually means the repoLink format differs from the "
              "SCM's web URL,")
        print("        not that most repos are genuinely un-onboarded. Sample:")
        for u in agg.unmatched[:5]:
            print(f"          {u['scm']}:{u['repo']}  url={u['url'] or '(none)'}")
        print("        Check a sub-product's Repository URL in ArmorCode against "
              "the URLs above.")
        if not args.force:
            print("        Re-run with --unmatched-csv to see them all, or --force "
                  "to proceed.")
            sys.exit(1)
        print("        [force] continuing anyway")

    if not agg.teams:
        print("\n[done] no teams to provision")
        return

    # Snapshot before the first write, never on a dry run (nothing to undo).
    # This is the recovery point if a run turns out to have done the wrong
    # thing — see restore.py.
    if not dry_run and not args.no_snapshot:
        snapshot.take_snapshot(ac, cfg.armorcode_url, cfg.snapshots.path,
                               command="provision.py --apply")

    state = ArmorCodeState(ac)
    user_records = provision_users(ac, state, agg, default_role, dry_run)
    from datetime import date
    stats = provision_teams(ac, state, agg, user_records, default_role, dry_run,
                            args.limit, args.exceptions_file, date.today().isoformat())

    # Pruned only after the run succeeded, so a failure can't expire the
    # snapshot that would be needed to recover from it.
    if not dry_run and not args.no_snapshot:
        snapshot.prune(cfg.snapshots.path, cfg.snapshots.retention_days)

    print(f"\n{'='*70}")
    if dry_run:
        print(f"  DRY RUN — nothing written. {len(agg.teams)} team(s), "
              f"{len(agg.users)} user(s) would be provisioned.")
        print("  Re-run with --apply to write.")
    else:
        print(f"  Done. {stats['created']} team(s) created, "
              f"{stats['scoped']} re-scoped, {stats['members_added']} membership(s) "
              f"added, {stats['errors']} error(s).")
    if agg.unmatched:
        print(f"\n  {len(agg.unmatched)} repo(s) matched no sub-product and were "
              f"skipped entirely")
        print("  (no Group means no team name). Create a sub-product whose "
              "Repository URL")
        print("  matches the repo, then re-run.")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()
