"""
Fetch repo→members data from GitHub using a Personal Access Token.

For each accessible repo, returns the list of collaborators (direct repo
members) plus the org members who have push access.  Falls back to commit
author emails for repos where collaborator lookup is forbidden.
"""

from __future__ import annotations  # enables X | Y type hints on Python 3.9

from github import Github, GithubException


class GitHubFetcher:
    def __init__(self, pat: str, repos_filter: list[str] | None = None):
        """
        Args:
            pat: GitHub Personal Access Token.
            repos_filter: Optional list of "owner/repo" strings.  If empty
                          or None, all repos accessible by the PAT are used.
        """
        self._gh = Github(pat)
        self._filter = set(repos_filter or [])

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_repo_members(self) -> dict[str, list[dict]]:
        """Return a mapping of repo full name → list of member dicts.

        Each member dict has keys: username, name, email.
        email may be None if the user has not made it public.
        """
        repos = self._get_repos()
        result: dict[str, list[dict]] = {}

        for repo in repos:
            full_name = repo.full_name
            print(f"  [github] {full_name} ...", end=" ", flush=True)
            try:
                members = self._get_members(repo)
                result[full_name] = members
                print(f"{len(members)} members")
            except GithubException as e:
                print(f"skipped ({e.status}: {e.data.get('message', '')})")

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_repos(self):
        """Return GitHub Repo objects to process."""
        if self._filter:
            repos = []
            for repo_path in self._filter:
                try:
                    repos.append(self._gh.get_repo(repo_path))
                except GithubException as e:
                    print(f"  [github] Could not access {repo_path}: {e}")
            return repos

        # All repos the PAT can see (own + org + collaborator)
        user = self._gh.get_user()
        return list(user.get_repos(type="all"))

    def _get_members(self, repo) -> list[dict]:
        """Get collaborators for a repo, falling back to commit authors."""
        members: dict[str, dict] = {}

        # Try collaborators endpoint first (needs push access or admin)
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

        # Fallback: scan recent commits for author emails
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
