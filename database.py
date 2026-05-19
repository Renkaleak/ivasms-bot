import json
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

DB_FILE = os.getenv("DB_PATH", "/data/db.json")

ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

_TEMPLATE = {
    "users": {},
    "admin_ids": ADMIN_IDS,
}


def load_db() -> dict:
    try:
        with open(DB_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(_TEMPLATE)


def save_db(data: dict):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_user(uid: int) -> dict | None:
    return load_db()["users"].get(str(uid))


def set_user_cookies(uid: int, cookies: str, ua: str = ""):
    db = load_db()
    db["users"].setdefault(str(uid), {})
    db["users"][str(uid)]["cookies"] = cookies
    db["users"][str(uid)]["user_agent"] = ua
    db["users"][str(uid)]["updated_at"] = datetime.now().isoformat()
    save_db(db)


def get_cookies(uid: int) -> str:
    user = get_user(uid)
    return user.get("cookies", "") if user else ""


def get_all_users_with_cookies() -> dict:
    db = load_db()
    return {
        uid: data["cookies"]
        for uid, data in db["users"].items()
        if data.get("cookies")
    }


def update_user_cookies(uid: int | str, new_cookies: str, new_ua: str = ""):
    set_user_cookies(int(uid), new_cookies, new_ua)


def is_admin(uid: int) -> bool:
    db = load_db()
    return uid in db.get("admin_ids", ADMIN_IDS)


def set_monitoring(uid: int, status: bool):
    db = load_db()
    db["users"].setdefault(str(uid), {})
    db["users"][str(uid)]["is_monitoring"] = status
    save_db(db)


def is_monitoring(uid: int) -> bool:
    user = get_user(uid)
    return bool(user.get("is_monitoring")) if user else False
