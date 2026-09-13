"""Rules loader — discover and load built-in SDLC rule files."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

logger = logging.getLogger(__name__)

_RULES_DIR = Path(__file__).parent
_NORMAL_PATH_START = "<!-- ai-sdlc:normal-path:start -->"
_NORMAL_PATH_END = "<!-- ai-sdlc:normal-path:end -->"
_MAX_EXCERPT_BYTES = 1200
_MAX_CONTEXT_BYTES = 2400


def quantified_stage_profiles() -> dict:
    """读取随安装包分发的目录，不把模板存在当成已接入能力。"""
    return json.loads(
        files(__package__)
        .joinpath("quantified-stage-profiles.json")
        .read_text(encoding="utf-8")
    )


def supported_decision_capabilities() -> dict[str, list[str]]:
    supported = quantified_stage_profiles()["supported_capabilities"]
    return {
        stage: (
            ["implementation-b1", *capabilities]
            if stage == "implementation"
            else list(capabilities)
        )
        for stage, capabilities in supported.items()
    }


_LOOP_RULES: dict[str, tuple[str, str]] = {
    "requirement": ("prd-guidance", "scenario-routing"),
    "design-contract": ("prd-guidance", "quality-gate"),
    "implementation": ("tdd", "verification"),
    "frontend-evidence": ("verification", "quality-gate"),
    "local-pr-review": ("code-review", "verification"),
}

_STAGE_HINTS: dict[str, list[str]] = {
    "pipeline": ["all"],
    "prd-guidance": ["init", "refine"],
    "scenario-routing": ["init"],
    "batch-protocol": ["execute"],
    "tdd": ["execute"],
    "debugging": ["execute"],
    "code-review": ["execute", "verify"],
    "quality-gate": ["verify", "close"],
    "verification": ["verify", "close"],
    "git-branch": ["design", "execute"],
    "multi-agent": ["execute"],
    "auto-decision": ["all"],
    "brownfield-corpus": ["init"],
}


class RuleContextError(ValueError):
    """Raised when a built-in normal-path rule excerpt is unsafe to load."""


@dataclass(frozen=True)
class RuleExcerpt:
    """A bounded rule excerpt safe to place in a normal Agent response."""

    name: str
    title: str
    content: str


@dataclass(frozen=True)
class NormalPathRuleContext:
    """Static rule context selected from current five-Loop truth."""

    excerpts: tuple[RuleExcerpt, ...] = ()


class RulesLoader:
    """Load and query built-in SDLC rule Markdown files."""

    def __init__(self, rules_dir: Path | None = None) -> None:
        """Initialize with optional custom rules directory.

        Args:
            rules_dir: Directory containing rule .md files.
                       Defaults to the package's built-in rules.
        """
        self._dir = rules_dir or _RULES_DIR

    def list_rules(self) -> list[str]:
        """Return sorted list of available rule names (without .md extension).

        Returns:
            List of rule names.
        """
        return sorted(p.stem for p in self._dir.glob("*.md") if p.is_file())

    def load_rule(self, name: str) -> str:
        """Load the full text content of a rule by name.

        Args:
            name: Rule name (without .md extension).

        Returns:
            Full Markdown content of the rule.

        Raises:
            FileNotFoundError: If the rule file does not exist.
        """
        path = self._dir / f"{name}.md"
        if not path.exists():
            raise FileNotFoundError(f"Rule not found: {name}")
        return path.read_text(encoding="utf-8")

    def get_active_rules(self, stage: str) -> list[str]:
        """Return rule names active for a given pipeline stage.

        Args:
            stage: Pipeline stage name (e.g. "execute", "verify").

        Returns:
            Sorted list of rule names applicable to the stage.
        """
        active: list[str] = []
        for rule_name in self.list_rules():
            stages = _STAGE_HINTS.get(rule_name, [])
            if "all" in stages or stage in stages:
                active.append(rule_name)
        return sorted(active)

    def get_rule_title(self, name: str) -> str:
        """Extract the first heading from a rule as its title.

        Args:
            name: Rule name (without .md extension).

        Returns:
            The title string, or the name if no heading found.
        """
        content = self.load_rule(name)
        match = re.match(r"^#\s+(.*)", content)
        return match.group(1).strip() if match else name

    def get_normal_path_context(
        self,
        loop_type: str,
        *,
        loop_status: str = "",
    ) -> NormalPathRuleContext:
        """Return at most two bounded excerpts for one current delivery Loop."""

        normalized_loop = loop_type.strip().lower()
        names = _LOOP_RULES.get(normalized_loop)
        if names is None:
            return NormalPathRuleContext()
        if normalized_loop == "implementation" and loop_status.strip().lower() in {
            "blocked",
            "needs_fix",
        }:
            names = ("debugging", "verification")

        excerpts: list[RuleExcerpt] = []
        total_bytes = 0
        for name in names:
            content = self.load_rule(name)
            excerpt = _extract_normal_path_excerpt(name, content)
            excerpt_bytes = len(excerpt.encode("utf-8"))
            if excerpt_bytes > _MAX_EXCERPT_BYTES:
                raise RuleContextError(
                    f"{name} normal-path excerpt exceeds its byte limit"
                )
            total_bytes += excerpt_bytes
            if total_bytes > _MAX_CONTEXT_BYTES:
                raise RuleContextError(
                    "normal-path rule context exceeds its byte limit"
                )
            excerpts.append(
                RuleExcerpt(
                    name=name,
                    title=self.get_rule_title(name),
                    content=excerpt,
                )
            )
        return NormalPathRuleContext(excerpts=tuple(excerpts))


def _extract_normal_path_excerpt(name: str, content: str) -> str:
    if content.count(_NORMAL_PATH_START) != 1 or content.count(_NORMAL_PATH_END) != 1:
        raise RuleContextError(
            f"{name} must contain exactly one normal-path markers pair"
        )
    before, remainder = content.split(_NORMAL_PATH_START, 1)
    excerpt, after = remainder.split(_NORMAL_PATH_END, 1)
    if _NORMAL_PATH_END in before or _NORMAL_PATH_START in after:
        raise RuleContextError(f"{name} normal-path markers are out of order")
    normalized = excerpt.strip()
    if not normalized:
        raise RuleContextError(f"{name} normal-path excerpt is empty")
    return normalized


__all__ = [
    "NormalPathRuleContext",
    "RuleContextError",
    "RuleExcerpt",
    "RulesLoader",
]
