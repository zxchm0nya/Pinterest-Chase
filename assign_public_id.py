import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOCIAL_FILE = ROOT / "social_state.json"


def load_social():
    if not SOCIAL_FILE.exists():
        return {"clients": {}, "friends": {}, "friendRequests": {}}
    return json.loads(SOCIAL_FILE.read_text(encoding="utf-8"))


def save_social(data):
    SOCIAL_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize(value):
    return str(value or "").strip().lower()


def clean_public_id(value):
    public_id = str(value or "").strip()
    for bad in ("\r", "\n", "\t", "<", ">"):
        public_id = public_id.replace(bad, "")
    public_id = "".join(public_id.split())
    if not public_id:
        raise ValueError("Публичный ID не может быть пустым")
    if len(public_id) > 32:
        raise ValueError("Публичный ID должен быть не длиннее 32 символов")
    return public_id


def player_label(player_id, client):
    return f"{client.get('nickname') or 'Без ника'} | внутренний id: {player_id} | публичный ID: {client.get('publicId') or 'нет'}"


def find_players(clients, value):
    target = str(value or "").strip()
    target_lower = target.lower()
    if not target:
        return []
    results = []
    for player_id, client in clients.items():
        nickname = str(client.get("nickname") or "").strip()
        public_id = str(client.get("publicId") or "").strip()
        if (
            player_id == target
            or public_id.lower() == target_lower
            or nickname.lower() == target_lower
            or target_lower in nickname.lower()
        ):
            results.append((player_id, client))
    return results


def choose_player(clients, value):
    matches = find_players(clients, value)
    if not matches:
        raise ValueError(f"Игрок не найден: {value}")
    if len(matches) == 1:
        return matches[0]

    print("Найдено несколько игроков:")
    for index, (player_id, client) in enumerate(matches, 1):
        print(f"{index}. {player_label(player_id, client)}")
    choice = input("Выбери номер игрока: ").strip()
    if not choice.isdigit() or not (1 <= int(choice) <= len(matches)):
        raise ValueError("Неверный номер игрока")
    return matches[int(choice) - 1]


def assign_public_id(data, player_query, public_id, force=False):
    public_id = clean_public_id(public_id)

    clients = data.setdefault("clients", {})
    player_id, client = choose_player(clients, player_query)

    for other_id, other in clients.items():
        if other_id == player_id:
            continue
        other_public_id = str(other.get("publicId") or "").strip()
        if other_public_id and clean_public_id(other_public_id).lower() == public_id.lower():
            if not force:
                raise ValueError(f"ID уже занят: {player_label(other_id, other)}")
            other.pop("publicId", None)

    client["publicId"] = public_id
    save_social(data)
    print(f"Готово: {player_label(player_id, client)}")


def print_search_results(data, query):
    clients = data.setdefault("clients", {})
    matches = find_players(clients, query)
    if not matches:
        print("Игрок не найден.")
        return
    print(f"Найдено: {len(matches)}")
    for player_id, client in matches:
        print("- " + player_label(player_id, client))


def interactive():
    data = load_social()
    while True:
        print("\n=== Выдача публичных ID ===")
        print("1. Найти игрока по нику или ID")
        print("2. Выдать игроку публичный ID")
        print("3. Показать всех игроков")
        print("0. Выход")
        choice = input("Выбор: ").strip()

        try:
            if choice == "1":
                query = input("Введи ник, внутренний id или публичный ID: ").strip()
                print_search_results(data, query)
            elif choice == "2":
                player = input("Кому выдать? Ник, внутренний id или текущий публичный ID: ").strip()
                public_id = input("Новый публичный ID (цифры или текст, до 32 символов): ").strip()
                force = input("Если ID занят, забрать его у другого игрока? y/N: ").strip().lower() == "y"
                assign_public_id(data, player, public_id, force)
                data = load_social()
            elif choice == "3":
                for player_id, client in data.setdefault("clients", {}).items():
                    print("- " + player_label(player_id, client))
            elif choice == "0":
                return
            else:
                print("Нет такого пункта.")
        except Exception as exc:
            print(f"Ошибка: {exc}")


def main():
    parser = argparse.ArgumentParser(description="Поиск игроков и выдача публичного ID.")
    parser.add_argument("player", nargs="?", help="Ник, внутренний id или публичный ID игрока")
    parser.add_argument("public_id", nargs="?", help="Новый публичный ID: цифры или текст, до 32 символов")
    parser.add_argument("--find", help="Только найти игрока по нику или ID")
    parser.add_argument("--force", action="store_true", help="Забрать ID, если он уже занят другим игроком")
    args = parser.parse_args()

    data = load_social()
    if args.find:
        print_search_results(data, args.find)
        return
    if args.player and args.public_id:
        assign_public_id(data, args.player, args.public_id, args.force)
        return
    interactive()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Ошибка: {exc}")
    if sys.stdin.isatty():
        input("\nНажми Enter, чтобы закрыть...")
