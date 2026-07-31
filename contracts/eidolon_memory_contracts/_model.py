"""Pydantic base for SDK-owned wire models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class EidolonWireModel(BaseModel):
    """Shared model settings for cross-project wire contracts."""

    model_config = ConfigDict(populate_by_name=True, validate_assignment=True)
