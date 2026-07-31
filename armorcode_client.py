"""
Thin wrapper around the ac-sdk-v2 ArmorCodeClient that adds the user
invite and team-assignment endpoints not yet in the SDK.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running from the project dir with the SDK on the path
_SDK_PATHS = [
    Path.home() / "Documents/claude/ac-sdk-v2",
    Path.home() / "Documents/armorcode/ac-sdk-v2",
    Path.home() / "Documents/ac-sdk-v2",
]
for _p in _SDK_PATHS:
    if (_p / "armorcode").is_dir():
        sys.path.insert(0, str(_p))
        break

from armorcode import ArmorCodeClient as _Base  # noqa: E402


class ArmorCodeClient(_Base):
    """SDK client extended with user-invite and team-management endpoints."""

    # ------------------------------------------------------------------
    # User management
    # ------------------------------------------------------------------

    def invite_user(self, email: str, name: str, role: str = "USER") -> dict:
        """Invite a new user to the tenant.

        Args:
            email: User's email address.
            name: Display name.
            role: ArmorCode role string — "USER", "ADMIN", or "READ_ONLY".

        Returns:
            dict: Created user record from the API.
        """
        payload = {
            "emailId": email,
            "name": name,
            "roleType": role,
        }
        resp = self._session.post(
            f"{self.base_url}/user/invite",
            json=payload,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Team management
    # ------------------------------------------------------------------

    def create_team(self, name: str, description: str = "", lead_id: int | None = None) -> dict:
        """Create a new team.

        Args:
            name: Team name (must be unique).
            description: Optional description.
            lead_id: Optional user ID for the team lead.

        Returns:
            dict: Created team record.
        """
        payload: dict = {"name": name, "description": description}
        if lead_id is not None:
            payload["leadId"] = lead_id
        resp = self._session.post(
            f"{self.base_url}/api/team",
            json=payload,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def add_team_member(self, team_id: int, user_id: int) -> dict:
        """Add a user to a team.

        Args:
            team_id: Team ID.
            user_id: User ID to add.

        Returns:
            dict: Updated team record.
        """
        resp = self._session.post(
            f"{self.base_url}/api/team/{team_id}/member/{user_id}",
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def assign_team_to_sub_product(self, sub_product_id: int, team_id: int) -> dict:
        """Assign a team to a sub-product.

        Args:
            sub_product_id: Sub-product ID.
            team_id: Team ID to assign.

        Returns:
            dict: Updated sub-product record.
        """
        resp = self._session.post(
            f"{self.base_url}/user/sub-product/{sub_product_id}/team/{team_id}",
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Convenience: look up user by email
    # ------------------------------------------------------------------

    def get_user_by_email(self, email: str) -> dict | None:
        """Return the AC user record for the given email, or None."""
        users = self.get_users()
        email_lower = email.lower()
        for u in users:
            # API returns "email" field (not "emailId")
            if u.get("email", "").lower() == email_lower:
                return u
        return None
