#!/usr/bin/env python3
"""
GitHub webhook listener — auto-deploys on every push to main.

Runs as a systemd service (outside Docker so it can control containers).
Listens on port 9000 for POST /deploy from GitHub.
Validates HMAC-SHA256 signature using WEBHOOK_SECRET from .env.

Setup: see scripts/webhook_setup.sh
"""

import hashlib
import hmac
import json
import logging
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

PORT           = int(os.environ.get("WEBHOOK_PORT", 9000))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").encode()
REPO_DIR       = os.environ.get("REPO_DIR", "/opt/polyfarm")
DEPLOY_BRANCH  = os.environ.get("DEPLOY_BRANCH", "main")
LOG_FILE       = "/var/log/polyfarm_deploy.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE),
    ]
)
logger = logging.getLogger("webhook")


# ── Signature validation ──────────────────────────────────────────────────────

def _valid_signature(body: bytes, sig_header: str) -> bool:
    if not WEBHOOK_SECRET:
        logger.warning("WEBHOOK_SECRET not set — skipping signature check")
        return True
    if not sig_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(WEBHOOK_SECRET, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig_header)


# ── Deploy script ─────────────────────────────────────────────────────────────

def _deploy():
    logger.info("=== Deploy triggered ===")
    commands = [
        ["git", "-C", REPO_DIR, "fetch", "origin"],
        ["git", "-C", REPO_DIR, "reset", "--hard", f"origin/{DEPLOY_BRANCH}"],
        ["docker", "compose", "-f", f"{REPO_DIR}/docker-compose.yml",
         "--project-directory", REPO_DIR, "up", "-d", "--build"],
    ]
    for cmd in commands:
        logger.info("Running: %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.stdout:
            logger.info(result.stdout.strip())
        if result.stderr:
            logger.info(result.stderr.strip())
        if result.returncode != 0:
            logger.error("Command failed (exit %d): %s", result.returncode, " ".join(cmd))
            return False
    logger.info("=== Deploy complete ===")
    return True


# ── HTTP handler ──────────────────────────────────────────────────────────────

class WebhookHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # Suppress default access log — we use our own logger

    def do_POST(self):
        if self.path != "/deploy":
            self._respond(404, "Not found")
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        # Validate GitHub signature
        sig = self.headers.get("X-Hub-Signature-256", "")
        if not _valid_signature(body, sig):
            logger.warning("Invalid signature — request rejected")
            self._respond(403, "Forbidden")
            return

        # Only deploy on push events to the deploy branch
        event = self.headers.get("X-GitHub-Event", "")
        if event == "ping":
            logger.info("GitHub ping received — webhook configured correctly")
            self._respond(200, "pong")
            return

        if event != "push":
            self._respond(200, f"Ignored event: {event}")
            return

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self._respond(400, "Invalid JSON")
            return

        ref = payload.get("ref", "")
        if ref != f"refs/heads/{DEPLOY_BRANCH}":
            logger.info("Push to %s — not deploying (only deploy %s)", ref, DEPLOY_BRANCH)
            self._respond(200, f"Ignored branch: {ref}")
            return

        pusher = payload.get("pusher", {}).get("name", "unknown")
        commits = len(payload.get("commits", []))
        logger.info("Push by %s (%d commit(s)) — deploying...", pusher, commits)

        self._respond(200, "Deploy started")

        # Deploy after responding (don't block GitHub's webhook timeout)
        _deploy()

    def _respond(self, code: int, msg: str):
        body = msg.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not WEBHOOK_SECRET:
        logger.warning("WEBHOOK_SECRET not set in environment — webhook is unauthenticated!")

    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    logger.info("Webhook server listening on port %d", PORT)
    logger.info("Deploy branch: %s | Repo: %s", DEPLOY_BRANCH, REPO_DIR)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopped.")
