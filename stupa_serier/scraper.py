from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Callable

from playwright.sync_api import ElementHandle, Page, sync_playwright

from .models import MatchRecord


DATE_RE = re.compile(r"\b\d{2}[-/.]\d{2}[-/.]\d{2,4}\b")
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\b")
SCORE_RE = re.compile(r"\b\d+\s*-\s*\d+\b")
ROUND_RE = re.compile(r"^Round\s+\d+$", re.IGNORECASE)


class ScrapeError(RuntimeError):
    pass


def _clean_lines(text: str) -> list[str]:
    """
    Gör STUPA:s tabelltext till en post per rad.

    Playwrights inner_text kan lämna tabellceller tab-separerade på samma
    textrad, exempelvis "20-09-26\t10:00". Tabbar måste därför behandlas
    som cell-/radgränser innan innehållet tolkas.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("\t", "\n")

    return [
        re.sub(r"[ ]+", " ", line).strip()
        for line in normalized.split("\n")
        if re.sub(r"[ ]+", " ", line).strip()
    ]


def _candidate_rows(page: Page) -> list[dict[str, str]]:
    """
    Hittar synliga matchrader genom att utgå från varje synlig
    'View Details'-knapp och gå uppåt i DOM-trädet tills både datum,
    tid och deltagare finns i samma element.
    """
    rows = page.evaluate(
        """
        () => {
          const dateRe = /\\b\\d{2}[-/.]\\d{2}[-/.]\\d{2,4}\\b/;
          const timeRe = /\\b\\d{1,2}:\\d{2}\\b/;
          const scoreRe = /\\b\\d+\\s*-\\s*\\d+\\b/;
          const visible = el => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
              && style.visibility !== 'hidden'
              && rect.width > 0
              && rect.height > 0;
          };

          const detailElements = [...document.querySelectorAll('button, a')]
            .filter(el =>
              visible(el)
              && (el.innerText || '').trim().toLowerCase().includes('view details')
            );

          const result = [];
          const seen = new Set();

          for (const detail of detailElements) {
            let node = detail;
            let best = null;

            for (let depth = 0; depth < 12 && node && node !== document.body; depth++) {
              const text = (node.innerText || '').trim();

              if (dateRe.test(text) && timeRe.test(text) && scoreRe.test(text)) {
                best = node;

                // När elementet börjar innehålla flera matcher har vi gått för långt.
                const detailCount = [...node.querySelectorAll('button, a')]
                  .filter(el =>
                    (el.innerText || '').trim().toLowerCase().includes('view details')
                  ).length;

                if (detailCount === 1) {
                  break;
                }
              }

              node = node.parentElement;
            }

            if (!best) {
              continue;
            }

            const normalized = (best.innerText || '')
              .split('\\n')
              .map(line => line.replace(/\\s+/g, ' ').trim())
              .filter(Boolean)
              .join('\\n');

            if (!seen.has(normalized)) {
              seen.add(normalized);
              result.push({
                text: normalized,
                html: best.outerHTML
              });
            }
          }

          return result;
        }
        """
    )
    return rows


def _find_round_labels(page: Page) -> list[str]:
    labels = page.evaluate(
        """
        () => {
          const roundRe = /^Round\\s+\\d+$/i;
          const values = [];

          for (const element of document.querySelectorAll('body *')) {
            const ownText = [...element.childNodes]
              .filter(node => node.nodeType === Node.TEXT_NODE)
              .map(node => node.textContent || '')
              .join(' ')
              .replace(/\\s+/g, ' ')
              .trim();

            if (roundRe.test(ownText)) {
              values.push(ownText);
            }
          }

          return [...new Set(values)].sort((a, b) => {
            const an = Number(a.match(/\\d+/)?.[0] || 0);
            const bn = Number(b.match(/\\d+/)?.[0] || 0);
            return an - bn;
          });
        }
        """
    )
    return list(labels)


def _find_clickable_round_header(page: Page, round_name: str) -> ElementHandle | None:
    """
    Hittar endast själva dragspelsrubriken eller en säker knappförälder.

    Tidigare kod kunde klättra upp till ett <a>-element eller en stor
    klickbar behållare och därmed navigera till en annan serie.
    """
    handle = page.evaluate_handle(
        """
        roundName => {
          const normalize = value => (value || '').replace(/\\s+/g, ' ').trim();

          const candidates = [...document.querySelectorAll('body *')]
            .filter(element => {
              const ownText = [...element.childNodes]
                .filter(node => node.nodeType === Node.TEXT_NODE)
                .map(node => node.textContent || '')
                .join(' ');

              return normalize(ownText).toLowerCase() === roundName.toLowerCase();
            })
            .filter(element => {
              const style = window.getComputedStyle(element);
              const rect = element.getBoundingClientRect();
              return style.display !== 'none'
                && style.visibility !== 'hidden'
                && rect.width > 0
                && rect.height > 0;
            });

          for (const label of candidates) {
            // Själva elementet är en säker accordion-kontroll.
            const ownTag = label.tagName?.toLowerCase();
            const ownRole = label.getAttribute?.('role');
            if (ownTag === 'button' || ownRole === 'button'
                || label.hasAttribute?.('aria-expanded')) {
              return label;
            }

            // Gå bara ett fåtal steg uppåt och acceptera aldrig länkar.
            let node = label.parentElement;
            for (let depth = 0; depth < 4 && node && node !== document.body; depth++) {
              const tag = node.tagName?.toLowerCase();
              const role = node.getAttribute?.('role');

              if (tag === 'a' || node.closest?.('a')) {
                break;
              }

              if (
                tag === 'button'
                || role === 'button'
                || node.hasAttribute?.('aria-expanded')
              ) {
                return node;
              }

              node = node.parentElement;
            }

            // Som sista utväg returneras själva textetiketten, aldrig en länk.
            if (!label.closest?.('a')) {
              return label;
            }
          }

          return null;
        }
        """,
        round_name,
    )
    return handle.as_element()

def _open_round(
    page: Page,
    round_name: str,
    expected_url: str,
) -> list[dict[str, str]]:
    """
    Öppnar en omgång utan att tillåta navigering till en annan serie.
    """
    header = _find_clickable_round_header(page, round_name)
    if header is None:
        return []

    before_url = page.url

    try:
        header.scroll_into_view_if_needed(timeout=2_000)
    except Exception:
        pass

    try:
        header.click(timeout=3_000)
    except Exception:
        try:
            page.evaluate(
                """
                element => {
                  element.dispatchEvent(new MouseEvent('click', {
                    bubbles: true,
                    cancelable: true,
                    view: window
                  }));
                }
                """,
                header,
            )
        except Exception:
            return []

    page.wait_for_timeout(400)

    # Ett accordion-klick får aldrig byta route/serie.
    if page.url != before_url:
        raise ScrapeError(
            f"Klick på {round_name} bytte oväntat adress från "
            f"{before_url} till {page.url}. Hämtningen avbröts för att "
            "inte importera fel serie."
        )

    rows = _candidate_rows(page)

    if not rows:
        # Klicka endast igen om samma säkra kontroll fortfarande finns.
        try:
            header = _find_clickable_round_header(page, round_name)
            if header is not None:
                header.click(timeout=3_000)
                page.wait_for_timeout(400)
        except Exception:
            pass

        if page.url != before_url:
            raise ScrapeError(
                f"Ett andra klick på {round_name} bytte oväntat serieadress."
            )

        rows = _candidate_rows(page)

    return rows

def _parse_row(
    raw_text: str,
    *,
    source_url: str,
    series_name: str,
    round_name: str,
) -> MatchRecord | None:
    lines = _clean_lines(raw_text)
    lines = [
        line
        for line in lines
        if line.lower() not in {
            "view details",
            "date",
            "time",
            "participants",
            "organiser",
            "match score",
        }
    ]

    date_index = next(
        (index for index, value in enumerate(lines) if DATE_RE.fullmatch(value)),
        None,
    )
    time_index = next(
        (index for index, value in enumerate(lines) if TIME_RE.fullmatch(value)),
        None,
    )

    if date_index is None or time_index is None:
        return None

    match_date = lines[date_index]
    match_time = lines[time_index]

    score_index = next(
        (
            index
            for index in range(len(lines) - 1, -1, -1)
            if SCORE_RE.fullmatch(lines[index])
        ),
        None,
    )
    score = lines[score_index] if score_index is not None else ""

    end_index = score_index if score_index is not None else len(lines)
    middle = lines[time_index + 1 : end_index]

    vs_index = next(
        (
            index
            for index, value in enumerate(middle)
            if value.strip().lower() == "vs"
        ),
        None,
    )

    if vs_index is None or vs_index < 1 or vs_index + 1 >= len(middle):
        return None

    home_team = middle[vs_index - 1]
    away_team = middle[vs_index + 1]
    organiser_candidates = middle[vs_index + 2 :]
    organiser = organiser_candidates[-1] if organiser_candidates else ""

    if not home_team or not away_team or not organiser:
        return None

    return MatchRecord(
        source_url=source_url,
        series_name=series_name.strip() or page_title_fallback(source_url),
        round_name=round_name,
        match_date=match_date,
        match_time=match_time,
        home_team=home_team,
        away_team=away_team,
        organiser=organiser,
        score=score,
    )



def _parse_body_text(
    body_text: str,
    *,
    source_url: str,
    series_name: str,
) -> tuple[list[MatchRecord], list[dict[str, object]]]:
    """
    Tolkar den synliga sidtexten direkt.

    STUPA:s matchkort verkar inte använda button/a-element för 'View Details',
    vilket gör DOM-selektorn opålitlig. Den synliga texten har däremot en stabil
    ordning: datum, tid, hemmalag, vs, bortalag, arrangör, resultat, View Details.
    """
    lines = _clean_lines(body_text)
    round_positions: list[tuple[int, str]] = [
        (index, line)
        for index, line in enumerate(lines)
        if ROUND_RE.fullmatch(line)
    ]

    records: list[MatchRecord] = []
    diagnostics: list[dict[str, object]] = []

    for position_index, (start, round_name) in enumerate(round_positions):
        end = (
            round_positions[position_index + 1][0]
            if position_index + 1 < len(round_positions)
            else len(lines)
        )
        section = lines[start + 1 : end]

        parsed_in_round: list[MatchRecord] = []
        raw_matches: list[dict[str, str]] = []
        i = 0

        while i < len(section):
            if not DATE_RE.fullmatch(section[i]):
                i += 1
                continue

            # Förväntad struktur från och med datumraden.
            if i + 6 >= len(section):
                break

            match_date = section[i]
            match_time = section[i + 1] if TIME_RE.fullmatch(section[i + 1]) else ""

            if not match_time:
                i += 1
                continue

            home_team = section[i + 2]
            vs_value = section[i + 3].lower()
            away_team = section[i + 4]
            organiser = section[i + 5]
            score = section[i + 6] if SCORE_RE.fullmatch(section[i + 6]) else ""

            if vs_value != "vs" or not score:
                i += 1
                continue

            record = MatchRecord(
                source_url=source_url,
                series_name=series_name.strip() or page_title_fallback(source_url),
                round_name=round_name,
                match_date=match_date,
                match_time=match_time,
                home_team=home_team,
                away_team=away_team,
                organiser=organiser,
                score=score,
            )
            parsed_in_round.append(record)
            records.append(record)

            raw_matches.append(
                {
                    "date": match_date,
                    "time": match_time,
                    "home_team": home_team,
                    "away_team": away_team,
                    "organiser": organiser,
                    "score": score,
                }
            )

            # Nästa rad är normalt "View Details".
            i += 8 if i + 7 < len(section) and section[i + 7].lower() == "view details" else 7

        diagnostics.append(
            {
                "round_name": round_name,
                "row_count": len(parsed_in_round),
                "rows": raw_matches,
            }
        )

    return records, diagnostics

def page_title_fallback(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1] or "Okänd serie"



def _start_url(url: str) -> str:
    """
    STUPA kräver en fullständig serieadress som ingång. En ren adress som
    /events/435 visar endast en tom evenemangssida.

    Vi behåller därför hela den adress användaren klistrar in. STUPA kan
    omdirigera den till den första serien, varefter rätt serie väljs i
    sidans eget gränssnitt.
    """
    value = url.strip()
    if not re.match(r"^https?://[^/]+/events/\d+/.+", value):
        raise ScrapeError(
            "Ange en fullständig STUPA-serieadress, exempelvis "
            "https://sbtfeventsott.stupaevents.com/events/435/1186/2/7/7"
        )
    return value

def _visible_series_candidates(page: Page, series_name: str) -> list[dict[str, object]]:
    """
    Söker efter synliga små element vars hela synliga text motsvarar serienamnet.

    STUPA:s dropdownalternativ kan ha texten i ett barn-element, så vi får inte
    begränsa oss till elementets egna textnoder.
    """
    return page.evaluate(
        """
        seriesName => {
          const normalize = value => (value || '')
            .replace(/\\s+/g, ' ')
            .trim()
            .toLowerCase();

          const target = normalize(seriesName);
          const visible = element => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none'
              && style.visibility !== 'hidden'
              && rect.width > 0
              && rect.height > 0;
          };

          const result = [];

          for (const element of document.querySelectorAll('body *')) {
            if (!visible(element)) continue;

            const rect = element.getBoundingClientRect();
            if (rect.width > 900 || rect.height > 180) continue;

            const fullText = normalize(element.innerText || element.textContent || '');
            if (fullText !== target) continue;

            result.push({
              tag: element.tagName?.toLowerCase() || '',
              role: element.getAttribute?.('role') || '',
              className: String(element.className || ''),
              text: (element.innerText || element.textContent || '').trim(),
              hasHref: Boolean(element.closest?.('a')),
              ariaSelected: element.getAttribute?.('aria-selected'),
              ariaCurrent: element.getAttribute?.('aria-current'),
              width: Math.round(rect.width),
              height: Math.round(rect.height),
            });
          }

          return result;
        }
        """,
        series_name,
    )

def _find_series_click_target(page: Page, series_name: str) -> ElementHandle | None:
    """
    Hittar ett synligt seriealternativ efter att dropdownen öppnats.

    Playwrights textlokalisering används först. Därefter används en DOM-sökning
    som accepterar text i underliggande element.
    """
    # Playwright kan hitta text även när den ligger i ett barn-element.
    try:
        locator = page.get_by_text(series_name, exact=True)
        count = locator.count()
        for index in range(count):
            item = locator.nth(index)
            if not item.is_visible():
                continue

            box = item.bounding_box()
            if not box:
                continue
            if box["width"] > 900 or box["height"] > 180:
                continue

            handle = item.element_handle()
            if handle is not None:
                return handle
    except Exception:
        pass

    handle = page.evaluate_handle(
        """
        seriesName => {
          const normalize = value => (value || '')
            .replace(/\\s+/g, ' ')
            .trim()
            .toLowerCase();

          const target = normalize(seriesName);
          const visible = element => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none'
              && style.visibility !== 'hidden'
              && rect.width > 0
              && rect.height > 0;
          };

          const labels = [...document.querySelectorAll('body *')]
            .filter(visible)
            .filter(element => {
              const rect = element.getBoundingClientRect();
              if (rect.width > 900 || rect.height > 180) return false;
              return normalize(element.innerText || element.textContent || '') === target;
            })
            .sort((a, b) => {
              const ar = a.getBoundingClientRect();
              const br = b.getBoundingClientRect();
              return (ar.width * ar.height) - (br.width * br.height);
            });

          for (const label of labels) {
            let node = label;

            for (let depth = 0; depth < 5 && node && node !== document.body; depth++) {
              const tag = node.tagName?.toLowerCase();
              const role = node.getAttribute?.('role');
              const style = window.getComputedStyle(node);
              const rect = node.getBoundingClientRect();

              const clickable =
                tag === 'a'
                || tag === 'button'
                || role === 'button'
                || role === 'option'
                || role === 'menuitem'
                || node.hasAttribute?.('onclick')
                || style.cursor === 'pointer';

              if (clickable && rect.width < 900 && rect.height < 180) {
                return node;
              }

              node = node.parentElement;
            }

            return label;
          }

          return null;
        }
        """,
        series_name,
    )
    return handle.as_element()


def _split_series_name(series_name: str) -> tuple[str, str]:
    """
    Division 4B -> ("Division 4", "Division 4B")
    Division 1 Norra -> ("Division 1", "Division 1 Norra")
    """
    normalized = re.sub(r"\s+", " ", series_name).strip()
    match = re.match(r"^(Division\s+\d+)", normalized, re.IGNORECASE)
    if not match:
        return normalized, normalized
    return match.group(1), normalized


def _visible_comboboxes(page: Page) -> list[dict[str, object]]:
    return page.evaluate(
        """
        () => {
          const visible = el => {
            const style = getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
              && style.visibility !== 'hidden'
              && rect.width > 0
              && rect.height > 0;
          };

          return [...document.querySelectorAll('[role="combobox"]')]
            .filter(visible)
            .map((element, index) => {
              const rect = element.getBoundingClientRect();
              return {
                index,
                text: (element.innerText || element.textContent || '')
                  .replace(/\\s+/g, ' ')
                  .trim(),
                x: Math.round(rect.x),
                y: Math.round(rect.y),
                width: Math.round(rect.width),
                height: Math.round(rect.height),
              };
            })
            .sort((a, b) => a.x - b.x || a.y - b.y);
        }
        """
    )


def _click_combobox_by_text(page: Page, current_text: str) -> bool:
    """
    Klickar på den synliga combobox vars text exakt motsvarar current_text.
    """
    boxes = page.locator("[role='combobox']")
    for index in range(boxes.count()):
        box = boxes.nth(index)
        try:
            if not box.is_visible():
                continue
            text = re.sub(r"\s+", " ", box.inner_text(timeout=300)).strip()
            if text.casefold() != current_text.casefold():
                continue
            box.click(timeout=3_000)
            page.wait_for_timeout(400)
            return True
        except Exception:
            continue
    return False


def _visible_option_texts(page: Page) -> list[str]:
    """
    Hämtar endast synliga dropdownalternativ. Toppmenyn filtreras bort genom
    att bara acceptera option/menuitem/listbox-innehåll samt små popup-rader.
    """
    return page.evaluate(
        """
        () => {
          const visible = el => {
            const style = getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
              && style.visibility !== 'hidden'
              && rect.width > 0
              && rect.height > 0;
          };

          const candidates = [
            ...document.querySelectorAll(
              '[role="option"], [role="menuitem"], [role="listbox"] li'
            )
          ];

          const result = [];
          for (const element of candidates) {
            if (!visible(element)) continue;
            const rect = element.getBoundingClientRect();
            if (rect.width > 600 || rect.height > 100) continue;

            const text = (element.innerText || element.textContent || '')
              .replace(/\\s+/g, ' ')
              .trim();

            if (text && !result.includes(text)) {
              result.push(text);
            }
          }
          return result;
        }
        """
    )


def _click_visible_option(page: Page, option_text: str) -> bool:
    """
    Klickar på ett synligt alternativ i en öppen dropdown.
    """
    selectors = [
        "[role='option']",
        "[role='menuitem']",
        "[role='listbox'] li",
    ]

    for selector in selectors:
        locator = page.locator(selector)
        for index in range(locator.count()):
            item = locator.nth(index)
            try:
                if not item.is_visible():
                    continue
                text = re.sub(r"\s+", " ", item.inner_text(timeout=300)).strip()
                if text.casefold() != option_text.casefold():
                    continue
                item.click(timeout=3_000)
                page.wait_for_timeout(700)
                return True
            except Exception:
                continue

    # Reservstrategi för komponenter utan ARIA-roller.
    handle = page.evaluate_handle(
        """
        optionText => {
          const normalize = value => (value || '')
            .replace(/\\s+/g, ' ')
            .trim()
            .toLowerCase();

          const visible = el => {
            const style = getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
              && style.visibility !== 'hidden'
              && rect.width > 0
              && rect.height > 0;
          };

          const target = normalize(optionText);
          const elements = [...document.querySelectorAll('body *')]
            .filter(visible)
            .filter(el => {
              const rect = el.getBoundingClientRect();
              if (rect.width > 600 || rect.height > 100) return false;
              return normalize(el.innerText || el.textContent || '') === target;
            })
            .sort((a, b) => {
              const ar = a.getBoundingClientRect();
              const br = b.getBoundingClientRect();
              return (ar.width * ar.height) - (br.width * br.height);
            });

          for (const element of elements) {
            let node = element;
            for (let depth = 0; depth < 4 && node && node !== document.body; depth++) {
              const role = node.getAttribute?.('role');
              const tag = node.tagName?.toLowerCase();
              const style = getComputedStyle(node);
              if (
                role === 'option'
                || role === 'menuitem'
                || tag === 'li'
                || tag === 'button'
                || style.cursor === 'pointer'
              ) {
                return node;
              }
              node = node.parentElement;
            }
          }

          return elements[0] || null;
        }
        """,
        option_text,
    )
    element = handle.as_element()
    if element is None:
        return False

    try:
        element.click(timeout=3_000)
    except Exception:
        try:
            page.evaluate("(element) => element.click()", element)
        except Exception:
            return False

    page.wait_for_timeout(700)
    return True

def _click_exact_series(page: Page, series_name: str) -> bool:
    target = _find_series_click_target(page, series_name)
    if target is None:
        return False

    try:
        target.scroll_into_view_if_needed(timeout=2_000)
    except Exception:
        pass

    try:
        target.click(timeout=4_000)
    except Exception:
        try:
            page.evaluate(
                """
                element => {
                  const clickable = element.closest(
                    '[role="option"], [role="menuitem"], button, a, li'
                  ) || element;

                  clickable.dispatchEvent(new MouseEvent('click', {
                    bubbles: true,
                    cancelable: true,
                    view: window
                  }));
                }
                """,
                target,
            )
        except Exception:
            return False

    try:
        page.wait_for_load_state("networkidle", timeout=12_000)
    except Exception:
        pass
    page.wait_for_timeout(800)
    return True


def _open_possible_series_selector(page: Page) -> list[str]:
    actions: list[str] = []

    selects = page.locator("select")
    for index in range(selects.count()):
        try:
            options = selects.nth(index).locator("option").all_inner_texts()
            actions.append(f"select[{index}] options={options}")
        except Exception:
            pass

    selectors = [
        "[role='combobox']",
        "[aria-haspopup='listbox']",
        "[aria-haspopup='menu']",
        "button",
    ]

    for selector in selectors:
        locator = page.locator(selector)
        for index in range(min(locator.count(), 100)):
            item = locator.nth(index)
            try:
                if not item.is_visible():
                    continue
                box = item.bounding_box()
                if not box:
                    continue
                if box["width"] > 700 or box["height"] > 150:
                    continue

                text = re.sub(r"\s+", " ", item.inner_text(timeout=200)).strip()
                aria = item.get_attribute("aria-label") or ""
                title = item.get_attribute("title") or ""
                combined = f"{text} {aria} {title}".casefold()

                relevant = any(
                    token in combined
                    for token in ("division", "league", "serie", "group")
                )
                if selector != "button" or relevant:
                    item.click(timeout=1_500)
                    page.wait_for_timeout(500)

                    option_texts = []
                    for option_selector in (
                        "[role='option']",
                        "[role='menuitem']",
                        "li",
                    ):
                        try:
                            option_locator = page.locator(option_selector)
                            for option_index in range(min(option_locator.count(), 100)):
                                option = option_locator.nth(option_index)
                                if option.is_visible():
                                    option_text = re.sub(
                                        r"\s+",
                                        " ",
                                        option.inner_text(timeout=200),
                                    ).strip()
                                    if option_text and option_text not in option_texts:
                                        option_texts.append(option_text)
                        except Exception:
                            pass

                    actions.append(
                        f"clicked {selector}[{index}] text={text!r} "
                        f"aria={aria!r} title={title!r} "
                        f"visible_options={option_texts!r}"
                    )
                    return actions
            except Exception:
                continue

    clicked = page.evaluate(
        """
        () => {
          const visible = el => {
            const s = getComputedStyle(el);
            const r = el.getBoundingClientRect();
            return s.display !== 'none' && s.visibility !== 'hidden'
              && r.width > 0 && r.height > 0;
          };

          const candidates = [...document.querySelectorAll('body *')]
            .filter(visible)
            .filter(el => {
              const text = (el.innerText || '').replace(/\\s+/g, ' ').trim();
              return /^Division\\s+\\d+[A-Za-z]?$/i.test(text);
            })
            .sort((a, b) => {
              const ar = a.getBoundingClientRect();
              const br = b.getBoundingClientRect();
              return (ar.width * ar.height) - (br.width * br.height);
            });

          for (const label of candidates) {
            let node = label;
            for (let depth = 0; depth < 5 && node && node !== document.body; depth++) {
              const r = node.getBoundingClientRect();
              const s = getComputedStyle(node);
              const tag = node.tagName.toLowerCase();
              const role = node.getAttribute('role');
              if (
                r.width < 700 && r.height < 150 &&
                (tag === 'button' || role === 'button' ||
                 role === 'combobox' || s.cursor === 'pointer')
              ) {
                node.click();
                return (node.innerText || '').trim();
              }
              node = node.parentElement;
            }
          }
          return null;
        }
        """
    )
    if clicked:
        actions.append(f"clicked current series control text={clicked!r}")
        page.wait_for_timeout(500)

    return actions



def _select_combobox_value(
    page: Page,
    current_text: str,
    target_text: str,
) -> tuple[bool, list[str]]:
    """
    Väljer ett värde i en STUPA-combobox.

    Först försöker vi klicka på ett synligt alternativ. Om komponenten inte
    exponerar alternativen med användbara HTML-/ARIA-roller används tangentbord:
    Home följt av ArrowDown tills comboboxens text motsvarar målet.
    """
    observed: list[str] = []

    if current_text.casefold() == target_text.casefold():
        return True, [current_text]

    boxes = page.locator("[role='combobox']")
    box = None

    for index in range(boxes.count()):
        candidate = boxes.nth(index)
        try:
            if not candidate.is_visible():
                continue
            value = re.sub(r"\s+", " ", candidate.inner_text(timeout=300)).strip()
            if value.casefold() == current_text.casefold():
                box = candidate
                break
        except Exception:
            continue

    if box is None:
        return False, observed

    # Försök först med ett vanligt dropdownval.
    try:
        box.click(timeout=3_000)
        page.wait_for_timeout(400)
        observed = _visible_option_texts(page)

        if _click_visible_option(page, target_text):
            return True, observed
    except Exception:
        pass

    # Stäng eventuell öppen meny före tangentbordsförsöket.
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass

    # Tangentbordsnavigering fungerar ofta även när popupens DOM är svårtolkad.
    try:
        box.click(timeout=3_000)
        page.wait_for_timeout(200)
        page.keyboard.press("Home")
        page.wait_for_timeout(100)

        for _ in range(60):
            try:
                current = re.sub(
                    r"\s+",
                    " ",
                    box.inner_text(timeout=300),
                ).strip()
            except Exception:
                current = ""

            if current and current not in observed:
                observed.append(current)

            if current.casefold() == target_text.casefold():
                page.keyboard.press("Enter")
                page.wait_for_timeout(700)
                return True, observed

            page.keyboard.press("ArrowDown")
            page.wait_for_timeout(100)

        page.keyboard.press("Escape")
    except Exception:
        pass

    return False, observed


def _get_visible_combobox_locators(page: Page):
    """
    Returnerar synliga comboboxar i vänster-till-höger-ordning.
    """
    boxes = []
    locator = page.locator("[role='combobox']")

    for index in range(locator.count()):
        item = locator.nth(index)
        try:
            if not item.is_visible():
                continue
            box = item.bounding_box()
            if not box:
                continue
            text = re.sub(r"\s+", " ", item.inner_text(timeout=300)).strip()
            boxes.append(
                {
                    "locator": item,
                    "text": text,
                    "x": box["x"],
                    "y": box["y"],
                    "width": box["width"],
                    "height": box["height"],
                }
            )
        except Exception:
            continue

    boxes.sort(key=lambda item: (item["y"], item["x"]))
    return boxes


def _popup_options_for_box(page: Page, box_info: dict) -> list[dict[str, object]]:
    """
    Läser alternativen i popupen som hör till en viss combobox.

    STUPA:s alternativ saknar ibland användbara ARIA-roller. Därför filtreras
    synliga små element geometriskt: de ska ligga under comboboxen och ungefär
    inom samma horisontella område.
    """
    box = {
        "x": box_info["x"],
        "y": box_info["y"],
        "width": box_info["width"],
        "height": box_info["height"],
    }

    return page.evaluate(
        """
        box => {
          const visible = el => {
            const style = getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
              && style.visibility !== 'hidden'
              && rect.width > 0
              && rect.height > 0;
          };

          const normalize = value => (value || '')
            .replace(/\\s+/g, ' ')
            .trim();

          const minX = box.x - 20;
          const maxX = box.x + Math.max(box.width, 220) + 20;
          const minY = box.y + box.height - 4;
          const maxY = minY + 600;

          const candidates = [];

          for (const el of document.querySelectorAll('body *')) {
            if (!visible(el)) continue;

            const rect = el.getBoundingClientRect();
            if (rect.width > 500 || rect.height > 90) continue;
            if (rect.x < minX || rect.x > maxX) continue;
            if (rect.y < minY || rect.y > maxY) continue;

            const text = normalize(el.innerText || el.textContent || '');
            if (!text) continue;

            const role = el.getAttribute?.('role') || '';
            const tag = el.tagName?.toLowerCase() || '';
            const style = getComputedStyle(el);

            const clickable =
              role === 'option'
              || role === 'menuitem'
              || tag === 'li'
              || tag === 'button'
              || style.cursor === 'pointer';

            if (!clickable) continue;

            candidates.push({
              text,
              role,
              tag,
              x: Math.round(rect.x),
              y: Math.round(rect.y),
              width: Math.round(rect.width),
              height: Math.round(rect.height),
            });
          }

          candidates.sort((a, b) => a.y - b.y || a.x - b.x);

          const result = [];
          const seen = new Set();

          for (const item of candidates) {
            const key = item.text.toLowerCase();
            if (seen.has(key)) continue;
            seen.add(key);
            result.push(item);
          }

          return result;
        }
        """,
        box,
    )


def _click_popup_option_for_box(
    page: Page,
    box_info: dict,
    option_text: str,
) -> tuple[bool, list[dict[str, object]]]:
    """
    Öppnar en viss combobox, läser dess egna alternativ och väljer exakt text.
    """
    box_locator = box_info["locator"]

    try:
        box_locator.click(timeout=3_000)
    except Exception:
        return False, []

    page.wait_for_timeout(450)
    options = _popup_options_for_box(page, box_info)

    # Första försöket: vanliga ARIA-/listelement.
    for selector in (
        "[role='option']",
        "[role='menuitem']",
        "li",
    ):
        locator = page.locator(selector)
        for index in range(locator.count()):
            item = locator.nth(index)
            try:
                if not item.is_visible():
                    continue

                bounds = item.bounding_box()
                if not bounds:
                    continue

                # Begränsa till popupområdet under just denna combobox.
                if bounds["x"] < box_info["x"] - 20:
                    continue
                if bounds["x"] > box_info["x"] + max(box_info["width"], 220) + 20:
                    continue
                if bounds["y"] < box_info["y"] + box_info["height"] - 4:
                    continue
                if bounds["y"] > box_info["y"] + box_info["height"] + 600:
                    continue

                value = re.sub(r"\s+", " ", item.inner_text(timeout=300)).strip()
                if value.casefold() != option_text.casefold():
                    continue

                item.click(timeout=3_000)
                page.wait_for_timeout(700)
                return True, options
            except Exception:
                continue

    # Reservstrategi: geometrisk DOM-sökning.
    clicked = page.evaluate(
        """
        ({box, optionText}) => {
          const normalize = value => (value || '')
            .replace(/\\s+/g, ' ')
            .trim()
            .toLowerCase();

          const visible = el => {
            const style = getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
              && style.visibility !== 'hidden'
              && rect.width > 0
              && rect.height > 0;
          };

          const minX = box.x - 20;
          const maxX = box.x + Math.max(box.width, 220) + 20;
          const minY = box.y + box.height - 4;
          const maxY = minY + 600;
          const target = normalize(optionText);

          const elements = [...document.querySelectorAll('body *')]
            .filter(visible)
            .filter(el => {
              const rect = el.getBoundingClientRect();
              if (rect.width > 500 || rect.height > 90) return false;
              if (rect.x < minX || rect.x > maxX) return false;
              if (rect.y < minY || rect.y > maxY) return false;
              return normalize(el.innerText || el.textContent || '') === target;
            })
            .sort((a, b) => {
              const ar = a.getBoundingClientRect();
              const br = b.getBoundingClientRect();
              return (ar.width * ar.height) - (br.width * br.height);
            });

          for (const element of elements) {
            let node = element;
            for (let depth = 0; depth < 4 && node && node !== document.body; depth++) {
              const role = node.getAttribute?.('role');
              const tag = node.tagName?.toLowerCase();
              const style = getComputedStyle(node);

              if (
                role === 'option'
                || role === 'menuitem'
                || tag === 'li'
                || tag === 'button'
                || style.cursor === 'pointer'
              ) {
                node.click();
                return true;
              }

              node = node.parentElement;
            }

            element.click();
            return true;
          }

          return false;
        }
        """,
        {
            "box": {
                "x": box_info["x"],
                "y": box_info["y"],
                "width": box_info["width"],
                "height": box_info["height"],
            },
            "optionText": option_text,
        },
    )

    if clicked:
        page.wait_for_timeout(700)
        return True, options

    try:
        page.keyboard.press("Escape")
    except Exception:
        pass

    return False, options

def _select_series(page: Page, series_name: str, diagnostics_dir: Path, stamp: str) -> None:
    """
    Väljer serie i två bestämda steg:

    1. Första divisionsrutan: Division 4, Division 5, ...
    2. Rutan direkt till höger: Division 4A, Division 4B, ...
    """
    page.wait_for_timeout(1_000)
    before_url = page.url

    division_level, target_series = _split_series_name(series_name)

    diagnostics: dict[str, object] = {
        "requested_series": target_series,
        "division_level": division_level,
        "before_url": before_url,
        "steps": [],
    }

    if target_series.casefold() == division_level.casefold():
        diagnostics["error"] = "series_name_is_only_level"
        diagnostics["message"] = (
            f"Ange en konkret serie, inte bara nivån '{division_level}'. "
            f"Exempel: '{division_level}A' eller '{division_level}B'."
        )
        (diagnostics_dir / f"{stamp}_series_navigation.json").write_text(
            json.dumps(diagnostics, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        raise ScrapeError(diagnostics["message"])

    boxes = _get_visible_combobox_locators(page)
    diagnostics["initial_comboboxes"] = [
        {
            "text": box["text"],
            "x": round(box["x"]),
            "y": round(box["y"]),
            "width": round(box["width"]),
            "height": round(box["height"]),
        }
        for box in boxes
    ]

    # Hitta nivårutan. Den har exakt format "Division N".
    level_index = next(
        (
            index
            for index, box in enumerate(boxes)
            if re.fullmatch(
                r"Division\s+\d+",
                box["text"],
                re.IGNORECASE,
            )
        ),
        None,
    )

    if level_index is None:
        raise ScrapeError(
            "Kunde inte hitta rutan för divisionsnivå."
        )

    level_box = boxes[level_index]

    # Byt nivå först, exempelvis Division 4 -> Division 5.
    if level_box["text"].casefold() != division_level.casefold():
        ok, options = _click_popup_option_for_box(
            page,
            level_box,
            division_level,
        )
        diagnostics["steps"].append(
            {
                "action": "select_level",
                "current": level_box["text"],
                "target": division_level,
                "options": options,
                "success": ok,
            }
        )

        if not ok:
            option_names = [str(item.get("text", "")) for item in options]
            raise ScrapeError(
                f"Kunde inte välja nivån '{division_level}'. "
                f"Alternativ i nivårutan: {option_names}"
            )

        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        page.wait_for_timeout(900)

    # Läs om boxarna efter nivåbytet. Rutan direkt till höger är serierutan.
    boxes = _get_visible_combobox_locators(page)
    diagnostics["comboboxes_after_level"] = [
        {
            "text": box["text"],
            "x": round(box["x"]),
            "y": round(box["y"]),
            "width": round(box["width"]),
            "height": round(box["height"]),
        }
        for box in boxes
    ]

    level_index = next(
        (
            index
            for index, box in enumerate(boxes)
            if box["text"].casefold() == division_level.casefold()
        ),
        None,
    )

    if level_index is None or level_index + 1 >= len(boxes):
        raise ScrapeError(
            "Kunde inte hitta serierutan direkt till höger om divisionsnivån."
        )

    series_box = boxes[level_index + 1]

    if series_box["text"].casefold() == "group stage":
        raise ScrapeError(
            "Rutan direkt till höger om divisionsnivån var 'Group Stage', "
            "vilket tyder på att serierutan inte kunde identifieras."
        )

    # Välj konkret serie, exempelvis Division 5B.
    if series_box["text"].casefold() != target_series.casefold():
        ok, options = _click_popup_option_for_box(
            page,
            series_box,
            target_series,
        )
        diagnostics["steps"].append(
            {
                "action": "select_series",
                "current": series_box["text"],
                "target": target_series,
                "options": options,
                "success": ok,
            }
        )

        if not ok:
            option_names = [str(item.get("text", "")) for item in options]
            raise ScrapeError(
                f"Kunde inte välja serien '{target_series}'. "
                f"Alternativ i den andra rutan: {option_names}"
            )

        try:
            page.wait_for_load_state("networkidle", timeout=12_000)
        except Exception:
            pass
        page.wait_for_timeout(1_000)

    # Verifiera slutligt val utifrån comboboxarnas aktuella texter.
    final_boxes = _get_visible_combobox_locators(page)
    diagnostics["final_comboboxes"] = [
        {
            "text": box["text"],
            "x": round(box["x"]),
            "y": round(box["y"]),
            "width": round(box["width"]),
            "height": round(box["height"]),
        }
        for box in final_boxes
    ]
    diagnostics["after_url"] = page.url

    final_level_index = next(
        (
            index
            for index, box in enumerate(final_boxes)
            if box["text"].casefold() == division_level.casefold()
        ),
        None,
    )

    selected_series = None
    if final_level_index is not None and final_level_index + 1 < len(final_boxes):
        selected_series = final_boxes[final_level_index + 1]["text"]

    if selected_series is None or selected_series.casefold() != target_series.casefold():
        raise ScrapeError(
            f"Seriebytet kunde inte verifieras. Förväntade '{target_series}', "
            f"men serierutan visar '{selected_series}'."
        )

    diagnostics["selection_method"] = "level_box_then_adjacent_series_box"

    (diagnostics_dir / f"{stamp}_series_navigation.json").write_text(
        json.dumps(diagnostics, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

def scrape_series(
    url: str,
    series_name: str,
    diagnostics_dir: Path,
    status: Callable[[str], None] | None = None,
) -> list[MatchRecord]:
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    def report(message: str) -> None:
        if status:
            status(message)

    with sync_playwright() as playwright:
        report("Startar Chromium…")
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})

        try:
            start_url = _start_url(url)

            report("Öppnar STUPA-seriens startadress…")
            page.goto(start_url, wait_until="domcontentloaded", timeout=60_000)

            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                pass

            report(f"Väljer serien {series_name}…")
            _select_series(
                page,
                series_name,
                diagnostics_dir,
                stamp,
            )

            round_names = _find_round_labels(page)
            if not round_names:
                raise ScrapeError(
                    "Inga omgångsrubriker av typen 'Round 1' kunde hittas."
                )

            # Öppna varje omgång. STUPA behåller i den här vyn de redan öppnade
            # omgångarna, vilket gör att all matchtext därefter finns i body.innerText.
            for index, round_name in enumerate(round_names, start=1):
                report(
                    f"Öppnar {round_name} "
                    f"({index} av {len(round_names)})…"
                )
                _open_round(page, round_name, url)

            page.wait_for_timeout(500)
            body_text = page.locator("body").inner_text()

            report("Tolkar matcherna från sidtexten…")
            parsed, all_raw_rows = _parse_body_text(
                body_text,
                source_url=page.url,
                series_name=series_name,
            )

            report("Sparar diagnostik…")
            page.screenshot(
                path=str(diagnostics_dir / f"{stamp}_page.png"),
                full_page=True,
            )
            (diagnostics_dir / f"{stamp}_page.html").write_text(
                page.content(),
                encoding="utf-8",
            )
            (diagnostics_dir / f"{stamp}_text.txt").write_text(
                body_text,
                encoding="utf-8",
            )
            (diagnostics_dir / f"{stamp}_raw_rows.json").write_text(
                json.dumps(all_raw_rows, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            unique: dict[tuple[str, ...], MatchRecord] = {}
            for item in parsed:
                key = (
                    item.series_name,
                    item.round_name,
                    item.match_date,
                    item.match_time,
                    item.home_team,
                    item.away_team,
                    item.organiser,
                )
                unique[key] = item

            records = list(unique.values())

            (diagnostics_dir / f"{stamp}_parsed.json").write_text(
                json.dumps(
                    [asdict(item) for item in records],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            if not records:
                round_counts = ", ".join(
                    f"{item['round_name']}: {item['row_count']}"
                    for item in all_raw_rows
                )
                raise ScrapeError(
                    "Omgångarna kunde öppnas, men inga matchrader kunde tolkas. "
                    f"Råa rader per omgång: {round_counts}. "
                    "Diagnostik har sparats i "
                    f"{diagnostics_dir.resolve()}."
                )

            report(
                f"Hittade {len(records)} matcher i "
                f"{len(round_names)} omgångar."
            )
            return records

        finally:
            browser.close()
