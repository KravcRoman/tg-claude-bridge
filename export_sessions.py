import datetime
import glob
import json
import os
import shutil

CLAUDE_PROJECTS = os.path.join(os.path.expanduser("~"), ".claude", "projects")
BACKUP_ROOT = os.path.join(os.path.expanduser("~"), "claude-sessions-backup")
JUNK_PREFIXES = ("[tgbridge-summary]", "Перескажи по-русски в 2-3 предложениях", "Reply with exactly", "Reply: OK")


def extract_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text")
    return ""


def read_session(path):
    title = ""
    first_user = ""
    branch = ""
    working_directory = ""
    dialogue = []
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("type") == "custom-title" and entry.get("customTitle"):
            title = entry["customTitle"]
        if not branch and entry.get("gitBranch"):
            branch = entry["gitBranch"]
        if not working_directory and entry.get("cwd"):
            working_directory = entry["cwd"]
        entry_type = entry.get("type")
        if entry_type in ("user", "assistant") and not entry.get("isMeta") and not entry.get("isSidechain"):
            message_object = entry.get("message")
            if isinstance(message_object, dict):
                text = extract_text(message_object.get("content")).strip()
                if not text:
                    continue
                if entry_type == "user" and text.startswith(("<command", "<local-command", "Caveat:", "[Request interrupted")):
                    continue
                if entry_type == "user" and not first_user:
                    first_user = text
                dialogue.append((entry_type, text))
    return {"title": title, "first_user": first_user, "branch": branch, "cwd": working_directory, "dialogue": dialogue}


def safe_name(text):
    cleaned = " ".join(text.split())[:55]
    return ("".join(c if (c.isalnum() or c in " -_") else "_" for c in cleaned)).strip() or "session"


def main():
    if not os.path.isdir(CLAUDE_PROJECTS):
        print("Не найден каталог сессий:", CLAUDE_PROJECTS)
        return
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_dir = os.path.join(BACKUP_ROOT, stamp)
    raw_dir = os.path.join(out_dir, "raw")
    md_dir = os.path.join(out_dir, "readable")
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(md_dir, exist_ok=True)
    rows = []
    skipped = 0
    for project_name in os.listdir(CLAUDE_PROJECTS):
        project_path = os.path.join(CLAUDE_PROJECTS, project_name)
        if not os.path.isdir(project_path):
            continue
        for transcript_path in glob.glob(os.path.join(project_path, "*.jsonl")):
            session_id = os.path.basename(transcript_path)[:-6]
            data = read_session(transcript_path)
            if not data["dialogue"] or data["first_user"].startswith(JUNK_PREFIXES):
                skipped += 1
                continue
            try:
                modified_at = os.path.getmtime(transcript_path)
            except OSError:
                continue
            shutil.copy2(transcript_path, os.path.join(raw_dir, session_id + ".jsonl"))
            date_label = datetime.datetime.fromtimestamp(modified_at).strftime("%Y-%m-%d")
            display_title = data["title"] or data["first_user"] or session_id[:8]
            md_name = f"{date_label}_{safe_name(display_title)}_{session_id[:8]}.md"
            with open(os.path.join(md_dir, md_name), "w", encoding="utf-8") as writer:
                writer.write(f"# {display_title}\n\n")
                writer.write(f"- Дата: {datetime.datetime.fromtimestamp(modified_at).strftime('%Y-%m-%d %H:%M')}\n")
                writer.write(f"- Сессия: `{session_id}`\n- Проект: `{data['cwd']}`\n- Ветка: `{data['branch']}`\n\n---\n\n")
                for role, text in data["dialogue"]:
                    writer.write(("## 🧑 Я\n\n" if role == "user" else "## 🤖 Claude\n\n") + text + "\n\n")
            rows.append((modified_at, display_title, session_id))
    rows.sort(reverse=True)
    with open(os.path.join(out_dir, "INDEX.md"), "w", encoding="utf-8") as writer:
        writer.write(f"# Бэкап сессий Claude — {stamp}\n\nСессий сохранено: {len(rows)} (пропущено служебных/пустых: {skipped})\n\n")
        for modified_at, title, session_id in rows:
            writer.write(f"- {datetime.datetime.fromtimestamp(modified_at).strftime('%Y-%m-%d %H:%M')} — {title} (`{session_id[:8]}`)\n")
    archive = shutil.make_archive(out_dir, "zip", out_dir)
    print("Готово.")
    print("Папка:", out_dir)
    print("Архив (zip):", archive)
    print(f"Сессий сохранено: {len(rows)} | пропущено служебных/пустых: {skipped}")
    input("Enter для выхода…")


if __name__ == "__main__":
    main()
