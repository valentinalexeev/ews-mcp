"""Tool registry: 24 tools in three packs (IDEATION §5), tier-filtered."""

from typing import Dict

from .base import CLASS_TIER, TIER_RANK, Context, ToolSpec
from . import mail_read, calendar_people, writes


def build_registry(ctx: Context) -> Dict[str, ToolSpec]:
    specs = [*mail_read.TOOLS, *calendar_people.TOOLS, *writes.TOOLS]
    tier = ctx.settings.ews_capability_tier
    registry: Dict[str, ToolSpec] = {}
    for spec in specs:
        need = CLASS_TIER.get(spec.side_effect_class, "draft")
        if TIER_RANK[need] <= TIER_RANK.get(tier, 2):
            registry[spec.name] = spec
    ctx.registry = registry
    return registry
