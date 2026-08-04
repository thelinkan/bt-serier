from __future__ import annotations

import csv
import re
import sqlite3
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .models import MatchRecord, SourcePage


_CLUB_WORD_REPLACEMENTS = (
    (r"\bbordtennisklubben\b", "btk"),
    (r"\bbordtennisklubb\b", "btk"),
    (r"\bbordtennisföreningen\b", "btf"),
    (r"\bbordtennisforeningen\b", "btf"),
    (r"\bbordtennisförening\b", "btf"),
    (r"\bbordtennisforening\b", "btf"),
    (r"\bpingisklubben\b", "pk"),
    (r"\bpingisklubb\b", "pk"),
    (r"\bsportklubben\b", "sk"),
    (r"\bsportklubb\b", "sk"),
    (r"\ballmänna idrottsföreningen\b", "aif"),
    (r"\ballmanna idrottsforeningen\b", "aif"),
    (r"\ballmänna idrottsförening\b", "aif"),
    (r"\ballmanna idrottsforening\b", "aif"),
    (r"\ballmänna if\b", "aif"),
    (r"\ballmanna if\b", "aif"),
    (r"\bidrottsföreningen\b", "if"),
    (r"\bidrottsforeningen\b", "if"),
    (r"\bidrottsförening\b", "if"),
    (r"\bidrottsforening\b", "if"),
    (r"\bidrottsklubben\b", "ik"),
    (r"\bidrottsklubb\b", "ik"),
    (r"\bgymnastikföreningen\b", "gf"),
    (r"\bgymnastikforeningen\b", "gf"),
    (r"\bgymnastikförening\b", "gf"),
    (r"\bgymnastikforening\b", "gf"),
    (r"\ballmänna idrottsklubben\b", "aik"),
    (r"\ballmanna idrottsklubben\b", "aik"),
    (r"\ballmänna idrottsklubb\b", "aik"),
    (r"\ballmanna idrottsklubb\b", "aik"),
)

_GENERIC_CLUB_TOKENS = {
    "aif", "aik", "bk", "bt", "btf", "btk", "ff", "gf", "gif",
    "if", "ik", "is", "kfuk", "kfum", "pk", "sisu", "sk",
}

_PROTECTED_CLUB_ABBREVIATIONS = {
    "AIF", "AIK", "BK", "BT", "BTF", "BTK", "FF", "GF", "GIF",
    "IF", "IK", "IS", "KFUM", "PK", "SISU", "SK",
}


def _fold_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(
        character
        for character in normalized
        if not unicodedata.combining(character)
    )


def _clean_club_source_name(value: str) -> str:
    """Remove UI artefacts sometimes included in organiser text."""
    cleaned = re.sub(r"\s+", " ", value).strip()
    cleaned = re.sub(r"\s+\+\d+\s*$", "", cleaned)
    return cleaned.strip(" \t-–—")


def _club_tokens(value: str) -> list[str]:
    text = _fold_text(_clean_club_source_name(value))
    text = text.replace("&", " och ")
    for pattern, replacement in _CLUB_WORD_REPLACEMENTS:
        text = re.sub(pattern, replacement, text)
    text = re.sub(r"\boch\b", " ", text)
    return re.findall(r"[a-z0-9]+", text)


def _club_key(value: str) -> str:
    return "|".join(sorted(_club_tokens(value)))


def _club_core_key(value: str) -> str:
    tokens = [
        token for token in _club_tokens(value)
        if token not in _GENERIC_CLUB_TOKENS
    ]
    if not tokens:
        tokens = _club_tokens(value)
    return "|".join(sorted(tokens))


def _looks_like_team_designation(token: str) -> bool:
    if not token:
        return False
    if token.upper() in _PROTECTED_CLUB_ABBREVIATIONS:
        return False
    return (
        token == token.upper()
        and re.fullmatch(r"[A-ZÅÄÖ]{1,3}\d{0,2}", token) is not None
    )


def _derived_club_name(team_name: str) -> str:
    value = re.sub(r"\s+", " ", team_name).strip()
    if ":" in value:
        left, right = value.rsplit(":", 1)
        if _looks_like_team_designation(right.strip()):
            value = left.strip()
    parts = value.split()
    if len(parts) >= 2 and _looks_like_team_designation(parts[-1]):
        value = " ".join(parts[:-1]).strip()
    return _clean_club_source_name(value)


def _display_choice(
    existing: tuple[int, str] | None,
    priority: int,
    candidate: str,
) -> tuple[int, str]:
    candidate = _clean_club_source_name(candidate)
    if existing is None or priority > existing[0]:
        return priority, candidate
    if priority == existing[0] and candidate.casefold() < existing[1].casefold():
        return priority, candidate
    return existing


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self._create_schema()

    def _column_names(self, table: str) -> set[str]:
        return {
            str(row["name"])
            for row in self.connection.execute(f"PRAGMA table_info({table})")
        }

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS source_pages (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                start_url TEXT NOT NULL,
                source_type TEXT NOT NULL CHECK (source_type IN ('national', 'regional')),
                season TEXT NOT NULL,
                region TEXT NOT NULL DEFAULT '',
                last_updated_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(start_url, season)
            );

            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY,
                source_url TEXT NOT NULL,
                series_name TEXT NOT NULL,
                round_name TEXT NOT NULL DEFAULT '',
                match_date TEXT NOT NULL DEFAULT '',
                match_time TEXT NOT NULL DEFAULT '',
                home_team TEXT NOT NULL DEFAULT '',
                away_team TEXT NOT NULL DEFAULT '',
                home_club TEXT NOT NULL DEFAULT '',
                away_club TEXT NOT NULL DEFAULT '',
                organiser TEXT NOT NULL DEFAULT '',
                score TEXT NOT NULL DEFAULT '',
                source_page_id INTEGER REFERENCES source_pages(id),
                season TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL DEFAULT '',
                region TEXT NOT NULL DEFAULT '',
                imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (
                    source_url, series_name, round_name, match_date, match_time,
                    home_team, away_team, organiser
                )
            );

            CREATE INDEX IF NOT EXISTS ix_matches_organiser
                ON matches(organiser COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS ix_matches_series
                ON matches(series_name COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS ix_matches_source_page
                ON matches(source_page_id);
            """
        )

        columns = self._column_names("matches")
        migrations = {
            "source_page_id": "ALTER TABLE matches ADD COLUMN source_page_id INTEGER REFERENCES source_pages(id)",
            "season": "ALTER TABLE matches ADD COLUMN season TEXT NOT NULL DEFAULT ''",
            "source_type": "ALTER TABLE matches ADD COLUMN source_type TEXT NOT NULL DEFAULT ''",
            "region": "ALTER TABLE matches ADD COLUMN region TEXT NOT NULL DEFAULT ''",
            "home_club": "ALTER TABLE matches ADD COLUMN home_club TEXT NOT NULL DEFAULT ''",
            "away_club": "ALTER TABLE matches ADD COLUMN away_club TEXT NOT NULL DEFAULT ''",
        }
        for column, sql in migrations.items():
            if column not in columns:
                self.connection.execute(sql)
        self.connection.commit()
        self._normalize_existing_match_dates()

    def _normalize_existing_match_dates(self) -> None:
        """Convert legacy DD-MM-YY/DD-MM-YYYY dates already stored in SQLite."""
        rows = self.connection.execute(
            "SELECT id, match_date FROM matches WHERE match_date <> ''"
        ).fetchall()

        updates: list[tuple[str, int]] = []
        for row in rows:
            value = str(row["match_date"]).strip()
            if len(value) == 10 and value[4] == "-" and value[7] == "-":
                continue

            normalized = value
            for format_string in ("%d-%m-%y", "%d-%m-%Y", "%d/%m/%y", "%d/%m/%Y", "%d.%m.%y", "%d.%m.%Y"):
                try:
                    normalized = datetime.strptime(value, format_string).strftime("%Y-%m-%d")
                    break
                except ValueError:
                    continue

            if normalized != value:
                updates.append((normalized, int(row["id"])))

        if updates:
            self.connection.executemany(
                "UPDATE matches SET match_date = ? WHERE id = ?",
                updates,
            )
            self.connection.commit()

    def list_source_pages(self) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            """
            SELECT id, name, start_url, source_type, season, region, last_updated_at
            FROM source_pages
            ORDER BY season DESC,
                     CASE source_type WHEN 'national' THEN 0 ELSE 1 END,
                     region, name
            """
        ))

    def get_source_page(self, source_page_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM source_pages WHERE id = ?", (source_page_id,)
        ).fetchone()

    def save_source_page(self, page: SourcePage) -> int:
        if page.source_type == "national":
            page.region = ""
        if page.id is None:
            cursor = self.connection.execute(
                """
                INSERT INTO source_pages(name, start_url, source_type, season, region)
                VALUES (?, ?, ?, ?, ?)
                """,
                (page.name, page.start_url, page.source_type, page.season, page.region),
            )
            page_id = int(cursor.lastrowid)
        else:
            self.connection.execute(
                """
                UPDATE source_pages
                SET name = ?, start_url = ?, source_type = ?, season = ?, region = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (page.name, page.start_url, page.source_type, page.season, page.region, page.id),
            )
            page_id = page.id
        self.connection.commit()
        return page_id

    def delete_source_page(self, source_page_id: int) -> None:
        self.connection.execute(
            "UPDATE matches SET source_page_id = NULL WHERE source_page_id = ?",
            (source_page_id,),
        )
        self.connection.execute("DELETE FROM source_pages WHERE id = ?", (source_page_id,))
        self.connection.commit()

    def mark_source_page_updated(self, source_page_id: int) -> None:
        self.connection.execute(
            """
            UPDATE source_pages
            SET last_updated_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (source_page_id,),
        )
        self.connection.commit()

    def upsert_matches(
        self,
        matches: Iterable[MatchRecord],
        source_page: sqlite3.Row | None = None,
    ) -> int:
        before = self.connection.total_changes
        source_page_id = source_page["id"] if source_page else None
        season = source_page["season"] if source_page else ""
        source_type = source_page["source_type"] if source_page else ""
        region = source_page["region"] if source_page else ""

        self.connection.executemany(
            """
            INSERT INTO matches (
                source_url, series_name, round_name, match_date, match_time,
                home_team, away_team, home_club, away_club, organiser, score,
                source_page_id, season, source_type, region
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (
                source_url, series_name, round_name, match_date, match_time,
                home_team, away_team, organiser
            )
            DO UPDATE SET
                score = excluded.score,
                home_club = excluded.home_club,
                away_club = excluded.away_club,
                source_page_id = excluded.source_page_id,
                season = excluded.season,
                source_type = excluded.source_type,
                region = excluded.region,
                imported_at = CURRENT_TIMESTAMP
            """,
            [
                (
                    item.source_url, item.series_name, item.round_name,
                    item.match_date, item.match_time, item.home_team,
                    item.away_team, item.home_club, item.away_club,
                    item.organiser, item.score,
                    source_page_id, season, source_type, region,
                )
                for item in matches
            ],
        )
        self.connection.commit()
        return self.connection.total_changes - before

    def list_seasons(self) -> list[str]:
        """Return all seasons that currently exist in imported match data."""
        return [
            str(row["season"])
            for row in self.connection.execute(
                """
                SELECT DISTINCT season
                FROM matches
                WHERE season <> ''
                ORDER BY season DESC
                """
            )
        ]

    def list_months(self, season: str = "") -> list[int]:
        """Return months present in match data, ordered July through June."""
        clauses = [
            "length(match_date) = 10",
            "substr(match_date, 5, 1) = '-'",
            "substr(match_date, 8, 1) = '-'",
        ]
        parameters: list[object] = []

        if season:
            clauses.append("season = ?")
            parameters.append(season)

        rows = self.connection.execute(
            f"""
            SELECT DISTINCT CAST(substr(match_date, 6, 2) AS INTEGER) AS month_number
            FROM matches
            WHERE {' AND '.join(clauses)}
            ORDER BY
                CASE
                    WHEN CAST(substr(match_date, 6, 2) AS INTEGER) >= 7
                        THEN CAST(substr(match_date, 6, 2) AS INTEGER) - 6
                    ELSE CAST(substr(match_date, 6, 2) AS INTEGER) + 6
                END
            """,
            parameters,
        )

        return [
            int(row["month_number"])
            for row in rows
            if 1 <= int(row["month_number"]) <= 12
        ]

    def _club_team_index(
        self,
        season: str,
    ) -> tuple[list[str], dict[str, list[dict[str, str]]]]:
        """Build a season-specific club registry from teams and organisers."""
        if not season:
            return [], {}

        rows = list(
            self.connection.execute(
                """
                SELECT
                    home_team,
                    away_team,
                    home_club,
                    away_club,
                    organiser,
                    series_name,
                    source_type,
                    region
                FROM matches
                WHERE season = ?
                """,
                (season,),
            )
        )

        canonical_by_full_key: dict[str, tuple[int, str]] = {}
        preferred_by_core: dict[str, set[str]] = {}

        for row in rows:
            preferred_candidates = (
                (3, str(row["organiser"] or "")),
                (2, str(row["home_club"] or "")),
                (2, str(row["away_club"] or "")),
            )
            for priority, raw_candidate in preferred_candidates:
                candidate = _clean_club_source_name(raw_candidate)
                if not candidate:
                    continue
                full_key = _club_key(candidate)
                core_key = _club_core_key(candidate)
                if not full_key:
                    continue
                canonical_by_full_key[full_key] = _display_choice(
                    canonical_by_full_key.get(full_key), priority, candidate
                )
                if core_key:
                    preferred_by_core.setdefault(core_key, set()).add(full_key)

        team_club_key: dict[tuple[str, str], str] = {}
        for row in rows:
            for side in ("home", "away"):
                team_name = str(row[f"{side}_team"] or "").strip()
                explicit_club = _clean_club_source_name(
                    str(row[f"{side}_club"] or "")
                )
                if not team_name:
                    continue
                candidate = explicit_club or _derived_club_name(team_name)
                full_key = _club_key(candidate)
                core_key = _club_core_key(candidate)
                if not full_key:
                    continue
                resolved_key = full_key
                if full_key not in canonical_by_full_key and core_key:
                    preferred_matches = preferred_by_core.get(core_key, set())
                    if len(preferred_matches) == 1:
                        resolved_key = next(iter(preferred_matches))
                if resolved_key not in canonical_by_full_key:
                    canonical_by_full_key[resolved_key] = _display_choice(
                        None, 2 if explicit_club else 1, candidate
                    )
                team_club_key[(team_name, str(row["series_name"] or ""))] = resolved_key

        teams_by_club: dict[str, dict[tuple[str, str, str, str], dict[str, str]]] = {}
        for row in rows:
            for side in ("home", "away"):
                team_name = str(row[f"{side}_team"] or "").strip()
                series_name = str(row["series_name"] or "")
                if not team_name:
                    continue
                resolved_key = team_club_key.get((team_name, series_name))
                if not resolved_key:
                    continue
                canonical = canonical_by_full_key[resolved_key][1]
                team_key = (
                    team_name,
                    series_name,
                    str(row["source_type"] or ""),
                    str(row["region"] or ""),
                )
                teams_by_club.setdefault(canonical, {})[team_key] = {
                    "team_name": team_name,
                    "series_name": series_name,
                    "source_type": str(row["source_type"] or ""),
                    "region": str(row["region"] or ""),
                }

        all_clubs = {
            display for _priority, display in canonical_by_full_key.values()
        } | set(teams_by_club)
        ordered_clubs = sorted(all_clubs, key=str.casefold)
        ordered_teams = {
            club: sorted(
                teams_by_club.get(club, {}).values(),
                key=lambda item: (
                    item["team_name"].casefold(),
                    item["series_name"].casefold(),
                ),
            )
            for club in ordered_clubs
        }
        return ordered_clubs, ordered_teams

    def list_clubs(self, season: str) -> list[str]:
        """Return the merged, alphabetically sorted club registry."""
        clubs, _teams = self._club_team_index(season)
        return clubs

    def list_teams_for_club(
        self,
        season: str,
        club: str,
    ) -> list[dict[str, str]]:
        """Return the teams and series belonging to one merged club."""
        _clubs, teams = self._club_team_index(season)
        return teams.get(club, [])


    def query_matches(
        self,
        organiser: str = "",
        series_name: str = "",
        season: str = "",
        month: int | None = None,
    ) -> list[sqlite3.Row]:
        clauses = ["organiser LIKE ?", "series_name LIKE ?"]
        parameters: list[object] = [
            f"%{organiser.strip()}%",
            f"%{series_name.strip()}%",
        ]

        if season:
            clauses.append("season = ?")
            parameters.append(season)
        if month is not None:
            clauses.append("substr(match_date, 6, 2) = ?")
            parameters.append(f"{month:02d}")

        sql = f"""
            SELECT match_date, match_time, series_name, round_name,
                   home_team, away_team, organiser, score, source_url,
                   season, source_type, region
            FROM matches
            WHERE {' AND '.join(clauses)}
            ORDER BY match_date, match_time, series_name, round_name, organiser
        """
        return list(self.connection.execute(sql, parameters))

    def organiser_summary(
        self,
        organiser: str = "",
        series_name: str = "",
        season: str = "",
        month: int | None = None,
    ) -> list[sqlite3.Row]:
        clauses = ["organiser LIKE ?", "series_name LIKE ?"]
        parameters: list[object] = [
            f"%{organiser.strip()}%",
            f"%{series_name.strip()}%",
        ]

        if season:
            clauses.append("season = ?")
            parameters.append(season)
        if month is not None:
            clauses.append("substr(match_date, 6, 2) = ?")
            parameters.append(f"{month:02d}")

        sql = f"""
            SELECT match_date, series_name, round_name, organiser,
                   season, source_type, region, COUNT(*) AS match_count
            FROM matches
            WHERE {' AND '.join(clauses)}
            GROUP BY match_date, series_name, round_name, organiser,
                     season, source_type, region
            ORDER BY match_date, series_name, round_name
        """
        return list(self.connection.execute(sql, parameters))

    @staticmethod
    def export_csv(rows: list[sqlite3.Row], path: Path) -> None:
        if not rows:
            return
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=";")
            writer.writerow(rows[0].keys())
            for row in rows:
                writer.writerow(tuple(row))
