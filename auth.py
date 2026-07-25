import json
import os
import re
import shutil
import time
import urllib.request
import urllib.parse
from typing import Optional

TOKEN_PATH = os.path.expanduser('~/.gemini/antigravity-cli/antigravity-oauth-token')
GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token'

CLIENT_ID = '1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com'
SECRET_FILE_PATH = os.path.expanduser('~/.aside-antigravity-proxy.secret')

_secret_cache: Optional[str] = None


def _find_agy() -> Optional[str]:
    path = shutil.which('agy') or os.path.expanduser('~/.local/bin/agy')
    return path if os.path.isfile(path) else None


def _secret_from_agy() -> Optional[str]:
    """Read the OAuth client secret out of the installed Antigravity CLI.

    This proxy already borrows agy's login, so it borrows agy's client identity
    too. Reading the value from the local binary at runtime keeps someone else's
    credential out of this repository — nothing to publish, nothing to rotate here.
    """
    agy = _find_agy()
    if not agy:
        return None
    try:
        with open(agy, 'rb') as f:
            # Google client secrets are GOCSPX- plus exactly 28 characters. An
            # open-ended match runs straight into whatever the binary stores next.
            match = re.search(rb'GOCSPX-[A-Za-z0-9_-]{28}', f.read())
        return match.group().decode() if match else None
    except Exception:
        return None


def _load_client_secret() -> str:
    global _secret_cache
    if _secret_cache:
        return _secret_cache

    # ponytail: env > secret file > read it back out of agy
    secret = os.getenv('ANTIGRAVITY_CLIENT_SECRET', '').strip()
    if not secret and os.path.exists(SECRET_FILE_PATH):
        try:
            with open(SECRET_FILE_PATH, 'r', encoding='utf-8') as f:
                secret = f.read().strip()
        except Exception:
            secret = ''
    if not secret:
        secret = _secret_from_agy() or ''
    if not secret:
        raise RuntimeError(
            "OAuth client secret not found. Install the Antigravity CLI (agy) and log in, "
            "or set ANTIGRAVITY_CLIENT_SECRET."
        )

    _secret_cache = secret
    return secret


class TokenManager:
    def __init__(self, token_path: str = TOKEN_PATH):
        self.token_path = token_path
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._expires_at: float = 0.0

    def _load_from_file(self):
        if not os.path.exists(self.token_path):
            raise FileNotFoundError(f"OAuth token file not found at {self.token_path}")
        with open(self.token_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        tok = data.get('token', {})
        self._access_token = tok.get('access_token')
        self._refresh_token = tok.get('refresh_token')

    def refresh(self) -> str:
        self._load_from_file()
        if not self._refresh_token:
            raise ValueError("No refresh token available in antigravity-oauth-token")

        params = {
            'client_id': CLIENT_ID,
            'client_secret': _load_client_secret(),
            'refresh_token': self._refresh_token,
            'grant_type': 'refresh_token'
        }
        req = urllib.request.Request(
            GOOGLE_TOKEN_URL,
            data=urllib.parse.urlencode(params).encode('utf-8'),
            headers={'Content-Type': 'application/x-www-form-urlencoded'}
        )
        with urllib.request.urlopen(req) as resp:
            res = json.loads(resp.read().decode('utf-8'))
            self._access_token = res['access_token']
            expires_in = res.get('expires_in', 3600)
            self._expires_at = time.time() + expires_in - 60  # 1-minute buffer
            return self._access_token

    def get_access_token(self, force_refresh: bool = False) -> str:
        if force_refresh or not self._access_token or time.time() >= self._expires_at:
            return self.refresh()
        return self._access_token
