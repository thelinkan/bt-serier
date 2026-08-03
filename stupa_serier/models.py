from dataclasses import dataclass


@dataclass(slots=True)
class MatchRecord:
    source_url: str
    series_name: str
    round_name: str
    match_date: str
    match_time: str
    home_team: str
    away_team: str
    organiser: str
    score: str


@dataclass(slots=True)
class SourcePage:
    id: int | None
    name: str
    start_url: str
    source_type: str
    season: str
    region: str
    last_updated_at: str | None = None
