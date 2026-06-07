import json
import os

PLAYLISTS_FILE = "data/playlists.json"


def load_playlists():
    os.makedirs("data", exist_ok=True)

    if not os.path.exists(PLAYLISTS_FILE):
        with open(PLAYLISTS_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=4)

    with open(PLAYLISTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_playlists(data):
    os.makedirs("data", exist_ok=True)
    with open(PLAYLISTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def create_playlist(user_id, playlist_name):
    data = load_playlists()
    user_id = str(user_id)

    if user_id not in data:
        data[user_id] = {}

    if playlist_name in data[user_id]:
        return False

    data[user_id][playlist_name] = []
    save_playlists(data)
    return True


def delete_playlist(user_id, playlist_name):
    data = load_playlists()
    user_id = str(user_id)

    if user_id not in data:
        return False

    if playlist_name not in data[user_id]:
        return False

    del data[user_id][playlist_name]
    save_playlists(data)
    return True


def get_playlists(user_id):
    data = load_playlists()
    user_id = str(user_id)

    if user_id not in data:
        return []

    return list(data[user_id].keys())


def get_playlist(user_id, playlist_name):
    data = load_playlists()
    user_id = str(user_id)

    if user_id not in data:
        return None

    return data[user_id].get(playlist_name)


def add_track(user_id, playlist_name, title, artist, query, track_id=None):
    data = load_playlists()
    user_id = str(user_id)

    if user_id not in data:
        return False

    if playlist_name not in data[user_id]:
        return False

    data[user_id][playlist_name].append({
        "title": title,
        "artist": artist,
        "query": query,
        "track_id": track_id,
    })

    save_playlists(data)
    return True


def remove_track(user_id, playlist_name, index):
    data = load_playlists()
    user_id = str(user_id)

    if user_id not in data:
        return False

    if playlist_name not in data[user_id]:
        return False

    playlist = data[user_id][playlist_name]
    if index < 0 or index >= len(playlist):
        return False

    playlist.pop(index)
    save_playlists(data)
    return True


def rename_playlist(user_id, old_name, new_name):
    data = load_playlists()
    user_id = str(user_id)

    if user_id not in data:
        return False

    if old_name not in data[user_id]:
        return False

    if new_name in data[user_id]:
        return False

    data[user_id][new_name] = data[user_id][old_name]
    del data[user_id][old_name]
    save_playlists(data)
    return True
