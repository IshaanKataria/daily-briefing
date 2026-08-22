"""
Daily Briefing: Fetches Gmail + Calendar from multiple accounts,
summarizes with Claude, emails the brief to you.

Delivery moved from Twilio SMS to email on 2026-08-22. Every SMS was being
rejected with Twilio error 30044 (trial accounts cap message length and the
briefs run ~1500 chars), so only the very first test message ever arrived.
Email has no length cap and no per-message cost.

Usage:
    uv run python3 briefing.py --mode morning
    uv run python3 briefing.py --mode evening
"""
import argparse
import os
import json
import base64
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import anthropic

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.readonly",
]

ACCOUNTS = ["personal", "uni"]


def load_credentials(account_name, base_dir):
    token_path = os.path.join(base_dir, f"token_{account_name}.json")
    if not os.path.exists(token_path):
        print(f"Warning: No token for {account_name}, skipping")
        return None

    try:
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(token_path, "w") as f:
                f.write(creds.to_json())

        return creds
    except Exception as e:
        print(f"Error loading credentials for {account_name}: {e}")
        return None


def fetch_emails(creds, account_name, max_results=10):
    try:
        service = build("gmail", "v1", credentials=creds)
        results = service.users().messages().list(
            userId="me", q="is:unread", maxResults=max_results
        ).execute()

        messages = results.get("messages", [])
        emails = []

        for msg in messages:
            detail = service.users().messages().get(
                userId="me", id=msg["id"], format="metadata",
                metadataHeaders=["Subject", "From", "Date"]
            ).execute()

            headers = {h["name"]: h["value"] for h in detail["payload"]["headers"]}
            snippet = detail.get("snippet", "")

            emails.append({
                "account": account_name,
                "from": headers.get("From", "Unknown"),
                "subject": headers.get("Subject", "No subject"),
                "date": headers.get("Date", ""),
                "snippet": snippet[:150],
            })

        return emails
    except Exception as e:
        print(f"Error fetching emails for {account_name}: {e}")
        return []


def fetch_calendar_events(creds, account_name, days_ahead=1):
    try:
        service = build("calendar", "v3", credentials=creds)

        now = datetime.now(timezone.utc)
        time_min = now.isoformat()
        time_max = (now + timedelta(days=days_ahead)).isoformat()

        events_result = service.events().list(
            calendarId="primary",
            timeMin=time_min,
            timeMax=time_max,
            maxResults=20,
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        events = events_result.get("items", [])
        parsed = []

        for event in events:
            start = event["start"].get("dateTime", event["start"].get("date"))
            end = event["end"].get("dateTime", event["end"].get("date"))

            parsed.append({
                "account": account_name,
                "summary": event.get("summary", "No title"),
                "start": start,
                "end": end,
                "location": event.get("location", ""),
            })

        return parsed
    except Exception as e:
        print(f"Error fetching calendar for {account_name}: {e}")
        return []


def summarize_with_claude(emails, events, mode, is_monday=False):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key)

    days_label = "today" if mode == "morning" else "tomorrow"
    week_section = ""
    if is_monday and mode == "morning":
        week_section = "\nAlso include a WEEK AHEAD section since it's Monday — summarize the full week's events."

    prompt = f"""You are Ishaan's personal briefing assistant. Write his {mode} briefing as a plain text email.

EMAILS ({len(emails)} unread):
{json.dumps(emails, indent=2)}

CALENDAR EVENTS for {days_label}:
{json.dumps(events, indent=2)}
{week_section}

Use these sections in this order:

CALENDAR
Events in time order, times converted to AEST. One line each. Say plainly if the day is clear.

INBOX
The messages that actually matter. Skip newsletters, job alerts and automated noise entirely rather than listing them. Group by account (personal/uni).

ACTION ITEMS
Anything with a deadline or that needs a decision today. If there is nothing, say so.

Rules:
- Plain text only, no markdown, no asterisks, no headers with hash symbols.
- Be direct and specific. Name people, subjects and day counts.
- No greeting, no sign-off, no filler.
- Aim for under 400 words."""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )

    return response.content[0].text


def send_email(creds, subject, body):
    to_address = os.environ.get("BRIEFING_TO_EMAIL")
    if not to_address:
        raise ValueError("BRIEFING_TO_EMAIL not set")
    if not creds:
        raise ValueError("No Google credentials available to send with")

    service = build("gmail", "v1", credentials=creds)

    message = EmailMessage()
    message.set_content(body)
    message["To"] = to_address
    message["Subject"] = subject

    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": encoded}).execute()

    print(f"Email sent to {to_address}")


def send_error_email(creds, error_msg, mode):
    """Send a short failure notice so a broken run isn't silent."""
    try:
        send_email(
            creds,
            f"Daily Briefing {mode.upper()} FAILED",
            f"The {mode} briefing crashed:\n\n{error_msg[:1000]}",
        )
    except Exception as e:
        print(f"Could not send error email: {e}")


def run_briefing(args, base_dir, sender_creds=None):
    all_emails = []
    all_events = []

    # Fetch from all accounts -- per-account try/except so one failure doesn't kill the run
    for account in ACCOUNTS:
        try:
            creds = load_credentials(account, base_dir)
            if not creds:
                continue

            if sender_creds is None:
                sender_creds = creds

            all_emails.extend(fetch_emails(creds, account))

            if args.mode == "morning":
                is_monday = datetime.now().weekday() == 0
                days = 7 if is_monday else 1
                all_events.extend(fetch_calendar_events(creds, account, days_ahead=days))
            else:
                all_events.extend(fetch_calendar_events(creds, account, days_ahead=2))
        except Exception as e:
            print(f"Error processing {account} account: {e}")
            continue

    if not all_emails and not all_events:
        print("No emails or events found. Skipping briefing.")
        return sender_creds

    is_monday = datetime.now().weekday() == 0
    briefing = summarize_with_claude(all_emails, all_events, args.mode, is_monday)

    print("=" * 50)
    print(briefing)
    print("=" * 50)

    if not args.dry_run:
        label = "Morning" if args.mode == "morning" else "Evening"
        subject = f"{label} briefing - {datetime.now().strftime('%a %d %b')}"
        send_email(sender_creds, subject, briefing)
    else:
        print("\n(Dry run — email not sent)")

    return sender_creds


def main():
    parser = argparse.ArgumentParser(description="Daily Briefing email")
    parser.add_argument("--mode", required=True, choices=["morning", "evening"],
                        help="morning = today's briefing, evening = recap + tomorrow preview")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print briefing without sending the email")
    args = parser.parse_args()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    sender_creds = None

    try:
        sender_creds = run_briefing(args, base_dir)
    except Exception as e:
        print(f"FATAL: briefing crashed: {e}")
        if not args.dry_run:
            if sender_creds is None:
                sender_creds = load_credentials("personal", base_dir)
            send_error_email(sender_creds, str(e), args.mode)
        raise


if __name__ == "__main__":
    main()
