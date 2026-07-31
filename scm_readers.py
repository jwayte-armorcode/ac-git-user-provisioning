"""Source-control readers for team_sync.py.

Both readers expose the same interface, so team_sync.py never branches on
which SCM it's talking to:

    load_repos(repo=None) -> list        # sorted by id ascending
    iter_repos(repos, rows, after_id)    # resume-skip + row cap
    get_team_names(repo) -> list[str]
    get_members(repo) -> list[dict]      # {username, name, email}
    repo_id(repo) -> int
    repo_name(repo) -> str               # short name, matched to AC sub-products
    repo_full_name(repo) -> str          # for logging

The genuine per-SCM differences live inside the classes:

  - Topic prefix. GitHub topics are lowercase-alphanumeric-and-hyphens
    only (no colons), so the convention there is "armorcode-team-<name>"
    and the topic IS the slugified team identifier. GitLab topics allow
    mixed case and colons, so it uses "armorcode-team:<Name>" and the
    team name survives as-is.
  - Identity fields. GitHub has full_name/name; GitLab has
    path_with_namespace/path.
  - Membership. GitLab includes inherited group members at or above
    MIN_ACCESS_LEVEL (Reporter). GitHub falls back to commit authors when
    the collaborators endpoint is forbidden, so a token without the
    Collaborators permission still yields a usable contributor list.

Sorting by id (not name/path) gives a resume order that's stable across
runs — ids never change once assigned, so a checkpoint recorded as "last
completed id" means the same resume point even if repos are added,
removed, or renamed in between.

Both load_repos() materialize the full list up front rather than
streaming page-by-page (needed in order to sort). At 100K+ repos that's
still only tens of MB, the same "preload and index" tradeoff already
made for ArmorCode's team/user/sub-product state.
"""

from __future__ import annotations

import gitlab
from gitlab.exceptions import GitlabError
from github import Github
from github.GithubException import GithubException

# GitLab: minimum access level to be considered a member (30 = Reporter).
MIN_ACCESS_LEVEL = 30

GITHUB_TEAM_TOPIC_PREFIX = "armorcode-team-"
GITLAB_TEAM_TOPIC_PREFIX = "armorcode-team:"


def _names_from_topics(topics, prefix: str) -> list[str]:
    """Pull team names out of whichever topics carry the prefix."""
    names = []
    for t in topics or []:
        if t.startswith(prefix):
            name = t[len(prefix):].strip()
            if name:
                names.append(name)
    return names


def _iter_sorted(items, rows: int | None, after_id: int | None):
    """Yield from an already-id-sorted list, applying resume-skip and a cap."""
    count = 0
    for item in items:
        if after_id is not None and item.id <= after_id:
            continue
        yield item
        count += 1
        if rows is not None and count >= rows:
            return


class GitHubTeamReader:
    source = "github"
    topic_prefix = GITHUB_TEAM_TOPIC_PREFIX

    def __init__(self, pat: str):
        self._gh = Github(pat)

    def load_repos(self, repo: str | None = None):
        """All repos the token can see, sorted by id ascending.

        repo: optional filter to a single repo as "owner/name"
              (case-insensitive). Fetched directly instead of listing
              everything — for one-off test runs.
        """
        if repo is not None:
            try:
                return [self._gh.get_repo(repo)]
            except GithubException as e:
                print(f"[error] could not access repo {repo!r}: {e}")
                return []

        repos = list(self._gh.get_user().get_repos(type="all"))
        repos.sort(key=lambda r: r.id)
        return repos

    def iter_repos(self, repos: list, rows: int | None = None, after_id: int | None = None):
        return _iter_sorted(repos, rows, after_id)

    def repo_id(self, repo) -> int:
        return repo.id

    def repo_name(self, repo) -> str:
        return repo.name

    def repo_full_name(self, repo) -> str:
        return repo.full_name

    def get_team_names(self, repo) -> list[str]:
        try:
            topics = repo.get_topics()
        except GithubException:
            topics = []
        return _names_from_topics(topics, self.topic_prefix)

    def get_members(self, repo) -> list[dict]:
        """Collaborators, falling back to commit authors when the
        collaborators endpoint is forbidden."""
        members: dict[str, dict] = {}

        try:
            for collab in repo.get_collaborators():
                members[collab.login] = {
                    "username": collab.login,
                    "name": collab.name or collab.login,
                    "email": self._resolve_email(collab),
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
                        members[login] = {
                            "username": login,
                            "name": author.name or raw.name or login,
                            "email": self._resolve_email(author) or raw.email,
                        }
                elif raw and raw.email:
                    if raw.email not in members:
                        members[raw.email] = {
                            "username": raw.name or raw.email,
                            "name": raw.name or "",
                            "email": raw.email,
                        }
        except GithubException:
            pass

        return list(members.values())

    def _resolve_email(self, gh_user) -> str | None:
        """Best-effort: the user's public email, if they publish one."""
        try:
            return self._gh.get_user(gh_user.login).email or None
        except GithubException:
            return None


class GitLabTeamReader:
    source = "gitlab"
    topic_prefix = GITLAB_TEAM_TOPIC_PREFIX

    def __init__(self, pat: str, url: str = "https://gitlab.com"):
        self._gl = gitlab.Gitlab(url=url, private_token=pat)
        self._gl.auth()

    def load_repos(self, repo: str | None = None):
        """All projects the token has membership on, sorted by id ascending.

        repo: optional filter, matched case-insensitively against either
              the short project path (e.g. "juice-shop") or the full
              "namespace/path".
        """
        projects = list(self._gl.projects.list(membership=True, all=True, iterator=True))
        projects.sort(key=lambda p: p.id)

        if repo is not None:
            repo_lower = repo.strip().lower()
            projects = [
                p for p in projects
                if p.path.lower() == repo_lower or p.path_with_namespace.lower() == repo_lower
            ]
        return projects

    def iter_repos(self, repos: list, rows: int | None = None, after_id: int | None = None):
        return _iter_sorted(repos, rows, after_id)

    def repo_id(self, repo) -> int:
        return repo.id

    def repo_name(self, repo) -> str:
        return repo.path

    def repo_full_name(self, repo) -> str:
        return repo.path_with_namespace

    def get_team_names(self, repo) -> list[str]:
        topics = getattr(repo, "topics", None) or getattr(repo, "tag_list", None) or []
        return _names_from_topics(topics, self.topic_prefix)

    def get_members(self, repo) -> list[dict]:
        """Direct + inherited group members at or above MIN_ACCESS_LEVEL."""
        members_map: dict[int, dict] = {}
        try:
            member_list = repo.members_all.list(all=True)
        except GitlabError:
            member_list = repo.members.list(all=True)

        for m in member_list:
            if m.access_level < MIN_ACCESS_LEVEL:
                continue
            members_map[m.id] = {
                "username": m.username,
                "name": m.name,
                "email": self._resolve_email(m),
            }
        return list(members_map.values())

    def _resolve_email(self, member) -> str | None:
        email = getattr(member, "email", None)
        if email:
            return email
        try:
            user = self._gl.users.get(member.id)
            return getattr(user, "email", None) or getattr(user, "public_email", None) or None
        except GitlabError:
            return None
