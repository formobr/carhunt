"""Domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Listing:
    source: str
    source_id: str
    url: str
    title: str
    price: int
    year: int | None = None
    make: str | None = None
    model: str | None = None
    trim: str | None = None
    mileage: int | None = None
    transmission: str | None = None
    title_status: str | None = None
    location: str | None = None
    description: str | None = None
    photos: list[str] = field(default_factory=list)
    seller_type: str | None = None
    published_at: datetime | None = None
    raw: dict = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        city = (self.location or "").split(",")[0].strip()
        miles = "" if self.mileage is None else str(round(self.mileage / 1000))
        year = "" if self.year is None else str(self.year)
        parts = [self.make or "", self.model or "", year, miles, city]
        return "|".join(p.strip() for p in parts).lower()
