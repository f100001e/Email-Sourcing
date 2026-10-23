#!/usr/bin/env python3
"""Export unique email addresses from Gmail message headers to SQLite and CSV.

Spam and Trash are excluded. The SQLite database resumes interrupted exports.
Only headers are requested; message bodies are never read.

Setup:
  pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib
  Save OAuth Desktop client credentials as credentials.json beside this script.

Examples:
  python email_sourcing.py
  python email_sourcing.py --start 2020-01-01 --end 2026-10-23
  python email_sourcing.py --my-address alias@example.com

Date filters are inclusive UTC calendar dates. Changing account, search, or
excluded addresses requires a separate database (rename emails.db while the
script is stopped). Do not run two copies against the same database at once.
"""

import argparse
import csv
import json
import sqlite3
import time
from datetime import date, datetime, time as clock_time, timedelta, timezone
from email.utils import getaddresses
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
HEADERS = ["From", "To", "Cc", "Bcc"]


def execute_retry(request, attempts=6):
    """Retry temporary API failures, including Gmail per-minute 403 limits."""
    for attempt in range(attempts):
        try:
            return request.execute(num_retries=0)
        except HttpError as error:
            status = getattr(error.resp, "status", None)
            detail = str(error).lower()
            retryable = status in (429, 500, 502, 503, 504) or (
                status == 403 and any(term in detail for term in (
                    "ratelimitexceeded", "userratelimitexceeded", "quota exceeded"
                ))
            )
            if not retryable or attempt == attempts - 1:
                raise
            delay = min(60, 5 * (2 ** attempt))
            print(f"Temporary Gmail API error ({status}); retrying in {delay}s")
            time.sleep(delay)


def authorize(script_dir):
    token_file = script_dir / "token.json"
    credentials_file = script_dir / "credentials.json"
    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not credentials_file.exists():
                raise FileNotFoundError(
                    f"Missing {credentials_file}. Download OAuth Desktop client "
                    "credentials and save them as credentials.json."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_file), SCOPES
            )
            creds = flow.run_local_server(port=0)
        token_file.write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds)


def init_db():
    db_path = Path(__file__).resolve().parent / "emails.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS contacts (
            email TEXT PRIMARY KEY COLLATE NOCASE,
            name TEXT NOT NULL DEFAULT '',
            first_seen_utc TEXT NOT NULL,
            last_seen_utc TEXT NOT NULL,
            sent_messages INTEGER NOT NULL DEFAULT 0,
            received_messages INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS processed_messages (
            message_id TEXT PRIMARY KEY,
            processed_at_utc TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS export_settings (
            setting TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    conn.commit()
    return conn


def build_search(start, end):
    if start and end and date.fromisoformat(start) > date.fromisoformat(end):
        raise ValueError("--start must be on or before --end")
    terms = []
    if start:
        instant = datetime.combine(date.fromisoformat(start), clock_time.min, tzinfo=timezone.utc)
        terms.append(f"after:{int(instant.timestamp())}")
    if end:
        end_date = date.fromisoformat(end) + timedelta(days=1)
        instant = datetime.combine(end_date, clock_time.min, tzinfo=timezone.utc)
        terms.append(f"before:{int(instant.timestamp())}")
    return " ".join(terms)


def safe_for_csv(value):
    value = str(value)
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def export(service, search, my_addresses, output, conn):
    my_addresses = {address.strip().lower() for address in my_addresses}
    profile = execute_retry(service.users().getProfile(userId="me"))
    my_addresses.add(profile["emailAddress"].lower())

    # A checkpoint database must not be reused with a different account or filter.
    settings = json.dumps({
        "account": profile["emailAddress"].lower(),
        "search": search,
        "my_addresses": sorted(my_addresses),
        "spam_trash": False,
    }, sort_keys=True)
    previous = conn.execute(
        "SELECT value FROM export_settings WHERE setting = 'run_config'"
    ).fetchone()
    if previous is not None and previous[0] != settings:
        raise ValueError(
            "This emails.db uses a different Gmail account/date range/alias list. "
            "Rename emails.db while the script is stopped and rerun to start fresh."
        )
    if previous is None:
        message_count = conn.execute("SELECT COUNT(*) FROM processed_messages").fetchone()[0]
        contact_count = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        if message_count or contact_count:
            raise ValueError(
                "Existing database data has no saved account settings. "
                "Rename emails.db and start a fresh export."
            )
        conn.execute(
            "INSERT INTO export_settings(setting, value) VALUES ('run_config', ?)",
            (settings,)
        )
        conn.commit()

    processed = 0
    skipped = 0
    page_token = None

    try:
        while True:
            params = {"userId": "me", "maxResults": 500, "includeSpamTrash": False}
            if search:
                params["q"] = search
            if page_token:
                params["pageToken"] = page_token

            page = execute_retry(service.users().messages().list(**params))
            for item in page.get("messages", []):
                message_id = item["id"]
                exists = conn.execute(
                    "SELECT 1 FROM processed_messages WHERE message_id = ?",
                    (message_id,)
                ).fetchone()
                if exists:
                    skipped += 1
                    continue

                # # Time constraint api max is 6,000 API units 
                # (300 messages * (20 per message) per minute) time delay of .24 seconds is 60/.24 = 250 messages per minute, 
                # which is under the limit of 300 messages per minute. This is to avoid hitting the API rate limit.
                time.sleep(0.24)
                try:
                    msg = execute_retry(service.users().messages().get(
                        userId="me", id=message_id, format="metadata",
                        metadataHeaders=HEADERS
                    ))
                except HttpError as error:
                    if getattr(error.resp, "status", None) == 404:
                        continue  # Message deleted during extraction.
                    raise

                headers = {name.lower(): [] for name in HEADERS}
                for entry in msg.get("payload", {}).get("headers", []):
                    key = entry["name"].lower()
                    if key in headers:
                        headers[key].append(entry["value"])

                participants = {}
                for values in headers.values():
                    for name, address in getaddresses(values):
                        address = address.strip().lower()
                        if not address or "@" not in address or address in my_addresses:
                            continue
                        participants[address] = name.strip() or participants.get(address, "")

                when = datetime.fromtimestamp(
                    int(msg["internalDate"]) / 1000, tz=timezone.utc
                ).isoformat(timespec="seconds")
                is_sent = "SENT" in msg.get("labelIds", [])

                # Contact counts and processed ID live in the same transaction.
                for address, name in participants.items():
                    conn.execute("""
                        INSERT INTO contacts (
                            email, name, first_seen_utc, last_seen_utc,
                            sent_messages, received_messages
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(email) DO UPDATE SET
                            name = CASE WHEN contacts.name = ''
                                        THEN excluded.name ELSE contacts.name END,
                            first_seen_utc = MIN(contacts.first_seen_utc, excluded.first_seen_utc),
                            last_seen_utc = MAX(contacts.last_seen_utc, excluded.last_seen_utc),
                            sent_messages = contacts.sent_messages + excluded.sent_messages,
                            received_messages = contacts.received_messages + excluded.received_messages
                    """, (address, name, when, when, int(is_sent), int(not is_sent)))

                conn.execute(
                    "INSERT INTO processed_messages(message_id) VALUES (?)",
                    (message_id,)
                )
                processed += 1
                if processed % 500 == 0:
                    conn.commit()
                    count = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
                    print(f"Processed {processed:,} new messages; {count:,} addresses; {skipped:,} skipped")

            page_token = page.get("nextPageToken")
            if not page_token:
                break
        conn.commit()
    except BaseException:
        conn.rollback()  # Keep previous committed batches, discard partial batch.
        raise

    # Write CSV from persisted records, including records from previous runs.
    with output.open("w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.writer(fp)
        writer.writerow([
            "email", "name", "first_seen_utc", "last_seen_utc",
            "sent_messages", "received_messages", "total_messages"
        ])
        for row in conn.execute("""
            SELECT email, name, first_seen_utc, last_seen_utc,
                   sent_messages, received_messages,
                   sent_messages + received_messages
            FROM contacts ORDER BY email COLLATE NOCASE
        """):
            writer.writerow([safe_for_csv(row[0]), safe_for_csv(row[1]), *row[2:]])

    count = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    print(f"Exported {count:,} unique addresses to {output}; {processed:,} new messages, {skipped:,} skipped")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", metavar="YYYY-MM-DD")
    parser.add_argument("--end", metavar="YYYY-MM-DD")
    parser.add_argument("--output", default="gmail_correspondents.csv")
    parser.add_argument("--my-address", action="append", default=[])
    args = parser.parse_args()
    search = build_search(args.start, args.end)

    conn = init_db()
    try:
        service = authorize(Path(__file__).resolve().parent)
        export(service, search, args.my_address, Path(args.output), conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
