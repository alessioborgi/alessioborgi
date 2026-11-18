"""Resumable public OpenReview API v2 ingestion for ICLR 2027.

Only public Official_Review notes are used. The database is swapped into place
only after every page has been retrieved successfully; partial refreshes do not
replace a previously complete snapshot.
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

VENUE_ID = "ICLR.cc/2027/Conference"
BASE_URL = "https://api2.openreview.net"
DEFAULT_PAGE_SIZE = 75
DATA_DIR = Path(__file__).parent / "data"
COMPLETE_DB = DATA_DIR / "iclr2027_complete.sqlite"
WORKING_DB = DATA_DIR / "iclr2027_incomplete.sqlite"


class CollectionError(RuntimeError):
    """Raised when a refresh cannot be completed and should not be published."""


def parse_score(raw) -> float | None:
    """Parse numeric, '3: Weak Accept', or {'value': ...} score fields."""
    while isinstance(raw, dict):
        raw = raw.get("value")
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    match = re.match(r"^\s*([+-]?(?:\d+\.?\d*|\.\d+))(?=\s|:|$)", str(raw))
    return float(match.group(1)) if match else None


def field(note: dict, name: str, default=None):
    value = note.get("content", {}).get(name, default)
    while isinstance(value, dict):
        value = value.get("value", default)
    return default if value is None else value


def build_session() -> requests.Session:
    retry = Retry(
        total=5,
        connect=5,
        read=4,
        status=5,
        backoff_factor=1.25,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "User-Agent": "ICLR2027ReviewAtlas/1.0 (academic review statistics)"})
    session.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=2, pool_maxsize=2))
    return session


def get_json(session: requests.Session, path: str, params: dict) -> dict:
    url = f"{BASE_URL}{path}"
    try:
        response = session.get(url, params=params, timeout=(15, 100))
        if response.status_code in (401, 403):
            raise CollectionError(
                f"OpenReview returned HTTP {response.status_code}. Public API access may be "
                "temporarily challenged or restricted. Try again later, or import a local "
                "OpenReview-derived CSV. The existing complete snapshot was preserved."
            )
        response.raise_for_status()
        payload = response.json()
    except CollectionError:
        raise
    except (requests.RequestException, ValueError) as exc:
        raise CollectionError(f"OpenReview request failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise CollectionError("OpenReview returned an unexpected API response shape.")
    return payload


def group_settings(group: dict) -> dict:
    content = group.get("content") or {}

    def setting(name: str, default: str) -> str:
        result = content.get(name, default)
        while isinstance(result, dict):
            result = result.get("value", default)
        return str(result or default)

    return {
        "submission_name": setting("submission_name", "Submission"),
        "review_name": setting("review_name", "Official_Review"),
        "active_venueid": setting("submission_venue_id", f"{VENUE_ID}/Submission"),
        "withdrawn_venueid": setting("withdrawn_venue_id", f"{VENUE_ID}/Withdrawn_Submission"),
        "desk_venueid": setting("desk_rejected_venue_id", f"{VENUE_ID}/Desk_Rejected_Submission"),
    }


def paper_status(note: dict, settings: dict) -> str:
    venueid = str(field(note, "venueid", "") or "")
    venue = str(field(note, "venue", "") or "").lower()
    if venueid == settings["withdrawn_venueid"] or "withdraw" in venueid.lower() or "withdraw" in venue:
        return "Withdrawn"
    if (venueid == settings["desk_venueid"] or "desk_reject" in venueid.lower()
            or "desk reject" in venue or "desk-reject" in venue):
        return "Desk rejected"
    if "reject" in venue or "reject" in venueid.lower():
        return "Rejected"
    return "Active"


def is_official_review(reply: dict, review_name: str) -> bool:
    invitations = reply.get("invitations") or ([reply["invitation"]] if reply.get("invitation") else [])
    name = review_name.strip("/")
    return any(str(inv).endswith(f"/-/{name}") for inv in invitations)


def public_review(reply: dict) -> bool:
    readers = reply.get("readers", [])
    return not readers or "everyone" in readers


def parse_paper(note: dict, settings: dict) -> tuple[tuple, list[tuple]]:
    paper_id = str(note.get("id") or "")
    if not paper_id:
        raise CollectionError("OpenReview provided a submission without an ID.")
    number = note.get("number")
    raw_title = field(note, "title", "Untitled")
    raw_area = field(note, "primary_area", "Unspecified")
    paper = (
        paper_id,
        int(number) if str(number or "").isdigit() else None,
        str(raw_title),
        str(raw_area),
        paper_status(note, settings),
        str(field(note, "venueid", "") or ""),
        f"https://openreview.net/forum?id={paper_id}",
    )
    details = note.get("details") or {}
    replies = details.get("directReplies")
    if replies is None:
        replies = details.get("replies")
    if replies is None and not (note.get("replyCount") or note.get("details")):
        # An OpenReview forum with zero replies may omit the empty details object.
        replies = []
    if not isinstance(replies, list):
        raise CollectionError("Submission has no embedded replies field. Ensure details=directReplies works.")
    reviews = []
    for reply in replies:
        if not is_official_review(reply, settings["review_name"]) or not public_review(reply):
            continue
        score = parse_score(field(reply, "rating"))
        conf = parse_score(field(reply, "confidence"))
        signatures = reply.get("signatures") or []
        reviewer = str(signatures[0].split("/")[-1] if signatures else "Anonymous")
        review_id = str(reply.get("id") or "")
        if not review_id:
            continue
        reviews.append((review_id, paper_id, score, conf, reviewer))
    return paper, reviews


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS papers (
            paper_id TEXT PRIMARY KEY,
            number INTEGER, title TEXT NOT NULL, area TEXT NOT NULL,
            status TEXT NOT NULL, venueid TEXT, url TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reviews (
            review_id TEXT PRIMARY KEY, paper_id TEXT NOT NULL,
            rating REAL, confidence REAL, reviewer TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS ix_reviews_paper ON reviews(paper_id);
        CREATE INDEX IF NOT EXISTS ix_papers_status ON papers(status);
    """)
    connection.commit()
    return connection


def meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def put_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))


def load_tables(db_path: Path = COMPLETE_DB):
    """Return (papers, reviews, info) as pandas dataframes and a metadata dict."""
    import pandas as pd

    with sqlite3.connect(db_path) as conn:
        papers = pd.read_sql_query("SELECT * FROM papers", conn)
        reviews = pd.read_sql_query("SELECT * FROM reviews", conn)
        info = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    return papers, reviews, info


def collection_progress(db_path: Path = WORKING_DB) -> tuple[int, str]:
    if not db_path.exists():
        return 0, ""
    with sqlite3.connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        return int(count), meta(conn, "after")


def collect_public_reviews(
    *,
    start_fresh: bool = False,
    page_size: int = DEFAULT_PAGE_SIZE,
    delay_seconds: float = 0.12,
    on_progress: Callable[[int, int | None, int], None] | None = None,
    session: requests.Session | None = None,
    complete_db: Path = COMPLETE_DB,
    working_db: Path = WORKING_DB,
) -> dict:
    """Download public submissions/reviews with resume support and safe atomic publication.

    Progress callback: (submission_count, server_expected_count_or_none, review_count).
    Incomplete working DB is kept on failures; a completed snapshot is never overwritten
    by a partial download.
    """
    if not 1 <= page_size <= 1000:
        raise ValueError("page_size must be between 1 and 1000")
    if start_fresh and working_db.exists():
        working_db.unlink()
    session = session or build_session()
    group_payload = get_json(session, "/groups", {"id": VENUE_ID})
    groups = group_payload.get("groups", [])
    if not groups:
        raise CollectionError(f"Conference group {VENUE_ID} was not found in public API.")
    settings = group_settings(groups[0])
    invitation = f"{VENUE_ID}/-/{settings['submission_name']}"
    conn = init_db(working_db)
    try:
        prior_invitation = meta(conn, "invitation")
        if prior_invitation and prior_invitation != invitation:
            raise CollectionError("Incomplete cache invitation has changed. Use 'Start fresh' to restart.")
        after = meta(conn, "after") or None
        count = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        expected = int(meta(conn, "expected_count")) if meta(conn, "expected_count") else None
        put_meta(conn, "invitation", invitation)
        put_meta(conn, "venue_id", VENUE_ID)
        conn.commit()
        if on_progress:
            review_count = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
            on_progress(count, expected, review_count)
        while True:
            params = {
                "invitation": invitation,
                "details": "directReplies",
                "limit": page_size,
                "stream": "true",
            }
            if after:
                params["after"] = after
            else:
                params["count"] = "true"
            payload = get_json(session, "/notes", params)
            notes = payload.get("notes")
            if not isinstance(notes, list):
                raise CollectionError("OpenReview did not return a 'notes' list.")
            if expected is None and payload.get("count") is not None:
                expected = int(payload["count"])
                put_meta(conn, "expected_count", str(expected))
            if not notes:
                break
            last_id = notes[-1].get("id")
            if not last_id or last_id == after:
                raise CollectionError("OpenReview pagination cursor did not advance; stopping to avoid duplicate pages.")
            paper_rows, review_rows = [], []
            for note in notes:
                paper, reviews = parse_paper(note, settings)
                paper_rows.append(paper)
                review_rows.extend(reviews)
            with conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO papers(paper_id, number, title, area, status, venueid, url) VALUES (?,?,?,?,?,?,?)",
                    paper_rows,
                )
                conn.executemany(
                    "INSERT OR REPLACE INTO reviews(review_id, paper_id, rating, confidence, reviewer) VALUES (?,?,?,?,?)",
                    review_rows,
                )
                put_meta(conn, "after", last_id)
                put_meta(conn, "pages_fetched", str(int(meta(conn, "pages_fetched", "0")) + 1))
            after = last_id
            count = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
            review_count = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
            if on_progress:
                on_progress(count, expected, review_count)
            if len(notes) < page_size:
                break
            if delay_seconds:
                time.sleep(delay_seconds)
        # A partial response must never be presented as a complete conference snapshot.
        if expected is not None and count < expected:
            raise CollectionError(
                f"OpenReview reported {expected:,} submissions but only {count:,} were received. "
                "Partial progress has been kept for resuming."
            )
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with conn:
            put_meta(conn, "last_updated_utc", now)
            put_meta(conn, "complete", "1")
            put_meta(conn, "total_submissions", str(count))
            put_meta(conn, "total_reviews", str(review_count))
        conn.close()
        os.replace(working_db, complete_db)
        return {"total_submissions": count, "total_reviews": review_count, "last_updated_utc": now}
    except Exception:
        conn.close()
        raise
