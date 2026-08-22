"""
One-time auth setup script.
Run this locally to generate OAuth tokens for each Google account.
Usage:
    uv run python3 auth_setup.py --account personal
    uv run python3 auth_setup.py --account uni
"""
import argparse
import os
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.readonly",
]

def main():
    parser = argparse.ArgumentParser(description="Authenticate a Google account for Daily Briefing")
    parser.add_argument("--account", required=True, choices=["personal", "uni"],
                        help="Which account to authenticate: personal or uni")
    args = parser.parse_args()

    creds_file = os.path.join(os.path.dirname(__file__), "credentials.json")
    token_file = os.path.join(os.path.dirname(__file__), f"token_{args.account}.json")

    if os.path.exists(token_file):
        print(f"Token already exists at {token_file}")
        overwrite = input("Overwrite? (y/n): ").strip().lower()
        if overwrite != "y":
            print("Aborted.")
            return

    flow = InstalledAppFlow.from_client_secrets_file(creds_file, SCOPES)
    creds = flow.run_local_server(port=0)

    with open(token_file, "w") as f:
        f.write(creds.to_json())

    print(f"Token saved to {token_file}")
    print(f"Authenticated as: {args.account}")

if __name__ == "__main__":
    main()
