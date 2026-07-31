#!/usr/bin/env python3
"""
github_team_sync: Read ArmorCode team ownership from GitHub repo topics
and provision the matching teams/users/scope in ArmorCode.

Self-contained — does not depend on a separate ac-sdk-v2 checkout. The
ArmorCode API methods it needs are inlined below (ported from ac-sdk-v2's
armorcode/client.py) so this script only needs `requests` and `PyGithub`.

Sibling of gitlab_team_sync.py — same ArmorCode-side logic (team
create/scope-merge, user create/add, email-exception logging), different
SCM-side reader. GitHub repo topics are restricted to lowercase
alphanumeric + hyphens (max 50 chars, no colons) — GitLab's
"armorcode-team:Name" convention doesn't fit that charset, so GitHub repos
use a hyphenated prefix instead: "armorcode-team-<name>" (name itself
lowercased/hyphenated too, e.g. "armorcode-team-web").

For each GitHub repo:
  1. Read topics of the form "armorcode-team-<name>" -> one or more team names.
  2. Read repo collaborators (falls back to commit-author emails if the
     collaborators endpoint is forbidden for this token — see
     github_fetcher.py, whose member-resolution logic this mirrors).
  3. For each team name:
       - Create the ArmorCode team if it doesn't exist (scope-only — see
         create_team_scoped's docstring for why members aren't passed there).
       - Find every ArmorCode sub-product whose name matches the GitHub repo
         name (there may be more than one). Merge all of them into the
         team's scope. Never drops existing scope.
  4. For each GitHub contributor with a resolvable email:
       - Create the ArmorCode user if they don't exist.
       - Add them to the team (GET-merge via PUT /user/update/user).
  5. Contributors with no resolvable email are logged to a CSV
     (email_exceptions.csv by default) instead of silently dropped, so an
     admin can track down the real email and reprocess later with
     --reprocess-from-exceptions.

Matching GitHub repo name -> ArmorCode sub-product name is by exact name
equality (case-insensitive). If zero sub-products match, the repo is
skipped with a warning (nothing to scope the team to). If multiple match,
all of them are added to the team's scope.

Usage:
    python github_team_sync.py [--env envfile] [--dry-run] [--rows N] [--repo owner/name]

Dry run is the default. Pass --apply to write changes to ArmorCode.
"""

from __future__ import annotations

import argparse
import configparser
import sys
import time
from datetime import date, datetime
from pathlib import Path

from github import Github, GithubException
import requests

import email_exceptions
import sync_checkpoint


TEAM_TOPIC_PREFIX = "armorcode-team-"


# ---------------------------------------------------------------------------
# ArmorCode client — inlined subset of ac-sdk-v2's armorcode/client.py.
# Identical to the one in gitlab_team_sync.py — kept as a separate copy
# (not a shared import) so each sync script stays fully self-contained and
# can be handed to a customer independently. If you fix a bug in one,
# port the fix to the other.
# ---------------------------------------------------------------------------

class _ThrottledRetrySession(requests.Session):
    """A ``requests.Session`` that paces requests and retries on throttling.

    Ported verbatim from ac-sdk-v2's armorcode/client.py. Retries 429/5xx
    with exponential backoff (honoring Retry-After); other statuses (incl.
    4xx) are returned immediately for raise_for_status() to handle.
    """

    def __init__(self, *args, min_interval=0.0, max_retries=8,
                 backoff_base=2.0, backoff_cap=60.0, **kwargs):
        super().__init__(*args, **kwargs)
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._last_request_ts = 0.0

    def _sleep_to_throttle(self):
        if self._min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_ts
        wait = self._min_interval - elapsed
        if wait > 0:
            time.sleep(wait)

    def request(self, method, url, **kwargs):
        delay = self._backoff_base
        last_resp = None
        for attempt in range(self._max_retries + 1):
            self._sleep_to_throttle()
            self._last_request_ts = time.monotonic()
            resp = super().request(method, url, **kwargs)
            if resp.status_code != 429 and resp.status_code < 500:
                return resp
            last_resp = resp
            if attempt >= self._max_retries:
                break
            retry_after = resp.headers.get("Retry-After")
            if retry_after and str(retry_after).isdigit():
                wait = float(retry_after)
            else:
                wait = delay
                delay = min(delay * 2, self._backoff_cap)
            wait = min(wait, self._backoff_cap)
            # A blind retry-on-5xx can silently sit for minutes on a
            # PERMANENT error (e.g. a 500 the server will never stop
            # returning, like "User Can Not Update Him/Her Self") — that
            # looks indistinguishable from a real hang unless it's logged.
            print(f"    [retry] {method} {url} -> {resp.status_code} "
                  f"(attempt {attempt + 1}/{self._max_retries + 1}, "
                  f"waiting {wait:.0f}s): {resp.text[:200]}")
            time.sleep(wait)
        return last_resp


class ArmorCodeClient:
    """Minimal ArmorCode REST client — teams, users, sub-products only.

    Ported from ac-sdk-v2's ArmorCodeClient. If you need other endpoints,
    pull the corresponding method from ac-sdk-v2/armorcode/client.py rather
    than reintroducing a dependency on the external SDK package.
    """

    def __init__(self, tenant_url, token, *, timeout=60,
                 min_request_interval=0.0, max_retries=8):
        self.base_url = f"https://{tenant_url.rstrip('/')}"
        self._session = _ThrottledRetrySession(
            min_interval=min_request_interval,
            max_retries=max_retries,
        )
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
        self._timeout = timeout

    # -- Teams -------------------------------------------------------------

    def get_teams(self):
        """List all teams (id + name)."""
        resp = self._session.get(f"{self.base_url}/api/team/all-teams", timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def get_team(self, team_id):
        """Get full detail for a specific team (members, properties/scope, lead)."""
        resp = self._session.get(f"{self.base_url}/api/team/{team_id}", timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def create_team_scoped(self, name, group_scopes, *, description="",
                           members=None, business_unit_id, business_unit_name,
                           email_alias="", extra=None):
        """Create a new team scoped to specific products/sub-products.

        group_scopes: iterable of (product_id, [sub_product_ids]) tuples.
        An empty/None sub_product_ids list means whole-product access.

        Members are intentionally NOT wired to POST /api/team's `members`
        field by callers of this method — that field rejects any user with
        "account level access" ("... cannot be added directly to Teams
        please update User access by updating user"), which every user
        this script creates hits. Team creation here is scope-only;
        membership is always added afterward via add_user_to_team()
        (PUT /user/update/user). The `members` param is kept for API
        parity with ac-sdk-v2 but callers in this script always omit it.
        """
        psp_map = []
        for entry in group_scopes:
            if isinstance(entry, (list, tuple)):
                pid, subgroups = entry[0], (entry[1] if len(entry) > 1 else None)
            else:
                pid, subgroups = entry, None
            subs = list(subgroups) if subgroups else []
            psp_map.append({
                "product": int(pid),
                "subProduct": subs,
                "accessOnAllSubProduct": not subs,
            })

        body = {
            "name": name,
            "description": description,
            "members": list(members) if members else [],
            "properties": [{
                "businessUnitId": business_unit_id,
                "businessUnitName": business_unit_name,
                "productSubProductMap": psp_map,
                "accessType": "individual",
                "groups": [],
            }],
            "emailAlias": email_alias,
            "accessOnAllBusinessUnits": False,
            "approvalWorkflow": {"approvers": []},
        }
        if extra:
            body.update(extra)
        resp = self._session.post(f"{self.base_url}/api/team", json=body, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def put_team(self, body):
        """Raw PUT of a full team body (used for scope-merge updates)."""
        resp = self._session.put(f"{self.base_url}/api/team", json=body, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def update_user_team_info(self, user_id, team_info):
        """Set a user's full team membership list via PUT /user/update/user.

        team_info: list of {"teamId": int, "role": <role name str>}. This
        REPLACES the user's entire teamInfo — callers must GET the user's
        current teamInfo and merge before calling, or existing memberships
        are lost. The API also rejects an empty list (a user must belong to
        at least one team), so never call this with team_info=[].

        Also confirmed: this endpoint 500s with "User Can Not Update
        Him/Her Self" if the token's own user tries to update its own
        teamInfo — use a token belonging to someone other than the account
        you're provisioning, or provision other users only.
        """
        resp = self._session.put(
            f"{self.base_url}/user/update/user",
            json={"id": user_id, "teamInfo": list(team_info)},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        # Confirmed inconsistent: a single-team update returns the full
        # updated user object, but a multi-team update returns 200 with an
        # empty body. Don't rely on the response for confirmation either
        # way — treat any 2xx as success and return {} when there's no body.
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    # -- Users ---------------------------------------------------------------

    def get_users(self):
        """List all users in the tenant."""
        resp = self._session.get(f"{self.base_url}/user/data/users", timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def get_business_units(self):
        """List business units (organizations) in the tenant: [{id, name}, ...]."""
        resp = self._session.get(f"{self.base_url}/user/business-units/", timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def create_user(self, name, email, tenant_role, *, disable_login=False,
                    team_info=None, extra=None):
        """Create a new user in the tenant."""
        body = {
            "name": name,
            "email": email,
            "tenantRole": tenant_role,
            "disableLogin": disable_login,
        }
        if team_info is not None:
            body["teamInfo"] = list(team_info)
        if extra:
            body.update(extra)
        resp = self._session.post(f"{self.base_url}/user/add/user", json=body, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    # -- Sub-products (repos/components) --------------------------------------

    def get_sub_products(self):
        """List all sub-products (lightweight: id + name)."""
        resp = self._session.get(
            f"{self.base_url}/user/sub-product/elastic/short", timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def get_sub_product(self, sub_product_id):
        """Get full detail for a sub-product, including its parent `product`."""
        resp = self._session.get(
            f"{self.base_url}/api/sub-product/{sub_product_id}", timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Env loading
# ---------------------------------------------------------------------------

def load_env_file(path: str) -> dict:
    """Parse a simple KEY=VALUE env file (lowercase or uppercase keys)."""
    out = {}
    p = Path(path)
    if not p.exists():
        print(f"[error] env file not found: {path}")
        sys.exit(1)
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


DEFAULT_ROLE = "Developer"


def load_default_role(config_path: str, cli_override: str | None) -> str:
    """Resolve the ArmorCode tenantRole for newly-created users.

    Precedence: --default-role CLI flag > [armorcode] default_role in the
    ini file > built-in default ("Developer"). Missing ini file is not an
    error — it's an optional override, not a requirement.
    """
    if cli_override:
        return cli_override

    p = Path(config_path)
    if p.exists():
        parser = configparser.ConfigParser()
        parser.read(p)
        if parser.has_option("armorcode", "default_role"):
            return parser.get("armorcode", "default_role").strip()

    return DEFAULT_ROLE


# ---------------------------------------------------------------------------
# GitHub side
# ---------------------------------------------------------------------------

class GitHubTeamReader:
    def __init__(self, pat: str):
        self._gh = Github(pat)

    def load_sorted_repos(self, repo: str | None = None):
        """Fetch all repos the token can see, sorted by repo id ascending,
        and return the full list.

        Sorting by id (not owner/name) gives a resume order that's stable
        across separate runs — ids never change once assigned, so a
        checkpoint recorded as "last completed id" always means the same
        resume point even if repos are added, removed, or renamed between
        runs.

        Materializes the full list up front rather than streaming
        page-by-page (needed to sort) — at 100K+ repos this is still only
        tens of MB (each repo object is small), the same "keep preloading"
        tradeoff already made for ArmorCode's team/user/sub-product state.

        repo: optional filter to a single repo, as "owner/name"
              (case-insensitive). Fetches that one repo directly instead of
              listing everything — for one-off test runs.
        """
        if repo is not None:
            try:
                return [self._gh.get_repo(repo)]
            except GithubException as e:
                print(f"[error] could not access repo {repo!r}: {e}")
                return []

        user = self._gh.get_user()
        repos = list(user.get_repos(type="all"))
        repos.sort(key=lambda r: r.id)
        return repos

    def iter_repos(self, repos: list, rows: int | None = None, after_id: int | None = None):
        """Yield from an already-sorted repo list (see load_sorted_repos),
        applying resume-skip and a row cap.

        after_id: optional resume point — skip every repo with
              id <= after_id (set automatically from the checkpoint file, if one exists).
        rows: optional cap on how many (post-skip) repos to yield.
        """
        count = 0
        for r in repos:
            if after_id is not None and r.id <= after_id:
                continue
            yield r
            count += 1
            if rows is not None and count >= rows:
                return

    def get_team_names(self, repo) -> list[str]:
        """Extract ArmorCode team names from the repo's topics.

        GitHub topics are lowercase-alphanumeric-and-hyphens only, so the
        team name embedded in the topic is whatever's after the prefix,
        as-is (already lowercase/hyphenated by GitHub's own constraint) —
        unlike GitLab, there's no colon-separated free-text team name to
        extract; the topic *is* the slugified team identifier.
        """
        try:
            topics = repo.get_topics()
        except GithubException:
            topics = []
        names = []
        for t in topics:
            if t.startswith(TEAM_TOPIC_PREFIX):
                name = t[len(TEAM_TOPIC_PREFIX):].strip()
                if name:
                    names.append(name)
        return names

    def get_members(self, repo) -> list[dict]:
        """Get collaborators for a repo, falling back to commit authors.

        Mirrors github_fetcher.py's _get_members exactly — same fallback
        behavior, so a token missing the Collaborators permission still
        gets a usable (if less complete) contributor list via commits.
        """
        members: dict[str, dict] = {}

        try:
            for collab in repo.get_collaborators():
                email = self._resolve_email(collab)
                members[collab.login] = {
                    "username": collab.login,
                    "name": collab.name or collab.login,
                    "email": email,
                }
            return list(members.values())
        except GithubException:
            pass  # Fall through to commit-based approach

        try:
            for commit in repo.get_commits()[:200]:
                author = commit.author
                raw = commit.commit.author
                if author:
                    login = author.login
                    if login not in members:
                        email = self._resolve_email(author) or raw.email
                        members[login] = {
                            "username": login,
                            "name": author.name or raw.name or login,
                            "email": email,
                        }
                elif raw and raw.email:
                    key = raw.email
                    if key not in members:
                        members[key] = {
                            "username": raw.name or raw.email,
                            "name": raw.name or "",
                            "email": raw.email,
                        }
        except GithubException:
            pass

        return list(members.values())

    def _resolve_email(self, gh_user) -> str | None:
        """Best-effort: return the public email for a GitHub user."""
        try:
            full = self._gh.get_user(gh_user.login)
            return full.email or None
        except GithubException:
            return None


# ---------------------------------------------------------------------------
# ArmorCode side helpers
# ---------------------------------------------------------------------------

class ArmorCodeState:
    """Caches ArmorCode teams/users/sub-products so we don't re-fetch per repo."""

    def __init__(self, ac: ArmorCodeClient):
        self.ac = ac
        print("[armorcode] Loading teams, users, sub-products, business units...")

        # Business unit ids are tenant-specific — never hardcode one. Resolve
        # the tenant's actual default org by name; fall back to whichever
        # business unit comes first if no "Default Organization" exists.
        business_units = ac.get_business_units()
        default_bu = next(
            (bu for bu in business_units if bu.get("name") == "Default Organization"),
            business_units[0] if business_units else None,
        )
        if default_bu is None:
            raise RuntimeError("No business units found in this tenant — cannot scope teams")
        self.business_unit_id = default_bu["id"]
        self.business_unit_name = default_bu["name"]
        print(f"[armorcode] Using business unit: {self.business_unit_name!r} (id={self.business_unit_id})")

        # Keyed by exact name AND lowercased name — see find_team()/register_team()
        # for why: GitHub topics are lowercase-only, so a topic-derived team
        # name (e.g. "api") must still match an existing team named "API".
        self.teams_by_name: dict[str, dict] = {}
        for t in ac.get_teams():
            self.register_team(t)

        self.users_by_email: dict[str, dict] = {}
        for u in ac.get_users():
            email = (u.get("email") or "").lower()
            if email:
                self.users_by_email[email] = u

        # sub-products: name (lowercased) -> list of {id, name} (may be >1)
        self.sub_products_by_name: dict[str, list[dict]] = {}
        for sp in ac.get_sub_products():
            key = sp["name"].strip().lower()
            self.sub_products_by_name.setdefault(key, []).append(sp)

        print(
            f"[armorcode] {len(self.teams_by_name)} teams, "
            f"{len(self.users_by_email)} users with email, "
            f"{sum(len(v) for v in self.sub_products_by_name.values())} sub-products"
        )

    # -- team lookup (case-insensitive) -----------------------------------

    def find_team(self, team_name: str) -> dict | None:
        """Look up a team by name, case-insensitively.

        GitHub topics are forced lowercase (e.g. "armorcode-team-api"), so
        the team name parsed from a topic must still match an existing
        team named e.g. "API" — exact-match lookup would miss it and
        create a duplicate "api" team instead.
        """
        return self.teams_by_name.get(team_name) or self.teams_by_name.get(team_name.lower())

    def register_team(self, team: dict) -> None:
        """Index a team (new or freshly fetched) under both its exact name
        and lowercased name, so find_team() can match it either way."""
        self.teams_by_name.setdefault(team["name"], team)
        self.teams_by_name.setdefault(team["name"].lower(), team)

    # -- sub-product matching --------------------------------------------

    def find_matching_sub_products(self, repo_name: str) -> list[dict]:
        """Return every AC sub-product whose name matches the GitHub repo
        name exactly (case-insensitive). May be 0, 1, or many."""
        return list(self.sub_products_by_name.get(repo_name.strip().lower(), []))

    # -- team scope (product/sub-product map) -----------------------------

    def build_scope_entries(self, sub_products: list[dict]) -> list[dict]:
        """For each sub-product, resolve its parent product id and build a
        productSubProductMap entry {product, subProduct:[...], accessOnAllSubProduct: False}.

        Sub-products belonging to the same parent product are merged into a
        single entry with all their ids listed.
        """
        by_product: dict[int, set[int]] = {}
        for sp in sub_products:
            detail = self.ac.get_sub_product(sp["id"])
            product = detail.get("product") or {}
            product_id = product.get("id")
            if product_id is None:
                print(f"    [warn] sub-product {sp['name']!r} (id {sp['id']}) has no parent product, skipping")
                continue
            by_product.setdefault(product_id, set()).add(sp["id"])

        return [
            {"product": pid, "subProduct": sorted(sub_ids), "accessOnAllSubProduct": False}
            for pid, sub_ids in by_product.items()
        ]


# ---------------------------------------------------------------------------
# Merge helpers (GET -> merge -> PUT, never drop existing scope/members)
# ---------------------------------------------------------------------------

def merge_scope_into_team(ac: ArmorCodeClient, team: dict, new_entries: list[dict],
                          business_unit_id: int, business_unit_name: str) -> tuple[dict, bool]:
    """Merge new productSubProductMap entries into a team's existing scope,
    without dropping anything already scoped.

    GET /api/team/{id} and PUT /api/team use two different shapes for the
    same data:
      - READ  (GET response "properties"): businessUnit is a nested
        {id, name} object; productSubProductMap entries nest product as
        {id, name} and use "subProducts" (plural) as a list of {id, name}.
      - WRITE (PUT request body): businessUnitId is a flat int; product is
        a flat id; the sub-product list key is "subProduct" (singular) of
        flat ids.
    Sending the read shape back on a PUT causes a 400 (Jackson can't
    deserialize an object where it expects a plain id). So this function
    reads via get_team(), converts each existing property entry into the
    flat write shape, merges the new entries into that, and returns a body
    built entirely in write shape.

    Returns (updated_team_body_that_will_be_sent, changed).
    """
    current = ac.get_team(team["id"])
    read_properties = current.get("properties") or []

    # Convert every existing property entry (read shape) to write shape.
    write_properties = []
    for entry in read_properties:
        bu = entry.get("businessUnit") or {}
        psp_map = []
        for e in entry.get("productSubProductMap") or []:
            product = e.get("product") or {}
            sub_products = e.get("subProducts") or []
            psp_map.append({
                "product": int(product["id"]),
                "subProduct": [int(sp["id"]) for sp in sub_products],
                "accessOnAllSubProduct": bool(e.get("accessOnAllSubProduct")),
            })
        write_properties.append({
            "businessUnitId": bu.get("id"),
            "businessUnitName": bu.get("name"),
            "productSubProductMap": psp_map,
            "accessType": "individual",
            "groups": entry.get("groups") or [],
        })

    bu_entry = None
    for entry in write_properties:
        if entry["businessUnitId"] == business_unit_id:
            bu_entry = entry
            break

    if bu_entry is None:
        bu_entry = {
            "businessUnitId": business_unit_id,
            "businessUnitName": business_unit_name,
            "productSubProductMap": [],
            "accessType": "individual",
            "groups": [],
        }
        write_properties.append(bu_entry)

    psp_map = bu_entry["productSubProductMap"]
    by_product = {e["product"]: e for e in psp_map}
    changed = False

    for new_entry in new_entries:
        pid = new_entry["product"]
        if pid in by_product:
            existing = by_product[pid]
            if existing.get("accessOnAllSubProduct"):
                continue  # already has whole-group access; nothing to add
            existing_subs = set(existing.get("subProduct") or [])
            new_subs = set(new_entry.get("subProduct") or [])
            merged_subs = existing_subs | new_subs
            if merged_subs != existing_subs:
                existing["subProduct"] = sorted(merged_subs)
                changed = True
        else:
            psp_map.append(dict(new_entry))
            by_product[pid] = new_entry
            changed = True

    body = dict(current)
    body["id"] = team["id"]
    body["properties"] = write_properties
    return body, changed


def user_label(user_record: dict) -> str:
    """Format a user for logging. Existing tenant users often have
    name == email, so only show the name when it adds something."""
    name, email = user_record.get("name"), user_record.get("email")
    return f"{name} <{email}>" if name and name != email else f"{email}"


def add_user_to_team(ac: ArmorCodeClient, user_record: dict, team_id: int, role: str) -> bool:
    """Ensure one user belongs to one team, without dropping their other
    team memberships.

    Team membership lives on the USER record (user_record["teamInfo"]), not
    on the team record — PUT /user/update/user replaces a user's entire
    teamInfo list, so this always GET-merges: read the user's current
    teamInfo (already cached on user_record from get_users()), skip if
    team_id is already present, otherwise append and write the full list
    back.

    Returns True if the user was newly added, False if they already had
    this team (no-op, no write performed).
    """
    user_id = user_record.get("userId") or user_record["id"]
    team_info = list(user_record.get("teamInfo") or [])

    if any(t.get("teamId") == team_id for t in team_info):
        return False

    team_info.append({"teamId": team_id, "role": role})
    updated = ac.update_user_team_info(user_id, team_info)
    user_record["teamInfo"] = updated.get("teamInfo", team_info)
    return True


# ---------------------------------------------------------------------------
# Main sync
# ---------------------------------------------------------------------------

def sync(gh_reader: GitHubTeamReader, state: ArmorCodeState, rows: int | None, dry_run: bool,
         default_role: str = "Developer", repo: str | None = None,
         exceptions_file: str = "email_exceptions.csv", today: str = "",
         checkpoint_file: str = "sync_checkpoint.json", sparse: bool = False):
    ac = state.ac
    mode = "DRY RUN" if dry_run else "APPLY"
    print(f"\n{'='*70}\n  GitHub -> ArmorCode team sync ({mode})\n{'='*70}\n")
    if repo:
        print(f"[filter] restricting to single repo: {repo!r}\n")

    print("[github] Fetching and sorting full repo list (by id, for a stable resume order)...")
    all_repos = gh_reader.load_sorted_repos(repo=repo)
    total_count = len(all_repos)
    print(f"[github] {total_count} repo(s) visible to this token")

    # Checkpointing is always on — every run checks for a prior checkpoint
    # and resumes from it automatically, no flag required. A run that
    # completes in full (no --repo/--rows filter) clears the checkpoint at
    # the end, so a plain first-ever run just proceeds normally: no
    # checkpoint file exists yet, so after_id is None and nothing is skipped.
    after_id = sync_checkpoint.load_checkpoint(checkpoint_file, "github")
    if after_id is not None:
        print(f"[resume] Checkpoint found: resuming after repo id {after_id}")
    else:
        print("[resume] No checkpoint found for github — starting from the beginning")

    repos_seen = 0
    repos_with_teams = 0
    last_completed_id = after_id

    for gh_repo in gh_reader.iter_repos(all_repos, rows=rows, after_id=after_id):
        repos_seen += 1
        full_name = gh_repo.full_name
        repo_name = gh_repo.name  # short name, used to match AC sub-products
        team_names = gh_reader.get_team_names(gh_repo)

        if not team_names:
            last_completed_id = gh_repo.id
            if not dry_run:
                sync_checkpoint.save_checkpoint(
                    checkpoint_file, "github", last_completed_id, repos_seen, total_count,
                    datetime.now().isoformat(),
                )
            continue

        repos_with_teams += 1
        print(f"\n[repo] {full_name}  (teams: {', '.join(team_names)})")

        members = gh_reader.get_members(gh_repo)
        members_with_email = [m for m in members if m["email"]]
        members_missing_email = [m for m in members if not m["email"]]
        print(f"    members: {len(members)} total, {len(members_with_email)} with email, "
              f"{len(members_missing_email)} without (cannot provision without email)")

        sub_products = state.find_matching_sub_products(repo_name)
        if not sub_products:
            print(f"    [warn] no ArmorCode sub-product named {repo_name!r} found — "
                  f"team scope cannot be set for this repo (team/users still processed)")
        elif len(sub_products) > 1:
            names = ", ".join(f"{sp['name']}(id={sp['id']})" for sp in sub_products)
            print(f"    [info] {len(sub_products)} matching sub-products found — scoping team to all: {names}")
        else:
            print(f"    [info] matched sub-product: {sub_products[0]['name']} (id={sub_products[0]['id']})")

        scope_entries = state.build_scope_entries(sub_products) if sub_products else []

        # ---- Ensure ArmorCode users exist for all members with email ----
        # Keep full user records (not just ids) — add_user_to_team() needs
        # each user's cached teamInfo to GET-merge team membership correctly.
        user_records_for_team: list[dict] = []
        # Dry run can't create users, so a would-be-created user never lands in
        # user_records_for_team and would be missing from the membership
        # preview. Track them separately so the preview reflects everyone who
        # would end up on the team, not just those who already exist.
        pending_user_labels: list[str] = []
        for m in members_with_email:
            email_lower = m["email"].lower()
            existing = state.users_by_email.get(email_lower)
            if existing:
                user_records_for_team.append(existing)
                continue

            print(f"    [create-user] {m['name']} <{m['email']}>")
            if dry_run:
                pending_user_labels.append(f"{m['name']} <{m['email']}> (would be created)")
                continue
            try:
                created = ac.create_user(name=m["name"], email=m["email"], tenant_role=default_role)
                created.setdefault("teamInfo", [])
                state.users_by_email[email_lower] = created
                user_records_for_team.append(created)
            except Exception as e:
                print(f"      [error] failed to create user {m['email']}: {e}")

        for team_name in team_names:
            team = state.find_team(team_name)

            if team is None:
                print(f"    [create-team] {team_name}")
                if dry_run:
                    team = {"id": None, "name": team_name}  # placeholder for dry-run logging
                else:
                    try:
                        group_scopes = [(e["product"], e["subProduct"]) for e in scope_entries]
                        team = ac.create_team_scoped(
                            name=team_name,
                            group_scopes=group_scopes,
                            business_unit_id=state.business_unit_id,
                            business_unit_name=state.business_unit_name,
                        )
                        state.register_team(team)
                    except Exception as e:
                        print(f"      [error] failed to create team {team_name!r}: {e}")
                        continue
            else:
                print(f"    [team] {team['name']} exists (id={team['id']})")
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

            # Membership: always add via add_user_to_team, whether the team
            # was just created (scope-only, no members yet) or already existed.
            if user_records_for_team or pending_user_labels:
                if dry_run:
                    labels = [user_label(r) for r in user_records_for_team] + pending_user_labels
                    print(f"      [dry_run] would ensure {len(labels)} member(s) on team"
                          f"{'' if sparse else ':'}")
                    if not sparse:
                        for label in labels:
                            print(f"        - {label}")
                else:
                    # Track who was actually added vs already present, so apply
                    # can name them the way dry run does — a bare "added 2"
                    # leaves no record of WHICH members changed.
                    added, already = [], []
                    for record in user_records_for_team:
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

            if members_missing_email and not dry_run:
                email_exceptions.log_exceptions(
                    exceptions_file, "github", full_name, team_name, members_missing_email, today,
                )

        if members_missing_email:
            names = ", ".join(f"{m['name']} ({m['username']})" for m in members_missing_email)
            print(f"    [warn] skipped (no public email, cannot provision): {names}")

        last_completed_id = gh_repo.id
        if not dry_run:
            sync_checkpoint.save_checkpoint(
                checkpoint_file, "github", last_completed_id, repos_seen, total_count,
                datetime.now().isoformat(),
            )

    print(f"\n{'='*70}")
    print(f"  Done. {repos_seen} repo(s) scanned, {repos_with_teams} had armorcode-team topics.")
    print(f"{'='*70}\n")

    # A full, unfiltered, non-dry-run pass reached the end without being
    # killed — clear the checkpoint so a later fresh run doesn't skip
    # anything because a stale "last completed" position is still on disk.
    if not dry_run and repo is None and rows is None:
        sync_checkpoint.clear_checkpoint(checkpoint_file)
        print("[resume] Full run completed — checkpoint cleared")


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
        description="Sync GitHub repo topics (armorcode-team-*) into ArmorCode teams/users/scope",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python github_team_sync.py\n"
            "  python github_team_sync.py --rows 10\n"
            "  python github_team_sync.py --repo owner/ac-sdk-v2\n"
            "  python github_team_sync.py --repo owner/ac-sdk-v2 --apply\n"
            "  python github_team_sync.py --apply\n"
            "  python github_team_sync.py --env /path/to/other/envfile --apply\n"
        ),
    )
    parser.add_argument("--env", default="envfile",
                        help="Path to the env file (default: envfile). Holds both the GitHub "
                             "token (GITHUB_PAT) and the ArmorCode tenant token "
                             "(API_TOKEN / TENANT_URL) — see env.example.")
    parser.add_argument("--ac-env", default=None,
                        help=argparse.SUPPRESS)
    parser.add_argument("--rows", type=int, default=None,
                        help="Limit to the first N GitHub repos (for testing)")
    parser.add_argument("--repo", default=None,
                        help="Restrict to a single GitHub repo for a one-off test run, "
                             "as owner/name (case-insensitive).")
    parser.add_argument("--config", default="github_team_sync.ini",
                        help="Path to config ini (default: github_team_sync.ini). "
                             "Currently provides [armorcode] default_role.")
    parser.add_argument("--default-role", default=None,
                        help="ArmorCode tenantRole assigned to newly-created users. "
                             "Overrides the ini file's [armorcode] default_role. "
                             "Must be a valid role name from the tenant's GET /user/roles "
                             "list, e.g. Developer, Admin, DevOps, Security Engineer, Read Only.")
    parser.add_argument("--exceptions-file", default="email_exceptions.csv",
                        help="CSV path for contributors with no resolvable email "
                             "(default: email_exceptions.csv — shared with gitlab_team_sync.py "
                             "so all unresolved contributors land in one file, distinguished by "
                             "the 'source' column). Appended to on every run; an admin fills in "
                             "the email column by hand, then re-run with "
                             "--reprocess-from-exceptions to provision them.")
    parser.add_argument("--reprocess-from-exceptions", action="store_true",
                        help="Instead of a normal sync, read --exceptions-file and provision "
                             "any row where the email column has been filled in (user created "
                             "if needed, added to the row's team). Does not re-scope teams.")
    parser.add_argument("--checkpoint-file", default="sync_checkpoint.json",
                        help="Path to the resume checkpoint (default: sync_checkpoint.json). "
                             "Every --apply run automatically writes progress here after each "
                             "repo and checks it at startup — if it exists, the run resumes "
                             "after that repo id instead of starting over; if not, it starts "
                             "from the beginning as normal (this is what happens on a plain "
                             "first-ever run). A full run that completes with no --repo/--rows "
                             "filter clears the checkpoint. Dry runs never write it. Same "
                             "default name as gitlab_team_sync.py's, but the file is "
                             "single-source: it's tagged with which source wrote it, and a run "
                             "for a DIFFERENT source ignores it as if no checkpoint existed. "
                             "This means the default path is NOT safe to share between a "
                             "concurrent GitHub run and GitLab run — each would overwrite the "
                             "other's progress. Pass distinct --checkpoint-file paths (e.g. "
                             "github_checkpoint.json / gitlab_checkpoint.json) if both sources "
                             "run around the same time.")

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

    # One env file holds everything: the qualified key names (GITHUB_PAT vs
    # API_TOKEN/TENANT_URL) don't collide. The legacy bare "token"/"url"
    # keys ARE ambiguous across services, so they're checked last, after
    # every qualified key — that way a combined file's "token=" (meant for
    # one service) can't be misread as the other's credential.
    # --ac-env is a hidden escape hatch for split-credential setups.
    ac_env_path = args.ac_env or args.env

    gh_env = load_env_file(args.env)
    gh_pat = gh_env.get("GITHUB_PAT") or gh_env.get("token")
    if not gh_pat:
        print(f"[error] no GitHub token found in {args.env} (expected 'GITHUB_PAT' or 'token')")
        sys.exit(1)

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

    default_role = load_default_role(args.config, args.default_role)
    print(f"[config] new ArmorCode users will be created with role: {default_role!r} "
          f"(from {'--default-role' if args.default_role else args.config})")

    ac = ArmorCodeClient(tenant_url=ac_url, token=ac_token)
    state = ArmorCodeState(ac)

    if args.reprocess_from_exceptions:
        reprocess_exceptions(state, args.exceptions_file, dry_run, default_role)
        return

    gh_reader = GitHubTeamReader(pat=gh_pat)
    sync(gh_reader, state, rows=args.rows, dry_run=dry_run, default_role=default_role, repo=args.repo,
         exceptions_file=args.exceptions_file, today=date.today().isoformat(),
         checkpoint_file=args.checkpoint_file, sparse=args.sparse)


if __name__ == "__main__":
    main()
