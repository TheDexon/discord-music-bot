import os
import re
import json
import asyncio

import discord
from discord import app_commands
from dotenv import load_dotenv
from yandex_music import Client

# Голосовое прослушивание — опционально. Если расширение/зависимости
# не установлены, бот всё равно запустится, просто без этой фичи.
try:
    from discord.ext import voice_recv
    VOICE_RECV_AVAILABLE = True
except Exception:
    voice_recv = None
    VOICE_RECV_AVAILABLE = False

from player import (
    get_queue,
    clear_queue,
    shuffle_queue,
    set_loop_mode,
    get_loop_mode,
    add_to_queue,
    pop_next_track,
    remove_track as queue_remove_track,
    get_volume,
    set_volume,
    get_now_playing,
    set_now_playing,
)
from playlists import (
    create_playlist,
    delete_playlist,
    get_playlists,
    get_playlist,
    add_track as playlist_add_track,
    remove_track as playlist_remove_track,
    rename_playlist,
)

# ----------------------------------------------------------------------------
# Конфигурация (берётся из .env, см. .env.example)
# ----------------------------------------------------------------------------

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
YANDEX_TOKEN = os.getenv("YANDEX_TOKEN")
FFMPEG_PATH = os.getenv("FFMPEG_PATH", r".\ffmpeg\bin\ffmpeg.exe")
GUILD_ID = os.getenv("GUILD_ID")  # для мгновенной синхронизации команд

# Telegram-управление (опционально)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID", "")  # твой Telegram ID (можно через запятую)
VOICE_CHANNEL_ID = (
    int(os.getenv("VOICE_CHANNEL_ID")) if os.getenv("VOICE_CHANNEL_ID") else None
)
ALLOWED_TG_USERS = {
    int(x) for x in TELEGRAM_ADMIN_ID.split(",") if x.strip().isdigit()
}

if not TOKEN or not YANDEX_TOKEN:
    raise RuntimeError(
        "Не заданы DISCORD_TOKEN и/или YANDEX_TOKEN. "
        "Создай файл .env по образцу .env.example."
    )

# Опции FFmpeg для потокового HTTP-источника: переподключение при обрыве
FFMPEG_OPTIONS = {
    "before_options": (
        "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
    ),
    "options": "-vn",
}

ym = Client(YANDEX_TOKEN).init()

intents = discord.Intents.default()
intents.message_content = True  # нужно для чтения текста сообщений
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# Текстовый канал, куда писать "сейчас играет" (на гильдию)
text_channels = {}
# Флаг "пропустить текущий", чтобы обойти повтор трека при /skip
skip_requested = {}

# Слова, за которые кикаем автора сообщения - можно через запятую
KICK_TRIGGERS = ("банворд", "банворд")

# Слова, за которые кикаем при распознавании в голосовом канале (UPD: не работает из-за обновления Discord - 2026 год) - можно через запятую
VOICE_TRIGGERS = ("слово", "слово")

# Путь к распакованной модели Vosk (русская). Скачивается отдельно. (UPD: не работает из-за обновления Discord - 2026 год)
VOSK_MODEL_PATH = os.getenv("VOSK_MODEL_PATH", "model")

# Лениво загружаемая модель Vosk и активные "уши" по серверам (UPD: не работает из-за обновления Discord - 2026 год)
_vosk_model = None
listen_sinks = {}  # guild_id -> KeywordSink

# Vosk model (UPD: не работает из-за обновления Discord - 2026 год)
def get_vosk_model():
    """Грузит модель Vosk один раз. Бросает понятную ошибку, если её нет."""
    global _vosk_model
    if _vosk_model is None:
        from vosk import Model  # импорт здесь, чтобы не требовать vosk при старте
        if not os.path.isdir(VOSK_MODEL_PATH):
            raise FileNotFoundError(
                f"Не найдена модель Vosk в '{VOSK_MODEL_PATH}'. "
                f"Скачай русскую модель и распакуй её туда."
            )
        _vosk_model = Model(VOSK_MODEL_PATH)
    return _vosk_model

# Vosk model (UPD: не работает из-за обновления Discord - 2026 год)
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

# Vosk model (UPD: не работает из-за обновления Discord - 2026 год)
if VOICE_RECV_AVAILABLE:

    class KeywordSink(voice_recv.AudioSink):
        """Распознаёт речь каждого участника и кикает за запретные слова."""

        def __init__(self, guild, loop):
            super().__init__()
            self.guild = guild
            self.loop = loop
            self.recognizers = {}  # user_id -> KaldiRecognizer

        def wants_opus(self):
            return False  # нам нужен раскодированный PCM
# Vosk model (UPD: не работает из-за обновления Discord - 2026 год)
        def _get_recognizer(self, user_id):
            from vosk import KaldiRecognizer
            rec = self.recognizers.get(user_id)
            if rec is None:
                rec = KaldiRecognizer(get_vosk_model(), 16000)
                self.recognizers[user_id] = rec
            return rec
# Vosk model (UPD: не работает из-за обновления Discord - 2026 год)
        def _to_mono16k(self, pcm_bytes):
            """48 кГц стерео s16le -> 16 кГц моно (через numpy, без audioop)."""
            import numpy as np
            samples = np.frombuffer(pcm_bytes, dtype=np.int16)
            if samples.size < 2:
                return b""
            samples = samples.reshape(-1, 2).mean(axis=1)  # стерео -> моно
            n = (samples.size // 3) * 3
            if n == 0:
                return b""
            # усреднение по 3 отсчёта = грубый фильтр + децимация 48k->16k
            mono = samples[:n].reshape(-1, 3).mean(axis=1)
            return mono.astype(np.int16).tobytes()

        def write(self, user, data):
            if user is None or not data.pcm:
                return

            audio = self._to_mono16k(data.pcm)
            if not audio:
                return

            rec = self._get_recognizer(user.id)
            if rec.AcceptWaveform(audio):
                text = json.loads(rec.Result()).get("text", "")
            else:
                text = json.loads(rec.PartialResult()).get("partial", "")

            if text and any(t in text for t in VOICE_TRIGGERS):
                # сбрасываем распознаватель, чтобы не кикать повторно за то же
                from vosk import KaldiRecognizer
                self.recognizers[user.id] = KaldiRecognizer(
                    get_vosk_model(), 16000
                )
                member = self.guild.get_member(user.id)
                if member:
                    asyncio.run_coroutine_threadsafe(
                        kick_for_voice(member), self.loop
                    )

        def cleanup(self):
            self.recognizers.clear()


async def connect_voice(channel):
    """Подключается к голосовому. С приёмным клиентом, если расширение есть."""
    if VOICE_RECV_AVAILABLE:
        return await channel.connect(cls=voice_recv.VoiceRecvClient)
    return await channel.connect()


# ----------------------------------------------------------------------------
# Работа с Яндекс Музыкой
# ----------------------------------------------------------------------------

def _resolve_track(query):
    """Находит трек по ссылке Яндекс Музыки или по текстовому запросу."""
    m = re.search(r"/track/(\d+)", query or "")
    if m:
        res = ym.tracks([m.group(1)])
        return res[0] if res else None

    search = ym.search(query)
    if not search.tracks or not search.tracks.results:
        return None
    return search.tracks.results[0]


def _resolve_for_play(track_dict):
    """Восстанавливает трек для воспроизведения: сначала по track_id, потом по query."""
    tid = track_dict.get("track_id")
    if tid:
        res = ym.tracks([tid])
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


# ----------------------------------------------------------------------------
# Embed-сообщения
# ----------------------------------------------------------------------------

def embed(title, description="", color=discord.Color.blurple()):
    return discord.Embed(title=title, description=description, color=color)


def now_playing_embed(track):
    return embed(
        "▶ Сейчас играет",
        f"**{track['title']}**\n{track['artist']}",
        discord.Color.green(),
    )


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

        url = await resolve_url(next_track)
        if url is None:
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

        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(url, executable=FFMPEG_PATH, **FFMPEG_OPTIONS),
            volume=get_volume(gid),
        )
        vc.play(source, after=lambda e: _after_playback(e, guild))

        if announce:
            ch = text_channels.get(gid)
            if ch:
                await ch.send(embed=now_playing_embed(next_track))
    except Exception as e:
        print(f"[play_next] {e}")


# ----------------------------------------------------------------------------
# События
# ----------------------------------------------------------------------------

@client.event
async def on_ready():
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)  # появляется сразу
    else:
        await tree.sync()  # глобально, может обновляться до часа
    print(f"Бот запущен как {client.user}")


@client.event
async def on_message(message):
    """Кикает автора, если в сообщении есть запретное слово."""
    if message.author.bot or message.guild is None:
        return

    content = message.content.lower()
    if not any(trigger in content for trigger in KICK_TRIGGERS):
        return

    try:
        await message.channel.send(
            f"👋 {message.author.mention} помянул запретное. На выход."
        )
        await message.author.kick(reason="Произнёс запретное слово")
    except discord.Forbidden:
        await message.channel.send(
            "Не могу кикнуть: нужно право «Kick Members», "
            "и моя роль должна быть выше его роли."
        )
    except discord.HTTPException as e:
        print(f"[kick] {e}")


@client.event
async def on_voice_state_update(member, before, after):
    """Автоотключение, если бот остался один в голосовом канале."""
    if member.id == client.user.id:
        return

    guild = member.guild
    vc = guild.voice_client
    if vc is None:
        return

    humans = [m for m in vc.channel.members if not m.bot]
    if humans:
        return

    await asyncio.sleep(60)

    vc = guild.voice_client
    if vc and not [m for m in vc.channel.members if not m.bot]:
        await vc.disconnect()
        set_now_playing(guild.id, None)


# ----------------------------------------------------------------------------
# Голосовой канал
# ----------------------------------------------------------------------------

@tree.command(name="join", description="Зайти в голосовой канал")
async def join(interaction: discord.Interaction):
    if interaction.user.voice is None:
        await interaction.response.send_message(
            "Зайди в голосовой канал.", ephemeral=True
        )
        return

    channel = interaction.user.voice.channel
    if interaction.guild.voice_client is None:
        await connect_voice(channel)

    await interaction.response.send_message(f"Подключился к {channel.name}")


@tree.command(name="leave", description="Покинуть голосовой канал")
async def leave(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc:
        listen_sinks.pop(interaction.guild.id, None)
        await vc.disconnect()
        set_now_playing(interaction.guild.id, None)
        await interaction.response.send_message("Отключился.")
    else:
        await interaction.response.send_message(
            "Я не в голосовом канале.", ephemeral=True
        )

# Vosk model (UPD: не работает из-за обновления Discord - 2026 год)
@tree.command(
    name="listen",
    description="Слушать голос и кикать за запретные слова"
)
async def listen(interaction: discord.Interaction):
    if not VOICE_RECV_AVAILABLE:
        await interaction.response.send_message(
            "Прослушивание недоступно: не установлено расширение "
            "`discord-ext-voice-recv`.",
            ephemeral=True,
        )
        return

    if interaction.user.voice is None:
        await interaction.response.send_message(
            "Зайди в голосовой канал.", ephemeral=True
        )
        return

    await interaction.response.defer()

    guild = interaction.guild
    text_channels[guild.id] = interaction.channel

    vc = guild.voice_client
    if vc is None:
        vc = await connect_voice(interaction.user.voice.channel)

    if not isinstance(vc, voice_recv.VoiceRecvClient):
        await interaction.followup.send(
            "Я подключён обычным клиентом без приёма звука. "
            "Сделай `/leave` и снова `/listen`."
        )
        return

    if vc.is_listening():
        await interaction.followup.send("Я уже слушаю.")
        return

    # Загрузка модели может занять секунду — делаем в отдельном потоке
    try:
        await asyncio.to_thread(get_vosk_model)
    except FileNotFoundError as e:
        await interaction.followup.send(str(e))
        return

    sink = KeywordSink(guild, client.loop)
    listen_sinks[guild.id] = sink
    vc.listen(sink)

    await interaction.followup.send(embed=embed(
        "🎙 Слушаю канал",
        "Кикаю за: " + ", ".join(VOICE_TRIGGERS),
    ))

# Vosk model (UPD: не работает из-за обновления Discord - 2026 год)
@tree.command(name="stoplisten", description="Перестать слушать голос")
async def stoplisten(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if VOICE_RECV_AVAILABLE and isinstance(vc, voice_recv.VoiceRecvClient) \
            and vc.is_listening():
        vc.stop_listening()
        listen_sinks.pop(interaction.guild.id, None)
        await interaction.response.send_message("🔇 Больше не слушаю.")
    else:
        await interaction.response.send_message(
            "Я и так не слушаю.", ephemeral=True
        )


# ----------------------------------------------------------------------------
# Воспроизведение
# ----------------------------------------------------------------------------

@tree.command(
    name="play",
    description="Поиск трека в Яндекс Музыке (название или ссылка)"
)
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()

    if interaction.user.voice is None:
        await interaction.followup.send("Зайди в голосовой канал.")
        return

    guild = interaction.guild
    gid = guild.id
    text_channels[gid] = interaction.channel

    vc = guild.voice_client
    if vc is None:
        vc = await connect_voice(interaction.user.voice.channel)

    meta = await resolve_meta(query)
    if meta is None:
        await interaction.followup.send("Ничего не найдено.")
        return

    add_to_queue(gid, meta["title"], meta["artist"], meta["query"], meta["track_id"])

    if vc.is_playing() or vc.is_paused():
        position = len(get_queue(gid))
        await interaction.followup.send(embed=embed(
            "➕ Добавлено в очередь",
            f"**{meta['title']}**\n{meta['artist']}\nПозиция: {position}",
        ))
    else:
        await play_next(guild, announce=False)
        np = get_now_playing(gid)
        await interaction.followup.send(
            embed=now_playing_embed(np if np else meta)
        )


@tree.command(name="skip", description="Пропустить текущий трек")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        skip_requested[interaction.guild.id] = True
        vc.stop()  # запустит _after_playback -> play_next
        await interaction.response.send_message("⏭ Пропущено")
    else:
        await interaction.response.send_message(
            "Сейчас ничего не играет.", ephemeral=True
        )


@tree.command(name="pause", description="Поставить на паузу")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸ Пауза")
    else:
        await interaction.response.send_message(
            "Сейчас ничего не играет.", ephemeral=True
        )


@tree.command(name="resume", description="Продолжить воспроизведение")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶ Продолжаю")
    else:
        await interaction.response.send_message(
            "Музыка не на паузе.", ephemeral=True
        )


@tree.command(
    name="stop",
    description="Остановить музыку и очистить очередь"
)
async def stop(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc:
        gid = interaction.guild.id
        # очищаем очередь и текущий трек ДО stop, чтобы автопереход не сработал
        clear_queue(gid)
        set_now_playing(gid, None)
        skip_requested.pop(gid, None)
        vc.stop()
        await interaction.response.send_message("⏹ Остановлено, очередь очищена")
    else:
        await interaction.response.send_message(
            "Я не подключен.", ephemeral=True
        )


@tree.command(name="volume", description="Установить громкость от 0 до 200")
async def volume(interaction: discord.Interaction, value: int):
    if value < 0 or value > 200:
        await interaction.response.send_message(
            "Укажи значение от 0 до 200.", ephemeral=True
        )
        return

    gid = interaction.guild.id
    level = value / 100
    set_volume(gid, level)

    vc = interaction.guild.voice_client
    if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = level

    await interaction.response.send_message(f"🔊 Громкость: {value}%")


@tree.command(name="nowplaying", description="Показать текущий трек")
async def nowplaying(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    np = get_now_playing(interaction.guild.id)

    if vc and (vc.is_playing() or vc.is_paused()) and np:
        await interaction.response.send_message(embed=now_playing_embed(np))
    else:
        await interaction.response.send_message("Сейчас ничего не играет.")


# ----------------------------------------------------------------------------
# Очередь
# ----------------------------------------------------------------------------

@tree.command(name="queue", description="Показать очередь")
async def queue_cmd(interaction: discord.Interaction):
    gid = interaction.guild.id
    queue = get_queue(gid)
    np = get_now_playing(gid)

    if not queue and not np:
        await interaction.response.send_message("Очередь пуста.")
        return

    lines = []
    if np:
        lines.append(f"▶ **{np['title']}** — {np['artist']}")
    for i, track in enumerate(queue, start=1):
        lines.append(f"{i}. {track['title']} — {track['artist']}")

    await interaction.response.send_message(
        embed=embed("📜 Очередь", "\n".join(lines))
    )


@tree.command(name="clear", description="Очистить очередь")
async def clear(interaction: discord.Interaction):
    clear_queue(interaction.guild.id)
    await interaction.response.send_message("🧹 Очередь очищена")


@tree.command(name="shuffle", description="Перемешать очередь")
async def shuffle(interaction: discord.Interaction):
    shuffle_queue(interaction.guild.id)
    await interaction.response.send_message("🔀 Очередь перемешана")


@tree.command(name="loop", description="Режим повтора: off / track / queue")
async def loop(interaction: discord.Interaction, mode: str):
    if mode not in ("off", "track", "queue"):
        await interaction.response.send_message("off / track / queue")
        return

    set_loop_mode(interaction.guild.id, mode)
    await interaction.response.send_message(f"🔁 Loop: {mode}")


# ----------------------------------------------------------------------------
# Плейлисты
# ----------------------------------------------------------------------------

@tree.command(name="playlist_create", description="Создать плейлист")
async def playlist_create(interaction: discord.Interaction, name: str):
    if create_playlist(interaction.user.id, name):
        await interaction.response.send_message(f"✅ Плейлист '{name}' создан")
    else:
        await interaction.response.send_message(
            "Такой плейлист уже существует.", ephemeral=True
        )


@tree.command(name="playlists", description="Мои плейлисты")
async def playlists_list(interaction: discord.Interaction):
    playlists = get_playlists(interaction.user.id)
    if not playlists:
        await interaction.response.send_message("У тебя нет плейлистов.")
        return

    text = "\n".join(f"• {x}" for x in playlists)
    await interaction.response.send_message(
        embed=embed("📁 Плейлисты", text)
    )


@tree.command(name="playlist_show", description="Показать плейлист")
async def playlist_show(interaction: discord.Interaction, name: str):
    playlist = get_playlist(interaction.user.id, name)
    if playlist is None:
        await interaction.response.send_message("Плейлист не найден.")
        return
    if len(playlist) == 0:
        await interaction.response.send_message("Плейлист пуст.")
        return

    lines = [
        f"{i}. {t['title']} — {t['artist']}"
        for i, t in enumerate(playlist, start=1)
    ]
    await interaction.response.send_message(
        embed=embed(f"📂 {name}", "\n".join(lines))
    )


@tree.command(name="playlist_delete", description="Удалить плейлист")
async def playlist_delete(interaction: discord.Interaction, name: str):
    if delete_playlist(interaction.user.id, name):
        await interaction.response.send_message(f"🗑 Плейлист '{name}' удалён")
    else:
        await interaction.response.send_message("Плейлист не найден.")


@tree.command(
    name="playlist_add",
    description="Добавить трек в плейлист (название или ссылка)"
)
async def playlist_add(interaction: discord.Interaction, name: str, query: str):
    await interaction.response.defer()

    if get_playlist(interaction.user.id, name) is None:
        await interaction.followup.send("Плейлист не найден.")
        return

    meta = await resolve_meta(query)
    if meta is None:
        await interaction.followup.send("Ничего не найдено.")
        return

    ok = playlist_add_track(
        interaction.user.id, name,
        meta["title"], meta["artist"], meta["query"], meta["track_id"],
    )
    if ok:
        await interaction.followup.send(embed=embed(
            "➕ Добавлено в плейлист",
            f"**{meta['title']}**\n{meta['artist']}\n→ {name}",
        ))
    else:
        await interaction.followup.send("Не удалось добавить трек.")


@tree.command(
    name="playlist_remove",
    description="Удалить трек из плейлиста по номеру"
)
async def playlist_remove(interaction: discord.Interaction, name: str, number: int):
    if get_playlist(interaction.user.id, name) is None:
        await interaction.response.send_message("Плейлист не найден.")
        return

    ok = playlist_remove_track(interaction.user.id, name, number - 1)
    if ok:
        await interaction.response.send_message(
            f"🗑 Трек #{number} удалён из '{name}'"
        )
    else:
        await interaction.response.send_message("Неверный номер трека.")


@tree.command(
    name="playlist_play",
    description="Добавить весь плейлист в очередь и запустить"
)
async def playlist_play(interaction: discord.Interaction, name: str):
    await interaction.response.defer()

    playlist = get_playlist(interaction.user.id, name)
    if playlist is None:
        await interaction.followup.send("Плейлист не найден.")
        return
    if len(playlist) == 0:
        await interaction.followup.send("Плейлист пуст.")
        return
    if interaction.user.voice is None:
        await interaction.followup.send("Зайди в голосовой канал.")
        return

    guild = interaction.guild
    gid = guild.id
    text_channels[gid] = interaction.channel

    vc = guild.voice_client
    if vc is None:
        vc = await connect_voice(interaction.user.voice.channel)

    for t in playlist:
        add_to_queue(gid, t["title"], t["artist"], t["query"], t.get("track_id"))

    if not (vc.is_playing() or vc.is_paused()):
        await play_next(guild, announce=False)

    await interaction.followup.send(embed=embed(
        "📂 Плейлист в очереди",
        f"Добавлено треков: {len(playlist)} из '{name}'",
    ))


@tree.command(name="playlist_rename", description="Переименовать плейлист")
async def playlist_rename(
    interaction: discord.Interaction, old_name: str, new_name: str
):
    ok = rename_playlist(interaction.user.id, old_name, new_name)
    if ok:
        await interaction.response.send_message(
            f"✏ '{old_name}' → '{new_name}'"
        )
    else:
        await interaction.response.send_message(
            "Не удалось переименовать (нет такого плейлиста или имя занято)."
        )


# ----------------------------------------------------------------------------
# Telegram-управление (опционально). Доступ только у ALLOWED_TG_USERS (.env).
# ----------------------------------------------------------------------------

try:
    from aiogram import Bot, Dispatcher, F
    from aiogram.filters import Command, CommandObject
    from aiogram.types import (
        Message,
        CallbackQuery,
        InlineKeyboardMarkup,
        InlineKeyboardButton,
    )
    TG_AVAILABLE = True
except Exception:
    TG_AVAILABLE = False

tg_bot = None
dp = None

if TG_AVAILABLE and TELEGRAM_TOKEN:
    tg_bot = Bot(token=TELEGRAM_TOKEN)
    dp = Dispatcher()

    # Глобальный фильтр: пропускаем только разрешённых пользователей.
    # Если список пуст — не пройдёт никто (безопасный дефолт).
    dp.message.filter(F.from_user.id.in_(ALLOWED_TG_USERS))
    dp.callback_query.filter(F.from_user.id.in_(ALLOWED_TG_USERS))

    def tg_guild():
        if GUILD_ID:
            g = client.get_guild(int(GUILD_ID))
            if g:
                return g
        return client.guilds[0] if client.guilds else None

    async def tg_ensure_voice(guild):
        """Возвращает голосовой клиент, подключаясь к VOICE_CHANNEL_ID при нужде."""
        vc = guild.voice_client
        if vc is not None:
            return vc
        if VOICE_CHANNEL_ID is None:
            return None
        channel = client.get_channel(VOICE_CHANNEL_ID)
        if channel is None:
            return None
        return await connect_voice(channel)

    HELP_TEXT = (
        "🎵 Музыка:\n"
        "/play <название|ссылка> — добавить и играть\n"
        "/skip — пропустить\n"
        "/pause /resume /stop\n"
        "/volume <0-200>\n"
        "/queue — очередь\n"
        "/np — текущий трек\n"
        "/clear /shuffle\n"
        "/loop off|track|queue\n"
        "/join /leave\n\n"
        "📁 Плейлисты (у каждого свои):\n"
        "/playlists — список\n"
        "/playlist_create <название>\n"
        "/playlist_show <название>\n"
        "/playlist_delete <название>\n"
        "/playlist_add <название> | <трек|ссылка>\n"
        "/playlist_remove <название> <номер>\n"
        "/playlist_play <название>\n"
        "/playlist_rename <старое> | <новое>"
    )

    @dp.message(Command("help"))
    async def tg_help(message: Message):
        await message.answer(HELP_TEXT)

    # --- Кнопочная панель управления ---

    def panel_text(guild):
        if guild is None:
            return "🎛 Панель управления\n(сервер недоступен)"
        np = get_now_playing(guild.id)
        q = get_queue(guild.id)
        vol = int(get_volume(guild.id) * 100)
        loop = get_loop_mode(guild.id)
        now = f"{np['title']} — {np['artist']}" if np else "—"
        return (
            "🎛 Панель управления\n\n"
            f"▶ Сейчас: {now}\n"
            f"📜 В очереди: {len(q)}\n"
            f"🔊 Громкость: {vol}%    🔁 Повтор: {loop}\n\n"
            "Чтобы добавить трек — просто пришли название или ссылку сообщением."
        )

    def main_menu_kb():
        b = InlineKeyboardButton
        return InlineKeyboardMarkup(inline_keyboard=[
            [b(text="⏸ Пауза", callback_data="ctl:pause"),
             b(text="▶ Продолжить", callback_data="ctl:resume"),
             b(text="⏭ Пропустить", callback_data="ctl:skip")],
            [b(text="⏹ Стоп", callback_data="ctl:stop"),
             b(text="🔀 Перемешать", callback_data="ctl:shuffle"),
             b(text="🧹 Очистить", callback_data="ctl:clear")],
            [b(text="🔉 −10", callback_data="vol:down"),
             b(text="🔊 +10", callback_data="vol:up"),
             b(text="🔁 Повтор", callback_data="loop:cycle")],
            [b(text="📜 Очередь", callback_data="show:queue"),
             b(text="📁 Плейлисты", callback_data="pl:list")],
            [b(text="🔄 Обновить", callback_data="menu")],
        ])

    def playlists_kb(tg_user_id):
        pls = get_playlists(f"tg:{tg_user_id}")
        rows = [
            [InlineKeyboardButton(text=f"▶ {name}", callback_data=f"pl:play:{i}")]
            for i, name in enumerate(pls)
        ]
        rows.append([InlineKeyboardButton(text="⬅ Назад", callback_data="menu")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def queue_text(guild):
        q = get_queue(guild.id)
        np = get_now_playing(guild.id)
        if not q and not np:
            return "Очередь пуста."
        lines = []
        if np:
            lines.append(f"▶ {np['title']} — {np['artist']}")
        for i, t in enumerate(q, start=1):
            lines.append(f"{i}. {t['title']} — {t['artist']}")
        return "\n".join(lines)

    async def show_panel(message):
        await message.answer(panel_text(tg_guild()), reply_markup=main_menu_kb())

    async def refresh_panel(call):
        try:
            await call.message.edit_text(
                panel_text(tg_guild()), reply_markup=main_menu_kb()
            )
        except Exception:
            pass

    @dp.message(Command("start", "menu"))
    async def tg_menu(message: Message):
        await show_panel(message)

    @dp.callback_query()
    async def on_callback(call: CallbackQuery):
        data = call.data or ""
        guild = tg_guild()

        # Навигация
        if data == "menu":
            await refresh_panel(call)
            await call.answer()
            return
        if data == "pl:list":
            try:
                await call.message.edit_text(
                    "📁 Твои плейлисты:", reply_markup=playlists_kb(call.from_user.id)
                )
            except Exception:
                pass
            await call.answer()
            return
        if data == "show:queue":
            if guild:
                try:
                    await call.message.edit_text(
                        "📜 Очередь:\n\n" + queue_text(guild),
                        reply_markup=main_menu_kb(),
                    )
                except Exception:
                    pass
            await call.answer()
            return

        if guild is None:
            await call.answer("Сервер недоступен.", show_alert=True)
            return

        # Проиграть плейлист по индексу
        if data.startswith("pl:play:"):
            idx = int(data.split(":")[2])
            owner = f"tg:{call.from_user.id}"
            pls = get_playlists(owner)
            if idx >= len(pls):
                await call.answer("Плейлист не найден.")
                return
            name = pls[idx]
            pl = get_playlist(owner, name)
            if not pl:
                await call.answer("Плейлист пуст.")
                return
            vc = await tg_ensure_voice(guild)
            if vc is None:
                await call.answer("Не удалось подключиться.", show_alert=True)
                return
            for t in pl:
                add_to_queue(
                    guild.id, t["title"], t["artist"], t["query"], t.get("track_id")
                )
            if not (vc.is_playing() or vc.is_paused()):
                await play_next(guild, announce=False)
            await call.answer(f"📂 Добавлено: {name}")
            await refresh_panel(call)
            return

        vc = guild.voice_client

        if data == "ctl:pause":
            if vc and vc.is_playing():
                vc.pause()
                await call.answer("⏸ Пауза")
            else:
                await call.answer("Ничего не играет.")
        elif data == "ctl:resume":
            if vc and vc.is_paused():
                vc.resume()
                await call.answer("▶ Продолжаю")
            else:
                await call.answer("Не на паузе.")
        elif data == "ctl:skip":
            if vc and (vc.is_playing() or vc.is_paused()):
                skip_requested[guild.id] = True
                vc.stop()
                await call.answer("⏭ Пропущено")
            else:
                await call.answer("Ничего не играет.")
        elif data == "ctl:stop":
            if vc:
                clear_queue(guild.id)
                set_now_playing(guild.id, None)
                skip_requested.pop(guild.id, None)
                vc.stop()
                await call.answer("⏹ Остановлено")
            else:
                await call.answer("Я не подключён.")
        elif data == "ctl:shuffle":
            shuffle_queue(guild.id)
            await call.answer("🔀 Перемешано")
        elif data == "ctl:clear":
            clear_queue(guild.id)
            await call.answer("🧹 Очередь очищена")
        elif data in ("vol:up", "vol:down"):
            step = 10 if data == "vol:up" else -10
            value = max(0, min(200, int(get_volume(guild.id) * 100) + step))
            set_volume(guild.id, value / 100)
            if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
                vc.source.volume = value / 100
            await call.answer(f"🔊 {value}%")
        elif data == "loop:cycle":
            order = ["off", "track", "queue"]
            cur = get_loop_mode(guild.id)
            nxt = order[(order.index(cur) + 1) % 3] if cur in order else "off"
            set_loop_mode(guild.id, nxt)
            await call.answer(f"🔁 {nxt}")
        else:
            await call.answer()
            return

        await refresh_panel(call)

    @dp.message(Command("play"))
    async def tg_play(message: Message, command: CommandObject):
        query = (command.args or "").strip()
        if not query:
            await message.answer("Использование: /play <название или ссылка>")
            return
        guild = tg_guild()
        if guild is None:
            await message.answer("Сервер недоступен.")
            return
        vc = await tg_ensure_voice(guild)
        if vc is None:
            await message.answer("Не удалось подключиться к голосовому каналу.")
            return
        meta = await resolve_meta(query)
        if meta is None:
            await message.answer("Ничего не найдено.")
            return
        add_to_queue(
            guild.id, meta["title"], meta["artist"],
            meta["query"], meta["track_id"],
        )
        if vc.is_playing() or vc.is_paused():
            pos = len(get_queue(guild.id))
            await message.answer(
                f"➕ В очередь (#{pos}): {meta['title']} — {meta['artist']}"
            )
        else:
            await play_next(guild, announce=False)
            np = get_now_playing(guild.id) or meta
            await message.answer(f"▶ Играю: {np['title']} — {np['artist']}")

    @dp.message(Command("skip"))
    async def tg_skip(message: Message):
        guild = tg_guild()
        vc = guild.voice_client if guild else None
        if vc and (vc.is_playing() or vc.is_paused()):
            skip_requested[guild.id] = True
            vc.stop()
            await message.answer("⏭ Пропущено")
        else:
            await message.answer("Сейчас ничего не играет.")

    @dp.message(Command("pause"))
    async def tg_pause(message: Message):
        guild = tg_guild()
        vc = guild.voice_client if guild else None
        if vc and vc.is_playing():
            vc.pause()
            await message.answer("⏸ Пауза")
        else:
            await message.answer("Сейчас ничего не играет.")

    @dp.message(Command("resume"))
    async def tg_resume(message: Message):
        guild = tg_guild()
        vc = guild.voice_client if guild else None
        if vc and vc.is_paused():
            vc.resume()
            await message.answer("▶ Продолжаю")
        else:
            await message.answer("Музыка не на паузе.")

    @dp.message(Command("stop"))
    async def tg_stop(message: Message):
        guild = tg_guild()
        vc = guild.voice_client if guild else None
        if vc:
            clear_queue(guild.id)
            set_now_playing(guild.id, None)
            skip_requested.pop(guild.id, None)
            vc.stop()
            await message.answer("⏹ Остановлено, очередь очищена")
        else:
            await message.answer("Я не подключён.")

    @dp.message(Command("volume"))
    async def tg_volume(message: Message, command: CommandObject):
        arg = (command.args or "").strip()
        if not arg.isdigit() or not (0 <= int(arg) <= 200):
            await message.answer("Использование: /volume <0-200>")
            return
        guild = tg_guild()
        if guild is None:
            await message.answer("Сервер недоступен.")
            return
        value = int(arg)
        set_volume(guild.id, value / 100)
        vc = guild.voice_client
        if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = value / 100
        await message.answer(f"🔊 Громкость: {value}%")

    @dp.message(Command("queue"))
    async def tg_queue(message: Message):
        guild = tg_guild()
        if guild is None:
            await message.answer("Сервер недоступен.")
            return
        q = get_queue(guild.id)
        np = get_now_playing(guild.id)
        if not q and not np:
            await message.answer("Очередь пуста.")
            return
        lines = []
        if np:
            lines.append(f"▶ {np['title']} — {np['artist']}")
        for i, t in enumerate(q, start=1):
            lines.append(f"{i}. {t['title']} — {t['artist']}")
        await message.answer("\n".join(lines))

    @dp.message(Command("np"))
    async def tg_np(message: Message):
        guild = tg_guild()
        np = get_now_playing(guild.id) if guild else None
        vc = guild.voice_client if guild else None
        if vc and (vc.is_playing() or vc.is_paused()) and np:
            await message.answer(f"🎵 {np['title']} — {np['artist']}")
        else:
            await message.answer("Сейчас ничего не играет.")

    @dp.message(Command("clear"))
    async def tg_clear(message: Message):
        guild = tg_guild()
        if guild:
            clear_queue(guild.id)
            await message.answer("🧹 Очередь очищена")

    @dp.message(Command("shuffle"))
    async def tg_shuffle(message: Message):
        guild = tg_guild()
        if guild:
            shuffle_queue(guild.id)
            await message.answer("🔀 Очередь перемешана")

    @dp.message(Command("loop"))
    async def tg_loop(message: Message, command: CommandObject):
        mode = (command.args or "").strip()
        if mode not in ("off", "track", "queue"):
            await message.answer("off / track / queue")
            return
        guild = tg_guild()
        if guild:
            set_loop_mode(guild.id, mode)
            await message.answer(f"🔁 Loop: {mode}")

    @dp.message(Command("join"))
    async def tg_join(message: Message):
        guild = tg_guild()
        if guild is None:
            await message.answer("Сервер недоступен.")
            return
        vc = await tg_ensure_voice(guild)
        if vc is None:
            await message.answer("Не удалось подключиться (проверь VOICE_CHANNEL_ID).")
        else:
            await message.answer("Подключился к голосовому каналу.")

    @dp.message(Command("leave"))
    async def tg_leave(message: Message):
        guild = tg_guild()
        vc = guild.voice_client if guild else None
        if vc:
            listen_sinks.pop(guild.id, None)
            await vc.disconnect()
            set_now_playing(guild.id, None)
            await message.answer("Отключился.")
        else:
            await message.answer("Я не в голосовом канале.")

    # --- Плейлисты (у каждого Telegram-пользователя свои) ---

    def tg_owner(message):
        return f"tg:{message.from_user.id}"

    @dp.message(Command("playlists"))
    async def tg_pl_list(message: Message):
        pls = get_playlists(tg_owner(message))
        if not pls:
            await message.answer("У тебя нет плейлистов.")
            return
        await message.answer("📁 Плейлисты:\n" + "\n".join(f"• {x}" for x in pls))

    @dp.message(Command("playlist_create"))
    async def tg_pl_create(message: Message, command: CommandObject):
        name = (command.args or "").strip()
        if not name:
            await message.answer("Использование: /playlist_create <название>")
            return
        if create_playlist(tg_owner(message), name):
            await message.answer(f"✅ Плейлист '{name}' создан")
        else:
            await message.answer("Такой плейлист уже существует.")

    @dp.message(Command("playlist_show"))
    async def tg_pl_show(message: Message, command: CommandObject):
        name = (command.args or "").strip()
        if not name:
            await message.answer("Использование: /playlist_show <название>")
            return
        pl = get_playlist(tg_owner(message), name)
        if pl is None:
            await message.answer("Плейлист не найден.")
            return
        if not pl:
            await message.answer("Плейлист пуст.")
            return
        lines = [f"{i}. {t['title']} — {t['artist']}" for i, t in enumerate(pl, 1)]
        await message.answer(f"📂 {name}:\n" + "\n".join(lines))

    @dp.message(Command("playlist_delete"))
    async def tg_pl_delete(message: Message, command: CommandObject):
        name = (command.args or "").strip()
        if not name:
            await message.answer("Использование: /playlist_delete <название>")
            return
        if delete_playlist(tg_owner(message), name):
            await message.answer(f"🗑 Плейлист '{name}' удалён")
        else:
            await message.answer("Плейлист не найден.")

    @dp.message(Command("playlist_add"))
    async def tg_pl_add(message: Message, command: CommandObject):
        raw = (command.args or "").strip()
        if "|" in raw:
            name, query = (p.strip() for p in raw.split("|", 1))
        else:
            parts = raw.split(" ", 1)
            name = parts[0] if parts and parts[0] else ""
            query = parts[1] if len(parts) > 1 else ""
        if not name or not query:
            await message.answer(
                "Использование: /playlist_add <название> | <трек или ссылка>"
            )
            return
        if get_playlist(tg_owner(message), name) is None:
            await message.answer("Плейлист не найден.")
            return
        meta = await resolve_meta(query)
        if meta is None:
            await message.answer("Ничего не найдено.")
            return
        ok = playlist_add_track(
            tg_owner(message), name,
            meta["title"], meta["artist"], meta["query"], meta["track_id"],
        )
        if ok:
            await message.answer(
                f"➕ В '{name}': {meta['title']} — {meta['artist']}"
            )
        else:
            await message.answer("Не удалось добавить трек.")

    @dp.message(Command("playlist_remove"))
    async def tg_pl_remove(message: Message, command: CommandObject):
        raw = (command.args or "").strip()
        parts = raw.rsplit(" ", 1)
        if len(parts) != 2 or not parts[1].isdigit():
            await message.answer("Использование: /playlist_remove <название> <номер>")
            return
        name, num = parts[0].strip(), int(parts[1])
        if get_playlist(tg_owner(message), name) is None:
            await message.answer("Плейлист не найден.")
            return
        if playlist_remove_track(tg_owner(message), name, num - 1):
            await message.answer(f"🗑 Трек #{num} удалён из '{name}'")
        else:
            await message.answer("Неверный номер трека.")

    @dp.message(Command("playlist_play"))
    async def tg_pl_play(message: Message, command: CommandObject):
        name = (command.args or "").strip()
        if not name:
            await message.answer("Использование: /playlist_play <название>")
            return
        pl = get_playlist(tg_owner(message), name)
        if pl is None:
            await message.answer("Плейлист не найден.")
            return
        if not pl:
            await message.answer("Плейлист пуст.")
            return
        guild = tg_guild()
        if guild is None:
            await message.answer("Сервер недоступен.")
            return
        vc = await tg_ensure_voice(guild)
        if vc is None:
            await message.answer("Не удалось подключиться к голосовому каналу.")
            return
        for t in pl:
            add_to_queue(
                guild.id, t["title"], t["artist"], t["query"], t.get("track_id")
            )
        if not (vc.is_playing() or vc.is_paused()):
            await play_next(guild, announce=False)
        await message.answer(f"📂 В очередь добавлено {len(pl)} треков из '{name}'")

    @dp.message(Command("playlist_rename"))
    async def tg_pl_rename(message: Message, command: CommandObject):
        raw = (command.args or "").strip()
        if "|" not in raw:
            await message.answer("Использование: /playlist_rename <старое> | <новое>")
            return
        old, new = (p.strip() for p in raw.split("|", 1))
        if not old or not new:
            await message.answer("Использование: /playlist_rename <старое> | <новое>")
            return
        if rename_playlist(tg_owner(message), old, new):
            await message.answer(f"✏ '{old}' → '{new}'")
        else:
            await message.answer("Не удалось переименовать (нет такого или имя занято).")

    # --- Любой обычный текст = добавить трек и играть ---

    @dp.message(F.text)
    async def tg_text_play(message: Message):
        query = (message.text or "").strip()
        if not query or query.startswith("/"):
            return
        guild = tg_guild()
        if guild is None:
            await message.answer("Сервер недоступен.")
            return
        vc = await tg_ensure_voice(guild)
        if vc is None:
            await message.answer("Не удалось подключиться к голосовому каналу.")
            return
        meta = await resolve_meta(query)
        if meta is None:
            await message.answer("Ничего не найдено.")
            return
        add_to_queue(
            guild.id, meta["title"], meta["artist"],
            meta["query"], meta["track_id"],
        )
        if vc.is_playing() or vc.is_paused():
            pos = len(get_queue(guild.id))
            await message.answer(
                f"➕ В очередь (#{pos}): {meta['title']} — {meta['artist']}",
                reply_markup=main_menu_kb(),
            )
        else:
            await play_next(guild, announce=False)
            np = get_now_playing(guild.id) or meta
            await message.answer(
                f"▶ Играю: {np['title']} — {np['artist']}",
                reply_markup=main_menu_kb(),
            )


# ----------------------------------------------------------------------------
# Запуск: Discord + (опционально) Telegram в одном цикле
# ----------------------------------------------------------------------------

async def main():
    tasks = [client.start(TOKEN)]
    if dp is not None and tg_bot is not None:
        tasks.append(dp.start_polling(tg_bot))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())