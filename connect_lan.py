import argparse
import json
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parent
LAST_HOST_FILE = ROOT / "last_lan_host.txt"
HOST_URL_FILE = ROOT / "host_lan_url.txt"
DEFAULT_PORT = 8787
UPDATE_STATE_FILE = ROOT / ".update_state.json"
UPDATE_REPO_OWNER = "zxchm0nya"
UPDATE_REPO_NAME = "Pinterest-Chase"
UPDATE_REPO_URL = f"https://github.com/{UPDATE_REPO_OWNER}/{UPDATE_REPO_NAME}"
UPDATE_COMMIT_URL = f"https://api.github.com/repos/{UPDATE_REPO_OWNER}/{UPDATE_REPO_NAME}/commits/main"
UPDATE_DOWNLOAD_URL = f"{UPDATE_REPO_URL}/archive/refs/heads/main.zip"
update_notice_printed_for = ""


def normalize_server_url(value: str) -> str:
    value = (value or "").strip().strip('"').strip("'")
    if not value:
        return ""
    if "://" not in value:
        value = "http://" + value
    parsed = urllib.parse.urlparse(value)
    host = parsed.hostname or ""
    if not host:
        return ""
    port = parsed.port or DEFAULT_PORT
    path = parsed.path if parsed.path and parsed.path != "/" else ""
    return urllib.parse.urlunparse(("http", f"{host}:{port}", path, "", "", ""))


def can_open_game(server_url: str, timeout: float = 3.0) -> tuple[bool, str]:
    try:
        request = urllib.request.Request(server_url, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "")
            if response.status != 200:
                return False, f"HTTP {response.status}"
            if "text/html" not in content_type:
                return False, f"сервер ответил, но это не страница игры ({content_type})"
            return True, "ок"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return False, str(exc.reason)
    except TimeoutError:
        return False, "время подключения истекло"
    except OSError as exc:
        return False, str(exc)


def request_json(url: str, timeout: float = 4.0) -> dict:
    request = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "Cache-Control": "no-cache",
        "User-Agent": "Pinterest-Chase-LAN-Client",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        return json.loads(response.read(512_000).decode("utf-8", errors="replace"))


def load_update_state() -> dict:
    try:
        data = json.loads(UPDATE_STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_update_state(data: dict) -> None:
    try:
        UPDATE_STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def short_sha(value: str) -> str:
    value = str(value or "").strip()
    return value[:7] if value else ""


def print_update_notice(status: dict, source: str) -> None:
    global update_notice_printed_for
    if not status.get("updateAvailable"):
        return
    latest_sha_full = str(status.get("latestSha") or "").strip()
    if latest_sha_full and update_notice_printed_for == latest_sha_full:
        return
    if latest_sha_full:
        update_notice_printed_for = latest_sha_full
    installed_sha = short_sha(status.get("installedSha", ""))
    latest_sha = short_sha(status.get("latestSha", ""))
    message = str(status.get("latestMessage") or "").strip()
    download_url = str(status.get("downloadUrl") or UPDATE_DOWNLOAD_URL)
    print()
    print("=== ДОСТУПНА ОБНОВА PINTEREST CHASE ===")
    print(f"Проверка: {source}")
    print(f"Установлен commit: {installed_sha or 'неизвестно'}")
    print(f"Новый commit: {latest_sha or 'неизвестно'}")
    if message:
        print(f"Последнее изменение: {message}")
    print(f"Скачать: {download_url}")
    print("В игре будет висеть плашка обновы, пока не скачаешь новую версию.")
    print("=======================================")
    print()


def check_update_from_host(server_url: str) -> bool:
    try:
        status = request_json(urllib.parse.urljoin(server_url.rstrip("/") + "/", "update/status"))
        print_update_notice(status, "сервер хоста")
        return bool(status.get("updateAvailable"))
    except Exception:
        return False


def check_update_from_github() -> bool:
    state = load_update_state()
    try:
        remote = request_json(UPDATE_COMMIT_URL + "?t=" + str(int(time.time())), timeout=6.0)
        latest_sha = str(remote.get("sha") or "").strip()
        commit = remote.get("commit") if isinstance(remote.get("commit"), dict) else {}
        commit_message = str(commit.get("message") or "").strip().splitlines()[0] if commit else ""
        installed_sha = str(state.get("installedSha") or "").strip()
        if not installed_sha and latest_sha:
            installed_sha = latest_sha
        status = {
            "repoUrl": UPDATE_REPO_URL,
            "downloadUrl": UPDATE_DOWNLOAD_URL,
            "installedSha": installed_sha,
            "latestSha": latest_sha,
            "latestMessage": commit_message,
            "latestTitle": "Pinterest Chase",
            "latestUrl": str(remote.get("html_url") or UPDATE_REPO_URL),
            "checkedAt": int(time.time()),
            "updateAvailable": bool(latest_sha and installed_sha and latest_sha != installed_sha),
            "error": "",
        }
        save_update_state(status)
        print_update_notice(status, "GitHub")
        return bool(status["updateAvailable"])
    except Exception as exc:
        if state.get("updateAvailable"):
            print_update_notice(state, "кэш прошлой проверки")
            return True
        print(f"Проверка обновлений недоступна: {exc}")
        return False


def load_last_host() -> str:
    for file in (HOST_URL_FILE, LAST_HOST_FILE):
        try:
            value = file.read_text(encoding="utf-8").strip()
            if value:
                return value
        except OSError:
            pass
    return ""


def load_saved_host() -> str:
    try:
        return LAST_HOST_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def save_last_host(server_url: str) -> None:
    try:
        LAST_HOST_FILE.write_text(server_url + "\n", encoding="utf-8")
    except OSError:
        pass


def local_ip_hint() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        value = sock.getsockname()[0]
        sock.close()
        return value
    except OSError:
        return "не удалось определить"


def ask_server_url(default_url: str) -> str:
    print()
    print("Pinterest Chase - подключение по LAN")
    print("------------------------------------")
    print("Чтобы подключиться к лобби, нужен IP хоста из Radmin VPN.")
    print("Введи адрес хоста из Radmin VPN. Порт обычно 8787.")
    print("Примеры: 26.12.34.56, 26.12.34.56:8787, http://26.12.34.56:8787")
    print(f"Твой локальный IP сейчас выглядит так: {local_ip_hint()}")
    if default_url:
        print(f"Нажми Enter, чтобы использовать прошлый адрес: {default_url}")
    print()
    entered = input("Адрес хоста: ").strip()
    return entered or default_url


def pause_if_needed(enabled: bool) -> None:
    if not enabled:
        return
    print()
    input("Нажми Enter, чтобы закрыть...")


def main() -> int:
    parser = argparse.ArgumentParser(description="Открыть клиент Pinterest Chase по LAN.")
    parser.add_argument("host", nargs="?", help="IP или URL хоста, например 26.12.34.56:8787")
    parser.add_argument("--no-pause", action="store_true", help="Не ждать перед закрытием.")
    args = parser.parse_args()

    raw_url = args.host or ask_server_url(load_last_host())
    server_url = normalize_server_url(raw_url)
    if not server_url:
        print("Адрес хоста не введён.")
        pause_if_needed(not args.no_pause)
        return 1

    print(f"Проверяю сервер игры: {server_url}")
    github_update_available = check_update_from_github()
    ok = False
    reason = ""
    for attempt in range(1, 4):
        ok, reason = can_open_game(server_url)
        if ok:
            break
        print(f"Попытка {attempt}/3 не удалась: {reason}")
        time.sleep(0.7)

    if not ok:
        print()
        print("Не удалось подключиться к серверу игры хоста.")
        print("Проверь это:")
        print("1. Хост запустил сервер игры.")
        print("2. У всех игроков открыт один и тот же Radmin VPN-сервер/сеть.")
        print("3. Брандмауэр Windows у хоста разрешает входящие подключения Python на порт 8787.")
        print("4. Ты ввёл IP хоста из Radmin VPN, а не свой IP.")
        pause_if_needed(not args.no_pause)
        return 2

    save_last_host(server_url)
    if not github_update_available:
        check_update_from_host(server_url)
    print("Сервер найден. Открываю игру...")
    webbrowser.open(server_url)
    pause_if_needed(not args.no_pause)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
