"""Pydantic base shared by memory domain models (no dependency on eidolon.agent)."""

from __future__ import annotations

from pydantic import BaseModel


class BaseEidolonModel(BaseModel):
    """Shared Pydantic model configuration for memory wire / payload models."""

    model_config = {
        "populate_by_name": True,
        "validate_assignment": True,
    }
