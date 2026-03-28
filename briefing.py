"""
Daily Briefing: Fetches Gmail + Calendar from multiple accounts,
summarizes with Claude, sends SMS via Twilio.

Usage:
    uv run python3 briefing.py --mode morning
    uv run python3 briefing.py --mode evening
"""
import argparse
import os
import json
import base64
from datetime import datetime, timedelta, timezone

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import anthropic
from twilio.rest import Client as TwilioClient

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]

ACCOUNTS = ["personal", "uni"]


def load_credentials(account_name, base_dir):
    token_path = os.path.join(base_dir, f"token_{account_name}.json")
    if not os.path.exists(token_path):
        print(f"Warning: No token for {account_name}, skipping")
        return None

    creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return creds


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

    prompt = f"""You are a personal briefing assistant. Create a concise SMS briefing (under 300 words, must fit in a text message).

MODE: {mode.upper()} BRIEFING

EMAILS ({len(emails)} unread):
{json.dumps(emails, indent=2)}

CALENDAR EVENTS for {days_label}:
{json.dumps(events, indent=2)}
{week_section}

Format the briefing as:
- Start with a one-line greeting based on time of day
- CALENDAR: list events with times (convert to AEST)
- EMAIL: highlight the most important ones, group by account (personal/uni)
- ACTION ITEMS: flag anything that needs a reply or action
- Keep it punchy and scannable — this is a text message, not an essay
- Use plain text only, no markdown"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )

    return response.content[0].text


def send_sms(message):
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    from_number = os.environ.get("TWILIO_FROM_NUMBER")
    to_number = os.environ.get("TWILIO_TO_NUMBER")

    if not all([account_sid, auth_token, from_number, to_number]):
        raise ValueError("Twilio env vars not fully set")

    client = TwilioClient(account_sid, auth_token)

    # Split into multiple messages if over SMS limit (1600 chars)
    if len(message) > 1600:
        parts = [message[i:i+1550] for i in range(0, len(message), 1550)]
        for i, part in enumerate(parts):
            prefix = f"[{i+1}/{len(parts)}] " if len(parts) > 1 else ""
            client.messages.create(
                body=prefix + part,
                from_=from_number,
                to=to_number,
            )
    else:
        client.messages.create(
            body=message,
            from_=from_number,
            to=to_number,
        )

    print(f"SMS sent to {to_number}")


def main():
    parser = argparse.ArgumentParser(description="Daily Briefing SMS")
    parser.add_argument("--mode", required=True, choices=["morning", "evening"],
                        help="morning = today's briefing, evening = recap + tomorrow preview")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print briefing without sending SMS")
    args = parser.parse_args()

    base_dir = os.path.dirname(os.path.abspath(__file__))

    all_emails = []
    all_events = []

    # Fetch from all accounts
    for account in ACCOUNTS:
        creds = load_credentials(account, base_dir)
        if not creds:
            continue

        all_emails.extend(fetch_emails(creds, account))

        # Morning: today's events (+ week if Monday)
        # Evening: tomorrow's events
        if args.mode == "morning":
            is_monday = datetime.now().weekday() == 0
            days = 7 if is_monday else 1
            all_events.extend(fetch_calendar_events(creds, account, days_ahead=days))
        else:
            all_events.extend(fetch_calendar_events(creds, account, days_ahead=2))

    if not all_emails and not all_events:
        print("No emails or events found. Skipping briefing.")
        return

    # Summarize with Claude
    is_monday = datetime.now().weekday() == 0
    briefing = summarize_with_claude(all_emails, all_events, args.mode, is_monday)

    print("=" * 50)
    print(briefing)
    print("=" * 50)

    if not args.dry_run:
        send_sms(briefing)
    else:
        print("\n(Dry run — SMS not sent)")


if __name__ == "__main__":
    main()
