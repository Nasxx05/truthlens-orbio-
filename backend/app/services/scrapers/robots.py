"""robots.txt matching, per RFC 9309.

The stdlib's ``urllib.robotparser`` applies rules in file order and returns the
first match, so a blanket ``Allow: /`` ahead of a specific ``Disallow:`` wins
and the disallowed path is treated as permitted. The standard says the *most
specific* (longest) matching rule wins, with ``Allow`` breaking ties. Sites do
write robots.txt in exactly the order the stdlib gets wrong, so this implements
the documented precedence instead.

Also supports the ``*`` and ``$`` wildcards that every major crawler honours
and the stdlib does not.
"""

import re
from typing import List, Optional, Tuple
from urllib.parse import unquote, urlparse


class RobotsRules:
    """Parsed robots.txt for one host."""

    def __init__(self) -> None:
        # (path_pattern, allowed) per user-agent group.
        self._groups: dict = {}

    # ------------------------------------------------------------------ parse

    @classmethod
    def parse(cls, text: str) -> "RobotsRules":
        rules = cls()
        agents: List[str] = []
        # A blank line ends a group, so the next User-agent starts a new one.
        expecting_agent = True

        for raw_line in (text or "").splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                expecting_agent = True
                continue

            if ":" not in line:
                continue
            field, _, value = line.partition(":")
            field = field.strip().lower()
            value = value.strip()

            if field == "user-agent":
                if expecting_agent:
                    agents = []
                    expecting_agent = False
                agents.append(value.lower())
                rules._groups.setdefault(value.lower(), [])
            elif field in ("allow", "disallow"):
                if not agents:
                    continue
                # An empty Disallow means "allow everything" and carries no path.
                if field == "disallow" and value == "":
                    continue
                for agent in agents:
                    rules._groups.setdefault(agent, []).append((value, field == "allow"))

        return rules

    # ------------------------------------------------------------------ match

    def _group_for(self, user_agent: str) -> List[Tuple[str, bool]]:
        """Most specific matching user-agent group, else the wildcard group.

        Matching is a case-insensitive substring test, which is how crawlers
        identify themselves against robots.txt tokens.
        """
        agent = (user_agent or "").lower()
        best_token, best_rules = None, None

        for token, rules in self._groups.items():
            if token == "*":
                continue
            if token and token in agent:
                if best_token is None or len(token) > len(best_token):
                    best_token, best_rules = token, rules

        if best_rules is not None:
            return best_rules
        return self._groups.get("*", [])

    @staticmethod
    def _matches(pattern: str, path: str) -> Optional[int]:
        """Length of ``pattern`` if it matches ``path``, else ``None``.

        Length stands in for specificity, which is what decides precedence.
        """
        if not pattern:
            return None

        anchored_end = pattern.endswith("$")
        body = pattern[:-1] if anchored_end else pattern

        if "*" not in body and not anchored_end:
            return len(pattern) if path.startswith(body) else None

        regex = "".join(".*" if char == "*" else re.escape(char) for char in body)
        regex = f"^{regex}$" if anchored_end else f"^{regex}"
        try:
            return len(pattern) if re.match(regex, path) else None
        except re.error:
            return None

    def allowed(self, user_agent: str, url: str) -> bool:
        """Is ``url`` fetchable? Absence of any rule means yes."""
        rules = self._group_for(user_agent)
        if not rules:
            return True

        parsed = urlparse(url)
        path = unquote(parsed.path or "/")
        if parsed.query:
            path = f"{path}?{parsed.query}"

        best_length, verdict = -1, True

        for pattern, is_allow in rules:
            length = self._matches(pattern, path)
            if length is None:
                continue
            # Longest match wins; Allow wins a tie.
            if length > best_length or (length == best_length and is_allow):
                best_length, verdict = length, is_allow

        return verdict
