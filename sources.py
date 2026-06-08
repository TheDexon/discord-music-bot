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
    """Синхронно: свежая прямая ссылка на поток для воспроизведения."""
    track = _resolve_for_play(track_dict)
    if track is None:
        return None
    try:
        return _get_stream_url(track)
    except Exception as e:
        print(f"[stream-url] {e}")
        return None


async def resolve_meta(query):
    return await asyncio.to_thread(_resolve_meta_sync, query)


async def resolve_url(track_dict):
    return await asyncio.to_thread(_resolve_url_sync, track_dict)
