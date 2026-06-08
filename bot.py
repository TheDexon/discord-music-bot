import asyncio

import discord
from discord import app_commands

import config
import voice_listen
import telegram_bot
from core import (
    client,
    tree,
    text_channels,
    skip_requested,
    listen_sinks,
    embed,
    now_playing_embed,
    connect_voice,
    play_next,
    kick_for_voice,
    mark_paused,
    mark_resumed,
)
from sources import resolve_meta, search_top, resolve_playlist
from player import (
    get_queue,
    clear_queue,
    shuffle_queue,
    set_loop_mode,
    add_to_queue,
    remove_track as queue_remove_track,
    move_track,
    set_volume,
    get_volume,
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
# Интерактивные элементы (Select результаты поиска, Пульт трека)
# ----------------------------------------------------------------------------

class SearchResultsView(discord.ui.View):
    def __init__(self, results, guild_id, channel, vc):
        super().__init__(timeout=60)
        self.results = results
        self.guild_id = guild_id
        self.channel = channel
        self.vc = vc

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="1", style=discord.ButtonStyle.primary)
    async def select_1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.select_track(interaction, 0)

    @discord.ui.button(label="2", style=discord.ButtonStyle.primary)
    async def select_2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.select_track(interaction, 1)

    @discord.ui.button(label="3", style=discord.ButtonStyle.primary)
    async def select_3(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.select_track(interaction, 2)

    @discord.ui.button(label="4", style=discord.ButtonStyle.primary)
    async def select_4(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.select_track(interaction, 3)

    @discord.ui.button(label="5", style=discord.ButtonStyle.primary)
    async def select_5(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.select_track(interaction, 4)

    async def select_track(self, interaction: discord.Interaction, index: int):
        if index >= len(self.results):
            await interaction.response.defer()
            return

        meta = self.results[index]
        add_to_queue(self.guild_id, meta["title"], meta["artist"], meta["query"], meta["track_id"])

        if self.vc.is_playing() or self.vc.is_paused():
            position = len(get_queue(self.guild_id))
            await interaction.response.send_message(embed=embed(
                "➕ Добавлено в очередь",
                f"**{meta['title']}**\n{meta['artist']}\nПозиция: {position}",
            ))
        else:
            await interaction.response.defer()
            await play_next(interaction.guild, announce=False)
            np = get_now_playing(self.guild_id)
            await self.channel.send(embed=now_playing_embed(np if np else meta))

        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)


class TrackControlsView(discord.ui.View):
    def __init__(self, guild):
        super().__init__(timeout=None)
        self.guild = guild

    @discord.ui.button(label="⏭ Skip", style=discord.ButtonStyle.danger)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            skip_requested[self.guild.id] = True
            vc.stop()
            await interaction.response.defer()
        else:
            await interaction.response.send_message("Ничего не играет.", ephemeral=True)

    @discord.ui.button(label="⏸ Pause", style=discord.ButtonStyle.primary)
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            mark_paused(self.guild.id)
            await interaction.response.defer()
        else:
            await interaction.response.send_message("Ничего не играет.", ephemeral=True)

    @discord.ui.button(label="▶ Resume", style=discord.ButtonStyle.success)
    async def resume_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            mark_resumed(self.guild.id)
            await interaction.response.defer()
        else:
            await interaction.response.send_message("Не на паузе.", ephemeral=True)

    @discord.ui.button(label="🔊 Volume", style=discord.ButtonStyle.primary)
    async def volume_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = VolumeModal(self.guild)
        await interaction.response.send_modal(modal)


class VolumeModal(discord.ui.Modal, title="Установить громкость"):
    volume_input = discord.ui.TextInput(
        label="Громкость (0-200)",
        placeholder="100",
        min_length=1,
        max_length=3,
    )

    def __init__(self, guild):
        super().__init__()
        self.guild = guild

    async def on_submit(self, interaction: discord.Interaction):
        try:
            value = int(self.volume_input.value)
            if value < 0 or value > 200:
                await interaction.response.send_message("Укажи 0-200.", ephemeral=True)
                return

            level = value / 100
            set_volume(self.guild.id, level)

            vc = self.guild.voice_client
            if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
                vc.source.volume = level

            await interaction.response.send_message(f"🔊 Громкость: {value}%", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("Неверное значение.", ephemeral=True)


# ----------------------------------------------------------------------------
# События
# ----------------------------------------------------------------------------

@client.event
async def on_ready():
    if config.GUILD_ID:
        guild = discord.Object(id=int(config.GUILD_ID))
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
    if not any(trigger in content for trigger in config.KICK_TRIGGERS):
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


# Голосовое прослушивание (UPD: не работает из-за обновления Discord — 2026)
@tree.command(
    name="listen",
    description="Слушать голос и кикать за запретные слова"
)
async def listen(interaction: discord.Interaction):
    if not voice_listen.VOICE_RECV_AVAILABLE:
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

    if not isinstance(vc, voice_listen.voice_recv.VoiceRecvClient):
        await interaction.followup.send(
            "Я подключён обычным клиентом без приёма звука. "
            "Сделай `/leave` и снова `/listen`."
        )
        return

    if vc.is_listening():
        await interaction.followup.send("Я уже слушаю.")
        return

    try:
        await asyncio.to_thread(voice_listen.get_vosk_model)
    except FileNotFoundError as e:
        await interaction.followup.send(str(e))
        return

    sink = voice_listen.KeywordSink(guild, client.loop, kick_for_voice)
    listen_sinks[guild.id] = sink
    vc.listen(sink)

    await interaction.followup.send(embed=embed(
        "🎙 Слушаю канал",
        "Кикаю за: " + ", ".join(config.VOICE_TRIGGERS),
    ))


@tree.command(name="stoplisten", description="Перестать слушать голос")
async def stoplisten(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if voice_listen.VOICE_RECV_AVAILABLE \
            and isinstance(vc, voice_listen.voice_recv.VoiceRecvClient) \
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

    # Сначала проверим, есть ли прямая ссылка на трек
    if "/track/" in query:
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
    else:
        results = await search_top(query, limit=5)
        if not results:
            await interaction.followup.send("Ничего не найдено.")
            return

        lines = [
            f"{i+1}. **{r['title']}** — {r['artist']}"
            for i, r in enumerate(results)
        ]
        view = SearchResultsView(results, gid, interaction.channel, vc)
        await interaction.followup.send(
            embed=embed("🔍 Выбери трек (5 лучших результатов)", "\n".join(lines)),
            view=view,
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
        mark_paused(interaction.guild.id)
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
        mark_resumed(interaction.guild.id)
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
        view = TrackControlsView(interaction.guild)
        await interaction.response.send_message(
            embed=now_playing_embed(np),
            view=view,
        )
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


@tree.command(name="remove", description="Удалить трек из очереди по номеру")
async def remove(interaction: discord.Interaction, number: int):
    if queue_remove_track(interaction.guild.id, number - 1):
        await interaction.response.send_message(
            f"🗑 Трек #{number} удалён из очереди"
        )
    else:
        await interaction.response.send_message(
            "Неверный номер трека.", ephemeral=True
        )


@tree.command(name="move", description="Переместить трек в очереди")
async def move(interaction: discord.Interaction, from_pos: int, to_pos: int):
    if move_track(interaction.guild.id, from_pos - 1, to_pos - 1):
        await interaction.response.send_message(
            f"↕ Трек #{from_pos} перемещён на позицию {to_pos}"
        )
    else:
        await interaction.response.send_message(
            "Неверные позиции.", ephemeral=True
        )


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


@tree.command(
    name="import",
    description="Импортировать плейлист/альбом Яндекса по ссылке"
)
async def import_playlist(interaction: discord.Interaction, url: str):
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

    tracks = await resolve_playlist(url)
    if not tracks:
        await interaction.followup.send("Не удалось загрузить плейлист.")
        return

    count = 0
    for track in tracks:
        add_to_queue(gid, track["title"], track["artist"], track["query"], track.get("track_id"))
        count += 1

    if not (vc.is_playing() or vc.is_paused()):
        await play_next(guild, announce=False)

    await interaction.followup.send(embed=embed(
        "📂 Плейлист загружен",
        f"Добавлено треков: {count}",
    ))


async def main():
    tasks = [client.start(config.TOKEN)]
    if telegram_bot.dp is not None and telegram_bot.tg_bot is not None:
        tasks.append(telegram_bot.dp.start_polling(telegram_bot.tg_bot))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
