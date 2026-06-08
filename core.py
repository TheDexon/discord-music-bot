import asyncio
import time

import discord
from discord import app_commands

import config
import sources
import voice_listen
from player import (
    get_loop_mode,
    get_now_playing,
    set_now_playing,
    add_to_queue,
    pop_next_track,
    get_volume,
)

# ----------------------------------------------------------------------------
# Клиент Discord и общее состояние
# ----------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True  # нужно для чтения текста сообщений
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# Текстовый канал, куда писать "сейчас играет" (на гильдию)
text_channels = {}
# Флаг "пропустить текущий", чтобы обойти повтор трека при /skip
skip_requested = {}
# Активные "уши" по серверам (голосовое прослушивание)
listen_sinks = {}


# ----------------------------------------------------------------------------
# Прогресс трека
# ----------------------------------------------------------------------------

def _elapsed(np):
    """Сколько секунд проиграно (с учётом паузы). None, если нет данных."""
    if not np or "started_at" not in np:
        return None
    base = np.get("paused_at") or time.time()
    el = base - np["started_at"]
    dur = np.get("duration") or 0
    if dur:
        el = min(el, dur)
    return max(0, el)


def _fmt_time(sec):
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


def format_progress(np):
    """Полоса прогресса + время. Пустая строка, если данных нет."""
    el = _elapsed(np)
    if el is None:
        return ""
    dur = (np or {}).get("duration") or 0
    if dur <= 0:
        return _fmt_time(el)
    length = 12
    pos = min(int(length * el / dur), length - 1)
    bar = "▬" * pos + "🔘" + "▬" * (length - pos - 1)
    return f"{bar} {_fmt_time(el)} / {_fmt_time(dur)}"


def mark_paused(gid):
    """Замораживает прогресс на паузе."""
    np = get_now_playing(gid)
    if np and not np.get("paused_at"):
        np["paused_at"] = time.time()
        set_now_playing(gid, np)


def mark_resumed(gid):
    """Сдвигает старт на длительность паузы, чтобы прогресс был точным."""
    np = get_now_playing(gid)
    if np and np.get("paused_at"):
        np["started_at"] = np.get("started_at", time.time()) + (
            time.time() - np["paused_at"]
        )
        np["paused_at"] = None
        set_now_playing(gid, np)


# ----------------------------------------------------------------------------
# Embed-сообщения
# ----------------------------------------------------------------------------

def embed(title, description="", color=discord.Color.blurple()):
    return discord.Embed(title=title, description=description, color=color)


def now_playing_embed(track):
    desc = f"**{track['title']}**\n{track['artist']}"
    prog = format_progress(track)
    if prog:
        desc += f"\n\n{prog}"
    return embed("▶ Сейчас играет", desc, discord.Color.green())


# ----------------------------------------------------------------------------
# Подключение к голосовому каналу
# ----------------------------------------------------------------------------

async def connect_voice(channel):
    """Подключается к голосовому. С приёмным клиентом, если расширение есть."""
    if voice_listen.VOICE_RECV_AVAILABLE:
        return await channel.connect(cls=voice_listen.voice_recv.VoiceRecvClient)
    return await channel.connect()


async def kick_for_voice(member):
    """Кикает участника, произнёсшего запретное слово голосом."""
    guild = member.guild
    ch = text_channels.get(guild.id) or guild.system_channel
    try:
        if ch:
            await ch.send(
                f"🎙 {member.mention} помянул запретное голосом. На выход."
            )
        await member.kick(reason="Произнёс запретное слово голосом")
    except discord.Forbidden:
        if ch:
            await ch.send(
                "Не могу кикнуть: нужно право «Kick Members» и роль выше."
            )
    except discord.HTTPException as e:
        print(f"[voice-kick] {e}")


# ----------------------------------------------------------------------------
# Ядро воспроизведения
# ----------------------------------------------------------------------------

def _after_playback(error, guild):
    """Колбэк FFmpeg (вызывается в отдельном потоке) -> запуск следующего трека."""
    if error:
        print(f"[after] {error}")
    asyncio.run_coroutine_threadsafe(play_next(guild), client.loop)


async def play_next(guild, announce=True):
    """Берёт следующий трек из очереди (с учётом режима повтора) и проигрывает его."""
    try:
        gid = guild.id
        vc = guild.voice_client
        if vc is None:
            return

        loop_mode = get_loop_mode(gid)
        np = get_now_playing(gid)
        skipped = skip_requested.pop(gid, False)

        if loop_mode == "track" and np is not None and not skipped:
            next_track = np
        else:
            if loop_mode == "queue" and np is not None:
                add_to_queue(
                    gid, np["title"], np["artist"],
                    np["query"], np.get("track_id"),
                )
            next_track = pop_next_track(gid)

        if next_track is None:
            set_now_playing(gid, None)
            return

        set_now_playing(gid, next_track)

        result = await sources.resolve_url(next_track)
        if result is None:
            ch = text_channels.get(gid)
            if ch:
                await ch.send(embed=embed(
                    "⚠ Не удалось воспроизвести",
                    f"{next_track['artist']} — {next_track['title']}\n"
                    f"Пропускаю.",
                    discord.Color.red(),
                ))
            set_now_playing(gid, None)
            await play_next(guild, announce=announce)
            return

        url, duration = result

        # обогащаем now_playing данными для прогресс-бара
        np_entry = dict(next_track)
        np_entry["duration"] = duration
        np_entry["started_at"] = time.time()
        np_entry["paused_at"] = None
        set_now_playing(gid, np_entry)

        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(
                url, executable=config.FFMPEG_PATH, **config.FFMPEG_OPTIONS
            ),
            volume=get_volume(gid),
        )
        vc.play(source, after=lambda e: _after_playback(e, guild))

        if announce:
            ch = text_channels.get(gid)
            if ch:
                await ch.send(embed=now_playing_embed(np_entry))
    except Exception as e:
        print(f"[play_next] {e}")
