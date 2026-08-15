# gen_token.py
# Run this LOCALLY, once, to generate a Gmail OAuth token for deployment.
#
# It runs the normal browser-based OAuth consent flow using credentials.json,
# then prints the resulting token as a single-line JSON string — paste that
# into Render (or wherever you deploy) as the GOOGLE_TOKEN_JSON env var.
# A copy is also saved to token.json locally, same as before, so local runs
# keep working without any env var set.
#
# Usage:
#   python gen_token.py

import json
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
]


def main() -> None:
    try:
        flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
    except FileNotFoundError:
        print("ERROR: credentials.json not found in the current directory.", file=sys.stderr)
        print("Download it from Google Cloud Console (OAuth client, type 'Desktop app')", file=sys.stderr)
        print("and save it as credentials.json here first.", file=sys.stderr)
        sys.exit(1)

    creds = flow.run_local_server(port=0)
    token_json = creds.to_json()

    with open("token.json", "w") as f:
        f.write(token_json)

    print("\nSaved token.json locally.\n")
    print("Copy the line below and paste it into Render as GOOGLE_TOKEN_JSON:\n")
    print(json.dumps(json.loads(token_json)))


if __name__ == "__main__":
    main()
