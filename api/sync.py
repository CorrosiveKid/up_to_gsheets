"""Vercel serverless entrypoint for the Up -> Google Sheets sync.

Deployed at /api/sync and invoked by Vercel Cron (see vercel.json). The
actual work lives in sync.py at the repo root so the same code runs from
the CLI; this file is only the HTTP wrapper around it.

Requests must present the CRON_SECRET as a bearer token. Vercel Cron sends
`Authorization: Bearer $CRON_SECRET` automatically when that env var is
set; without the check, anyone who found the URL could spam the Up API and
your Sheets quota.
"""

import hmac
import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler

# The function's bundle keeps the repo layout, but the root isn't on
# sys.path by default — add it so `import sync` resolves.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sync import ConfigError, run_sync  # noqa: E402


def check_auth(headers):
    """Return None if the caller is authorised, else (status, message)."""
    secret = os.environ.get("CRON_SECRET")
    if not secret:
        # Fail closed: an unset secret would otherwise leave the endpoint
        # open to the whole internet.
        return 500, "CRON_SECRET is not set on this deployment."

    provided = headers.get("Authorization", "")
    if not hmac.compare_digest(provided, f"Bearer {secret}"):
        return 401, "Unauthorized."

    return None


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        auth_error = check_auth(self.headers)
        if auth_error:
            status, message = auth_error
            self.respond(status, {"ok": False, "error": message})
            return

        lines = []

        def log(message):
            print(message)  # shows up in the Vercel function logs
            lines.append(str(message))

        try:
            summary = run_sync(log=log)
        except ConfigError as exc:
            self.respond(400, {"ok": False, "error": str(exc), "log": lines})
        except Exception as exc:  # noqa: BLE001 - surface the failure to the caller
            traceback.print_exc()
            self.respond(
                500,
                {"ok": False, "error": f"{type(exc).__name__}: {exc}", "log": lines},
            )
        else:
            self.respond(200, {"ok": True, "log": lines, **summary})

    # Manual triggers from curl/Postman tend to be POSTs; same behaviour.
    do_POST = do_GET

    def respond(self, status, body):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        pass  # suppress the default per-request stderr noise
