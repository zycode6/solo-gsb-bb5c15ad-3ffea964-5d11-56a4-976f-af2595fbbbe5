from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, RootModel, model_validator


class EventIn(BaseModel):
    """Write model for a points event (immutable once accepted)."""

    event_id: str = Field(min_length=1, max_length=128)
    participant_id: str = Field(min_length=1, max_length=128)
    points: int
    correction_of: Optional[str] = Field(
        default=None,
        description="event_id of an existing event this event corrects; "
        "corrections may themselves be corrected (chain).",
    )


class EventBatchIn(RootModel[List[EventIn]]):
    """Write model for ``POST /events/batch``: 1–100 events, unique event_ids."""

    root: List[EventIn] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _unique_event_ids(self) -> "EventBatchIn":
        seen: set[str] = set()
        duplicates: list[str] = []
        for ev in self.root:
            if ev.event_id in seen and ev.event_id not in duplicates:
                duplicates.append(ev.event_id)
            seen.add(ev.event_id)
        if duplicates:
            raise ValueError(
                "event_id values must be unique within the batch; "
                f"duplicates: {duplicates}"
            )
        return self


class EventOut(BaseModel):
    event_id: str
    participant_id: str
    points: int
    correction_of: Optional[str]
    revision: int
    created_at: str


class EffectiveEvent(BaseModel):
    event_id: str
    points: int
    revision: int
    correction_of: Optional[str]


class LeaderboardEntry(BaseModel):
    rank: int
    participant_id: str
    total_points: int
    effective_events: List[EffectiveEvent]


class LeaderboardOut(BaseModel):
    revision: int
    entries: List[LeaderboardEntry]


class LeaderboardPageOut(BaseModel):
    revision: int
    entries: List[LeaderboardEntry]
    next_cursor: Optional[str] = Field(
        default=None,
        description="Signed cursor for the next page; null on the last page.",
    )


class RankPosition(BaseModel):
    """A participant's rank and total at one snapshot revision."""

    rank: int
    total_points: int


class LeaderboardChangeOut(BaseModel):
    participant_id: str
    before: Optional[RankPosition] = Field(
        default=None, description="Null when the participant is absent at from_revision."
    )
    after: Optional[RankPosition] = Field(
        default=None, description="Null when the participant is absent at to_revision."
    )
    points_delta: int = Field(
        description="after.total_points - before.total_points; an absent side counts as 0."
    )


class LeaderboardChangesOut(BaseModel):
    from_revision: int
    to_revision: int
    changes: List[LeaderboardChangeOut]
