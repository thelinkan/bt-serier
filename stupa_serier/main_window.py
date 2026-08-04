from __future__ import annotations

import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QThread, Signal, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .database import Database
from .models import SourcePage
from .scraper import discover_series, scrape_series


APP_DIR = Path.cwd()
DATA_DIR = APP_DIR / "data"
DIAGNOSTICS_DIR = APP_DIR / "diagnostics"
DATA_DIR.mkdir(exist_ok=True)


class SourceUpdateWorker(QThread):
    status_changed = Signal(str)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, source_pages: list[dict]) -> None:
        super().__init__()
        self.source_pages = source_pages

    def run(self) -> None:
        try:
            result: list[dict] = []
            for page_index, source in enumerate(self.source_pages, start=1):
                prefix = f"Källsida {page_index}/{len(self.source_pages)}: {source['name']}"
                self.status_changed.emit(f"{prefix} – upptäcker serier…")
                series_names = discover_series(
                    source["start_url"],
                    source["source_type"],
                    DIAGNOSTICS_DIR,
                    status=lambda message, p=prefix: self.status_changed.emit(f"{p} – {message}"),
                )
                if not series_names:
                    raise RuntimeError(f"Inga serier hittades på källsidan {source['name']}.")

                page_records = []
                errors: list[str] = []
                for series_index, series_name in enumerate(series_names, start=1):
                    self.status_changed.emit(
                        f"{prefix} – hämtar {series_name} "
                        f"({series_index}/{len(series_names)})…"
                    )
                    try:
                        records = scrape_series(
                            source["start_url"],
                            series_name,
                            DIAGNOSTICS_DIR,
                            status=lambda message, p=prefix, s=series_name: self.status_changed.emit(
                                f"{p} – {s}: {message}"
                            ),
                        )
                        page_records.extend(records)
                    except Exception as error:
                        errors.append(f"{series_name}: {error}")

                result.append(
                    {
                        "source": source,
                        "series_names": series_names,
                        "records": page_records,
                        "errors": errors,
                    }
                )

            self.completed.emit(result)
        except Exception as error:
            self.failed.emit(f"{error}\n\n{traceback.format_exc()}")


class MainWindow(QMainWindow):
    SOURCE_COLUMNS = [
        ("Namn", "name"),
        ("Typ", "source_type"),
        ("Säsong", "season"),
        ("Region", "region"),
        ("Senast uppdaterad", "last_updated_at"),
        ("Startadress", "start_url"),
    ]

    MATCH_COLUMNS = [
        ("Datum", "match_date"),
        ("Tid", "match_time"),
        ("Serie", "series_name"),
        ("Säsong", "season"),
        ("Område", "region"),
        ("Omgång", "round_name"),
        ("Hemma", "home_team"),
        ("Borta", "away_team"),
        ("Arrangör", "organiser"),
        ("Resultat", "score"),
    ]

    TEAM_COLUMNS = [
        ("Lag", "team_name"),
        ("Serie", "series_name"),
        ("Typ", "source_type"),
        ("Region", "region"),
    ]

    CLUB_MATCH_COLUMNS = [
        ("Datum", "match_date"),
        ("Tid", "match_time"),
        ("Serie", "series_name"),
        ("Omgång", "round_name"),
        ("Hemma", "home_team"),
        ("Borta", "away_team"),
        ("Arrangör", "organiser"),
        ("Resultat", "score"),
        ("Region", "region"),
    ]

    SUMMARY_COLUMNS = [
        ("Datum", "match_date"),
        ("Serie", "series_name"),
        ("Säsong", "season"),
        ("Område", "region"),
        ("Omgång", "round_name"),
        ("Arrangör", "organiser"),
        ("Matcher", "match_count"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("STUPA-serier")
        self.resize(1450, 900)

        self.database = Database(DATA_DIR / "stupa_serier.sqlite")
        self.current_rows = []
        self.worker: SourceUpdateWorker | None = None
        self.editing_source_id: int | None = None

        tabs = QTabWidget()
        tabs.addTab(self._create_source_pages_tab(), "Källsidor")
        tabs.addTab(self._create_matches_tab(), "Matcher och arrangörer")
        tabs.addTab(self._create_clubs_teams_tab(), "Förening och lag")

        self.status_label = QLabel("Klar.")
        self.status_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(tabs, 1)
        layout.addWidget(self.status_label)
        self.setCentralWidget(central)

        self.refresh_source_pages()
        self.refresh_filter_options()
        self.refresh_tables()
        self.refresh_club_season_options()

    def _create_source_pages_tab(self) -> QWidget:
        self.source_table = QTableWidget()
        self.source_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.source_table.setColumnCount(len(self.SOURCE_COLUMNS))
        self.source_table.setHorizontalHeaderLabels([label for label, _ in self.SOURCE_COLUMNS])
        self.source_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.source_table.horizontalHeader().setStretchLastSection(True)
        self.source_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.source_table.setSelectionMode(QTableWidget.SingleSelection)
        self.source_table.itemSelectionChanged.connect(self.load_selected_source)

        self.source_name_edit = QLineEdit()
        self.source_url_edit = QLineEdit()
        self.source_url_edit.setPlaceholderText("Fullständig fungerande serieadress")
        self.source_type_combo = QComboBox()
        self.source_type_combo.addItem("Nationella serier", "national")
        self.source_type_combo.addItem("Regionala serier", "regional")
        self.source_type_combo.currentIndexChanged.connect(self.update_region_enabled)
        self.season_edit = QLineEdit()
        self.season_edit.setPlaceholderText("Exempel: 2026/2027")
        self.region_edit = QLineEdit()
        self.region_edit.setPlaceholderText("Exempel: Nordöstra Svealand")

        form = QFormLayout()
        form.addRow("Namn:", self.source_name_edit)
        form.addRow("Startadress:", self.source_url_edit)
        form.addRow("Typ:", self.source_type_combo)
        form.addRow("Säsong:", self.season_edit)
        form.addRow("Region:", self.region_edit)

        self.new_source_button = QPushButton("Ny")
        self.new_source_button.clicked.connect(self.clear_source_form)
        self.save_source_button = QPushButton("Spara")
        self.save_source_button.clicked.connect(self.save_source)
        self.delete_source_button = QPushButton("Ta bort")
        self.delete_source_button.clicked.connect(self.delete_source)

        form_buttons = QHBoxLayout()
        form_buttons.addWidget(self.new_source_button)
        form_buttons.addWidget(self.save_source_button)
        form_buttons.addWidget(self.delete_source_button)

        form_widget = QWidget()
        form_layout = QVBoxLayout(form_widget)
        form_layout.addLayout(form)
        form_layout.addLayout(form_buttons)
        form_layout.addStretch(1)

        splitter = QSplitter()
        splitter.addWidget(self.source_table)
        splitter.addWidget(form_widget)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        self.update_selected_button = QPushButton("Uppdatera markerad källsida")
        self.update_selected_button.clicked.connect(self.update_selected_source)
        self.update_all_button = QPushButton("Uppdatera alla källsidor")
        self.update_all_button.clicked.connect(self.update_all_sources)
        diagnostics_button = QPushButton("Öppna diagnostikmapp")
        diagnostics_button.clicked.connect(self.open_diagnostics)

        action_row = QHBoxLayout()
        action_row.addWidget(self.update_selected_button)
        action_row.addWidget(self.update_all_button)
        action_row.addStretch(1)
        action_row.addWidget(diagnostics_button)

        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.addWidget(splitter, 1)
        layout.addLayout(action_row)
        return widget

    def _create_matches_tab(self) -> QWidget:
        self.organiser_filter = QLineEdit()
        self.organiser_filter.setPlaceholderText("Exempel: Stratos")
        self.organiser_filter.textChanged.connect(self.refresh_tables)
        self.series_filter = QLineEdit()
        self.series_filter.setPlaceholderText("Filtrera på serienamn")
        self.series_filter.textChanged.connect(self.refresh_tables)

        self.season_filter = QComboBox()
        self.season_filter.currentIndexChanged.connect(
            self.on_season_filter_changed
        )

        self.month_filter = QComboBox()
        self.month_filter.currentIndexChanged.connect(self.refresh_tables)

        export_button = QPushButton("Exportera CSV")
        export_button.clicked.connect(self.export_csv)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Arrangör:"))
        filter_row.addWidget(self.organiser_filter, 1)
        filter_row.addWidget(QLabel("Serie:"))
        filter_row.addWidget(self.series_filter, 1)
        filter_row.addWidget(QLabel("Säsong:"))
        filter_row.addWidget(self.season_filter)
        filter_row.addWidget(QLabel("Månad:"))
        filter_row.addWidget(self.month_filter)
        filter_row.addWidget(export_button)

        self.matches_table = QTableWidget()
        self.matches_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.matches_table.setColumnCount(len(self.MATCH_COLUMNS))
        self.matches_table.setHorizontalHeaderLabels([label for label, _ in self.MATCH_COLUMNS])
        self.matches_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.matches_table.horizontalHeader().setStretchLastSection(True)
        self.matches_table.setSortingEnabled(True)
        self.matches_table.cellDoubleClicked.connect(self.open_source_url)

        self.summary_table = QTableWidget()
        self.summary_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.summary_table.setColumnCount(len(self.SUMMARY_COLUMNS))
        self.summary_table.setHorizontalHeaderLabels([label for label, _ in self.SUMMARY_COLUMNS])
        self.summary_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.summary_table.horizontalHeader().setStretchLastSection(True)
        self.summary_table.setSortingEnabled(True)

        tabs = QTabWidget()
        tabs.addTab(self.summary_table, "Per seriehelg/omgång")
        tabs.addTab(self.matches_table, "Alla matcher")

        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.addLayout(filter_row)
        layout.addWidget(tabs, 1)
        return widget

    def _create_clubs_teams_tab(self) -> QWidget:
        self.club_season_filter = QComboBox()
        self.club_season_filter.currentIndexChanged.connect(
            self.on_club_season_changed
        )

        self.club_list = QListWidget()
        self.club_list.setSortingEnabled(False)
        self.club_list.currentItemChanged.connect(
            self.on_selected_club_changed
        )

        self.selected_club_label = QLabel("Välj en förening")

        # Lagläge
        self.club_team_table = QTableWidget()
        self.club_team_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.club_team_table.setColumnCount(len(self.TEAM_COLUMNS))
        self.club_team_table.setHorizontalHeaderLabels(
            [label for label, _ in self.TEAM_COLUMNS]
        )
        self.club_team_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeToContents
        )
        self.club_team_table.horizontalHeader().setStretchLastSection(True)
        self.club_team_table.setSortingEnabled(True)

        teams_widget = QWidget()
        teams_layout = QVBoxLayout(teams_widget)
        teams_layout.addWidget(self.club_team_table, 1)

        # Matchläge
        self.club_match_filter_type = QComboBox()
        self.club_match_filter_type.addItem("Filtrera på månad", "month")
        self.club_match_filter_type.addItem("Filtrera på lag", "team")
        self.club_match_filter_type.currentIndexChanged.connect(
            self.refresh_club_match_filter_values
        )

        self.club_match_filter_value = QComboBox()
        self.club_match_filter_value.currentIndexChanged.connect(
            self.refresh_club_matches
        )

        self.highlight_organised_matches = QCheckBox(
            "Markera matcher som föreningen arrangerar"
        )
        self.highlight_organised_matches.toggled.connect(
            self.refresh_club_matches
        )

        match_filter_row = QHBoxLayout()
        match_filter_row.addWidget(QLabel("Filter:"))
        match_filter_row.addWidget(self.club_match_filter_type)
        match_filter_row.addWidget(self.club_match_filter_value, 1)
        match_filter_row.addWidget(self.highlight_organised_matches)
        match_filter_row.addStretch(1)

        self.club_match_table = QTableWidget()
        self.club_match_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.club_match_table.setColumnCount(len(self.CLUB_MATCH_COLUMNS))
        self.club_match_table.setHorizontalHeaderLabels(
            [label for label, _ in self.CLUB_MATCH_COLUMNS]
        )
        self.club_match_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeToContents
        )
        self.club_match_table.horizontalHeader().setStretchLastSection(True)
        self.club_match_table.setSortingEnabled(True)
        self.club_match_table.cellDoubleClicked.connect(
            self.open_club_match_source_url
        )

        matches_widget = QWidget()
        matches_layout = QVBoxLayout(matches_widget)
        matches_layout.addLayout(match_filter_row)
        matches_layout.addWidget(self.club_match_table, 1)

        self.club_detail_tabs = QTabWidget()
        self.club_detail_tabs.addTab(teams_widget, "Lag")
        self.club_detail_tabs.addTab(matches_widget, "Matcher")
        self.club_detail_tabs.currentChanged.connect(
            self.refresh_selected_club
        )

        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.addWidget(QLabel("Säsong:"))
        left_layout.addWidget(self.club_season_filter)
        left_layout.addWidget(QLabel("Föreningar:"))
        left_layout.addWidget(self.club_list, 1)

        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.addWidget(self.selected_club_label)
        right_layout.addWidget(self.club_detail_tabs, 1)

        splitter = QSplitter()
        splitter.addWidget(left_widget)
        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([350, 1000])

        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.addWidget(splitter, 1)
        return widget

    def update_region_enabled(self) -> None:
        regional = self.source_type_combo.currentData() == "regional"
        self.region_edit.setEnabled(regional)
        if not regional:
            self.region_edit.clear()

    def clear_source_form(self) -> None:
        self.editing_source_id = None
        self.source_name_edit.clear()
        self.source_url_edit.clear()
        self.source_type_combo.setCurrentIndex(0)
        self.season_edit.clear()
        self.region_edit.clear()
        self.source_table.clearSelection()
        self.update_region_enabled()

    def save_source(self) -> None:
        name = self.source_name_edit.text().strip()
        url = self.source_url_edit.text().strip()
        source_type = str(self.source_type_combo.currentData())
        season = self.season_edit.text().strip()
        region = self.region_edit.text().strip()

        if not name or not url or not season:
            QMessageBox.warning(self, "Ofullständig källsida", "Namn, startadress och säsong måste anges.")
            return
        if not url.startswith(("http://", "https://")):
            QMessageBox.warning(self, "Felaktig adress", "Ange en fullständig webbadress.")
            return
        if source_type == "regional" and not region:
            QMessageBox.warning(self, "Saknad region", "Regionala källsidor måste ha en region.")
            return

        try:
            page_id = self.database.save_source_page(SourcePage(
                id=self.editing_source_id,
                name=name,
                start_url=url,
                source_type=source_type,
                season=season,
                region=region,
            ))
        except Exception as error:
            QMessageBox.critical(self, "Kunde inte spara", str(error))
            return

        self.editing_source_id = page_id
        self.refresh_source_pages(select_id=page_id)
        self.status_label.setText(f"Källsidan '{name}' sparades.")

    def delete_source(self) -> None:
        if self.editing_source_id is None:
            return
        if QMessageBox.question(self, "Ta bort källsida", "Ta bort den markerade källsidan?") != QMessageBox.Yes:
            return
        self.database.delete_source_page(self.editing_source_id)
        self.clear_source_form()
        self.refresh_source_pages()

    def refresh_source_pages(self, select_id: int | None = None) -> None:
        rows = self.database.list_source_pages()
        self.source_table.setRowCount(len(rows))
        selected_row = None
        for row_index, row in enumerate(rows):
            for column_index, (_, field) in enumerate(self.SOURCE_COLUMNS):
                value = row[field] or ""
                if field == "source_type":
                    value = "Nationell" if value == "national" else "Regional"
                item = QTableWidgetItem(str(value))
                item.setData(Qt.UserRole, row["id"])
                self.source_table.setItem(row_index, column_index, item)
            if select_id == row["id"]:
                selected_row = row_index
        if selected_row is not None:
            self.source_table.selectRow(selected_row)

    def selected_source_id(self) -> int | None:
        row = self.source_table.currentRow()
        if row < 0:
            return None
        item = self.source_table.item(row, 0)
        return int(item.data(Qt.UserRole)) if item else None

    def load_selected_source(self) -> None:
        source_id = self.selected_source_id()
        if source_id is None:
            return
        row = self.database.get_source_page(source_id)
        if row is None:
            return
        self.editing_source_id = source_id
        self.source_name_edit.setText(row["name"])
        self.source_url_edit.setText(row["start_url"])
        self.source_type_combo.setCurrentIndex(0 if row["source_type"] == "national" else 1)
        self.season_edit.setText(row["season"])
        self.region_edit.setText(row["region"] or "")
        self.update_region_enabled()

    @staticmethod
    def row_to_dict(row) -> dict:
        return {key: row[key] for key in row.keys()}

    def update_selected_source(self) -> None:
        source_id = self.selected_source_id()
        if source_id is None:
            QMessageBox.information(self, "Ingen källsida", "Markera en källsida först.")
            return
        row = self.database.get_source_page(source_id)
        if row:
            self.start_source_update([self.row_to_dict(row)])

    def update_all_sources(self) -> None:
        rows = self.database.list_source_pages()
        if not rows:
            QMessageBox.information(self, "Inga källsidor", "Lägg först upp minst en källsida.")
            return
        self.start_source_update([self.row_to_dict(row) for row in rows])

    def start_source_update(self, sources: list[dict]) -> None:
        self.update_selected_button.setEnabled(False)
        self.update_all_button.setEnabled(False)
        self.worker = SourceUpdateWorker(sources)
        self.worker.status_changed.connect(self.status_label.setText)
        self.worker.completed.connect(self.source_update_completed)
        self.worker.failed.connect(self.source_update_failed)
        self.worker.start()

    def source_update_completed(self, result: list[dict]) -> None:
        total_records = 0
        total_changes = 0
        errors: list[str] = []
        for page_result in result:
            source = page_result["source"]
            source_row = self.database.get_source_page(int(source["id"]))
            records = page_result["records"]
            total_records += len(records)
            total_changes += self.database.upsert_matches(records, source_row)
            self.database.mark_source_page_updated(int(source["id"]))
            errors.extend(f"{source['name']} – {message}" for message in page_result["errors"])

        self.update_selected_button.setEnabled(True)
        self.update_all_button.setEnabled(True)
        self.refresh_source_pages()
        self.refresh_filter_options()
        self.refresh_tables()
        self.refresh_club_season_options()
        self.status_label.setText(
            f"Uppdateringen klar: {total_records} matcher lästes och "
            f"{total_changes} databasändringar gjordes."
        )
        if errors:
            QMessageBox.warning(
                self,
                "Uppdateringen slutfördes med fel",
                "\n\n".join(errors[:20]),
            )

    def source_update_failed(self, message: str) -> None:
        self.update_selected_button.setEnabled(True)
        self.update_all_button.setEnabled(True)
        self.status_label.setText("Uppdateringen misslyckades.")
        QMessageBox.critical(self, "Kunde inte uppdatera källsidor", message)

    def refresh_club_season_options(self) -> None:
        """
        Refresh the mandatory season selector for the club/team tab.

        There is deliberately no "all seasons" choice because team names and
        affiliations may differ between seasons.
        """
        if not hasattr(self, "club_season_filter"):
            return

        current = str(self.club_season_filter.currentData() or "")
        seasons = self.database.list_seasons()

        self.club_season_filter.blockSignals(True)
        self.club_season_filter.clear()

        for season in seasons:
            self.club_season_filter.addItem(season, season)

        selected_index = self.club_season_filter.findData(current)
        if selected_index < 0 and seasons:
            selected_index = 0
        self.club_season_filter.setCurrentIndex(selected_index)
        self.club_season_filter.blockSignals(False)

        self.refresh_club_list()

    def on_club_season_changed(self) -> None:
        self.refresh_club_list()

    def refresh_club_list(self) -> None:
        if not hasattr(self, "club_list"):
            return

        current_club = (
            self.club_list.currentItem().text()
            if self.club_list.currentItem()
            else ""
        )
        season = str(self.club_season_filter.currentData() or "")

        self.club_list.blockSignals(True)
        self.club_list.clear()
        for club in self.database.list_clubs(season):
            self.club_list.addItem(QListWidgetItem(club))

        matching_items = self.club_list.findItems(
            current_club,
            Qt.MatchExactly,
        ) if current_club else []

        if matching_items:
            self.club_list.setCurrentItem(matching_items[0])
        elif self.club_list.count() > 0:
            self.club_list.setCurrentRow(0)

        self.club_list.blockSignals(False)
        self.refresh_selected_club()

    def on_selected_club_changed(self, _current=None, _previous=None) -> None:
        self.refresh_selected_club()

    def current_club_context(self) -> tuple[str, str]:
        season = str(self.club_season_filter.currentData() or "")
        item = self.club_list.currentItem()
        club = item.text() if item else ""
        return season, club

    def refresh_selected_club(self, _index=None) -> None:
        if not hasattr(self, "club_team_table"):
            return

        season, club = self.current_club_context()

        if not season:
            self.selected_club_label.setText("Ingen importerad säsong finns")
            self._fill_table(self.club_team_table, [], self.TEAM_COLUMNS)
            self._fill_table(
                self.club_match_table,
                [],
                self.CLUB_MATCH_COLUMNS,
            )
            return

        if not club:
            self.selected_club_label.setText(
                f"Inga föreningar finns för säsongen {season}"
            )
            self._fill_table(self.club_team_table, [], self.TEAM_COLUMNS)
            self._fill_table(
                self.club_match_table,
                [],
                self.CLUB_MATCH_COLUMNS,
            )
            return

        self.selected_club_label.setText(f"{club} – säsong {season}")

        team_rows = self.database.list_teams_for_club(season, club)
        self._fill_table(
            self.club_team_table,
            team_rows,
            self.TEAM_COLUMNS,
        )

        self.refresh_club_match_filter_values()

    def refresh_club_match_filter_values(self, _index=None) -> None:
        if not hasattr(self, "club_match_filter_value"):
            return

        season, club = self.current_club_context()
        filter_type = str(
            self.club_match_filter_type.currentData() or "month"
        )
        previous = self.club_match_filter_value.currentData()

        month_names = {
            1: "Januari",
            2: "Februari",
            3: "Mars",
            4: "April",
            5: "Maj",
            6: "Juni",
            7: "Juli",
            8: "Augusti",
            9: "September",
            10: "Oktober",
            11: "November",
            12: "December",
        }

        self.club_match_filter_value.blockSignals(True)
        self.club_match_filter_value.clear()

        if filter_type == "month":
            self.club_match_filter_value.addItem("Alla månader", None)
            if season and club:
                for month_number in self.database.list_club_months(
                    season,
                    club,
                ):
                    self.club_match_filter_value.addItem(
                        month_names[month_number],
                        month_number,
                    )
        else:
            self.club_match_filter_value.addItem("Alla lag", "")
            if season and club:
                for team_name in self.database.list_club_team_names(
                    season,
                    club,
                ):
                    self.club_match_filter_value.addItem(
                        team_name,
                        team_name,
                    )

        selected_index = self.club_match_filter_value.findData(previous)
        self.club_match_filter_value.setCurrentIndex(
            selected_index if selected_index >= 0 else 0
        )
        self.club_match_filter_value.blockSignals(False)
        self.refresh_club_matches()

    def refresh_club_matches(self, _index=None) -> None:
        if not hasattr(self, "club_match_table"):
            return

        season, club = self.current_club_context()
        if not season or not club:
            self._fill_table(
                self.club_match_table,
                [],
                self.CLUB_MATCH_COLUMNS,
            )
            return

        filter_type = str(
            self.club_match_filter_type.currentData() or "month"
        )
        value = self.club_match_filter_value.currentData()

        month = value if filter_type == "month" else None
        team_name = str(value or "") if filter_type == "team" else ""

        rows = self.database.query_club_matches(
            season,
            club,
            month=month,
            team_name=team_name,
        )
        self._fill_club_match_table(rows, club)

    def _fill_club_match_table(self, rows, club: str) -> None:
        """
        Fill the club match table and optionally highlight rows arranged by
        the selected club.
        """
        highlight = self.highlight_organised_matches.isChecked()

        self.club_match_table.setSortingEnabled(False)
        self.club_match_table.setRowCount(len(rows))

        for row_index, row in enumerate(rows):
            arranged_by_club = self.database.organiser_matches_club(
                str(row["organiser"] or ""),
                club,
            )

            for column_index, (_, field) in enumerate(
                self.CLUB_MATCH_COLUMNS
            ):
                item = QTableWidgetItem(str(row[field] or ""))
                item.setData(Qt.UserRole, row["source_url"])

                if highlight and arranged_by_club:
                    item.setBackground(QColor(255, 244, 184))
                    font = QFont(item.font())
                    font.setBold(True)
                    item.setFont(font)

                self.club_match_table.setItem(
                    row_index,
                    column_index,
                    item,
                )

        self.club_match_table.setSortingEnabled(True)
        self.club_match_table.resizeRowsToContents()

    def open_club_match_source_url(self, row: int, _column: int) -> None:
        item = self.club_match_table.item(row, 0)
        if item and item.data(Qt.UserRole):
            QDesktopServices.openUrl(
                QUrl(str(item.data(Qt.UserRole)))
            )


    def refresh_filter_options(self) -> None:
        """Refresh season and month choices while preserving selections."""
        if not hasattr(self, "season_filter"):
            return

        current_season = str(self.season_filter.currentData() or "")
        self.season_filter.blockSignals(True)
        self.season_filter.clear()
        self.season_filter.addItem("Alla säsonger", "")
        for season in self.database.list_seasons():
            self.season_filter.addItem(season, season)

        selected_index = self.season_filter.findData(current_season)
        self.season_filter.setCurrentIndex(
            selected_index if selected_index >= 0 else 0
        )
        self.season_filter.blockSignals(False)

        self.refresh_month_filter()

    def on_season_filter_changed(self) -> None:
        self.refresh_month_filter()
        self.refresh_tables()

    def refresh_month_filter(self) -> None:
        """
        Show only months present in the database.

        Months follow the table-tennis season order: July through June.
        When a season is selected, only months in that season are shown.
        """
        if not hasattr(self, "month_filter"):
            return

        month_names = {
            1: "Januari",
            2: "Februari",
            3: "Mars",
            4: "April",
            5: "Maj",
            6: "Juni",
            7: "Juli",
            8: "Augusti",
            9: "September",
            10: "Oktober",
            11: "November",
            12: "December",
        }

        current_month = self.month_filter.currentData()
        season = str(self.season_filter.currentData() or "")

        self.month_filter.blockSignals(True)
        self.month_filter.clear()
        self.month_filter.addItem("Alla månader", None)

        for month_number in self.database.list_months(season):
            self.month_filter.addItem(
                month_names[month_number],
                month_number,
            )

        selected_index = self.month_filter.findData(current_month)
        self.month_filter.setCurrentIndex(
            selected_index if selected_index >= 0 else 0
        )
        self.month_filter.blockSignals(False)

    def refresh_tables(self) -> None:
        organiser = self.organiser_filter.text() if hasattr(self, "organiser_filter") else ""
        series_name = self.series_filter.text() if hasattr(self, "series_filter") else ""
        season = (
            str(self.season_filter.currentData() or "")
            if hasattr(self, "season_filter")
            else ""
        )
        month = (
            self.month_filter.currentData()
            if hasattr(self, "month_filter")
            else None
        )

        rows = self.database.query_matches(
            organiser=organiser,
            series_name=series_name,
            season=season,
            month=month,
        )
        self.current_rows = rows
        self._fill_table(self.matches_table, rows, self.MATCH_COLUMNS)

        summary_rows = self.database.organiser_summary(
            organiser=organiser,
            series_name=series_name,
            season=season,
            month=month,
        )
        self._fill_table(self.summary_table, summary_rows, self.SUMMARY_COLUMNS)

    @staticmethod
    def _fill_table(table, rows, columns) -> None:
        table.setSortingEnabled(False)
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column_index, (_, field) in enumerate(columns):
                item = QTableWidgetItem(str(row[field] or ""))
                if field == "match_count":
                    item.setData(Qt.EditRole, int(row[field]))
                if "source_url" in row.keys():
                    item.setData(Qt.UserRole, row["source_url"])
                table.setItem(row_index, column_index, item)
        table.setSortingEnabled(True)
        table.resizeRowsToContents()

    def export_csv(self) -> None:
        if not self.current_rows:
            QMessageBox.information(self, "Inget att exportera", "Filtret gav inga rader.")
            return
        filename, _ = QFileDialog.getSaveFileName(
            self, "Exportera matcher", "stupa_matcher.csv", "CSV-filer (*.csv)"
        )
        if filename:
            self.database.export_csv(self.current_rows, Path(filename))

    def open_diagnostics(self) -> None:
        DIAGNOSTICS_DIR.mkdir(exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(DIAGNOSTICS_DIR.resolve())))

    def open_source_url(self, row: int, _column: int) -> None:
        item = self.matches_table.item(row, 0)
        if item and item.data(Qt.UserRole):
            QDesktopServices.openUrl(QUrl(str(item.data(Qt.UserRole))))


def run() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
