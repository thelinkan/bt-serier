from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import Iterable

from .models import MatchRecord, SourcePage


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
        }
        for column, sql in migrations.items():
            if column not in columns:
                self.connection.execute(sql)
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
                home_team, away_team, organiser, score,
                source_page_id, season, source_type, region
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (
                source_url, series_name, round_name, match_date, match_time,
                home_team, away_team, organiser
            )
            DO UPDATE SET
                score = excluded.score,
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
                    item.away_team, item.organiser, item.score,
                    source_page_id, season, source_type, region,
                )
                for item in matches
            ],
        )
        self.connection.commit()
        return self.connection.total_changes - before

    def query_matches(self, organiser: str = "", series_name: str = "") -> list[sqlite3.Row]:
        return list(self.connection.execute(
            """
            SELECT match_date, match_time, series_name, round_name,
                   home_team, away_team, organiser, score, source_url,
                   season, source_type, region
            FROM matches
            WHERE organiser LIKE ? AND series_name LIKE ?
            ORDER BY match_date, match_time, series_name, round_name, organiser
            """,
            (f"%{organiser.strip()}%", f"%{series_name.strip()}%"),
        ))

    def organiser_summary(self, organiser: str) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            """
            SELECT match_date, series_name, round_name, organiser,
                   season, source_type, region, COUNT(*) AS match_count
            FROM matches
            WHERE organiser LIKE ?
            GROUP BY match_date, series_name, round_name, organiser,
                     season, source_type, region
            ORDER BY match_date, series_name, round_name
            """,
            (f"%{organiser.strip()}%",),
        ))

    @staticmethod
    def export_csv(rows: list[sqlite3.Row], path: Path) -> None:
        if not rows:
            return
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=";")
            writer.writerow(rows[0].keys())
            for row in rows:
                writer.writerow(tuple(row))
