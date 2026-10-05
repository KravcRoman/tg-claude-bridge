import json
import os
import secrets
import shutil
import sys
import time

SCRIPT_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIRECTORY, "config.json")
EXAMPLE_PATH = os.path.join(SCRIPT_DIRECTORY, "config.example.json")
HOOK_PATH = os.path.join(SCRIPT_DIRECTORY, "hook.py")

HOOK_EVENTS_WITHOUT_MATCHER = ("Stop",)
HOOK_EVENTS_WITH_MATCHER = ("Notification", "SubagentStop", "PermissionRequest")


def resolve_pythonw():
    executable = sys.executable or "python.exe"
    candidate = executable.replace("python.exe", "pythonw.exe")
    if os.path.isfile(candidate):
        return candidate
    return executable


def write_config(token):
    with open(EXAMPLE_PATH, encoding="utf-8") as example_file:
        config = json.load(example_file)
    if os.path.isfile(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as existing_file:
            config.update(json.load(existing_file))
    config["telegram_bot_token"] = token
    config["claude_home"] = os.path.join(os.path.expanduser("~"), ".claude")
    if not config.get("event_secret"):
        config["event_secret"] = secrets.token_hex(16)
    with open(CONFIG_PATH, "w", encoding="utf-8") as config_file:
        json.dump(config, config_file, ensure_ascii=False, indent=2)
    return config


def hook_command():
    return f'"{resolve_pythonw()}" "{HOOK_PATH}"'


def group_already_has_hook(group_list, command):
    for group in group_list:
        if not isinstance(group, dict):
            continue
        for hook in group.get("hooks", []):
            if isinstance(hook, dict) and hook.get("command") == command:
                return True
    return False


def patch_settings(settings_path):
    try:
        with open(settings_path, encoding="utf-8") as settings_file:
            settings = json.load(settings_file)
    except ValueError:
        print("settings.json повреждён (невалидный JSON) — почини его и повтори.")
        sys.exit(1)
    hooks = settings.setdefault("hooks", {})
    command = hook_command()
    added = []
    events = [(event, False) for event in HOOK_EVENTS_WITHOUT_MATCHER] + [(event, True) for event in HOOK_EVENTS_WITH_MATCHER]
    for event, with_matcher in events:
        group_list = hooks.setdefault(event, [])
        if not isinstance(group_list, list):
            print(f"Пропускаю событие {event}: неожиданный формат в settings.json.")
            continue
        if group_already_has_hook(group_list, command):
            continue
        entry = {"hooks": [{"type": "command", "command": command, "timeout": 5}]}
        if with_matcher:
            entry = {"matcher": ".*", **entry}
        group_list.append(entry)
        added.append(event)
    if not added:
        return added
    shutil.copyfile(settings_path, settings_path + f".bak-{int(time.time())}")
    temporary_path = settings_path + ".tmp"
    with open(temporary_path, "w", encoding="utf-8") as settings_file:
        json.dump(settings, settings_file, ensure_ascii=False, indent=2)
    os.replace(temporary_path, settings_path)
    return added


def main():
    token = sys.argv[1].strip() if len(sys.argv) >= 2 else ""
    if not token and os.path.isfile(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as config_file:
            token = json.load(config_file).get("telegram_bot_token", "")
    if not token or token.startswith("PUT-"):
        print("Использование: python install.py <TELEGRAM_BOT_TOKEN>")
        print("(или положи токен в config.json и запусти без аргумента)")
        sys.exit(1)
    config = write_config(token)
    settings_path = os.path.join(config["claude_home"], "settings.json")
    if not os.path.isfile(settings_path):
        print(f"Не найден settings.json по пути {settings_path}")
        sys.exit(1)
    added = patch_settings(settings_path)
    print("config.json записан (токен + event_secret).")
    if added:
        print("Создана резервная копия settings.json рядом (.bak-*).")
        print(f"Хуки добавлены для событий: {added}.")
    else:
        print("Хуки уже на месте — settings.json не менялся.")
    print("\nДальше:")
    print("  1) Запусти бота: python enable.py")
    print("  2) В Telegram напиши боту /start — первый написавший закрепляется как владелец.")


if __name__ == "__main__":
    main()
