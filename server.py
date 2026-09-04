# server.py — локальный сервер для игры "Pinterest Chase"
# Написан на чистом asyncio (как и присланный server.py) — без внешних зависимостей.
# Запуск: python server.py   (или через start_server.bat)

import argparse
import asyncio
import base64
import contextlib
import ipaddress
import json
import mimetypes
import os
import random
import re
import secrets
import socket
import string
import time
import urllib.parse
import urllib.request
import html as html_lib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORDS_FILE = ROOT / "words.json"
SOCIAL_FILE = ROOT / "social_state.json"
PUBLIC_ID_MAX_LENGTH = 32
AVATAR_MAX_LENGTH = 24000
MAX_HEADER_BYTES = 32_768
MAX_BODY_BYTES = 128_000
MAX_QUERY_BYTES = 2_048
MAX_STATIC_FILE_BYTES = 8_000_000
MAX_CONCURRENT_CONNECTIONS = 64
RATE_WINDOW_SECONDS = 10
RATE_MAX_REQUESTS = 90
STRICT_ALLOWED_ORIGINS = {
    "null",
    "http://localhost:8787",
    "http://127.0.0.1:8787",
}
# Бэкенд может стоять отдельно от фронта (например фронт на GitHub Pages,
# а server.py на Render). Тогда браузер шлёт Origin вида
# https://zxchm0nya.github.io — его надо разрешить для CORS, иначе fetch
# с Pages до бэкенда будет заблокирован.
# Дополнительные origins можно задать через env ALLOWED_ORIGINS
# (список через запятую), напр.: ALLOWED_ORIGINS=https://zxchm0nya.github.io/Pinterest-Chase,https://example.com
EXTRA_ALLOWED_ORIGINS = {
    origin.strip()
    for origin in os.environ.get("ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
}
ALLOWED_VPN_NETWORKS = (
    ipaddress.ip_network("26.0.0.0/8"),
)
ALLOWED_STATIC_SUFFIXES = {
    ".html", ".css", ".js", ".png", ".jpg", ".jpeg", ".webp", ".gif",
    ".svg", ".ico", ".mp3", ".wav", ".ogg", ".m4a",
}
BLOCKED_STATIC_NAMES = {
    "social_state.json",
    "last_lan_host.txt",
    "server.py",
    "connect_lan.py",
    "assign_public_id.py",
    "assign_public_id.bat",
}
ALLOWED_STATIC_NAMES = {
}
request_buckets = {}

DEFAULT_WORDS = ["камень", "дерево", "роналду", "электростанция", "зубочистка", "ложка"]


def load_words():
    try:
        data = json.loads(WORDS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list) and data:
            return [str(w) for w in data]
    except Exception:
        pass
    return DEFAULT_WORDS


WORDS = load_words()


def gen_id():
    return secrets.token_hex(6)


def gen_public_id():
    return "".join(secrets.choice(string.digits) for _ in range(12))


def clean_public_id(value):
    value = str(value or "").strip()
    value = re.sub(r"[\r\n\t<>]", "", value)
    value = re.sub(r"\s+", "", value)
    return value[:PUBLIC_ID_MAX_LENGTH]


def clean_avatar(value):
    value = str(value or "").strip()
    if not value or len(value) > AVATAR_MAX_LENGTH:
        return ""
    header, separator, payload = value.partition(",")
    if not separator or not payload:
        return ""
    if header.lower() not in {
        "data:image/png;base64",
        "data:image/jpeg;base64",
        "data:image/jpg;base64",
        "data:image/webp;base64",
    }:
        return ""
    try:
        base64.b64decode(payload, validate=True)
    except Exception:
        return ""
    return value


class HttpRequestError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def parse_header_map(header_text):
    headers = {}
    for line in header_text.split("\r\n")[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    return headers


def is_allowed_origin(origin):
    if not origin:
        return False
    if origin in STRICT_ALLOWED_ORIGINS or origin in EXTRA_ALLOWED_ORIGINS:
        return True
    parsed = urllib.parse.urlparse(origin)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https"):
        return False
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    # Любой проект на GitHub Pages: https://<user>.github.io
    if parsed.scheme == "https" and (host.endswith(".github.io") or host == "github.io"):
        return True
    with contextlib.suppress(ValueError):
        ip = ipaddress.ip_address(host)
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or any(ip in network for network in ALLOWED_VPN_NETWORKS)
        )
    return False


def cors_origin(origin):
    return origin if is_allowed_origin(origin) else "null"


def client_ip_from_writer(writer):
    peer = writer.get_extra_info("peername")
    if isinstance(peer, tuple) and peer:
        return str(peer[0])
    return "unknown"


def rate_limited(ip):
    now = time.monotonic()
    bucket = request_buckets.setdefault(ip, [])
    cutoff = now - RATE_WINDOW_SECONDS
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    bucket.append(now)
    if len(request_buckets) > 512:
        for key in list(request_buckets.keys()):
            values = request_buckets.get(key) or []
            if not values or values[-1] < cutoff:
                request_buckets.pop(key, None)
    return len(bucket) > RATE_MAX_REQUESTS


def gen_lobby_code():
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(5))


def pick_pair():
    a = random.choice(WORDS)
    b = random.choice(WORDS)
    tries = 0
    while b == a and tries < 20:
        b = random.choice(WORDS)
        tries += 1
    return a, b


def clean_game_mode(value):
    return "wikipedia" if str(value or "").strip().lower() == "wikipedia" else "pinterest"


class GameServer:
    def __init__(self):
        self.lobbies = {}              # lobbyId -> lobby dict
        self.clients_by_nickname = {}  # nickname.lower() -> id
        self.invites = {}              # id -> [ {lobbyId, from} ]
        self.clients = {}              # id -> {nickname, lastSeen}
        self.friends = {}              # id -> set(friend ids)
        self.friend_requests = {}      # id -> [ {fromId, fromNickname} ]
        self.social_mtime = 0
        self.public_id_guard = {}
        self.load_social()

    def social_file_mtime(self):
        with contextlib.suppress(Exception):
            return SOCIAL_FILE.stat().st_mtime
        return 0

    def remember_public_ids(self):
        self.public_id_guard = {
            player_id: clean_public_id(client.get("publicId"))
            for player_id, client in self.clients.items()
            if clean_public_id(client.get("publicId"))
        }

    def load_social(self):
        try:
            data = json.loads(SOCIAL_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        self.clients = data.get("clients") or {}
        self.friends = {
            player_id: set(friend_ids)
            for player_id, friend_ids in (data.get("friends") or {}).items()
        }
        self.friend_requests = data.get("friendRequests") or {}
        self.clients_by_nickname = {}
        for player_id, client in self.clients.items():
            nickname = str(client.get("nickname") or "").strip()
            if nickname:
                self.clients_by_nickname[nickname.lower()] = player_id
            public_id = clean_public_id(client.get("publicId"))
            if public_id:
                client["publicId"] = public_id
            client["avatar"] = clean_avatar(client.get("avatar"))
        self.social_mtime = self.social_file_mtime()
        self.remember_public_ids()

    def save_social(self):
        data = {
            "clients": self.clients,
            "friends": {player_id: sorted(friend_ids) for player_id, friend_ids in self.friends.items()},
            "friendRequests": self.friend_requests,
        }
        with contextlib.suppress(Exception):
            SOCIAL_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.social_mtime = self.social_file_mtime()
        self.remember_public_ids()

    def audit_social_file(self):
        mtime = self.social_file_mtime()
        if not mtime or mtime <= self.social_mtime:
            return
        try:
            data = json.loads(SOCIAL_FILE.read_text(encoding="utf-8"))
        except Exception:
            self.social_mtime = mtime
            return

        disk_clients = data.get("clients") or {}
        disk_friends = {
            player_id: set(friend_ids)
            for player_id, friend_ids in (data.get("friends") or {}).items()
        }
        disk_requests = data.get("friendRequests") or {}
        used = {}
        changed = False
        current_counts = {}
        for client in disk_clients.values():
            current_public_id = clean_public_id(client.get("publicId"))
            if current_public_id:
                key = current_public_id.lower()
                current_counts[key] = current_counts.get(key, 0) + 1

        for player_id, client in disk_clients.items():
            raw_public_id = str(client.get("publicId") or "")
            current_public_id = clean_public_id(raw_public_id)
            previous_public_id = self.public_id_guard.get(player_id) or clean_public_id(
                self.clients.get(player_id, {}).get("publicId")
            )
            if current_public_id and current_public_id != raw_public_id:
                client["publicId"] = current_public_id
                changed = True
            if not current_public_id:
                current_public_id = previous_public_id or self.create_public_id(player_id, client.get("nickname"))
                client["publicId"] = current_public_id
                changed = True

            public_id_key = current_public_id.lower()
            if (
                current_counts.get(public_id_key, 0) > 1
                and previous_public_id
                and previous_public_id.lower() != public_id_key
            ):
                current_public_id = previous_public_id
                client["publicId"] = current_public_id
                public_id_key = current_public_id.lower()
                changed = True

            owner = used.get(public_id_key)
            if owner and owner != player_id:
                fallback = previous_public_id
                if not fallback or fallback.lower() in used:
                    fallback = self.create_public_id(player_id, client.get("nickname"))
                    while fallback.lower() in used:
                        fallback = gen_public_id()
                client["publicId"] = fallback
                current_public_id = fallback
                changed = True
                public_id_key = current_public_id.lower()

            used[public_id_key] = player_id

        self.clients = disk_clients
        self.friends = disk_friends
        self.friend_requests = disk_requests
        self.clients_by_nickname = {}
        for player_id, client in self.clients.items():
            nickname = str(client.get("nickname") or "").strip()
            if nickname:
                self.clients_by_nickname[nickname.lower()] = player_id

        if changed:
            self.save_social()
        else:
            self.social_mtime = mtime
            self.remember_public_ids()

    def create_public_id(self, player_id, nickname):
        taken = {
            clean_public_id(client.get("publicId")).lower()
            for existing_id, client in self.clients.items()
            if existing_id != player_id and client.get("publicId")
        }
        preferred_ids = {
            "zxchmonya": "777777777777",
            "том": "148814881488",
        }
        preferred = clean_public_id(preferred_ids.get(str(nickname or "").strip().lower(), ""))
        if preferred and preferred.lower() not in taken:
            return preferred
        public_id = gen_public_id()
        while public_id.lower() in taken:
            public_id = gen_public_id()
        return public_id

    def touch(self, player_id, nickname=None):
        if not player_id:
            return
        self.audit_social_file()
        client = self.clients.setdefault(player_id, {"nickname": "", "lastSeen": 0})
        changed = False
        if nickname:
            nickname = str(nickname).strip()[:24]
            if client.get("nickname") != nickname:
                client["nickname"] = nickname
                changed = True
            self.clients_by_nickname[nickname.lower()] = player_id
        if not client.get("publicId"):
            client["publicId"] = self.create_public_id(player_id, client.get("nickname"))
            changed = True
        client["lastSeen"] = time.time()
        if changed:
            self.save_social()

    def is_online(self, player_id):
        client = self.clients.get(player_id)
        return bool(client and time.time() - client.get("lastSeen", 0) < 20)

    def lobby_public(self, lobby):
        return {
            "lobbyId": lobby["lobbyId"],
            "hostId": lobby["hostId"],
            "status": lobby["status"],
            "gameMode": lobby.get("gameMode", "pinterest"),
            "from": None if lobby["status"] == "lobby" else lobby["from"],
            "to": None if lobby["status"] == "lobby" else lobby["to"],
            "startTime": lobby["startTime"],
            "serverTime": int(time.time() * 1000),
            "members": [
                {
                    "id": player_id,
                    "nickname": m["nickname"],
                    "publicId": self.clients.get(player_id, {}).get("publicId") or "",
                    "avatar": self.clients.get(player_id, {}).get("avatar") or "",
                    "finished": m["finished"],
                    "surrendered": bool(m.get("surrendered")),
                    "time": m["time"],
                }
                for player_id, m in lobby["members"].items()
            ],
        }

    def register(self, payload):
        nickname = str(payload.get("nickname") or "").strip()[:24]
        if not nickname:
            nickname = "Игрок" + str(random.randint(0, 999))
        player_id = self.clients_by_nickname.get(nickname.lower()) or gen_id()
        self.touch(player_id, nickname)
        client = self.clients.get(player_id, {})
        return 200, {"id": player_id, "nickname": nickname, "publicId": client.get("publicId") or "", "avatar": client.get("avatar") or ""}

    def presence(self, payload):
        player_id = payload.get("id")
        self.touch(player_id, payload.get("nickname"))
        client = self.clients.get(player_id, {})
        return 200, {"ok": True, "publicId": client.get("publicId") or "", "avatar": client.get("avatar") or ""}

    def create_lobby(self, payload):
        player_id = payload.get("id")
        nickname = payload.get("nickname")
        game_mode = clean_game_mode(payload.get("gameMode"))
        self.touch(player_id, nickname)
        lobby_id = gen_lobby_code()
        while lobby_id in self.lobbies:
            lobby_id = gen_lobby_code()
        lobby = {
            "lobbyId": lobby_id,
            "hostId": player_id,
            "status": "lobby",
            "gameMode": game_mode,
            "from": None,
            "to": None,
            "startTime": None,
            "members": {player_id: {"nickname": nickname, "finished": False, "surrendered": False, "time": None}},
        }
        self.lobbies[lobby_id] = lobby
        return 200, self.lobby_public(lobby)

    def join_lobby(self, payload):
        lobby = self.lobbies.get(payload.get("lobbyId"))
        if not lobby:
            return 404, {"error": "Лобби не найдено"}
        self.touch(payload.get("id"), payload.get("nickname"))
        lobby["members"][payload.get("id")] = {
            "nickname": payload.get("nickname"), "finished": False, "surrendered": False, "time": None,
        }
        return 200, self.lobby_public(lobby)

    def invite(self, payload):
        target_id = str(payload.get("targetId") or "").strip()
        target_nick = str(payload.get("targetNickname") or "").strip().lower()
        if not target_id:
            target_id = self.clients_by_nickname.get(target_nick)
        if not target_id:
            return 404, {"error": "Игрок с таким ником не в сети"}
        if target_id not in self.clients:
            return 404, {"error": "Игрок не найден"}
        self.invites.setdefault(target_id, []).append(
            {"lobbyId": payload.get("lobbyId"), "from": payload.get("fromNickname")}
        )
        return 200, {"ok": True}

    def pop_invites(self, player_id):
        items = self.invites.get(player_id, [])
        self.invites[player_id] = []
        return 200, {"invites": items}

    def clear_invite(self, payload):
        player_id = payload.get("id")
        lobby_id = payload.get("lobbyId")
        items = self.invites.get(player_id, [])
        self.invites[player_id] = [item for item in items if item.get("lobbyId") != lobby_id]
        return 200, {"ok": True}

    def notifications(self, player_id):
        return 200, {
            "invites": self.invites.get(player_id, []),
            "friendRequests": self.friend_requests.get(player_id, []),
        }

    def friends_list(self, player_id):
        friends = []
        for friend_id in sorted(self.friends.get(player_id, set())):
            client = self.clients.get(friend_id, {})
            friends.append({
                "id": friend_id,
                "nickname": client.get("nickname") or "Игрок",
                "publicId": client.get("publicId") or "",
                "avatar": client.get("avatar") or "",
                "online": self.is_online(friend_id),
            })
        return 200, {"friends": friends}

    def set_avatar(self, payload):
        player_id = payload.get("id")
        if not player_id:
            return 400, {"error": "Player is not connected"}
        self.touch(player_id, payload.get("nickname"))
        avatar = clean_avatar(payload.get("avatar"))
        client = self.clients.setdefault(player_id, {"nickname": "", "lastSeen": 0})
        client["avatar"] = avatar
        self.save_social()
        return 200, {"ok": True, "avatar": avatar}

    def friend_request(self, payload):
        from_id = payload.get("fromId")
        from_nickname = str(payload.get("fromNickname") or "").strip()[:24]
        target_id = payload.get("targetId")
        target_nick = str(payload.get("targetNickname") or "").strip().lower()
        if not target_id and target_nick:
            target_id = self.clients_by_nickname.get(target_nick)
        if not target_id and target_nick:
            for client_id, client in self.clients.items():
                if clean_public_id(client.get("publicId")).lower() == target_nick:
                    target_id = client_id
                    break
        if not from_id or not target_id:
            return 404, {"error": "Игрок не найден"}
        if from_id == target_id:
            return 400, {"error": "Нельзя добавить себя"}
        self.touch(from_id, from_nickname)
        if target_id in self.friends.get(from_id, set()):
            return 200, {"ok": True, "message": "Вы уже друзья"}
        requests = self.friend_requests.setdefault(target_id, [])
        if not any(item.get("fromId") == from_id for item in requests):
            requests.append({"fromId": from_id, "fromNickname": from_nickname})
            self.save_social()
        return 200, {"ok": True}

    def accept_friend(self, payload):
        player_id = payload.get("id")
        from_id = payload.get("fromId")
        if not player_id or not from_id:
            return 400, {"error": "Некорректная заявка"}
        self.friends.setdefault(player_id, set()).add(from_id)
        self.friends.setdefault(from_id, set()).add(player_id)
        requests = self.friend_requests.get(player_id, [])
        self.friend_requests[player_id] = [item for item in requests if item.get("fromId") != from_id]
        self.save_social()
        return 200, {"ok": True}

    def decline_friend(self, payload):
        player_id = payload.get("id")
        from_id = payload.get("fromId")
        requests = self.friend_requests.get(player_id, [])
        self.friend_requests[player_id] = [item for item in requests if item.get("fromId") != from_id]
        self.save_social()
        return 200, {"ok": True}

    def remove_friend(self, payload):
        player_id = payload.get("id")
        friend_id = payload.get("friendId")
        if not player_id or not friend_id:
            return 400, {"error": "Некорректный друг"}
        self.friends.setdefault(player_id, set()).discard(friend_id)
        self.friends.setdefault(friend_id, set()).discard(player_id)
        self.save_social()
        return 200, {"ok": True}

    def start_round(self, payload):
        lobby = self.lobbies.get(payload.get("lobbyId"))
        if not lobby:
            return 404, {"error": "Лобби не найдено"}
        if lobby["hostId"] != payload.get("hostId"):
            return 403, {"error": "Только хост может начать раунд"}
        a, b = pick_pair()
        lobby["gameMode"] = clean_game_mode(payload.get("gameMode") or lobby.get("gameMode"))
        lobby["from"], lobby["to"] = a, b
        lobby["status"] = "running"
        lobby["startTime"] = int(time.time() * 1000)
        for m in lobby["members"].values():
            m["finished"] = False
            m["surrendered"] = False
            m["time"] = None
        return 200, self.lobby_public(lobby)

    def win(self, payload):
        lobby = self.lobbies.get(payload.get("lobbyId"))
        if not lobby or lobby["status"] != "running":
            return 400, {"error": "Раунд сейчас не идёт"}
        member = lobby["members"].get(payload.get("id"))
        if not member:
            return 400, {"error": "Игрок не в лобби"}
        if member["finished"]:
            return 200, self.lobby_public(lobby)
        member["finished"] = True
        member["surrendered"] = False
        member["time"] = int(time.time() * 1000) - lobby["startTime"]
        return 200, self.lobby_public(lobby)

    def surrender(self, payload):
        lobby = self.lobbies.get(payload.get("lobbyId"))
        if not lobby or lobby["status"] != "running":
            return 400, {"error": "Раунд сейчас не идёт"}
        member = lobby["members"].get(payload.get("id"))
        if not member:
            return 400, {"error": "Игрок не в лобби"}
        if member["finished"]:
            return 200, self.lobby_public(lobby)
        member["finished"] = True
        member["surrendered"] = True
        member["time"] = int(time.time() * 1000) - lobby["startTime"]
        return 200, self.lobby_public(lobby)

    def leave_lobby(self, payload):
        lobby_id = payload.get("lobbyId")
        player_id = payload.get("id")
        lobby = self.lobbies.get(lobby_id)
        if not lobby:
            return 200, {"ok": True}
        lobby["members"].pop(player_id, None)
        if not lobby["members"]:
            self.lobbies.pop(lobby_id, None)
            return 200, {"ok": True}
        if lobby["hostId"] == player_id:
            lobby["hostId"] = next(iter(lobby["members"]))
        return 200, self.lobby_public(lobby)

    def reset_round(self, payload):
        lobby = self.lobbies.get(payload.get("lobbyId"))
        if not lobby:
            return 404, {"error": "Лобби не найдено"}
        if lobby["hostId"] != payload.get("hostId"):
            return 403, {"error": "Только хост может сбросить раунд"}
        lobby["status"] = "lobby"
        lobby["gameMode"] = clean_game_mode(lobby.get("gameMode"))
        lobby["from"] = None
        lobby["to"] = None
        lobby["startTime"] = None
        for m in lobby["members"].values():
            m["finished"] = False
            m["surrendered"] = False
            m["time"] = None
        return 200, self.lobby_public(lobby)

    def state(self, lobby_id):
        lobby = self.lobbies.get(lobby_id)
        if not lobby:
            return 404, {"error": "Лобби не найдено"}
        return 200, self.lobby_public(lobby)


def http_response(status, body=b"", content_type="text/plain; charset=utf-8", origin=None, extra_headers=None):
    reason = {
        200: "OK", 204: "No Content", 400: "Bad Request", 401: "Unauthorized",
        403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
        409: "Conflict", 413: "Payload Too Large", 429: "Too Many Requests",
        500: "Server Error", 502: "Bad Gateway",
    }.get(status, "OK")
    headers = [
        f"HTTP/1.1 {status} {reason}",
        f"Content-Type: {content_type}",
        f"Content-Length: {len(body)}",
        "Connection: close",
        "Cache-Control: no-store",
        f"Access-Control-Allow-Origin: {cors_origin(origin)}",
        "Access-Control-Allow-Methods: GET, POST, OPTIONS",
        "Access-Control-Allow-Headers: Content-Type",
        "Vary: Origin",
        "X-Content-Type-Options: nosniff",
        "Referrer-Policy: no-referrer",
        "X-Frame-Options: SAMEORIGIN",
        "Permissions-Policy: camera=(), microphone=(), geolocation=()",
        "", "",
    ]
    if extra_headers:
        headers[-2:-2] = extra_headers
    return "\r\n".join(headers).encode("utf-8") + body


def json_response(status, payload, origin=None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return http_response(status, body, "application/json; charset=utf-8", origin=origin)


async def read_http_request(reader):
    data = b""
    while b"\r\n\r\n" not in data and len(data) <= MAX_HEADER_BYTES:
        chunk = await asyncio.wait_for(reader.read(4096), timeout=5)
        if not chunk:
            break
        data += chunk
    if len(data) > MAX_HEADER_BYTES:
        raise HttpRequestError(413, "Request header is too large")
    header, _, body = data.partition(b"\r\n\r\n")
    if not header:
        return header, body
    header_text = header.decode("utf-8", errors="ignore")
    content_length = 0
    headers = parse_header_map(header_text)
    if "content-length" in headers:
        with contextlib.suppress(ValueError):
            content_length = max(0, int(headers["content-length"]))
    if content_length > MAX_BODY_BYTES:
        raise HttpRequestError(413, "Request body is too large")
    while len(body) < content_length and len(body) <= MAX_BODY_BYTES:
        chunk = await asyncio.wait_for(reader.read(min(4096, content_length - len(body))), timeout=5)
        if not chunk:
            break
        body += chunk
    if len(body) > MAX_BODY_BYTES:
        raise HttpRequestError(413, "Request body is too large")
    return header, body


async def serve_static(writer, path, origin=None):
    if path == "/":
        path = "/index.html"
    relative = urllib.parse.unquote(path.lstrip("/"))
    if "\x00" in relative or "://" in relative:
        writer.write(http_response(400, b"Bad Request", origin=origin))
        await writer.drain()
        return
    target = (ROOT / relative).resolve()
    if ROOT not in target.parents and target != ROOT:
        writer.write(http_response(403, b"Forbidden", origin=origin))
        await writer.drain()
        return
    if target.name in BLOCKED_STATIC_NAMES or (
        target.name not in ALLOWED_STATIC_NAMES and target.suffix.lower() not in ALLOWED_STATIC_SUFFIXES
    ):
        writer.write(http_response(403, b"Forbidden", origin=origin))
        await writer.drain()
        return
    if not target.exists() or not target.is_file():
        writer.write(http_response(404, b"Not Found", origin=origin))
        await writer.drain()
        return
    if target.stat().st_size > MAX_STATIC_FILE_BYTES:
        writer.write(http_response(413, b"File is too large", origin=origin))
        await writer.drain()
        return
    body = target.read_bytes()
    content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
        content_type += "; charset=utf-8"
    writer.write(http_response(200, body, content_type, origin=origin))
    await writer.drain()


async def serve_pinterest_proxy(writer, query):
    raw_url = (query.get("url") or [""])[0]
    parsed = urllib.parse.urlparse(raw_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or host not in ("pinterest.com", "www.pinterest.com"):
        writer.write(json_response(400, {"error": "Можно проксировать только Pinterest"}))
        await writer.drain()
        return

    request = urllib.request.Request(
        raw_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            body = response.read(4_000_000)
            content_type = response.headers.get("Content-Type", "text/html; charset=utf-8")
    except Exception as exc:
        message = f"""
<!doctype html><meta charset="utf-8">
<style>
body{{margin:0;font-family:Segoe UI,Arial,sans-serif;background:#fff8f9;color:#321015;display:grid;place-items:center;min-height:100vh;text-align:center;padding:24px}}
.box{{max-width:520px;border:1px solid #ffd4dc;border-radius:16px;padding:18px;background:#fff}}
</style>
<div class="box">
  <b>Pinterest не удалось встроить через локальный прокси.</b><br>
  Открой его в новой вкладке кнопкой ниже в игре.<br><br>
  <small>{str(exc)}</small>
</div>
"""
        writer.write(http_response(502, message.encode("utf-8"), "text/html; charset=utf-8"))
        await writer.drain()
        return

    if "text/html" in content_type:
        text = body.decode("utf-8", errors="replace")
        text = text.replace("http-equiv=\"Content-Security-Policy\"", "data-disabled-csp=\"true\"")
        base_tag = '<base href="https://www.pinterest.com/">'
        if "<head" in text.lower():
            text = text.replace("<head>", f"<head>{base_tag}", 1)
        else:
            text = base_tag + text
        body = text.encode("utf-8")
        content_type = "text/html; charset=utf-8"

    writer.write(http_response(200, body, content_type))
    await writer.drain()


def fetch_url(url, timeout=12, limit=4_000_000):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(limit), response.headers.get("Content-Type", "application/octet-stream")


def pinterest_search_url(query_text):
    return "https://www.pinterest.com/search/pins/?q=" + urllib.parse.quote(query_text)


def pinterest_resource_url(query_text):
    source_url = "/search/pins/?q=" + urllib.parse.quote(query_text)
    data = {
        "options": {
            "query": query_text,
            "scope": "pins",
            "page_size": 50,
        },
        "context": {},
    }
    return (
        "https://www.pinterest.com/resource/BaseSearchResource/get/?source_url="
        + urllib.parse.quote(source_url, safe="")
        + "&data="
        + urllib.parse.quote(json.dumps(data, ensure_ascii=False), safe="")
    )


def extract_pin_images(text):
    text = (
        text
        .replace("\\u002F", "/")
        .replace("\\/", "/")
        .replace("\\u0026", "&")
        .replace("&amp;", "&")
    )
    text = text + " " + urllib.parse.unquote(text)
    found = []
    seen = set()
    patterns = [
        r'https?://i\.pinimg\.com/[^"\'<>\s\\)]+',
        r'//i\.pinimg\.com/[^"\'<>\s\\)]+',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            url = match.group(0)
            if url.startswith("//"):
                url = "https:" + url
            url = html_lib.unescape(url)
            url = url.split("\\")[0]
            if not re.search(r'\.(jpg|jpeg|png|webp)(\?|$)', url, re.IGNORECASE):
                continue
            if url in seen:
                continue
            seen.add(url)
            found.append(url)
            if len(found) >= 60:
                return found
    return found


async def serve_pinterest_image(writer, query):
    raw_url = (query.get("url") or [""])[0]
    parsed = urllib.parse.urlparse(raw_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or host != "i.pinimg.com":
        writer.write(json_response(400, {"error": "Можно загружать только картинки Pinterest"}))
        await writer.drain()
        return
    try:
        body, content_type = fetch_url(raw_url, timeout=10, limit=6_000_000)
    except Exception as exc:
        writer.write(json_response(502, {"error": str(exc)}))
        await writer.drain()
        return
    writer.write(http_response(200, body, content_type))
    await writer.drain()


async def serve_pinterest_embed(writer, query):
    search = str((query.get("q") or [""])[0]).strip()
    target = str((query.get("target") or [""])[0]).strip()
    if not search:
        writer.write(http_response(400, b"Missing search", "text/plain; charset=utf-8"))
        await writer.drain()
        return
    try:
        body, _ = fetch_url(pinterest_search_url(search), timeout=12)
        images = extract_pin_images(body.decode("utf-8", errors="replace"))
        if not images:
            body, _ = fetch_url(pinterest_resource_url(search), timeout=12)
            images = extract_pin_images(body.decode("utf-8", errors="replace"))
    except Exception as exc:
        images = []
        error = str(exc)
    else:
        error = ""

    cards = "".join(
        f'<button class="pin" data-src="{html_lib.escape(src)}">'
        f'<img loading="lazy" referrerpolicy="no-referrer" src="{html_lib.escape(src)}" '
        f'data-proxy="/pinterest/image?url={urllib.parse.quote(src, safe="")}" alt="">'
        f'</button>'
        for src in images
    )
    empty = ""
    if not images:
        empty = f'<div class="empty">Не удалось быстро получить картинки Pinterest.<br><small>{html_lib.escape(error)}</small></div>'
    if not images:
        detail = html_lib.escape(error or "Pinterest не отдал картинки в быстрой выдаче")
        empty = f'<div class="empty"><b>Картинки не пришли</b><br>Открой полный Pinterest кнопкой снизу, игра останется открытой.<br><small>{detail}</small></div>'
    target_html = html_lib.escape(target)
    search_html = html_lib.escape(search)
    page = f"""<!doctype html>
<html lang="ru">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  *{{box-sizing:border-box}}
  body{{margin:0;font-family:Segoe UI,Arial,sans-serif;background:#fff;color:#211316}}
  header{{position:sticky;top:0;z-index:2;display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 14px;background:rgba(255,255,255,.94);border-bottom:1px solid #f1d4d9;backdrop-filter:blur(10px)}}
  .title{{font-weight:800;color:#e60023}}
  .hint{{font-size:12px;color:#7a6368}}
  .grid{{columns:4 150px;column-gap:12px;padding:12px}}
  .pin{{display:block;width:100%;min-height:150px;break-inside:avoid;margin:0 0 12px;padding:0;border:0;border-radius:14px;overflow:hidden;background:linear-gradient(135deg,#fff1f3,#f6f1f2);cursor:pointer;box-shadow:0 1px 0 rgba(0,0,0,.05)}}
  .pin:hover{{outline:3px solid rgba(230,0,35,.2)}}
  .pin img{{display:block;width:100%;height:auto}}
  .empty{{padding:40px 18px;text-align:center;color:#6f4b51;line-height:1.55}}
  .found{{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);border:0;border-radius:999px;background:#e60023;color:#fff;font-weight:800;padding:12px 18px;box-shadow:0 10px 28px rgba(230,0,35,.28);display:none}}
  .found.show{{display:block}}
</style>
<header>
  <div><div class="title">Pinterest Lite</div><div class="hint">Поиск: <b>{search_html}</b> → цель: <b>{target_html}</b></div></div>
  <div class="hint">Нажми картинку, если нашёл</div>
</header>
{empty}
<main class="grid">{cards}</main>
<div id="late-empty" class="empty" style="display:none"><b>Картинки не загрузились</b><br>Попробуй полный Pinterest кнопкой снизу, игра останется открытой.</div>
<button id="found" class="found">Я нашёл</button>
<script>
  const found = document.getElementById('found');
  document.querySelectorAll('.pin img').forEach((img) => {{
    img.addEventListener('error', () => {{
      if (img.dataset.proxy && img.src !== location.origin + img.dataset.proxy) {{
        img.src = img.dataset.proxy;
        img.dataset.proxy = '';
      }} else {{
        img.closest('.pin').style.display = 'none';
      }}
    }});
  }});
  setTimeout(() => {{
    const visible = Array.from(document.querySelectorAll('.pin img')).some((img) => img.naturalWidth > 20);
    if (!visible && document.querySelectorAll('.pin').length) {{
      document.getElementById('late-empty').style.display = 'block';
    }}
  }}, 5000);
  document.querySelectorAll('.pin').forEach((pin) => {{
    pin.addEventListener('click', () => {{
      found.classList.add('show');
      parent.postMessage({{ type: 'pc-lite-pin-click', src: pin.dataset.src }}, '*');
    }});
  }});
  found.addEventListener('click', () => {{
    parent.postMessage({{ type: 'pc-target-found-lite' }}, '*');
  }});
</script>
</html>"""
    writer.write(http_response(200, page.encode("utf-8"), "text/html; charset=utf-8"))
    await writer.drain()


def normalize_wiki_title(value):
    return re.sub(r"\s+", " ", str(value or "").replace("_", " ").strip()).lower()


def wikipedia_api_url(title):
    params = {
        "action": "parse",
        "format": "json",
        "redirects": "1",
        "disableeditsection": "1",
        "prop": "text|displaytitle",
        "page": title,
    }
    return "https://ru.wikipedia.org/w/api.php?" + urllib.parse.urlencode(params)


def wikipedia_search_url(title):
    params = {
        "action": "query",
        "format": "json",
        "list": "search",
        "srlimit": "1",
        "srsearch": title,
    }
    return "https://ru.wikipedia.org/w/api.php?" + urllib.parse.urlencode(params)


def wikipedia_rest_url(title):
    return "https://ru.wikipedia.org/api/rest_v1/page/html/" + urllib.parse.quote(str(title or "").replace(" ", "_"))


def wikipedia_mobile_url(title):
    return "https://ru.m.wikipedia.org/wiki/" + urllib.parse.quote(str(title or "").replace(" ", "_"))


def wikipedia_reader_url(title):
    wiki_url = "http://ru.wikipedia.org/wiki/" + urllib.parse.quote(str(title or "").replace(" ", "_"))
    return "https://r.jina.ai/http://" + wiki_url


def reader_text_to_wikipedia_html(text, target):
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"^Title:\s*", "# ", text, flags=re.IGNORECASE | re.MULTILINE)
    text = re.sub(r"^URL Source:\s*.*$", "", text, flags=re.IGNORECASE | re.MULTILINE)
    text = re.sub(r"^Markdown Content:\s*", "", text, flags=re.IGNORECASE | re.MULTILINE)

    def inline_markup(value):
        value = html_lib.escape(value)

        def replace_markdown_link(match):
            label = match.group(1)
            href = html_lib.unescape(match.group(2))
            if href.startswith("https://ru.wikipedia.org/wiki/") or href.startswith("http://ru.wikipedia.org/wiki/"):
                parsed = urllib.parse.urlparse(href)
                title = urllib.parse.unquote(parsed.path[len("/wiki/"):]).replace("_", " ")
                local = "/wiki/page?title=" + urllib.parse.quote(title)
                if target:
                    local += "&target=" + urllib.parse.quote(target)
                return f'<a href="{html_lib.escape(local)}">{label}</a>'
            return label

        return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", replace_markdown_link, value)

    parts = []
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            level = min(3, max(1, len(line) - len(line.lstrip("#"))))
            label = line.lstrip("#").strip()
            if label:
                parts.append(f"<h{level}>{inline_markup(label)}</h{level}>")
            continue
        if line.startswith(("* ", "- ")):
            parts.append(f"<p>• {inline_markup(line[2:].strip())}</p>")
            continue
        parts.append(f"<p>{inline_markup(line)}</p>")
    return "\n".join(parts)


def extract_wikipedia_mobile_html(text):
    title = ""
    title_match = re.search(r"<title[^>]*>([\s\S]*?)</title>", text, flags=re.IGNORECASE)
    if title_match:
        title = html_lib.unescape(re.sub(r"\s*[-—]\s*Wikipedia.*$", "", title_match.group(1)).strip())
    h1_match = re.search(r"<h1[^>]*>([\s\S]*?)</h1>", text, flags=re.IGNORECASE)
    if h1_match:
        title = html_lib.unescape(re.sub(r"<[^>]+>", "", h1_match.group(1)).strip()) or title

    main_match = re.search(r"<main\b[^>]*>([\s\S]*?)</main>", text, flags=re.IGNORECASE)
    if main_match:
        content = main_match.group(1)
    else:
        body_match = re.search(r"<body\b[^>]*>([\s\S]*?)</body>", text, flags=re.IGNORECASE)
        content = body_match.group(1) if body_match else text

    content = re.sub(r"<header[\s\S]*?</header>", "", content, flags=re.IGNORECASE)
    content = re.sub(r"<footer[\s\S]*?</footer>", "", content, flags=re.IGNORECASE)
    content = re.sub(r"<nav[\s\S]*?</nav>", "", content, flags=re.IGNORECASE)
    return title, content


def wiki_asset_proxy_url(url):
    return "/wiki/asset?url=" + urllib.parse.quote(url, safe="")


def rewrite_wikipedia_assets(fragment):
    fragment = re.sub(r"\s+srcset=(['\"])[\s\S]*?\1", "", fragment, flags=re.IGNORECASE)

    def replace_src(match):
        quote = match.group(1)
        src = html_lib.unescape(match.group(2))
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            src = "https://ru.wikipedia.org" + src
        parsed = urllib.parse.urlparse(src)
        host = (parsed.hostname or "").lower()
        if parsed.scheme in ("http", "https") and (
            host.endswith("wikimedia.org") or host in ("ru.wikipedia.org", "ru.m.wikipedia.org")
        ):
            return f'src={quote}{wiki_asset_proxy_url(src)}{quote}'
        return match.group(0)

    return re.sub(r'src=(["\'])([^"\']+)\1', replace_src, fragment, flags=re.IGNORECASE)


def rewrite_wikipedia_html(fragment, target):
    fragment = re.sub(r"<script[\s\S]*?</script>", "", fragment, flags=re.IGNORECASE)
    fragment = re.sub(r"<style[\s\S]*?</style>", "", fragment, flags=re.IGNORECASE)
    fragment = re.sub(r"<noscript[\s\S]*?</noscript>", "", fragment, flags=re.IGNORECASE)
    fragment = re.sub(r"\s+target=(['\"])[^'\"]*\1", "", fragment, flags=re.IGNORECASE)
    fragment = fragment.replace('src="//', 'src="https://').replace("src='//", "src='https://")
    fragment = fragment.replace('href="//', 'href="https://').replace("href='//", "href='https://")

    def replace_href(match):
        quote = match.group(1)
        href = html_lib.unescape(match.group(2))
        if href.startswith("/wiki/"):
            raw_title = href[len("/wiki/"):].split("#", 1)[0].split("?", 1)[0]
            title = urllib.parse.unquote(raw_title).replace("_", " ")
            local = "/wiki/page?title=" + urllib.parse.quote(title)
            if target:
                local += "&target=" + urllib.parse.quote(target)
            return f'href={quote}{local}{quote}'
        if href.startswith("./"):
            raw_title = href[2:].split("#", 1)[0].split("?", 1)[0]
            title = urllib.parse.unquote(raw_title).replace("_", " ")
            if title:
                local = "/wiki/page?title=" + urllib.parse.quote(title)
                if target:
                    local += "&target=" + urllib.parse.quote(target)
                return f'href={quote}{local}{quote}'
        if (
            href.startswith("https://ru.wikipedia.org/wiki/")
            or href.startswith("http://ru.wikipedia.org/wiki/")
            or href.startswith("https://ru.m.wikipedia.org/wiki/")
            or href.startswith("http://ru.m.wikipedia.org/wiki/")
        ):
            parsed = urllib.parse.urlparse(href)
            raw_title = parsed.path[len("/wiki/"):].split("#", 1)[0].split("?", 1)[0]
            title = urllib.parse.unquote(raw_title).replace("_", " ")
            local = "/wiki/page?title=" + urllib.parse.quote(title)
            if target:
                local += "&target=" + urllib.parse.quote(target)
            return f'href={quote}{local}{quote}'
        if href.startswith("/w/index.php"):
            parsed = urllib.parse.urlparse(href)
            params = urllib.parse.parse_qs(parsed.query)
            title = (params.get("title") or [""])[0]
            if title:
                local = "/wiki/page?title=" + urllib.parse.quote(urllib.parse.unquote(title).replace("_", " "))
                if target:
                    local += "&target=" + urllib.parse.quote(target)
                return f'href={quote}{local}{quote}'
        if href.startswith("/"):
            return f'href={quote}https://ru.wikipedia.org{href}{quote}'
        if href.startswith("http"):
            return f'href={quote}{href}{quote}'
        return match.group(0)

    fragment = re.sub(r'href=(["\'])([^"\']+)\1', replace_href, fragment)
    return rewrite_wikipedia_assets(fragment)


async def serve_wikipedia_asset(writer, query):
    raw_url = (query.get("url") or [""])[0]
    parsed = urllib.parse.urlparse(raw_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not (
        host.endswith("wikimedia.org") or host in ("ru.wikipedia.org", "ru.m.wikipedia.org")
    ):
        writer.write(json_response(400, {"error": "Bad Wikipedia asset"}))
        await writer.drain()
        return
    try:
        body, content_type = fetch_url(raw_url, timeout=12, limit=6_000_000)
    except Exception:
        writer.write(http_response(502, b"", "text/plain; charset=utf-8"))
        await writer.drain()
        return
    writer.write(http_response(200, body, content_type))
    await writer.drain()


async def serve_wikipedia_page(writer, query):
    title = str((query.get("title") or [""])[0]).strip()
    target = str((query.get("target") or [""])[0]).strip()
    if not title:
        writer.write(http_response(400, b"Missing title", "text/plain; charset=utf-8"))
        await writer.drain()
        return

    error = ""
    page_title = title
    content = ""
    try:
        body, _ = fetch_url(wikipedia_api_url(title), timeout=12, limit=6_000_000)
        data = json.loads(body.decode("utf-8", errors="replace"))
        if "error" in data:
            search_body, _ = fetch_url(wikipedia_search_url(title), timeout=12, limit=2_000_000)
            search_data = json.loads(search_body.decode("utf-8", errors="replace"))
            hits = (search_data.get("query") or {}).get("search") or []
            if hits:
                page_title = hits[0].get("title") or title
                body, _ = fetch_url(wikipedia_api_url(page_title), timeout=12, limit=6_000_000)
                data = json.loads(body.decode("utf-8", errors="replace"))
            else:
                raise RuntimeError(data.get("error", {}).get("info") or "Статья не найдена")
        parsed = data.get("parse") or {}
        page_title = parsed.get("title") or page_title
        content = ((parsed.get("text") or {}).get("*") or "")
    except Exception as exc:
        error = str(exc)
        for fallback_title in (page_title, title):
            if content:
                break
            try:
                body, _ = fetch_url(wikipedia_rest_url(fallback_title), timeout=12, limit=6_000_000)
                content = body.decode("utf-8", errors="replace")
                page_title = fallback_title
            except Exception as rest_exc:
                error = str(rest_exc)
            if content:
                break
            try:
                body, _ = fetch_url(wikipedia_mobile_url(fallback_title), timeout=12, limit=6_000_000)
                mobile_title, mobile_content = extract_wikipedia_mobile_html(body.decode("utf-8", errors="replace"))
                content = mobile_content
                page_title = mobile_title or fallback_title
            except Exception as mobile_exc:
                error = str(mobile_exc)
            if content:
                break
            try:
                body, _ = fetch_url(wikipedia_reader_url(fallback_title), timeout=14, limit=3_000_000)
                content = reader_text_to_wikipedia_html(body.decode("utf-8", errors="replace"), target)
                page_title = fallback_title
            except Exception as reader_exc:
                error = str(reader_exc)

    target_hit = bool(target and normalize_wiki_title(page_title) == normalize_wiki_title(target))
    if content:
        content = rewrite_wikipedia_html(content, target)
    else:
        content = f'<div class="empty"><b>Статья не загрузилась</b><br><small>{html_lib.escape(error)}</small></div>'

    page_title_html = html_lib.escape(page_title)
    target_html = html_lib.escape(target)
    target_json = json.dumps(target, ensure_ascii=False)
    hit_class = " show" if target_hit else ""
    page = f"""<!doctype html>
<html lang="ru">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  *{{box-sizing:border-box}}
  body{{margin:0;background:radial-gradient(circle at 18% 10%, rgba(255,255,255,.16), transparent 32%),radial-gradient(circle at 86% 22%, rgba(255,255,255,.10), transparent 30%),#050506;color:#202122;font-family:Arial,sans-serif}}
  header{{position:sticky;top:0;z-index:5;display:grid;grid-template-columns:auto minmax(0,1fr) auto;align-items:center;gap:12px;padding:10px 16px;border-bottom:1px solid #a2a9b1;background:rgba(255,255,255,.96);backdrop-filter:blur(8px);box-shadow:0 8px 24px rgba(0,0,0,.18)}}
  .brand{{font-family:Georgia,serif;font-size:20px;font-weight:700;color:#202122}}
  .nav-title{{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px;color:#54595d}}
  .goal{{font-size:12px;color:#54595d;text-align:right}}
  .goal b{{color:#202122}}
  .wiki-back{{display:inline-flex;align-items:center;gap:7px;border:1px solid #202122;border-radius:10px;background:linear-gradient(145deg,#54595d,#050506);color:#fff;font-weight:800;font-size:13px;padding:9px 12px;cursor:pointer;box-shadow:0 8px 18px rgba(0,0,0,.22);transition:transform .16s ease,box-shadow .16s ease,opacity .16s ease}}
  .wiki-back:hover{{transform:translateY(-1px);box-shadow:0 10px 24px rgba(0,0,0,.28)}}
  .wiki-back:disabled{{opacity:.45;cursor:default;transform:none;box-shadow:none}}
  main{{width:calc(100% - 32px);max-width:980px;margin:16px auto 18px;padding:22px 26px 80px;background:#fff;min-height:calc(100vh - 86px);border:1px solid #eaecf0;border-radius:12px;box-shadow:0 18px 42px rgba(0,0,0,.30)}}
  h1{{font-family:Georgia,serif;font-size:34px;font-weight:400;border-bottom:1px solid #a2a9b1;padding-bottom:8px;margin:0 0 18px}}
  a{{color:#0645ad;text-decoration:none}}
  a:hover{{text-decoration:underline}}
  img{{max-width:100%;height:auto}}
  table{{max-width:100%;border-collapse:collapse}}
  .infobox{{float:right;max-width:320px;margin:0 0 16px 18px;border:1px solid #a2a9b1;background:#f8f9fa;font-size:13px}}
  .thumb{{background:#f8f9fa;border:1px solid #c8ccd1;padding:4px;margin:6px 0 12px 16px}}
  .mw-editsection,.reference,.navbox,.metadata,.ambox{{display:none!important}}
  .empty{{padding:44px 18px;text-align:center;color:#54595d;line-height:1.55}}
  .target-found{{display:none;margin:0 auto 16px;padding:12px 14px;border:1px solid #36c;border-radius:4px;background:#eef6ff;color:#202122;font-weight:700}}
  .target-found.show{{display:block}}
  @media(max-width:720px){{main{{width:calc(100% - 20px);padding:16px 14px 80px}}.infobox{{float:none;max-width:100%;margin:0 0 16px}}header{{grid-template-columns:1fr;align-items:stretch}}.goal{{text-align:left}}.nav-title{{white-space:normal}}}}
</style>
<header>
  <button id="wiki-back" class="wiki-back" type="button">&larr; Назад</button>
  <div class="nav-title"><b>Wikipedia Chase</b> · {page_title_html}</div>
  <div class="goal">Цель: <b>{target_html}</b></div>
</header>
<main>
  <div id="target-found" class="target-found{hit_class}">Целевая статья найдена. Результат засчитывается автоматически.</div>
  <h1>{page_title_html}</h1>
  {content}
</main>
<script>
  const targetHit = {str(target_hit).lower()};
  const targetTitle = {target_json};
  const backButton = document.getElementById('wiki-back');
  if (backButton) {{
    const updateBack = () => {{ backButton.disabled = history.length <= 1; }};
    updateBack();
    window.addEventListener('pageshow', updateBack);
    backButton.addEventListener('click', () => {{
      if (history.length > 1) history.back();
    }});
  }}
  document.addEventListener('click', (event) => {{
    const link = event.target.closest('a');
    if (!link) return;
    link.removeAttribute('target');
    const href = link.getAttribute('href') || '';
    if (!href || href.startsWith('#') || href.startsWith('javascript:')) return;
    if (href.startsWith('/wiki/page')) return;
    const absolute = new URL(href, location.href);
    if ((absolute.hostname === 'ru.wikipedia.org' || absolute.hostname === 'ru.m.wikipedia.org') && absolute.pathname.startsWith('/wiki/')) {{
      event.preventDefault();
      const title = decodeURIComponent(absolute.pathname.slice('/wiki/'.length)).replace(/_/g, ' ');
      location.href = '/wiki/page?title=' + encodeURIComponent(title) + (targetTitle ? '&target=' + encodeURIComponent(targetTitle) : '');
    }}
  }}, true);
  if (targetHit) {{
    setTimeout(() => parent.postMessage({{ type: 'pc-target-found-wiki' }}, '*'), 500);
  }}
</script>
</html>"""
    writer.write(http_response(200, page.encode("utf-8"), "text/html; charset=utf-8"))
    await writer.drain()


async def handle_connection(reader, writer, game: GameServer, connection_guard):
    try:
        async with connection_guard:
            await handle_connection_guarded(reader, writer, game)
    finally:
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()


async def handle_connection_guarded(reader, writer, game: GameServer):
    origin = None
    try:
        client_ip = client_ip_from_writer(writer)
        if rate_limited(client_ip):
            writer.write(json_response(429, {"error": "Too many requests"}))
            await writer.drain()
            return

        header, body = await read_http_request(reader)
        if not header:
            return
        header_text = header.decode("utf-8", errors="ignore")
        header_map = parse_header_map(header_text)
        origin = header_map.get("origin")
        request_line, *_ = header_text.split("\r\n")
        try:
            method, path, _ = request_line.split(" ", 2)
        except ValueError:
            writer.write(json_response(400, {"error": "Bad request"}, origin=origin))
            await writer.drain()
            return
        method = method.upper()
        if method not in {"GET", "POST", "OPTIONS"}:
            writer.write(json_response(405, {"error": "Method not allowed"}, origin=origin))
            await writer.drain()
            return
        if len(path.encode("utf-8", errors="ignore")) > MAX_QUERY_BYTES:
            writer.write(json_response(413, {"error": "Request URL is too large"}, origin=origin))
            await writer.drain()
            return
        parsed = urllib.parse.urlparse(path)
        route = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if method == "OPTIONS":
            writer.write(http_response(204, origin=origin))
            await writer.drain()
            return

        payload = {}
        if body:
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                writer.write(json_response(400, {"error": "Invalid JSON"}, origin=origin))
                await writer.drain()
                return

        if method == "GET" and route == "/pinterest/embed":
            await serve_pinterest_embed(writer, query)
            return

        if method == "GET" and route == "/pinterest/image":
            await serve_pinterest_image(writer, query)
            return

        if method == "GET" and route == "/pinterest/proxy":
            await serve_pinterest_proxy(writer, query)
            return

        if method == "GET" and route == "/wiki/page":
            await serve_wikipedia_page(writer, query)
            return

        if method == "GET" and route == "/wiki/asset":
            await serve_wikipedia_asset(writer, query)
            return

        routes_post = {
            "/register": lambda: game.register(payload),
            "/presence": lambda: game.presence(payload),
            "/lobby/create": lambda: game.create_lobby(payload),
            "/lobby/join": lambda: game.join_lobby(payload),
            "/lobby/invite": lambda: game.invite(payload),
            "/lobby/invite/clear": lambda: game.clear_invite(payload),
            "/lobby/start": lambda: game.start_round(payload),
            "/lobby/win": lambda: game.win(payload),
            "/lobby/surrender": lambda: game.surrender(payload),
            "/lobby/reset": lambda: game.reset_round(payload),
            "/lobby/leave": lambda: game.leave_lobby(payload),
            "/friends/request": lambda: game.friend_request(payload),
            "/friends/accept": lambda: game.accept_friend(payload),
            "/friends/decline": lambda: game.decline_friend(payload),
            "/friends/remove": lambda: game.remove_friend(payload),
            "/profile/avatar": lambda: game.set_avatar(payload),
        }
        routes_get = {
            "/invites": lambda: game.pop_invites((query.get("id") or [""])[0]),
            "/notifications": lambda: game.notifications((query.get("id") or [""])[0]),
            "/friends/list": lambda: game.friends_list((query.get("id") or [""])[0]),
            "/lobby/state": lambda: game.state((query.get("lobbyId") or [""])[0]),
        }

        if method == "POST" and route in routes_post:
            status, result = routes_post[route]()
            writer.write(json_response(status, result, origin=origin))
        elif method == "GET" and route in routes_get:
            status, result = routes_get[route]()
            writer.write(json_response(status, result, origin=origin))
        elif method == "GET":
            await serve_static(writer, route, origin=origin)
            return
        else:
            writer.write(json_response(404, {"error": "Route not found"}, origin=origin))

        await writer.drain()
    except HttpRequestError as exc:
        with contextlib.suppress(Exception):
            writer.write(json_response(exc.status, {"error": exc.message}, origin=origin))
            await writer.drain()
    except asyncio.TimeoutError:
        with contextlib.suppress(Exception):
            writer.write(json_response(400, {"error": "Request timeout"}, origin=origin))
            await writer.drain()
    except Exception:
        with contextlib.suppress(Exception):
            writer.write(json_response(500, {"error": "Internal server error"}, origin=origin))
            await writer.drain()

def local_hint_ip():
    with contextlib.suppress(Exception):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        host = sock.getsockname()[0]
        sock.close()
        return host
    return "127.0.0.1"


async def main():
    parser = argparse.ArgumentParser(description="Pinterest Chase server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 8787))
    args = parser.parse_args()

    game = GameServer()
    connection_guard = asyncio.Semaphore(MAX_CONCURRENT_CONNECTIONS)
    server = await asyncio.start_server(
        lambda r, w: handle_connection(r, w, game, connection_guard), host=args.host, port=args.port
    )
    actual_port = server.sockets[0].getsockname()[1]
    print(f"Pinterest Chase сервер запущен: http://127.0.0.1:{actual_port}")
    print("Для друзей по локальной сети: ваш айпи хоста из Radmin Vpn")
    print("Не закрывай это окно, пока идёт игра.")

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
