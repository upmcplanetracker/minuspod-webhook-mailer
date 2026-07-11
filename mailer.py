#!/usr/bin/env python3
"""
minuspod-mailer

Minimal webhook receiver for MinusPod's default JSON payloads. Formats
each of the four failure/error event types into a plain-text email and
relays it through Postfix.

No auth, no persistence, no dependencies beyond the stdlib. Intended to
run bridge-internal on the same Podman network as MinusPod, with no
published host ports.
"""
import json
import logging
import os
import smtplib
import socket
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class DualStackHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer variant that accepts both IPv4 and IPv6 on one
    socket. Binds AF_INET6 to '::' and clears IPV6_V6ONLY so IPv4 clients
    (via mapped addresses) and native IPv6 clients both work."""

    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("minuspod-mailer")

SMTP_HOST = os.environ.get("SMTP_HOST", "host.containers.internal")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "25"))
MAIL_FROM = os.environ["MAIL_FROM"]
MAIL_TO = os.environ["MAIL_TO"]
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8080"))


def fmt_episode_failed(p: dict) -> tuple[str, str]:
    podcast = p.get("podcast", {})
    episode = p.get("episode", {})
    subject = f"[MinusPod] Episode Failed: {podcast.get('name', 'unknown')} — {episode.get('title', 'unknown')}"
    body = (
        f"Podcast:    {podcast.get('name')} ({podcast.get('slug')})\n"
        f"Episode:    {episode.get('title')}\n"
        f"Episode ID: {episode.get('id')}\n"
        f"URL:        {episode.get('url')}\n"
        f"Error:      {episode.get('error_message')}\n"
        f"Processing time: {episode.get('processing_time')}\n"
        f"LLM cost:   {episode.get('llm_cost_display')}\n"
        f"Timestamp:  {p.get('timestamp')}\n"
    )
    return subject, body


def fmt_auth_failure(p: dict) -> tuple[str, str]:
    subject = f"[MinusPod] Auth Failure: {p.get('provider')} / {p.get('model')}"
    body = (
        f"Provider:    {p.get('provider')}\n"
        f"Model:       {p.get('model')}\n"
        f"Status code: {p.get('status_code')}\n"
        f"Error:       {p.get('error_message')}\n"
        f"Timestamp:   {p.get('timestamp')}\n\n"
        f"Action: check/rotate the API key for this provider.\n"
    )
    return subject, body


def fmt_limit_exceeded(p: dict) -> tuple[str, str]:
    subject = f"[MinusPod] Limit Exceeded: {p.get('provider')} / {p.get('model')}"
    body = (
        f"Provider:    {p.get('provider')}\n"
        f"Model:       {p.get('model')}\n"
        f"Status code: {p.get('status_code')}\n"
        f"Error:       {p.get('error_message')}\n"
        f"Timestamp:   {p.get('timestamp')}\n\n"
        f"Action: add credits or raise the limit, then reprocess the "
        f"affected episode manually (it will not auto-retry).\n"
    )
    return subject, body


def fmt_rate_limit_structural(p: dict) -> tuple[str, str]:
    subject = f"[MinusPod] Structural Rate Limit: {p.get('provider')} / {p.get('model')}"
    body = (
        f"Provider:   {p.get('provider')}\n"
        f"Model:      {p.get('model')}\n"
        f"Per-min cap: {p.get('limit')}\n"
        f"Already used this minute: {p.get('used')}\n"
        f"This request requested: {p.get('requested')}\n"
        f"Error:      {p.get('error_message')}\n"
        f"Timestamp:  {p.get('timestamp')}\n\n"
        f"Action: retrying will not help. Shrink the detection window or "
        f"move to a higher provider tier.\n"
    )
    return subject, body


FORMATTERS = {
    "Episode Failed": fmt_episode_failed,
    "Auth Failure": fmt_auth_failure,
    "Limit Exceeded": fmt_limit_exceeded,
    "Rate Limit Structural": fmt_rate_limit_structural,
}


def send_mail(subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = MAIL_TO
    msg.set_content(body)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
        smtp.send_message(msg)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Route through our logger instead of stderr default
        log.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self):
        if self.path == "/healthz":
            self._respond(200, b"ok")
            return
        self._respond(404, b"not found")

    def do_POST(self):
        if self.path != "/webhook":
            self._respond(404, b"not found")
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Received non-JSON body, ignoring")
            self._respond(400, b"invalid json")
            return

        event = payload.get("event")

        if payload.get("test"):
            log.info("Test webhook received for event=%s, not emailing", event)
            self._respond(200, b"test ok (no email sent)")
            return

        formatter = FORMATTERS.get(event)
        if formatter is None:
            # Episode Processed or anything unrecognized/future — ignore silently
            log.info("Ignoring event=%s (not in formatter set)", event)
            self._respond(200, b"ignored")
            return

        try:
            subject, body = formatter(payload)
            send_mail(subject, body)
            log.info("Emailed event=%s", event)
            self._respond(200, b"ok")
        except Exception:
            log.exception("Failed to process/email event=%s", event)
            self._respond(500, b"error")

    def _respond(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    server = DualStackHTTPServer(("::", LISTEN_PORT), Handler)
    log.info(
        "minuspod-mailer listening on [::]:%d (dual-stack), relaying via %s:%d, from=%s to=%s",
        LISTEN_PORT, SMTP_HOST, SMTP_PORT, MAIL_FROM, MAIL_TO,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
