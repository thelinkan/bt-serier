from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import Iterable

from .models import MatchRecord


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY,
                source_url TEXT NOT NULL,
                series_name TEXT NOT NULL,
                round_name TEXT NOT NULL DEFAULT '',
                match_date TEXT NOT NULL DEFAULT '',
                match_time TEXT NOT NULL DEFAULT '',
                home_team TEXT NOT NULL DEFAULT '',
                away_team TEXT NOT NULL DEFAULT '',
                organiser TEXT NOT NULL DEFAULT '',
                score TEXT NOT NULL DEFAULT '',
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
            """
        )
        self.connection.commit()

    def upsert_matches(self, matches: Iterable[MatchRecord]) -> int:
        before = self.connection.total_changes
        self.connection.executemany(
            """
            INSERT INTO matches (
                source_url, series_name, round_name, match_date, match_time,
                home_team, away_team, organiser, score
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (
                source_url, series_name, round_name, match_date, match_time,
                home_team, away_team, organiser
            )
            DO UPDATE SET
                score = excluded.score,
                imported_at = CURRENT_TIMESTAMP
            """,
            [
                (
                    item.source_url,
                    item.series_name,
                    item.round_name,
                    item.match_date,
                    item.match_time,
                    item.home_team,
                    item.away_team,
                    item.organiser,
                    item.score,
                )
                for item in matches
            ],
        )
        self.connection.commit()
        return self.connection.total_changes - before

    def query_matches(
        self,
        organiser: str = "",
        series_name: str = "",
    ) -> list[sqlite3.Row]:
        sql = """
            SELECT
                match_date,
                match_time,
                series_name,
                round_name,
                home_team,
                away_team,
                organiser,
                score,
                source_url
            FROM matches
            WHERE organiser LIKE ?
              AND series_name LIKE ?
            ORDER BY
                match_date,
                match_time,
                series_name,
                round_name,
                organiser
        """
        return list(
            self.connection.execute(
                sql,
                (f"%{organiser.strip()}%", f"%{series_name.strip()}%"),
            )
        )

    def organiser_summary(self, organiser: str) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                """
                SELECT
                    match_date,
                    series_name,
                    round_name,
                    organiser,
                    COUNT(*) AS match_count
                FROM matches
                WHERE organiser LIKE ?
                GROUP BY match_date, series_name, round_name, organiser
                ORDER BY match_date, series_name, round_name
                """,
                (f"%{organiser.strip()}%",),
            )
        )

    @staticmethod
    def export_csv(rows: list[sqlite3.Row], path: Path) -> None:
        if not rows:
            return
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=";")
            writer.writerow(rows[0].keys())
            for row in rows:
                writer.writerow(tuple(row))
