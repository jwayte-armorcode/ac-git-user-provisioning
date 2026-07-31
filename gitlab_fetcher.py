"""
Fetch project→members data from GitLab using a Personal Access Token.

For each accessible project, returns direct members plus inherited group
members with at least Reporter access.
"""

from __future__ import annotations

import gitlab
from gitlab.exceptions import GitlabError


# Minimum GitLab access level to consider someone a contributor
# 20 = Reporter, 30 = Developer, 40 = Maintainer, 50 = Owner
MIN_ACCESS_LEVEL = 20


class GitLabFetcher:
    def __init__(self, pat: str, url: str = "https://gitlab.com",
                 projects_filter: list[str] | None = None):
        """
        Args:
            pat: GitLab Personal Access Token.
            url: GitLab instance URL (default: https://gitlab.com).
            projects_filter: Optional list of project IDs (int strings) or
                             namespace/path strings.  If empty or None, all
                             projects accessible by the PAT are used.
        """
        self._gl = gitlab.Gitlab(url=url, private_token=pat)
        self._gl.auth()
        self._filter = list(projects_filter or [])

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_repo_members(self) -> dict[str, list[dict]]:
        """Return a mapping of project path → list of member dicts.

        Each member dict has keys: username, name, email.
        """
        projects = self._get_projects()
        result: dict[str, list[dict]] = {}

        for project in projects:
            path = project.path_with_namespace
            print(f"  [gitlab] {path} ...", end=" ", flush=True)
            try:
                members = self._get_members(project)
                result[path] = members
                print(f"{len(members)} members")
            except GitlabError as e:
                print(f"skipped ({e})")

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_projects(self):
        """Return GitLab project objects to process."""
        if self._filter:
            projects = []
            for ref in self._filter:
                try:
                    # ref can be an integer ID or a namespace/path string
                    if ref.isdigit():
                        projects.append(self._gl.projects.get(int(ref)))
                    else:
                        projects.append(self._gl.projects.get(ref))
                except GitlabError as e:
                    print(f"  [gitlab] Could not access {ref}: {e}")
            return projects

        # All projects visible to the token
        return list(self._gl.projects.list(membership=True, all=True))

    def _get_members(self, project) -> list[dict]:
        """Get all members (direct + inherited) for a project."""
        members_map: dict[int, dict] = {}

        # all_members includes inherited group members
        try:
            for m in project.members_all.list(all=True):
                if m.access_level < MIN_ACCESS_LEVEL:
                    continue
                email = self._resolve_email(m)
                members_map[m.id] = {
                    "username": m.username,
                    "name": m.name,
                    "email": email,
                }
        except GitlabError:
            # Fall back to direct members only
            for m in project.members.list(all=True):
                if m.access_level < MIN_ACCESS_LEVEL:
                    continue
                email = self._resolve_email(m)
                members_map[m.id] = {
                    "username": m.username,
                    "name": m.name,
                    "email": email,
                }

        return list(members_map.values())

    def _resolve_email(self, member) -> str | None:
        """Return email for a member if available."""
        # Email is only visible when the token has admin scope or when the
        # member has made it public.  The member object may already have it.
        email = getattr(member, "email", None)
        if email:
            return email

        # Try fetching the full user object
        try:
            user = self._gl.users.get(member.id)
            return getattr(user, "email", None) or getattr(user, "public_email", None)
        except GitlabError:
            return None
