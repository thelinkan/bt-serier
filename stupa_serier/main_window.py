from __future__ import annotations

import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .database import Database
from .scraper import ScrapeError, scrape_series


APP_DIR = Path.cwd()
DATA_DIR = APP_DIR / "data"
DIAGNOSTICS_DIR = APP_DIR / "diagnostics"
DATA_DIR.mkdir(exist_ok=True)


class ScrapeWorker(QThread):
    status_changed = Signal(str)
    completed = Signal(list)
    failed = Signal(str)

    def __init__(self, url: str, series_name: str) -> None:
        super().__init__()
        self.url = url
        self.series_name = series_name

    def run(self) -> None:
        try:
            records = scrape_series(
                self.url,
                self.series_name,
                DIAGNOSTICS_DIR,
                status=self.status_changed.emit,
            )
            self.completed.emit(records)
        except Exception as error:
            details = traceback.format_exc()
            self.failed.emit(f"{error}\n\n{details}")


class MainWindow(QMainWindow):
    MATCH_COLUMNS = [
        ("Datum", "match_date"),
        ("Tid", "match_time"),
        ("Serie", "series_name"),
        ("Omgång", "round_name"),
        ("Hemma", "home_team"),
        ("Borta", "away_team"),
        ("Arrangör", "organiser"),
        ("Resultat", "score"),
    ]

    SUMMARY_COLUMNS = [
        ("Datum", "match_date"),
        ("Serie", "series_name"),
        ("Omgång", "round_name"),
        ("Arrangör", "organiser"),
        ("Matcher", "match_count"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("STUPA-serier")
        self.resize(1350, 800)

        self.database = Database(DATA_DIR / "stupa_serier.sqlite")
        self.current_rows = []
        self.worker: ScrapeWorker | None = None

        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText(
            "Fullständig serieadress, exempel: .../events/435/1186/2/7/7"
        )
        self.series_edit = QLineEdit()
        self.series_edit.setPlaceholderText("Konkret serie, exempel: Division 4B")

        self.fetch_button = QPushButton("Hämta serie")
        self.fetch_button.clicked.connect(self.fetch_series)

        import_form = QFormLayout()
        import_form.addRow("Startadress:", self.url_edit)
        import_form.addRow("Konkret serie:", self.series_edit)

        import_row = QHBoxLayout()
        import_row.addLayout(import_form, 1)
        import_row.addWidget(self.fetch_button)

        self.organiser_filter = QLineEdit()
        self.organiser_filter.setPlaceholderText("Exempel: Stratos")
        self.organiser_filter.textChanged.connect(self.refresh_tables)

        self.series_filter = QLineEdit()
        self.series_filter.setPlaceholderText("Filtrera på serienamn")
        self.series_filter.textChanged.connect(self.refresh_tables)

        self.export_button = QPushButton("Exportera CSV")
        self.export_button.clicked.connect(self.export_csv)

        self.diagnostics_button = QPushButton("Öppna diagnostikmapp")
        self.diagnostics_button.clicked.connect(self.open_diagnostics)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Arrangör:"))
        filter_row.addWidget(self.organiser_filter, 1)
        filter_row.addWidget(QLabel("Serie:"))
        filter_row.addWidget(self.series_filter, 1)
        filter_row.addWidget(self.export_button)
        filter_row.addWidget(self.diagnostics_button)

        self.matches_table = QTableWidget()
        self.matches_table.setColumnCount(len(self.MATCH_COLUMNS))
        self.matches_table.setHorizontalHeaderLabels(
            [label for label, _ in self.MATCH_COLUMNS]
        )
        self.matches_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeToContents
        )
        self.matches_table.horizontalHeader().setStretchLastSection(True)
        self.matches_table.setSortingEnabled(True)
        self.matches_table.cellDoubleClicked.connect(self.open_source_url)

        self.summary_table = QTableWidget()
        self.summary_table.setColumnCount(len(self.SUMMARY_COLUMNS))
        self.summary_table.setHorizontalHeaderLabels(
            [label for label, _ in self.SUMMARY_COLUMNS]
        )
        self.summary_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeToContents
        )
        self.summary_table.horizontalHeader().setStretchLastSection(True)
        self.summary_table.setSortingEnabled(True)

        tabs = QTabWidget()
        tabs.addTab(self.summary_table, "Per seriehelg/omgång")
        tabs.addTab(self.matches_table, "Alla matcher")

        self.status_label = QLabel("Klar.")
        self.status_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(import_row)
        layout.addLayout(filter_row)
        layout.addWidget(tabs, 1)
        layout.addWidget(self.status_label)
        self.setCentralWidget(central)

        self.refresh_tables()

    def fetch_series(self) -> None:
        url = self.url_edit.text().strip()
        series_name = self.series_edit.text().strip()
        if not url.startswith(("http://", "https://")):
            QMessageBox.warning(self, "Saknad adress", "Ange en fullständig STUPA-serieadress.")
            return
        if not series_name:
            QMessageBox.warning(self, "Saknat serienamn", "Ange seriens namn.")
            return

        self.fetch_button.setEnabled(False)
        self.worker = ScrapeWorker(url, series_name)
        self.worker.status_changed.connect(self.status_label.setText)
        self.worker.completed.connect(self.scrape_completed)
        self.worker.failed.connect(self.scrape_failed)
        self.worker.start()

    def scrape_completed(self, records: list) -> None:
        changes = self.database.upsert_matches(records)
        self.status_label.setText(
            f"Hämtningen klar: {len(records)} matcher lästes, "
            f"{changes} databasändringar."
        )
        self.fetch_button.setEnabled(True)
        self.refresh_tables()

    def scrape_failed(self, message: str) -> None:
        self.fetch_button.setEnabled(True)
        self.status_label.setText("Hämtningen misslyckades.")
        QMessageBox.critical(
            self,
            "Kunde inte hämta serien",
            message,
        )

    def refresh_tables(self) -> None:
        organiser = self.organiser_filter.text()
        series_name = self.series_filter.text()

        rows = self.database.query_matches(organiser, series_name)
        self.current_rows = rows
        self._fill_table(self.matches_table, rows, self.MATCH_COLUMNS)

        summary_rows = self.database.organiser_summary(organiser)
        if series_name.strip():
            summary_rows = [
                row
                for row in summary_rows
                if series_name.casefold() in row["series_name"].casefold()
            ]
        self._fill_table(self.summary_table, summary_rows, self.SUMMARY_COLUMNS)

        self.status_label.setText(
            f"Visar {len(rows)} matcher och {len(summary_rows)} sammanfattningsrader."
        )

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
            self,
            "Exportera matcher",
            "stupa_matcher.csv",
            "CSV-filer (*.csv)",
        )
        if not filename:
            return
        self.database.export_csv(self.current_rows, Path(filename))
        self.status_label.setText(f"Exporterade {len(self.current_rows)} matcher.")

    def open_diagnostics(self) -> None:
        DIAGNOSTICS_DIR.mkdir(exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(DIAGNOSTICS_DIR.resolve())))

    def open_source_url(self, row: int, _column: int) -> None:
        item = self.matches_table.item(row, 0)
        if not item:
            return
        url = item.data(Qt.UserRole)
        if url:
            QDesktopServices.openUrl(QUrl(url))


def run() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
