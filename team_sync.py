#!/usr/bin/env python3
"""Sync SCM repo topics into ArmorCode teams, users, and scope.

One entry point for both GitHub and GitLab — pick with --source.

Repos declare their owning ArmorCode team via a topic:

  GitHub   armorcode-team-<name>   (topics are lowercase-alphanumeric-and
                                    -hyphens only, so no colon and the topic
                                    IS the slugified team identifier)
  GitLab   armorcode-team:<Name>   (topics allow mixed case and colons, so
                                    the team name survives as typed)

A repo may carry more than one team topic; each becomes a separate team
that the repo's sub-product is scoped into.

Flow, per repo:
  1. Read the repo's topics -> one or more team names.
  2. Read its members, split into those with a resolvable email and those
     without.
  3. Create the ArmorCode team (scope-only) if missing, or GET the existing
     team and merge in newly matched product/sub-product scope without
     dropping anything already scoped.
  4. Create any missing ArmorCode user, then add every member to the team
     via a GET-merge on the user's teamInfo (team membership lives on the
     user record, not the team).
  5. Members with no resolvable email are appended to email_exceptions.csv
     rather than silently dropped. Once an admin fills in the email column,
     --reprocess-from-exceptions provisions them.

Dry run is the default; nothing is written without --apply.

Usage:
    python team_sync.py --source github [--rows N] [--repo owner/name]
    python team_sync.py --source gitlab --apply
"""

from __future__ import annotations

import argparse
import configparser
import csv
import json
import sys
import time
from datetime import date, datetime

import email_exceptions
import repo_spool
from armorcode import (
    ArmorCodeClient,
    ArmorCodeState,
    add_user_to_team,
    load_default_role,
    load_env_file,
    merge_scope_into_team,
    user_label,
)
from scm_readers import GitHubTeamReader, GitLabTeamReader


def build_reader(source: str, env: dict):
    """Construct the reader for --source, failing loudly on a missing token."""
    if source == "github":
        pat = env.get("GITHUB_PAT") or env.get("token")
        if not pat:
            print("[error] no GitHub token found (expected 'GITHUB_PAT')")
            sys.exit(1)
        return GitHubTeamReader(pat)

    pat = env.get("GITLAB_PAT") or env.get("token")
    if not pat:
        print("[error] no GitLab token found (expected 'GITLAB_PAT')")
        sys.exit(1)
    url = env.get("GITLAB_URL") or env.get("url") or "https://gitlab.com"
    return GitLabTeamReader(pat, url)


# How often to print the progress heartbeat, in repos. Small enough that a
# large tenant shows movement, large enough not to drown the per-repo output
# on a small one.
PROGRESS_EVERY = 25

UNMATCHED_CSV_COLUMNS = ["source", "repo", "expected_sub_product", "teams"]


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


def write_unmatched_csv(path: str, rows: list[dict]) -> None:
    """Write the unmatched-repo report, overwriting any previous one.

    Overwrite rather than append: the file is a report of THIS run, so a
    stale row from an earlier run (for a repo since fixed) would be
    misleading. Written even when a run resumes from a spool, in which
    case it covers only the repos that run actually processed.
    """
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=UNMATCHED_CSV_COLUMNS)
        writer.writeheader()
        for r in rows:
            writer.writerow({col: r.get(col, "") for col in UNMATCHED_CSV_COLUMNS})


def collect(reader, state: ArmorCodeState, rows: int | None, repo: str | None,
            spool_file: str, dry_run: bool, unmatched_repos: list[dict],
            repos_out: dict) -> tuple[int, int]:
    """Read one SCM and accumulate its repo -> {teams, members} mapping.

    This phase makes NO ArmorCode writes. It only reads the SCM and resolves
    sub-product matches, so the whole picture is in memory before anything is
    provisioned. That's what lets the apply phase touch each team exactly
    once instead of once per repo — a team owning 25 repos previously drove
    25 GET+PUT round trips for the same team.

    Every collected repo is spooled to CSV as it's read, and the spool
    doubles as the resume position: a killed run reloads what it already
    gathered rather than re-reading it. Appends to repos_out, keyed
    "<source>-<full_name>" so GitHub and GitLab repos of the same name can't
    collide. Returns (repos_seen, repos_with_teams).
    """
    source = reader.source

    # Recover anything a previous run spooled BEFORE listing repos, so the
    # resume position is known up front. The spool holds the collected data
    # itself, not just a position — a position alone would let a resumed run
    # skip repos whose teams and members were only ever in the dead run's
    # memory, silently provisioning from a fraction of the tenant.
    spool = repo_spool.RepoSpool(spool_file, source)
    recovered, after_id, data_rows = spool.load()
    if after_id is not None:
        repos_out.update(recovered)
        print(f"\n[resume] {source}: reloaded {data_rows} repo(s) with teams from "
              f"{spool_file}, resuming after repo id {after_id}")
    else:
        print(f"\n[resume] {source}: no spool found — starting from the beginning")

    print(f"[{source}] Fetching and sorting full repo list "
          f"(by id, for a stable resume order)...")
    all_repos = reader.load_repos(repo=repo)
    total_count = len(all_repos)
    print(f"[{source}] {total_count} repo(s) visible to this token")

    repos_seen = 0
    repos_with_teams = 0

    # Denominator for progress. A resumed or --rows run processes fewer repos
    # than the tenant holds, so count what THIS run will actually touch.
    skipped_by_resume = sum(
        1 for r in all_repos if after_id is not None and reader.repo_id(r) <= after_id
    )
    to_process = total_count - skipped_by_resume
    if rows is not None:
        to_process = min(to_process, rows)
    started_at = time.monotonic()

    # Dry runs never write the spool — previewing must not create a resume
    # position that makes the next real run skip repos it never provisioned.
    if not dry_run:
        spool.open_append()
    try:
        repos_seen, repos_with_teams = _collect_loop(
            reader, state, all_repos, rows, after_id, dry_run, spool,
            unmatched_repos, repos_out, to_process, total_count, started_at,
        )
    finally:
        spool.close()
    return repos_seen, repos_with_teams


def _collect_loop(reader, state, all_repos, rows, after_id, dry_run, spool,
                  unmatched_repos, repos_out, to_process, total_count, started_at):
    """The per-repo read loop. Split out so the spool is always closed."""
    source = reader.source
    repos_seen = 0
    repos_with_teams = 0

    for scm_repo in reader.iter_repos(all_repos, rows=rows, after_id=after_id):
        repos_seen += 1
        repo_id = reader.repo_id(scm_repo)
        full_name = reader.repo_full_name(scm_repo)
        repo_name = reader.repo_name(scm_repo)  # short name, matched to AC sub-products

        # Heartbeat. Repos without a team topic produce no other output, so
        # without this a large tenant looks hung for long stretches. Printed
        # every PROGRESS_EVERY repos, and on the last one so the tail is
        # never silent.
        if repos_seen % PROGRESS_EVERY == 0 or repos_seen == to_process:
            elapsed = time.monotonic() - started_at
            rate = repos_seen / elapsed if elapsed > 0 else 0
            pct = (repos_seen / to_process * 100) if to_process else 100.0
            msg = (f"[progress] {source}: {repos_seen}/{to_process} repos ({pct:.0f}%), "
                   f"{repos_with_teams} with team topics, {rate:.1f} repo/s")
            if rate > 0 and repos_seen < to_process:
                eta = (to_process - repos_seen) / rate
                msg += f", ~{_fmt_duration(eta)} remaining"
            print(msg)

        team_names = reader.get_team_names(scm_repo)

        if not team_names:
            # Nothing to provision, but advance the resume position so a long
            # stretch of untagged repos isn't re-read after a crash.
            if not dry_run:
                spool.append_progress(repo_id)
            continue

        repos_with_teams += 1
        print(f"\n[repo] {full_name}  (teams: {', '.join(team_names)})")

        members = reader.get_members(scm_repo)
        members_with_email = [m for m in members if m["email"]]
        members_missing_email = [m for m in members if not m["email"]]
        print(f"    members: {len(members)} total, {len(members_with_email)} with email, "
              f"{len(members_missing_email)} without (cannot provision without email)")

        sub_products = state.find_matching_sub_products(repo_name)
        if not sub_products:
            unmatched_repos.append({
                "source": source,
                "repo": full_name,
                "expected_sub_product": repo_name,
                "teams": ";".join(team_names),
            })
            print(f"    [warn] no ArmorCode sub-product named {repo_name!r} found — "
                  f"team scope cannot be set for this repo (team/users still processed)")
        elif len(sub_products) > 1:
            names = ", ".join(f"{sp['name']}(id={sp['id']})" for sp in sub_products)
            print(f"    [info] {len(sub_products)} matching sub-products found — scoping team to all: {names}")
        else:
            print(f"    [info] matched sub-product: {sub_products[0]['name']} (id={sub_products[0]['id']})")

        if members_missing_email:
            names = ", ".join(f"{m['name']} ({m['username']})" for m in members_missing_email)
            print(f"    [warn] skipped (no public email, cannot provision): {names}")

        entry = {
            "source": source,
            "repo": full_name,
            "repo_name": repo_name,
            "teams": team_names,
            "members": members_with_email,
            "members_missing_email": members_missing_email,
            "sub_product_ids": [sp["id"] for sp in sub_products],
        }
        repos_out[f"{source}-{full_name}"] = entry
        if not dry_run:
            spool.append(repo_id, entry)

    return repos_seen, repos_with_teams


def build_users_and_teams(repos: dict) -> tuple[dict, dict]:
    """Invert the repo mapping into users and teams.

    users: email -> {name, email, sources}
    teams: team name -> {members: {email: name}, sub_product_ids: set, repos: [...]}

    Teams are keyed on NAME ALONE, deliberately: a team owning repos in both
    GitHub and GitLab must merge into one team with the union of its scope
    and members, not split into two. This is the whole point of aggregating
    before provisioning.
    """
    users: dict[str, dict] = {}
    teams: dict[str, dict] = {}

    for entry in repos.values():
        for m in entry["members"]:
            email = m["email"].strip().lower()
            if email not in users:
                users[email] = {"name": m["name"], "email": m["email"].strip(),
                                "sources": set()}
            users[email]["sources"].add(entry["source"])

        for team_name in entry["teams"]:
            team = teams.setdefault(team_name, {
                "members": {}, "sub_product_ids": set(), "repos": [],
                "missing_email": [],
            })
            team["repos"].append(entry["repo"])
            team["sub_product_ids"].update(entry["sub_product_ids"])
            for m in entry["members"]:
                team["members"][m["email"].strip().lower()] = m["name"]
            for m in entry["members_missing_email"]:
                team["missing_email"].append({
                    "source": entry["source"], "repo": entry["repo"], "member": m,
                })

    return users, teams


def write_json_dump(path: str, obj) -> None:
    """Write a JSON snapshot, converting sets to sorted lists."""
    def default(o):
        if isinstance(o, set):
            return sorted(o)
        raise TypeError(f"not JSON serializable: {type(o).__name__}")
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, default=default, sort_keys=True)


def apply_users(ac, state, users: dict, default_role: str, dry_run: bool,
                sparse: bool) -> dict:
    """Create any ArmorCode users that don't exist yet, once per distinct user.

    Previously a user appearing on 50 repos was evaluated 50 times. Keyed on
    email here, so each is considered exactly once.

    Returns email -> user record for everyone that exists (or would exist).
    """
    print(f"\n{'-'*70}\n  Users: {len(users)} distinct member(s) with a resolvable email\n{'-'*70}")
    records: dict[str, dict] = {}
    created = 0
    would_create: list[str] = []

    for email, info in sorted(users.items()):
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
              f"{':' if would_create and not sparse else ''}")
        if would_create and not sparse:
            for label in would_create:
                print(f"    - {label}")
    else:
        print(f"  {len(records) - created} already existed, {created} created")
    return records


def apply_teams(ac, state, teams: dict, user_records: dict, default_role: str,
                dry_run: bool, sparse: bool, exceptions_file: str, today: str) -> None:
    """Provision each team exactly once: create or scope-merge, then members.

    This is the efficiency win. Previously the team block ran once per repo,
    so a team owning N repos drove N GET+PUT round trips and re-evaluated its
    members N times. Here each team's complete scope (the union across all
    its repos, and across both SCMs) is computed in memory first, then
    written in a single merge.
    """
    print(f"\n{'-'*70}\n  Teams: {len(teams)}\n{'-'*70}")

    for team_name, info in sorted(teams.items()):
        sub_product_ids = sorted(info["sub_product_ids"])
        repo_count = len(info["repos"])
        print(f"\n[team] {team_name}  ({repo_count} repo(s), "
              f"{len(info['members'])} member(s), {len(sub_product_ids)} sub-product(s))")

        # One scope computation for the whole team, not one per repo.
        scope_entries = []
        if sub_product_ids:
            sps = [{"id": sid, "name": team_name} for sid in sub_product_ids]
            try:
                scope_entries = state.build_scope_entries(sps)
            except Exception as e:
                print(f"    [error] failed to resolve parent products for scope: {e}")

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
                except Exception as e:
                    print(f"      [error] failed to create team {team_name!r}: {e}")
                    continue
        else:
            print(f"    [team] exists (id={team['id']})")
            if scope_entries:
                if dry_run:
                    print(f"      [dry_run] would merge scope entries: {scope_entries}")
                else:
                    try:
                        body, changed = merge_scope_into_team(
                            ac, team, scope_entries,
                            business_unit_id=state.business_unit_id,
                            business_unit_name=state.business_unit_name,
                        )
                        if changed:
                            ac.put_team(body)
                            print(f"      [update] scope merged for team {team_name!r}")
                            if not sparse:
                                print(f"        scope entries: {scope_entries}")
                        else:
                            print("      [noop] scope already covers these sub-products")
                    except Exception as e:
                        print(f"      [error] failed to merge scope for team {team_name!r}: {e}")

        # Membership, once per (team, user) rather than per (team, user, repo).
        member_records = [user_records[e] for e in sorted(info["members"]) if e in user_records]
        pending = [f"{n} <{e}> (would be created)"
                   for e, n in sorted(info["members"].items()) if e not in user_records]

        if member_records or pending:
            if dry_run:
                labels = [user_label(r) for r in member_records] + pending
                print(f"      [dry_run] would ensure {len(labels)} member(s) on team"
                      f"{'' if sparse else ':'}")
                if not sparse:
                    for label in labels:
                        print(f"        - {label}")
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
                        print(f"      [error] failed to add user {uid} to team {team_name!r}: {e}")
                if added:
                    print(f"      [update] added {len(added)} new member(s) to team {team_name!r}:")
                    if not sparse:
                        for label in added:
                            print(f"        - {label}")
                    if already and not sparse:
                        print(f"      [noop] {len(already)} member(s) already on team:")
                        for label in already:
                            print(f"        - {label}")
                else:
                    print(f"      [noop] all {len(already)} member(s) already on team"
                          f"{':' if already and not sparse else ''}")
                    if already and not sparse:
                        for label in already:
                            print(f"        - {label}")

        # No-email exceptions are logged per (repo, team) as before, so the
        # CSV keeps naming which repo the person was found on. Grouped by
        # repo so the file is rewritten once per repo rather than once per
        # member — log_exceptions() does a full read-modify-write each call.
        if not dry_run and info["missing_email"]:
            by_repo: dict[tuple, list] = {}
            for miss in info["missing_email"]:
                by_repo.setdefault((miss["source"], miss["repo"]), []).append(miss["member"])
            for (src, repo_full), members in by_repo.items():
                email_exceptions.log_exceptions(
                    exceptions_file, src, repo_full, team_name, members, today,
                )



def sync(readers: list, state: ArmorCodeState, rows: int | None, dry_run: bool,
         default_role: str, repo: str | None, exceptions_file: str, today: str,
         spool_files: dict, sparse: bool = False, unmatched_csv: str | None = None,
         dump_json: bool = False) -> None:
    """Two-phase sync: read every SCM into memory, then provision once per team.

    Phase 1 (collect) makes no ArmorCode writes — it reads each SCM and
    resolves sub-product matches. Phase 2 (apply) walks the aggregated users
    and teams, so each is touched exactly once regardless of how many repos
    reference it.
    """
    mode = "DRY RUN" if dry_run else "APPLY"
    sources = "+".join(r.source for r in readers)
    print(f"\n{'='*70}\n  {sources} -> ArmorCode team sync ({mode})\n{'='*70}")
    if repo:
        print(f"\n[filter] restricting to single repo: {repo!r}")

    repos: dict = {}
    unmatched_repos: list[dict] = []
    total_seen = 0
    total_with_teams = 0

    # Sources are read in a fixed order (github then gitlab) so a resumed run
    # picks up deterministically. Each keeps its own spool, so a crash partway
    # through GitLab doesn't re-read the 50,000 GitHub repos already gathered.
    for reader in readers:
        seen, with_teams = collect(
            reader, state, rows=rows, repo=repo,
            spool_file=spool_files[reader.source], dry_run=dry_run,
            unmatched_repos=unmatched_repos, repos_out=repos,
        )
        total_seen += seen
        total_with_teams += with_teams

    users, teams = build_users_and_teams(repos)

    if dump_json:
        write_json_dump("repos.json", repos)
        write_json_dump("users.json", users)
        write_json_dump("teams.json", teams)
        print("\n[dump] wrote repos.json, users.json, teams.json")

    user_records = apply_users(state.ac, state, users, default_role, dry_run, sparse)
    apply_teams(state.ac, state, teams, user_records, default_role, dry_run, sparse,
                exceptions_file, today)

    print(f"\n{'='*70}")
    print(f"  Done. {total_seen} repo(s) scanned, {total_with_teams} had armorcode-team "
          f"topics -> {len(teams)} team(s), {len(users)} user(s).")
    if unmatched_repos:
        print(f"\n  {len(unmatched_repos)} repo(s) had a team topic but NO matching "
              f"ArmorCode sub-product.")
        print("  Their teams and users were provisioned, but those repos contribute no")
        print("  product/sub-product scope. Create a sub-product whose name matches the")
        print("  repo name (or correct the topic), then re-run to attach the scope:")
        if sparse:
            print(f"    ({len(unmatched_repos)} listed above; re-run without --sparse to "
                  f"see them here)")
        else:
            for entry in unmatched_repos:
                print(f"    - {entry['repo']} (looked for sub-product "
                      f"{entry['expected_sub_product']!r}, teams: {entry['teams']})")
        if unmatched_csv:
            write_unmatched_csv(unmatched_csv, unmatched_repos)
            print(f"\n  Written to {unmatched_csv}")
    elif unmatched_csv:
        # Nothing unmatched: still write a header-only CSV. Skipping the write
        # would leave a previous run's file in place, reporting repos that
        # have since been fixed as though they were still broken.
        write_unmatched_csv(unmatched_csv, [])
        print(f"\n  All matched — wrote empty {unmatched_csv}")
    print(f"{'='*70}\n")

    # Discard the spools only now — after collect AND apply both finished. The
    # spool is the durable record of collected work, so removing it earlier
    # (e.g. at the end of collect) would mean a crash during apply lost
    # everything and the next run silently started over.
    #
    # Filtered runs keep their spool: --repo/--rows covered only part of the
    # tenant, so that position isn't a valid "we got this far" marker.
    if not dry_run and repo is None and rows is None:
        for reader in readers:
            repo_spool.RepoSpool(spool_files[reader.source], reader.source).discard()
        print("[resume] Full run completed — spool(s) cleared")
    elif not dry_run:
        kept = ", ".join(spool_files[r.source] for r in readers)
        print(f"[resume] Filtered run (--repo/--rows) — spool(s) kept: {kept}")


def reprocess_exceptions(state: ArmorCodeState, exceptions_file: str, dry_run: bool,
                         default_role: str) -> None:
    """Provision contributors whose email exception row now has an email filled in.

    Does NOT re-touch team scope (that's driven by the repo's topics, which
    this mode doesn't re-read) — it only ensures the user exists in
    ArmorCode and is added to the team named in their exception row. Rows
    are only marked "reprocessed" after their team add succeeds.
    """
    ac = state.ac
    candidates = email_exceptions.load_reprocess_candidates(exceptions_file)
    if not candidates:
        print(f"[reprocess] No pending rows with an email filled in, in {exceptions_file}")
        return

    print(f"[reprocess] {len(candidates)} row(s) with an email now filled in\n")
    succeeded = []

    for row in candidates:
        email = row["email"].strip().lower()
        name = row.get("name") or row["username"]
        team_name = row["team"]
        print(f"  {name} <{email}> -> team {team_name!r} (from {row['source']} repo {row['repo']!r})")

        team = state.find_team(team_name)
        if team is None:
            print(f"    [error] team {team_name!r} no longer exists in ArmorCode — skipping, "
                  f"re-run the normal sync first so the team exists")
            continue

        if dry_run:
            print("    [dry_run] would ensure user exists and is added to this team")
            continue

        try:
            user_record = state.users_by_email.get(email)
            if user_record is None:
                user_record = ac.create_user(name=name, email=row["email"].strip(), tenant_role=default_role)
                user_record.setdefault("teamInfo", [])
                state.users_by_email[email] = user_record
                print("    [create-user] created")

            if add_user_to_team(ac, user_record, team["id"], default_role):
                print(f"    [update] added to team {team_name!r}")
            else:
                print("    [noop] already on team")
            succeeded.append(row)
        except Exception as e:
            print(f"    [error] failed to provision: {e}")

    if succeeded:
        email_exceptions.mark_reprocessed(exceptions_file, succeeded)
        print(f"\n[reprocess] {len(succeeded)} row(s) marked reprocessed in {exceptions_file}")


def main():
    parser = argparse.ArgumentParser(
        description="Sync SCM repo topics (armorcode-team-*) into ArmorCode teams/users/scope",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python team_sync.py --source github --rows 10\n"
            "  python team_sync.py --source both --rows 10\n"
            "  python team_sync.py --source github --repo owner/ac-sdk-v2 --apply\n"
            "  python team_sync.py --source both --apply\n"
        ),
    )
    parser.add_argument("--source", required=True, choices=["github", "gitlab", "both"],
                        help="Which SCM to sync from. Determines the topic convention read "
                             "(armorcode-team-<name> on GitHub, armorcode-team:<Name> on "
                             "GitLab) and which token is used from the env file. 'both' reads "
                             "GitHub and GitLab into one picture before provisioning, so a "
                             "team owning repos in both gets a single team with the union of "
                             "its scope and members.")
    parser.add_argument("--env", default="envfile",
                        help="Path to the env file (default: envfile). Holds the SCM token "
                             "(GITHUB_PAT / GITLAB_PAT) and the ArmorCode tenant credentials "
                             "(TENANT_URL / API_TOKEN) — see env.example.")
    parser.add_argument("--ac-env", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--rows", type=int, default=None,
                        help="Process at most N repos. Upper bound, not a requirement — good "
                             "for a small-blast-radius smoke test before a full run.")
    parser.add_argument("--repo", default=None,
                        help="Restrict to a single repo. GitHub: 'owner/name'. GitLab: either "
                             "the short path ('juice-shop') or 'namespace/path'.")
    parser.add_argument("--config", default="team_sync.ini",
                        help="Path to the ini file holding default_role (default: team_sync.ini). "
                             "Looked up in the [<source>] section first, then [armorcode].")
    parser.add_argument("--default-role", default=None,
                        help="tenantRole for newly-created ArmorCode users. Overrides the ini "
                             "file. Must be a valid role in the tenant (e.g. Developer, Admin, "
                             "Security Engineer) — a wrong value returns 400 'Provided Tenant "
                             "Role Not Found'.")
    parser.add_argument("--exceptions-file", default=None,
                        help="CSV of members with no resolvable email (default: "
                             "email_exceptions_<source>.csv). They're logged here rather than "
                             "dropped; once an admin fills in the email column, re-run with "
                             "--reprocess-from-exceptions to provision them. Per-source by "
                             "default because the file is rewritten whole on each update — "
                             "pointing a concurrent GitHub run and GitLab run at one path "
                             "would let them silently drop each other's rows.")
    parser.add_argument("--reprocess-from-exceptions", action="store_true",
                        help="Instead of a normal sync, read --exceptions-file and provision "
                             "any row where the email column has been filled in (user created "
                             "if needed, added to the row's team). Does not re-scope teams.")
    parser.add_argument("--spool-file", default=None,
                        help="Path to the resume spool (default: "
                             "<source>-repos.csv, e.g. github-repos.csv). Every --apply run "
                             "appends each collected repo here as it is read — teams, members "
                             "and matched sub-products — and reloads it at startup, resuming "
                             "after the highest repo id present. The spool holds the collected "
                             "DATA, not just a position, so a run killed after 99,000 of "
                             "100,000 repos restarts by re-reading only the last 1,000 rather "
                             "than losing the work or provisioning from a fraction of the "
                             "tenant. Cleared once a full unfiltered run finishes applying. "
                             "Dry runs never write it.")
    parser.add_argument("--unmatched-csv", nargs="?", const="unmatched_repos.csv",
                        default=None, metavar="PATH",
                        help="Also write the repos that had a team topic but no matching "
                             "ArmorCode sub-product to a CSV (source, repo, "
                             "expected_sub_product, teams). Pass the flag alone for "
                             "unmatched_repos.csv, or give a path. Overwrites on each run, "
                             "since it reports that run rather than accumulating history. "
                             "Written in dry runs too, so it can be used to plan the missing "
                             "sub-products before writing anything.")
    parser.add_argument("--dump-json", action="store_true",
                        help="Write repos.json, users.json and teams.json — the in-memory "
                             "picture built from the SCMs before anything is provisioned. "
                             "Useful for reviewing what a run will do, or for diffing "
                             "against a previous run. Off by default: on a very large "
                             "tenant repos.json is a big file, and the same information is "
                             "in the log.")
    parser.add_argument("--sparse", action="store_true",
                        help="Condense logging to counts and summary lines, omitting the "
                             "per-user names and scope payloads printed by default. Applies "
                             "to both dry runs and --apply. Useful on very large tenants "
                             "where full per-user output is too verbose; the default "
                             "(non-sparse) output is what you want for an auditable record "
                             "of exactly which users and scope changed.")

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--dry-run", action="store_true", default=None,
                            help="Print what would happen without writing anything (default)")
    mode_group.add_argument("--apply", action="store_true", default=None,
                            help="Write changes to ArmorCode")

    args = parser.parse_args()
    dry_run = not args.apply  # default True unless --apply passed
    source = args.source

    sources = ["github", "gitlab"] if source == "both" else [source]

    # Per-source state files by default: with one entry point for both SCMs,
    # a shared default path would let a GitHub run and a GitLab run clobber
    # each other. The spool would resume from the wrong position; the
    # exceptions CSV is rewritten whole on every update, so two interleaved
    # runs would silently drop each other's rows.
    #
    # The spool stays per-SCM even under --source both: each SCM is read
    # separately, so a crash partway through GitLab must not re-read the
    # GitHub repos already gathered. A single combined file couldn't express
    # "GitHub done, GitLab halfway".
    if args.spool_file and source == "both":
        print("[error] --spool-file cannot be used with --source both "
              "(each SCM needs its own spool; omit the flag to use the "
              "per-source defaults)")
        sys.exit(1)
    spool_files = {
        s: (args.spool_file or f"{s}-repos.csv") for s in sources
    }
    exceptions_file = args.exceptions_file or f"email_exceptions_{source}.csv"

    # One env file holds everything: the qualified key names (GITHUB_PAT vs
    # API_TOKEN/TENANT_URL) don't collide. The legacy bare "token"/"url"
    # keys ARE ambiguous across services, so they're checked last, after
    # every qualified key — that way a combined file's "token=" (meant for
    # one service) can't be misread as the other's credential.
    # --ac-env is a hidden escape hatch for split-credential setups.
    ac_env_path = args.ac_env or args.env

    scm_env = load_env_file(args.env)
    readers = [build_reader(s, scm_env) for s in sources]

    ac_env = load_env_file(ac_env_path)
    ac_token = ac_env.get("API_TOKEN") or ac_env.get("token")
    # No default tenant — an unset TENANT_URL must fail loudly rather than
    # silently targeting some other tenant than the operator intended.
    ac_url = ac_env.get("TENANT_URL") or ac_env.get("url")
    if not ac_token:
        print(f"[error] no ArmorCode token found in {ac_env_path}")
        sys.exit(1)
    if not ac_url:
        print(f"[error] no ArmorCode tenant URL found in {ac_env_path} "
              f"(expected 'TENANT_URL', e.g. TENANT_URL=https://xxxx.armorcode.xxx)")
        sys.exit(1)
    ac_url = ac_url.replace("https://", "").replace("http://", "")

    # Under --source both there is one shared user pool, so a single role
    # applies. Per-source [github]/[gitlab] sections would be ambiguous here
    # (the same user can appear on repos from both), so only [armorcode] is
    # consulted — pass source=None to skip the per-source lookup.
    role_source = None if source == "both" else source
    default_role = load_default_role(args.config, args.default_role, role_source)
    print(f"[config] new ArmorCode users will be created with role: {default_role!r} "
          f"(from {'--default-role' if args.default_role else args.config})")
    if source == "both":
        cfg = configparser.ConfigParser()
        cfg.read(args.config)
        per_source = [s for s in ("github", "gitlab")
                      if cfg.has_option(s, "default_role")]
        if per_source and not args.default_role:
            print(f"[warn] --source both ignores the per-source "
                  f"{', '.join('[' + s + ']' for s in per_source)} default_role "
                  f"section(s) in {args.config}; using [armorcode] "
                  f"({default_role!r}) for the shared user pool")

    ac = ArmorCodeClient(tenant_url=ac_url, token=ac_token)

    # Validate the role up front. Otherwise a wrong name isn't caught until
    # the first create_user call, which 400s with "Provided Tenant Role Not
    # Found" — potentially deep into a long run, once teams and scope have
    # already been written. Checked in dry run too, so a preview catches a
    # bad --default-role before the real run.
    try:
        valid_roles = ac.get_role_names()
    except Exception as e:
        print(f"[warn] could not fetch tenant roles to validate {default_role!r}: {e}")
        print("       continuing — an invalid role will fail at user-creation time instead")
    else:
        if default_role not in valid_roles:
            print(f"[error] role {default_role!r} does not exist in this tenant.")
            print(f"        Valid roles: {', '.join(sorted(valid_roles))}")
            print("        Set a valid one via --default-role or default_role in "
                  f"{args.config}.")
            sys.exit(1)

    state = ArmorCodeState(ac)

    if args.reprocess_from_exceptions:
        reprocess_exceptions(state, exceptions_file, dry_run, default_role)
        return

    sync(readers, state, rows=args.rows, dry_run=dry_run, default_role=default_role,
         repo=args.repo, exceptions_file=exceptions_file,
         today=date.today().isoformat(), spool_files=spool_files,
         sparse=args.sparse, unmatched_csv=args.unmatched_csv,
         dump_json=args.dump_json)


if __name__ == "__main__":
    main()
