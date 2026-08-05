"""Aggregate extracted repos into the users and teams to provision.

Pure functions over plain dicts — no I/O, no API calls — so the whole
mapping can be unit-tested and inspected (via --dump-json) before anything
is written to ArmorCode.

The shape of the problem:

    repos (from every SCM)  ->  match on URL  ->  sub-product
                                                     |
                                                  its Group (parentName)
                                                     |
                                                  the TEAM

A team is therefore the union, across EVERY SCM, of:
  - the members of every repo matching a sub-product in that Group, and
  - the ids of those sub-products (which become the team's scope).

Aggregating before provisioning is what keeps the run cheap and correct: a
Group owning 25 repos is created, scoped and populated once rather than 25
times, and its scope is computed whole instead of growing through 25
read-merge-write round trips.

This is also why provisioning is never per-SCM. A Group can hold
sub-products whose repoLinks point at different SCMs, so a single-SCM view
would compute a partial member set and write it as if complete.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from matching import SubProductIndex, team_name_for_group


@dataclass
class TeamPlan:
    """Everything to provision for one team, aggregated across all SCMs."""

    name: str                                    # sanitised, API-safe
    group_name: str                              # raw parentName, for logs
    members: dict = field(default_factory=dict)  # lowercased email -> display name
    sub_product_ids: set = field(default_factory=set)
    product_ids: set = field(default_factory=set)
    repos: list = field(default_factory=list)    # "<scm>:<full_name>", for logs
    members_missing_email: list = field(default_factory=list)

    @property
    def scope_entries(self) -> list[dict]:
        """The productSubProductMap for this team, in PUT (write) shape.

        Built entirely from the bulk sub-product response — the parent
        product id arrives with each sub-product, so unlike the previous
        implementation this needs no per-sub-product API call.
        """
        by_product: dict[int, set] = {}
        for pid, spid in self._pairs:
            by_product.setdefault(pid, set()).add(spid)
        return [
            {"product": pid, "subProduct": sorted(sp), "accessOnAllSubProduct": False}
            for pid, sp in sorted(by_product.items())
        ]

    def __post_init__(self):
        # (product_id, sub_product_id) pairs, so scope survives a team whose
        # sub-products span more than one product.
        self._pairs: set = set()


@dataclass
class Aggregate:
    """The complete picture built from every SCM's extract."""

    users: dict = field(default_factory=dict)    # lowercased email -> {name,email,scms}
    teams: dict = field(default_factory=dict)    # team name -> TeamPlan
    unmatched: list = field(default_factory=list)
    matched_repos: int = 0
    total_repos: int = 0
    folded_matches: list = field(default_factory=list)
    skipped_groups: list = field(default_factory=list)

    @property
    def match_rate(self) -> float:
        """Fraction of repos that matched a sub-product, 0.0-1.0."""
        if not self.total_repos:
            return 1.0
        return self.matched_repos / self.total_repos

    @property
    def unmatched_rate(self) -> float:
        return 1.0 - self.match_rate


def aggregate(repos: list[dict], index: SubProductIndex) -> Aggregate:
    """Fold every extracted repo into users and teams.

    `repos` is the concatenation of every SCM's repos.json, so a team that
    owns repos in more than one SCM is merged here rather than split.

    Repos that match no sub-product are recorded in `unmatched` and
    contribute nothing: without a sub-product there is no Group, and without
    a Group there is no team name to provision under.
    """
    agg = Aggregate(total_repos=len(repos))

    for repo in repos:
        url = repo.get("url") or ""
        matches, folded = index.lookup(url)

        if not matches:
            agg.unmatched.append({
                "scm": repo.get("scm", ""),
                "repo": repo.get("full_name", ""),
                "url": url,
                "reason": "no sub-product with this repoLink" if url
                          else "repo has no web URL",
            })
            continue

        agg.matched_repos += 1
        if folded:
            agg.folded_matches.append({
                "scm": repo.get("scm", ""),
                "repo": repo.get("full_name", ""),
                "url": url,
            })

        # Users are global: one person on repos in both SCMs is one user.
        for member in repo.get("members") or []:
            email = (member.get("email") or "").strip()
            if not email:
                continue
            key = email.lower()
            entry = agg.users.setdefault(
                key, {"name": member.get("name") or email, "email": email,
                      "scms": set()})
            entry["scms"].add(repo.get("scm", ""))

        # One repo can match several sub-products, and they may sit in
        # different Groups — in which case the repo's members belong to
        # every one of those teams.
        for sub in matches:
            team_name = team_name_for_group(sub.group_name)
            if not team_name:
                agg.skipped_groups.append({
                    "scm": repo.get("scm", ""),
                    "repo": repo.get("full_name", ""),
                    "group": sub.group_name,
                    "reason": "Group name is empty after sanitising",
                })
                continue

            plan = agg.teams.get(team_name)
            if plan is None:
                plan = TeamPlan(name=team_name, group_name=sub.group_name or "")
                agg.teams[team_name] = plan

            plan.repos.append(f"{repo.get('scm','')}:{repo.get('full_name','')}")
            plan.sub_product_ids.add(sub.id)
            if sub.product_id is not None:
                plan.product_ids.add(sub.product_id)
                plan._pairs.add((sub.product_id, sub.id))

            for member in repo.get("members") or []:
                email = (member.get("email") or "").strip()
                if email:
                    plan.members[email.lower()] = member.get("name") or email

            for member in repo.get("members_missing_email") or []:
                plan.members_missing_email.append({
                    "scm": repo.get("scm", ""),
                    "repo": repo.get("full_name", ""),
                    "member": member,
                })

    return agg


def to_jsonable(agg: Aggregate) -> dict:
    """Convert an Aggregate to something json.dump can handle.

    Sets become sorted lists so the output is stable and diffable between
    runs — the point of --dump-json is comparing what changed.
    """
    return {
        "summary": {
            "total_repos": agg.total_repos,
            "matched_repos": agg.matched_repos,
            "unmatched_repos": len(agg.unmatched),
            "match_rate": round(agg.match_rate, 4),
            "teams": len(agg.teams),
            "users": len(agg.users),
        },
        "users": {
            email: {"name": u["name"], "email": u["email"],
                    "scms": sorted(u["scms"])}
            for email, u in sorted(agg.users.items())
        },
        "teams": {
            name: {
                "group_name": plan.group_name,
                "members": dict(sorted(plan.members.items())),
                "sub_product_ids": sorted(plan.sub_product_ids),
                "product_ids": sorted(plan.product_ids),
                "scope_entries": plan.scope_entries,
                "repos": sorted(plan.repos),
                "members_missing_email": plan.members_missing_email,
            }
            for name, plan in sorted(agg.teams.items())
        },
        "unmatched": agg.unmatched,
        "folded_matches": agg.folded_matches,
        "skipped_groups": agg.skipped_groups,
    }
