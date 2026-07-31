#!/usr/bin/env python3
"""
set_repo_teams: Bulk-apply "armorcode-team" topics to GitLab projects and
GitHub repos from a CSV, so gitlab_team_sync.py / github_team_sync.py have
something to read.

CSV format (header required):

    source,repo,teams
    gitlab,julianwayte/juice-shop,Web
    github,jwayte-armorcode/ac-sdk-v2,API
    github,jwayte-armorcode/add_jira_mappings,"Ticketing;Support"

- source: "gitlab" or "github"
- repo: GitLab uses the full "namespace/path"; GitHub uses "owner/repo"
- teams: one team name, or multiple separated by ";" inside a quoted field
  (a plain "," can't be used as the multi-team separator — CSV already uses
  "," as the column delimiter, so "Ticketing,Support" in an unquoted cell
  would silently parse as two extra columns instead of two team names)

Each script's own convention is applied automatically:
- GitLab topic written as "armorcode-team:<name>" (name kept as-is — GitLab
  topics allow mixed case and colons)
- GitHub topic written as "armorcode-team-<name>" (name lowercased and
  spaces turned into hyphens — GitHub topics are lowercase-alphanumeric-
  and-hyphens only, max 50 chars; see github_team_sync.py's docstring)

By default this MERGES new armorcode-team topics into whatever topics the
repo already has (e.g. a "javascript" topic, or an existing armorcode-team
topic for a different team) rather than replacing the whole topics list —
GitLab's API in particular replaces topics wholesale on write, so a naive
overwrite would silently delete unrelated topics. Pass --replace-all-teams
to instead replace only the armorcode-team-* topics for a repo (still
preserving non-team topics), useful when a CSV row represents "these are
now the ONLY teams for this repo."

Usage:
    python set_repo_teams.py --csv teams.csv --gitlab-env env_gitlab --github-env env_github [--dry-run] [--apply]

Dry run is the default. Pass --apply to write changes.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import gitlab
from gitlab.exceptions import GitlabError
import requests


GITLAB_PREFIX = "armorcode-team:"
GITHUB_PREFIX = "armorcode-team-"


def load_env_file(path: str) -> dict:
    """Parse a simple KEY=VALUE env file (lowercase or uppercase keys)."""
    out = {}
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_rows(csv_path: str) -> list[dict]:
    """Read the CSV and split each row's teams column on ";"."""
    rows = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        required = {"source", "repo", "teams"}
        if not required.issubset(reader.fieldnames or []):
            print(f"[error] CSV must have columns: {', '.join(sorted(required))} "
                  f"(found: {reader.fieldnames})")
            sys.exit(1)
        for row in reader:
            source = row["source"].strip().lower()
            repo = row["repo"].strip()
            teams = [t.strip() for t in row["teams"].split(";") if t.strip()]
            if source not in ("gitlab", "github"):
                print(f"[warn] skipping row with unknown source {row['source']!r}: {repo}")
                continue
            if not repo or not teams:
                print(f"[warn] skipping incomplete row: {row}")
                continue
            rows.append({"source": source, "repo": repo, "teams": teams})
    return rows


def gitlab_team_topic(team_name: str) -> str:
    return f"{GITLAB_PREFIX}{team_name}"


def github_team_topic(team_name: str) -> str:
    # GitHub topics: lowercase alphanumeric + hyphens only, max 50 chars.
    slug = team_name.strip().lower().replace(" ", "-")
    slug = "".join(c for c in slug if c.isalnum() or c == "-")
    return f"{GITHUB_PREFIX}{slug}"[:50]


def apply_gitlab(gl, repo_path: str, team_names: list[str], replace_all_teams: bool,
                 dry_run: bool) -> None:
    try:
        project = gl.projects.get(repo_path)
    except GitlabError as e:
        print(f"    [error] could not access GitLab project {repo_path!r}: {e}")
        return

    current = list(project.topics or [])
    new_team_topics = [gitlab_team_topic(t) for t in team_names]

    if replace_all_teams:
        kept = [t for t in current if not t.startswith(GITLAB_PREFIX)]
    else:
        kept = current

    merged = kept + [t for t in new_team_topics if t not in kept]

    if set(merged) == set(current):
        print(f"    [noop] {repo_path}: topics already correct")
        return

    print(f"    [{'dry_run' if dry_run else 'update'}] {repo_path}: "
          f"{current} -> {merged}")
    if not dry_run:
        project.topics = merged
        project.save()


def apply_github(session: requests.Session, repo_path: str, team_names: list[str],
                 replace_all_teams: bool, dry_run: bool) -> None:
    url = f"https://api.github.com/repos/{repo_path}/topics"
    resp = session.get(url, timeout=30)
    if resp.status_code != 200:
        print(f"    [error] could not read topics for GitHub repo {repo_path!r}: "
              f"{resp.status_code} {resp.text[:200]}")
        return

    current = resp.json().get("names", [])
    new_team_topics = [github_team_topic(t) for t in team_names]

    if replace_all_teams:
        kept = [t for t in current if not t.startswith(GITHUB_PREFIX)]
    else:
        kept = current

    merged = kept + [t for t in new_team_topics if t not in kept]

    if set(merged) == set(current):
        print(f"    [noop] {repo_path}: topics already correct")
        return

    print(f"    [{'dry_run' if dry_run else 'update'}] {repo_path}: "
          f"{current} -> {merged}")
    if not dry_run:
        put_resp = session.put(url, json={"names": merged}, timeout=30)
        if put_resp.status_code != 200:
            print(f"    [error] failed to write topics for {repo_path!r}: "
                  f"{put_resp.status_code} {put_resp.text[:200]}")


def main():
    parser = argparse.ArgumentParser(
        description="Bulk-apply armorcode-team topics to GitLab/GitHub repos from a CSV",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "CSV format:\n"
            "  source,repo,teams\n"
            "  gitlab,julianwayte/juice-shop,Web\n"
            "  github,jwayte-armorcode/ac-sdk-v2,API\n"
            "  github,jwayte-armorcode/add_jira_mappings,\"Ticketing;Support\"\n"
            "\n"
            "Examples:\n"
            "  python set_repo_teams.py --csv teams.csv --gitlab-env env_gitlab --github-env env_github\n"
            "  python set_repo_teams.py --csv teams.csv --gitlab-env env_gitlab --github-env env_github --apply\n"
            "  python set_repo_teams.py --csv teams.csv --gitlab-env env_gitlab --github-env env_github --apply --replace-all-teams\n"
        ),
    )
    parser.add_argument("--csv", required=True, help="Path to the input CSV")
    parser.add_argument("--gitlab-env", default="env_gitlab",
                        help="GitLab token env file (only needed if the CSV has gitlab rows)")
    parser.add_argument("--github-env", default="env_github",
                        help="GitHub token env file (only needed if the CSV has github rows)")
    parser.add_argument("--gitlab-url", default="https://gitlab.com",
                        help="GitLab instance URL (default: https://gitlab.com)")
    parser.add_argument("--replace-all-teams", action="store_true",
                        help="Replace all existing armorcode-team-* topics on a repo with "
                             "exactly the teams in that row's CSV line, instead of merging "
                             "new teams in alongside whatever was already there. Non-team "
                             "topics (e.g. 'javascript') are always preserved either way.")

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--dry-run", action="store_true", default=None,
                            help="Print what would happen without writing anything (default)")
    mode_group.add_argument("--apply", action="store_true", default=None,
                            help="Write topics for real")

    args = parser.parse_args()
    dry_run = not args.apply

    rows = load_rows(args.csv)
    if not rows:
        print("[error] no valid rows found in CSV")
        sys.exit(1)

    gitlab_rows = [r for r in rows if r["source"] == "gitlab"]
    github_rows = [r for r in rows if r["source"] == "github"]

    print(f"\n{'='*60}\n  set_repo_teams ({'DRY RUN' if dry_run else 'APPLY'})\n{'='*60}\n")
    print(f"{len(rows)} row(s): {len(gitlab_rows)} gitlab, {len(github_rows)} github\n")

    if gitlab_rows:
        gl_env = load_env_file(args.gitlab_env)
        gl_pat = gl_env.get("token2") or gl_env.get("GITLAB_PAT") or gl_env.get("token")
        if not gl_pat:
            print(f"[error] no GitLab token found in {args.gitlab_env}, "
                  f"but {len(gitlab_rows)} gitlab row(s) need one")
            sys.exit(1)
        gl = gitlab.Gitlab(url=args.gitlab_url, private_token=gl_pat)
        gl.auth()

        print("[gitlab]")
        for row in gitlab_rows:
            print(f"  {row['repo']}  (teams: {', '.join(row['teams'])})")
            apply_gitlab(gl, row["repo"], row["teams"], args.replace_all_teams, dry_run)

    if github_rows:
        gh_env = load_env_file(args.github_env)
        gh_pat = gh_env.get("token") or gh_env.get("GITHUB_PAT")
        if not gh_pat:
            print(f"[error] no GitHub token found in {args.github_env}, "
                  f"but {len(github_rows)} github row(s) need one")
            sys.exit(1)
        session = requests.Session()
        session.headers.update({
            "Authorization": f"Bearer {gh_pat}",
            "Accept": "application/vnd.github+json",
        })

        print("\n[github]")
        for row in github_rows:
            print(f"  {row['repo']}  (teams: {', '.join(row['teams'])})")
            apply_github(session, row["repo"], row["teams"], args.replace_all_teams, dry_run)

    print(f"\n{'='*60}\n  Done.\n{'='*60}\n")


if __name__ == "__main__":
    main()
