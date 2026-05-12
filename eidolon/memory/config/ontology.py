"""Load and validate ontology + MCP tool mapping + recall policy (YAML-driven)."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


class WingDefinition(BaseModel):
    id: str
    display_name: str = ""
    description: str = ""
    sort_order: int = 0


class McpToolNames(BaseModel):
    search_drawers: str = "search_drawers"
    add_to_drawer: str = "add_to_drawer"
    check_or_create_room: str = "check_or_create_room"
    search_existing_rooms: str = "search_existing_rooms"
    archive_room: str = "archive_room"
    set_room_status: str = "set_room_status"
    delete_document: str = ""


class RecallPolicy(BaseModel):
    top_k: int = 5
    timeout_seconds: float = 0.12
    recency_weight: float = 0.35
    filter_taboo_statuses: list[str] = Field(default_factory=lambda: ["taboo", "archived"])


class StewardConfig(BaseModel):
    prompt_template_path: str = ""


class FilterConfig(BaseModel):
    ignore_smalltalk_regex: str = ""


class OntologyConfig(BaseModel):
    wings: list[WingDefinition] = Field(default_factory=list)
    mcp_tools: McpToolNames = Field(default_factory=McpToolNames)
    recall: RecallPolicy = Field(default_factory=RecallPolicy)
    steward: StewardConfig = Field(default_factory=StewardConfig)
    filters: FilterConfig = Field(default_factory=FilterConfig)

    @field_validator("wings")
    @classmethod
    def _unique_wing_ids(cls, wings: list[WingDefinition]) -> list[WingDefinition]:
        if not wings:
            msg = "ontology: at least one wing is required"
            raise ValueError(msg)
        seen: set[str] = set()
        for w in wings:
            if w.id in seen:
                msg = f"ontology: duplicate wing id {w.id!r}"
                raise ValueError(msg)
            seen.add(w.id)
        return wings

    def wings_prompt_block(self) -> str:
        lines = []
        for w in sorted(self.wings, key=lambda x: x.sort_order):
            lines.append(f"- **{w.id}** ({w.display_name}): {w.description}")
        return "\n".join(lines)

    def tool_names_prompt_block(self) -> str:
        d = self.mcp_tools.model_dump()
        return "\n".join(f"- `{k}` → call MCP tool `{v}`" for k, v in d.items())

    def resolve_steward_template(self) -> Path | None:
        raw = (self.steward.prompt_template_path or "").strip()
        if raw:
            p = Path(raw).expanduser()
            return p if p.is_file() else None
        pkg = Path(__file__).resolve().parent / "prompts" / "memory_steward.md"
        return pkg if pkg.is_file() else None

    def render_steward_prompt(self, *, locale: str = "zh") -> str:
        path = self.resolve_steward_template()
        if path is None:
            msg = "steward prompt template not found"
            raise FileNotFoundError(msg)
        template = path.read_text(encoding="utf-8")
        return (
            template.replace("{{ wings_block }}", self.wings_prompt_block())
            .replace("{{ tool_names }}", self.tool_names_prompt_block())
            .replace("{{ locale }}", locale)
        )


def default_ontology_path() -> Path:
    env = os.environ.get("EIDOLON_MEMORY_ONTOLOGY_YAML", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent / "ontology.default.yaml"


def load_ontology(path: Path | None = None) -> OntologyConfig:
    p = path or default_ontology_path()
    if not p.is_file():
        log.warning("ontology_missing_using_embedded_defaults", path=str(p))
        p = Path(__file__).resolve().parent / "ontology.default.yaml"
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return OntologyConfig.model_validate(raw)
