import re
import asyncio

from yandex_music import Client

import config

# ----------------------------------------------------------------------------
# Работа с Яндекс Музыкой. Клиент инициализируется лениво (при первом запросе),
# чтобы не ходить в сеть на старте/импорте.
# ----------------------------------------------------------------------------

_ym = None


def ym():
    global _ym
    if _ym is None:
        _ym = Client(config.YANDEX_TOKEN).init()
    return _ym


def _resolve_track(query):
    """Находит трек по ссылке Яндекс Музыки или по текстовому запросу."""
    m = re.search(r"/track/(\d+)", query or "")
    if m:
        res = ym().tracks([m.group(1)])
        return res[0] if res else None

    search = ym().search(query)
    if not search.tracks or not search.tracks.results:
        return None
    return search.tracks.results[0]


def _resolve_for_play(track_dict):
    """Восстанавливает трек для воспроизведения: сначала по track_id, потом по query."""
    tid = track_dict.get("track_id")
    if tid:
        res = ym().tracks([tid])
        if res:
            return res[0]
    return _resolve_track(track_dict.get("query"))


def _get_stream_url(track):
    info = track.get_download_info()
    best = max(info, key=lambda x: x.bitrate_in_kbps)
    return best.get_direct_link()


def _resolve_meta_sync(query):
    """Синхронно: метаданные трека для добавления в очередь/плейлист."""
    track = _resolve_track(query)
    if track is None:
        return None
    return {
        "title": track.title,
        "artist": ", ".join(a.name for a in track.artists),
        "query": query,
        "track_id": str(track.id),
    }


def _resolve_url_sync(track_dict):
    """Синхронно: (ссылка на поток, длительность в секундах) или None."""
    track = _resolve_for_play(track_dict)
    if track is None:
        return None
    try:
        url = _get_stream_url(track)
    except Exception as e:
        print(f"[stream-url] {e}")
        return None
    ms = getattr(track, "duration_ms", None) or 0
    duration = int(ms / 1000)
    return url, duration


async def resolve_meta(query):
    return await asyncio.to_thread(_resolve_meta_sync, query)


def _search_top_sync(query, limit=5):
    """Возвращает топ N результатов поиска (список с title, artist, track_id)."""
    search = ym().search(query)
    if not search.tracks or not search.tracks.results:
        return []

    results = []
    for track in search.tracks.results[:limit]:
        results.append({
            "title": track.title,
            "artist": ", ".join(a.name for a in track.artists),
            "track_id": str(track.id),
            "query": f"https://music.yandex.ru/track/{track.id}",
        })
    return results


def _resolve_playlist_sync(url_or_kind_id):
    """Парсит плейлист по ссылке или kind:id (e.g. '3:playlist_id').
    Возвращает список треков или None."""
    import re

    m = re.search(r"/playlist/([^/?]+)", url_or_kind_id or "")
    playlist_id = None
    user_id = None

    if m:
        playlist_id = m.group(1)
    elif ":" in url_or_kind_id:
        user_id, playlist_id = url_or_kind_id.split(":", 1)

    if not playlist_id:
        return None

    try:
        if user_id:
            playlist = ym().users_playlists(int(user_id), int(playlist_id))
        else:
            playlist = ym().playlists_list([playlist_id])[0]
    except Exception as e:
        print(f"[playlist-parse] {e}")
        return None

    if not playlist or not playlist.tracks:
        return None

    results = []
    for t in playlist.tracks:
        if t.track:
            track = t.track
            results.append({
                "title": track.title,
                "artist": ", ".join(a.name for a in track.artists),
                "track_id": str(track.id),
                "query": f"https://music.yandex.ru/track/{track.id}",
            })
    return results


async def search_top(query, limit=5):
    return await asyncio.to_thread(_search_top_sync, query, limit)


async def resolve_playlist(url_or_kind_id):
    return await asyncio.to_thread(_resolve_playlist_sync, url_or_kind_id)


async def resolve_url(track_dict):
    return await asyncio.to_thread(_resolve_url_sync, track_dict)
