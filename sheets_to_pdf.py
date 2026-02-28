#!/usr/bin/env python3
"""Convert each sheet in a Google Spreadsheet to a separate PDF and optionally upload to Google Drive."""

import argparse
import io
import os
import re
import sys

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
]


def extract_spreadsheet_id(input_str):
    """Extract spreadsheet ID from a Google Sheets URL or return as-is if already an ID."""
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", input_str)
    if match:
        return match.group(1)
    if re.fullmatch(r"[a-zA-Z0-9-_]+", input_str):
        return input_str
    sys.exit(f"Error: Could not extract spreadsheet ID from: {input_str}")


def extract_folder_id(input_str):
    """Extract folder ID from a Google Drive folder URL or return as-is if already an ID."""
    match = re.search(r"/folders/([a-zA-Z0-9-_]+)", input_str)
    if match:
        return match.group(1)
    if re.fullmatch(r"[a-zA-Z0-9-_]+", input_str):
        return input_str
    sys.exit(f"Error: Could not extract folder ID from: {input_str}")


def sanitize_filename(name):
    """Replace characters that are invalid in filenames with underscores."""
    safe = re.sub(r'[^\w\s\-]', '_', name).strip()
    return safe if safe else "sheet"


def load_credentials(creds_path):
    """Load service account credentials from a JSON key file."""
    if not os.path.isfile(creds_path):
        sys.exit(f"Error: Credentials file not found: {creds_path}")
    try:
        return service_account.Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    except Exception as e:
        sys.exit(f"Error loading credentials: {e}")


def get_sheets(credentials, spreadsheet_id):
    """Fetch the list of sheets (title + gid) from a spreadsheet."""
    service = build("sheets", "v4", credentials=credentials)
    try:
        result = service.spreadsheets().get(
            spreadsheetId=spreadsheet_id, fields="sheets.properties"
        ).execute()
    except Exception as e:
        error_msg = str(e)
        if "404" in error_msg:
            sys.exit(f"Error: Spreadsheet not found: {spreadsheet_id}")
        if "403" in error_msg:
            sys.exit(
                f"Error: Permission denied. Share the spreadsheet with the service account email "
                f"listed in your credentials JSON (client_email field)."
            )
        sys.exit(f"Error fetching spreadsheet metadata: {e}")

    sheets = []
    for sheet in result.get("sheets", []):
        props = sheet["properties"]
        sheets.append({"title": props["title"], "gid": props["sheetId"]})
    return sheets


def export_sheet_pdf(session, spreadsheet_id, gid):
    """Download a single sheet as PDF bytes using the export endpoint."""
    url = (
        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export"
        f"?format=pdf"
        f"&gid={gid}"
        f"&portrait=true"
        f"&fitw=true"
        f"&gridlines=false"
    )
    resp = session.get(url)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    content_type = resp.headers.get("Content-Type", "")
    if "pdf" not in content_type:
        raise RuntimeError(
            f"Expected PDF but got Content-Type: {content_type}. "
            "Authentication may have failed — ensure the spreadsheet is shared with the service account."
        )
    return resp.content


def upload_to_drive(drive_service, file_name, pdf_bytes, folder_id):
    """Upload a PDF to a Google Drive folder."""
    file_metadata = {"name": file_name, "parents": [folder_id]}
    media = MediaIoBaseUpload(io.BytesIO(pdf_bytes), mimetype="application/pdf")
    uploaded = drive_service.files().create(
        body=file_metadata, media_body=media, fields="id,webViewLink"
    ).execute()
    return uploaded.get("webViewLink", uploaded.get("id"))


def main():
    parser = argparse.ArgumentParser(
        description="Convert each sheet in a Google Spreadsheet to a separate PDF."
    )
    parser.add_argument(
        "spreadsheet",
        help="Google Sheets URL or spreadsheet ID",
    )
    parser.add_argument(
        "-c", "--credentials",
        help="Path to service account JSON key file (default: GOOGLE_APPLICATION_CREDENTIALS env var)",
    )
    parser.add_argument(
        "-d", "--drive-folder",
        help="Google Drive folder URL or ID to upload PDFs to (optional)",
    )
    parser.add_argument(
        "-o", "--output-dir",
        default="output",
        help="Local directory to save PDFs (default: ./output)",
    )
    args = parser.parse_args()

    # Resolve credentials
    creds_path = args.credentials or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not creds_path:
        sys.exit(
            "Error: No credentials provided.\n"
            "Use --credentials <path> or set the GOOGLE_APPLICATION_CREDENTIALS environment variable."
        )

    spreadsheet_id = extract_spreadsheet_id(args.spreadsheet)
    folder_id = extract_folder_id(args.drive_folder) if args.drive_folder else None

    # Authenticate
    credentials = load_credentials(creds_path)

    # Fetch sheet metadata
    print(f"Fetching sheets for spreadsheet {spreadsheet_id}...")
    sheets = get_sheets(credentials, spreadsheet_id)
    if not sheets:
        sys.exit("Error: No sheets found in the spreadsheet.")
    print(f"Found {len(sheets)} sheet(s): {', '.join(s['title'] for s in sheets)}")

    # Prepare output directory
    os.makedirs(args.output_dir, exist_ok=True)

    # Prepare authenticated session for export and optional Drive service
    session = AuthorizedSession(credentials)
    drive_service = build("drive", "v3", credentials=credentials) if folder_id else None

    # Track filenames to handle duplicates
    used_names = {}

    for sheet in sheets:
        title = sheet["title"]
        gid = sheet["gid"]
        print(f"  Exporting '{title}' (gid={gid})...", end=" ")

        try:
            pdf_bytes = export_sheet_pdf(session, spreadsheet_id, gid)
        except RuntimeError as e:
            print(f"FAILED: {e}")
            continue

        # Deduplicate filenames
        base_name = sanitize_filename(title)
        if base_name in used_names:
            used_names[base_name] += 1
            file_name = f"{base_name}_{used_names[base_name]}.pdf"
        else:
            used_names[base_name] = 1
            file_name = f"{base_name}.pdf"

        # Save locally
        local_path = os.path.join(args.output_dir, file_name)
        with open(local_path, "wb") as f:
            f.write(pdf_bytes)
        print(f"saved to {local_path}", end="")

        # Upload to Drive
        if drive_service and folder_id:
            try:
                link = upload_to_drive(drive_service, file_name, pdf_bytes, folder_id)
                print(f" | uploaded to Drive ({link})", end="")
            except Exception as e:
                print(f" | Drive upload FAILED: {e}", end="")

        print()

    print(f"\nDone. {len(sheets)} sheet(s) processed. Local PDFs in: {os.path.abspath(args.output_dir)}")
    if folder_id:
        print(f"Drive folder: https://drive.google.com/drive/folders/{folder_id}")


if __name__ == "__main__":
    main()
