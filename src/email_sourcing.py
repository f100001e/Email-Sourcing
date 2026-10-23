#!/usr/bin/env python3
"""Export unique email addresses in Gmail messages, not just Google Contacts.

Setup:
  pip install --upgrade google-api-python-client google-auth-httplib2 google-auth-oauthlib
  Save OAuth Desktop credentials as credentials.json beside this script.

Examples:
  python gmail_correspondents.py
  python gmail_correspondents.py --start 2020-01-01 --end 2026-10-23
  python gmail_correspondents.py --include-spam-trash --my-address alias@example.com

The optional start/end dates use UTC calendar days and are inclusive.
Only message headers are read, never message bodies.
"""

import argparse
import csv
from datetime import date, datetime, time, timedelta, timezone
from email.utils import getaddresses
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
HEADERS = ["From", "To", "Cc", "Bcc"]

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
                    f"Missing {credentials_file}. Download OAuth Desktop client credentials "
                    "from Google Cloud and save them as credentials.json."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_file), SCOPES
            )
            creds = flow.run_local_server(port=0)
        token_file.write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds)


def build_search(start, end):
    terms = []
    if start:
        start_date = date.fromisoformat(start)
        instant = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
        terms.append(f"after:{int(instant.timestamp())}")
    if end:
        end_date = date.fromisoformat(end) + timedelta(days=1)
        instant = datetime.combine(end_date, time.min, tzinfo=timezone.utc)
        terms.append(f"before:{int(instant.timestamp())}")
    if start and end and date.fromisoformat(start) > date.fromisoformat(end):
        raise ValueError("--start must be on or before --end")
    return " ".join(terms)


def safe_for_csv(value):
    # Prevent spreadsheet apps from treating a sender's display name as a formula.
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def export(service, search, include_spam_trash, my_addresses, output):
    my_addresses = {address.strip().lower() for address in my_addresses}
    profile = service.users().getProfile(userId="me").execute(num_retries=5)
    my_addresses.add(profile["emailAddress"].lower())
    contacts = {}
    page_token = None
    processed = 0

    while True:
        params = {
            "userId": "me",
            "maxResults": 500,
            "includeSpamTrash": include_spam_trash,
        }
        if search:
            params["q"] = search
        if page_token:
            params["pageToken"] = page_token

        page = service.users().messages().list(**params).execute(num_retries=5)
        for item in page.get("messages", []):
            try:
                msg = service.users().messages().get(
                    userId="me",
                    id=item["id"],
                    format="metadata",
                    metadataHeaders=HEADERS,
                ).execute(num_retries=5)
            except HttpError as exc:
                if exc.resp.status == 404:  # Message deleted during export
                    continue
                raise

            headers = {name.lower(): [] for name in HEADERS}
            for entry in msg.get("payload", {}).get("headers", []):
                key = entry["name"].lower()
                if key in headers:
                    headers[key].append(entry["value"])

            participants = {}
            for header_values in headers.values():
                for name, address in getaddresses(header_values):
                    address = address.strip().lower()
                    if not address or "@" not in address or address in my_addresses:
                        continue
                    participants[address] = name.strip() or participants.get(address, "")

            when = datetime.fromtimestamp(
                int(msg["internalDate"]) / 1000, tz=timezone.utc
            )
            is_sent = "SENT" in msg.get("labelIds", [])
            for address, name in participants.items():
                record = contacts.setdefault(address, {
                    "name": "",
                    "first": when,
                    "last": when,
                    "sent": 0,
                    "received": 0,
                })
                if name and not record["name"]:
                    record["name"] = name
                record["first"] = min(record["first"], when)
                record["last"] = max(record["last"], when)
                record["sent" if is_sent else "received"] += 1

            processed += 1
            if processed % 500 == 0:
                print(f"Processed {processed:,} messages; found {len(contacts):,} addresses")

        page_token = page.get("nextPageToken")
        if not page_token:
            break

    with output.open("w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.writer(fp)
        writer.writerow([
            "email", "name", "first_seen_utc", "last_seen_utc",
            "sent_messages", "received_messages", "total_messages",
        ])
        for address, record in sorted(contacts.items()):
            writer.writerow([
                safe_for_csv(address),
                safe_for_csv(record["name"]),
                record["first"].isoformat(timespec="seconds"),
                record["last"].isoformat(timespec="seconds"),
                record["sent"],
                record["received"],
                record["sent"] + record["received"],
            ])
    print(f"Exported {len(contacts):,} unique addresses from {processed:,} messages to {output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", metavar="YYYY-MM-DD", help="First UTC date (inclusive)")
    parser.add_argument("--end", metavar="YYYY-MM-DD", help="Last UTC date (inclusive)")
    parser.add_argument("--output", default="gmail_correspondents.csv")
    parser.add_argument("--include-spam-trash", action="store_true")
    parser.add_argument("--my-address", action="append", default=[],
                        help="Other email address you own; repeat for aliases")
    args = parser.parse_args()
    search = build_search(args.start, args.end)
    service = authorize(Path(__file__).resolve().parent)
    export(service, search, args.include_spam_trash,
           args.my_address, Path(args.output))


if __name__ == "__main__":
    main()
