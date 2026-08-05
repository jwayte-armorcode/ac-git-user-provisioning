"""Source-control readers for the extract phase.

Both readers expose the same interface, so extract.py never branches on
which SCM it's talking to:

    load_repos(repo=None, changed_since=None) -> list   # sorted by id asc
    iter_repos(repos, limit, after_id)                  # cap + resume-skip
    get_members(repo) -> list[dict]                     # {username,name,email}
    repo_id(repo) -> int
    repo_name(repo) -> str                              # short name
    repo_full_name(repo) -> str                         # owner/name, for logs
    repo_url(repo) -> str                               # web URL, THE join key

The genuine per-SCM differences live inside the classes:

  - Identity fields. GitHub has full_name/name/html_url; GitLab has
    path_with_namespace/path/web_url.
  - Membership. GitLab includes inherited group members at or above
    MIN_ACCESS_LEVEL (Reporter). GitHub falls back to commit authors when
    the collaborators endpoint is forbidden, so a token without the
    Collaborators permission still yields a usable contributor list.

No topics. Team names come from the ArmorCode Group of the sub-product
whose repoLink matches repo_url(), so the SCM side no longer has any say in
naming and reads nothing but URLs and members.

Sorting by id (not name/path) gives a resume order that's stable across
runs — ids never change once assigned, so a recorded "last completed id"
means the same resume point even if repos are added, removed or renamed in
between.

Both load_repos() materialise the full list up front rather than streaming
page-by-page (needed in order to sort). At 100K+ repos that's still only
tens of MB.
"""

from __future__ import annotations

import gitlab
from gitlab.exceptions import GitlabError
from github import Github
from github.GithubException import GithubException

# GitLab: minimum access level to count as a member (30 = Reporter).
MIN_ACCESS_LEVEL = 30

# GitHub: how many recent commits to scan for authors when the collaborators
# endpoint is forbidden. Bounded because it's a fallback, not the main path.
COMMIT_FALLBACK_LIMIT = 200


def _iter_sorted(items, limit: int | None, after_id: int | None):
    """Yield from an already-id-sorted list, applying resume-skip and a cap."""
    count = 0
    for item in items:
        if after_id is not None and item.id <= after_id:
            continue
        yield item
        count += 1
        if limit is not None and count >= limit:
            return


class GitHubTeamReader:
    source = "github"

    def __init__(self, token: str, url: str = "https://api.github.com"):
        # base_url matters for GitHub Enterprise; PyGithub wants the API
        # root, which is what config.py stores.
        if url and url.rstrip("/") != "https://api.github.com":
            self._gh = Github(token, base_url=url.rstrip("/"))
        else:
            self._gh = Github(token)
        # login -> email or None. See _resolve_email().
        self._email_cache: dict[str, str | None] = {}

    def load_repos(self, repo: str | None = None, changed_since=None):
        """All repos the token can see, sorted by id ascending.

        repo: optional filter to a single repo as "owner/name". Fetched
              directly instead of listing everything.
        changed_since: optional datetime; keep only repos whose updated_at is
              at or after it. GitHub has no server-side filter for this on the
              repo list, so the full list is still paginated and the filter is
              applied client-side. That saves the per-repo member calls (the
              expensive part) but not the listing.
        """
        if repo is not None:
            try:
                return [self._gh.get_repo(repo)]
            except GithubException as e:
                print(f"[error] could not access repo {repo!r}: {e}")
                return []

        repos = list(self._gh.get_user().get_repos(type="all"))
        if changed_since is not None:
            before = len(repos)
            repos = [r for r in repos
                     if r.updated_at is not None and r.updated_at >= changed_since]
            print(f"[github] --changed-since: {len(repos)} of {before} repo(s) "
                  f"updated at or after {changed_since.isoformat()}")
        repos.sort(key=lambda r: r.id)
        return repos

    def iter_repos(self, repos: list, limit: int | None = None,
                   after_id: int | None = None):
        return _iter_sorted(repos, limit, after_id)

    def repo_id(self, repo) -> int:
        return repo.id

    def repo_name(self, repo) -> str:
        return repo.name

    def repo_full_name(self, repo) -> str:
        return repo.full_name

    def repo_url(self, repo) -> str:
        """The repo's web URL — the key matched against ArmorCode repoLink.

        html_url rather than clone_url/ssh_url: it's the form ArmorCode
        stores in repoLink, so the two usually agree before normalisation
        even has to do anything. matching.normalise_repo_url() handles the
        cases where they don't.
        """
        return repo.html_url or ""

    def get_members(self, repo) -> list[dict]:
        """Collaborators, falling back to recent commit authors when the
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
            pass  # Fall through to the commit-based approach.

        try:
            for commit in repo.get_commits()[:COMMIT_FALLBACK_LIMIT]:
                author = commit.author
                raw = commit.commit.author
                if author:
                    login = author.login
                    if login not in members:
                        members[login] = {
                            "username": login,
                            "name": author.name or (raw.name if raw else None) or login,
                            "email": self._resolve_email(author) or (raw.email if raw else None),
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
        """Best-effort: the user's public email, if they publish one.

        Cached per login for the life of the run. This is a full profile
        fetch, and the same person typically appears on many repos — without
        the cache a user on 50 repos costs 50 identical API calls. Misses are
        cached too (as None): users with no public email are the common case
        and recur just as often.
        """
        login = gh_user.login
        if login in self._email_cache:
            return self._email_cache[login]
        try:
            email = self._gh.get_user(login).email or None
        except GithubException:
            email = None
        self._email_cache[login] = email
        return email


class GitLabTeamReader:
    source = "gitlab"

    def __init__(self, token: str, url: str = "https://gitlab.com"):
        self._gl = gitlab.Gitlab(url=url, private_token=token)
        self._gl.auth()
        # user id -> email or None. See _resolve_email().
        self._email_cache: dict[int, str | None] = {}

    def load_repos(self, repo: str | None = None, changed_since=None):
        """All projects the token has membership on, sorted by id ascending.

        repo: optional filter, matched case-insensitively against either the
              short project path ("juice-shop") or "namespace/path".
        changed_since: passed to the API as last_activity_after, so unlike
              GitHub this is a real server-side filter and fewer pages are
              fetched.
        """
        list_kwargs = {"membership": True, "all": True, "iterator": True}
        if changed_since is not None:
            list_kwargs["last_activity_after"] = changed_since.isoformat()
        projects = list(self._gl.projects.list(**list_kwargs))
        if changed_since is not None:
            print(f"[gitlab] --changed-since: {len(projects)} project(s) with activity "
                  f"at or after {changed_since.isoformat()} (server-side filter)")
        projects.sort(key=lambda p: p.id)

        if repo is not None:
            repo_lower = repo.strip().lower()
            projects = [
                p for p in projects
                if p.path.lower() == repo_lower
                or p.path_with_namespace.lower() == repo_lower
            ]
        return projects

    def iter_repos(self, repos: list, limit: int | None = None,
                   after_id: int | None = None):
        return _iter_sorted(repos, limit, after_id)

    def repo_id(self, repo) -> int:
        return repo.id

    def repo_name(self, repo) -> str:
        return repo.path

    def repo_full_name(self, repo) -> str:
        return repo.path_with_namespace

    def repo_url(self, repo) -> str:
        """The project's web URL — the key matched against ArmorCode repoLink."""
        return getattr(repo, "web_url", "") or ""

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
        """The member's email, falling back to a profile fetch.

        The profile fetch is cached per user id for the life of the run — the
        same person typically appears on many projects, and without the cache
        each occurrence costs another call. Misses are cached too (as None),
        since users with no visible email are common and recur just as often.
        """
        email = getattr(member, "email", None)
        if email:
            return email  # already on the member record, no fetch needed

        uid = member.id
        if uid in self._email_cache:
            return self._email_cache[uid]
        try:
            user = self._gl.users.get(uid)
            email = (getattr(user, "email", None)
                     or getattr(user, "public_email", None) or None)
        except GitlabError:
            email = None
        self._email_cache[uid] = email
        return email


def build_reader(scm_config):
    """Construct the right reader for a config.ScmConfig."""
    if scm_config.type == "github":
        return GitHubTeamReader(scm_config.token, scm_config.url)
    return GitLabTeamReader(scm_config.token, scm_config.url)
