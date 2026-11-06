"""Localhost redirect so vault links are clickable in the Claude desktop app.

The app only linkifies http(s) URLs, not obsidian:// or paths with spaces. This
server turns http://localhost:8799/<vault-relative path> into a redirect to
obsidian://open, which the browser hands to Obsidian.

Mac only (Obsidian runs there). Bound to 127.0.0.1, never exposed.
Run by launchd: ~/Library/LaunchAgents/com.example.obsidian-links.plist
"""
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlsplit

VAULT = "<VAULT_NAME>"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = unquote(urlsplit(self.path).path).lstrip("/")
        if path.endswith(".md"):
            path = path[:-3]
        if not path or ".." in path.split("/"):
            self.send_error(400, "Usage: /<vault-relative path to note>")
            return
        target = f"obsidian://open?vault={VAULT}&file={quote(path, safe='')}"
        self.send_response(302)
        self.send_header("Location", target)
        self.end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
