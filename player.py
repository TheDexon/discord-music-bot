import json
import os
import random

QUEUE_FILE = "data/queue.json"
SETTINGS_FILE = "data/settings.json"


# ----------------------------------------------------------------------------
# Очередь сервера
# ----------------------------------------------------------------------------

def load_queue():
    os.makedirs("data", exist_ok=True)

    if not os.path.exists(QUEUE_FILE):
        with open(QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=4)

    with open(QUEUE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_queue(data):
    os.makedirs("data", exist_ok=True)
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def ensure_guild(guild_id):
    data = load_queue()
    guild_id = str(guild_id)

    if guild_id not in data:
        data[guild_id] = {"queue": [], "loop": "off"}
        save_queue(data)

    return data


def add_to_queue(guild_id, title, artist, query, track_id=None):
    data = ensure_guild(guild_id)
    guild_id = str(guild_id)

    data[guild_id]["queue"].append({
        "title": title,
        "artist": artist,
        "query": query,
        "track_id": track_id,
    })

    save_queue(data)


def get_queue(guild_id):
    data = ensure_guild(guild_id)
    return data[str(guild_id)]["queue"]


def clear_queue(guild_id):
    data = ensure_guild(guild_id)
    data[str(guild_id)]["queue"] = []
    save_queue(data)


def pop_next_track(guild_id):
    data = ensure_guild(guild_id)
    guild_id = str(guild_id)

    queue = data[guild_id]["queue"]
    if not queue:
        return None

    track = queue.pop(0)
    save_queue(data)
    return track


def remove_track(guild_id, index):
    data = ensure_guild(guild_id)
    guild_id = str(guild_id)

    queue = data[guild_id]["queue"]
    if index < 0 or index >= len(queue):
        return False

    queue.pop(index)
    save_queue(data)
    return True


def shuffle_queue(guild_id):
    data = ensure_guild(guild_id)
    random.shuffle(data[str(guild_id)]["queue"])
    save_queue(data)


def set_loop_mode(guild_id, mode):
    if mode not in ("off", "track", "queue"):
        return False

    data = ensure_guild(guild_id)
    data[str(guild_id)]["loop"] = mode
    save_queue(data)
    return True


def get_loop_mode(guild_id):
    data = ensure_guild(guild_id)
    return data[str(guild_id)]["loop"]


# ----------------------------------------------------------------------------
# Настройки сервера (громкость + текущий трек)
# ----------------------------------------------------------------------------

def load_settings():
    os.makedirs("data", exist_ok=True)

    if not os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=4)

    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_settings(data):
    os.makedirs("data", exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def ensure_settings(guild_id):
    data = load_settings()
    guild_id = str(guild_id)

    if guild_id not in data:
        data[guild_id] = {"volume": 1.0, "now_playing": None}
        save_settings(data)

    return data


def get_volume(guild_id):
    data = ensure_settings(guild_id)
    return data[str(guild_id)].get("volume", 1.0)


def set_volume(guild_id, value):
    data = ensure_settings(guild_id)
    data[str(guild_id)]["volume"] = value
    save_settings(data)


def get_now_playing(guild_id):
    data = ensure_settings(guild_id)
    return data[str(guild_id)].get("now_playing")


def set_now_playing(guild_id, track):
    data = ensure_settings(guild_id)
    data[str(guild_id)]["now_playing"] = track
    save_settings(data)
