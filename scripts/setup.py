#!/usr/bin/env python3
"""
Nextcloud setup for Hermes Agent.

  python3 setup.py                 interactive setup (app password is not echoed)
  python3 setup.py --check         test the saved credentials
  python3 setup.py --url URL --user NAME --token-stdin < token.txt
                                   non-interactive (token read from stdin)

The app password is never printed and never passed on a command line to
another program. It is written to ~/.hermes/nextcloud.env with mode 0600.
"""

import argparse
import getpass
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nextcloud_api import DEFAULT_ENV_FILE, Client, NCError, load_env, _zone  # noqa: E402


def detect_timezone():
    tz = os.environ.get("TZ", "").lstrip(":")
    if "/" in tz:
        return tz
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    try:
        with open("/etc/timezone", encoding="utf-8") as f:
            v = f.read().strip()
            if v:
                return v
    except OSError:
        pass
    return "UTC"


def validate_value(name, value):
    if any(ch in value for ch in "\r\n\"'") or value != value.strip():
        raise NCError(f"{name} contains invalid characters (quotes, newlines or surrounding spaces)")
    return value


def test_connection(url, user, token, allow_http=False):
    """-> (ok, info dict or error message). Checks real HTTP status codes."""
    env = {"NEXTCLOUD_URL": url, "NEXTCLOUD_USER": user, "NEXTCLOUD_TOKEN": token,
           "NEXTCLOUD_ALLOW_HTTP": "1" if allow_http else ""}
    try:
        c = Client(env)
        # 1. Who am I? The DAV paths need the user *id*, which can differ from
        #    the login name (e.g. when logging in with an e-mail address).
        prefix = c.base[len(c.origin):]
        _, _, body = c.request("GET", f"{prefix}/ocs/v2.php/cloud/user?format=json",
                               headers={"Accept": "application/json"}, ok=(200,))
        try:
            data = json.loads(body.decode("utf-8"))["ocs"]["data"]
            user_id = data.get("id") or user
            display = data.get("display-name") or data.get("displayname") or ""
        except (ValueError, KeyError, TypeError):
            return False, "Server answered, but not like a Nextcloud OCS API. Check the URL."
        # 2. WebDAV reachable with that id?
        c.user_id = user_id
        c.request("PROPFIND", c.dav_path(""), None, {"Depth": "0"}, ok=(207,))
        return True, {"user_id": user_id, "display_name": display}
    except NCError as e:
        return False, str(e)


def save_env(path, values):
    """Write atomically with mode 0600 from the start (no window where it is readable)."""
    path = os.path.expanduser(path)
    d = os.path.dirname(path) or "."
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".nextcloud.env.", dir=d)  # mkstemp creates it 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("# Nextcloud credentials for Hermes Agent. Keep private.\n")
            for k, v in values.items():
                if v:
                    f.write(f"{k}={v}\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    print(f"Credentials saved to {path} (mode 600)")


def ask(prompt, default=""):
    v = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return v or default


def interactive(env_file, allow_http):
    existing = load_env(env_file)
    print("=" * 50)
    print("  Nextcloud setup for Hermes Agent")
    print("=" * 50)
    print("Use an APP PASSWORD (Settings > Security > Devices & sessions),")
    print("not your normal login password.\n")

    for attempt in range(3):
        url = ""
        while not url:
            url = ask("Nextcloud URL (https://...)", existing.get("NEXTCLOUD_URL", "")).rstrip("/")
        user = ""
        while not user:
            user = ask("Login name", existing.get("NEXTCLOUD_USER", ""))
        token = ""
        while not token:
            token = getpass.getpass("App password (input hidden): ").strip()
        tz = ask("Timezone", existing.get("NEXTCLOUD_TIMEZONE") if existing.get("NEXTCLOUD_TIMEZONE") not in (None, "", "UTC")
                 else detect_timezone())
        try:
            for name, v in (("URL", url), ("Login name", user), ("App password", token), ("Timezone", tz)):
                validate_value(name, v)
            _zone(tz)
        except NCError as e:
            print(f"✗ {e}\n")
            continue

        print("\nTesting connection...")
        ok, info = test_connection(url, user, token, allow_http)
        if ok:
            print(f"✓ Connected as {info['display_name'] or user} (user id: {info['user_id']})")
            save_env(env_file, {"NEXTCLOUD_URL": url, "NEXTCLOUD_USER": user, "NEXTCLOUD_TOKEN": token,
                                "NEXTCLOUD_USER_ID": info["user_id"] if info["user_id"] != user else "",
                                "NEXTCLOUD_TIMEZONE": tz,
                                "NEXTCLOUD_ALLOW_HTTP": "1" if allow_http else ""})
            print("Setup complete.")
            return 0
        print(f"✗ Connection failed: {info}\n")
        if attempt < 2 and input("Retry? [Y/n]: ").strip().lower() not in ("", "y", "yes", "j", "ja"):
            break
    print("Setup aborted.")
    return 1


def check_saved(env_file):
    env = load_env(env_file)
    if not all(env.get(k) for k in ("NEXTCLOUD_URL", "NEXTCLOUD_USER", "NEXTCLOUD_TOKEN")):
        print("NOT_CONFIGURED - run: python3 setup.py")
        return 1
    ok, info = test_connection(env["NEXTCLOUD_URL"], env["NEXTCLOUD_USER"], env["NEXTCLOUD_TOKEN"],
                               env.get("NEXTCLOUD_ALLOW_HTTP") == "1")
    if ok:
        print(f"AUTHENTICATED as {info['display_name'] or env['NEXTCLOUD_USER']} "
              f"(user id {info['user_id']}, timezone {env.get('NEXTCLOUD_TIMEZONE', 'UTC')})")
        if info["user_id"] != env.get("NEXTCLOUD_USER_ID"):
            print(f"Note: user id differs from the saved one; run setup again to fix.")
        return 0
    print(f"NOT_AUTHENTICATED - {info}")
    return 1


def main():
    p = argparse.ArgumentParser(description="Nextcloud setup for Hermes Agent")
    p.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    p.add_argument("--check", "--test", action="store_true", help="Test the saved credentials")
    p.add_argument("--url"); p.add_argument("--user"); p.add_argument("--timezone")
    p.add_argument("--token-stdin", action="store_true", help="Read the app password from stdin")
    p.add_argument("--token", help=argparse.SUPPRESS)  # kept for compatibility; discouraged
    p.add_argument("--allow-http", action="store_true", help="Allow plain http:// (not recommended)")
    p.add_argument("--overwrite", action="store_true", help="Do not ask before replacing saved credentials")
    a = p.parse_args()

    if a.check:
        return check_saved(a.env_file)

    existing = load_env(a.env_file)
    configured = all(existing.get(k) for k in ("NEXTCLOUD_URL", "NEXTCLOUD_USER", "NEXTCLOUD_TOKEN"))
    non_interactive = a.url and a.user and (a.token_stdin or a.token or os.environ.get("NEXTCLOUD_TOKEN"))

    if configured and not a.overwrite:
        if non_interactive:
            print("Credentials already configured. Use --overwrite to replace them.")
            return 1
        print(f"Credentials already configured for {existing['NEXTCLOUD_USER']} at {existing['NEXTCLOUD_URL']}.")
        if input("Overwrite? [y/N]: ").strip().lower() not in ("y", "yes", "j", "ja"):
            print("Aborted.")
            return 0

    if not non_interactive:
        return interactive(a.env_file, a.allow_http)

    if a.token:
        print("Warning: --token puts the password in your shell history and process list. "
              "Prefer --token-stdin.", file=sys.stderr)
    token = (sys.stdin.readline().strip() if a.token_stdin else (a.token or os.environ["NEXTCLOUD_TOKEN"]))
    url = a.url.rstrip("/")
    tz = a.timezone or detect_timezone()
    try:
        for name, v in (("URL", url), ("User", a.user), ("Token", token), ("Timezone", tz)):
            validate_value(name, v)
        _zone(tz)
    except NCError as e:
        print(f"✗ {e}")
        return 1
    print(f"Testing connection to {url}...")
    ok, info = test_connection(url, a.user, token, a.allow_http)
    if not ok:
        print(f"✗ Connection failed: {info}")
        return 1
    print(f"✓ Connected (user id: {info['user_id']})")
    save_env(a.env_file, {"NEXTCLOUD_URL": url, "NEXTCLOUD_USER": a.user, "NEXTCLOUD_TOKEN": token,
                          "NEXTCLOUD_USER_ID": info["user_id"] if info["user_id"] != a.user else "",
                          "NEXTCLOUD_TIMEZONE": tz, "NEXTCLOUD_ALLOW_HTTP": "1" if a.allow_http else ""})
    return 0


if __name__ == "__main__":
    sys.exit(main())
