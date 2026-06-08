import config
from core import (
    client,
    connect_voice,
    play_next,
    listen_sinks,
)
from sources import resolve_meta
from player import (
    get_queue,
    clear_queue,
    shuffle_queue,
    set_loop_mode,
    get_loop_mode,
    add_to_queue,
    remove_track as queue_remove_track,
    move_track,
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
    import discord  # для isinstance(vc.source, PCMVolumeTransformer)
    TG_AVAILABLE = True
except Exception:
    TG_AVAILABLE = False

tg_bot = None
dp = None

if TG_AVAILABLE and config.TELEGRAM_TOKEN:
    tg_bot = Bot(token=config.TELEGRAM_TOKEN)
    dp = Dispatcher()

    # Глобальный фильтр: пропускаем только разрешённых пользователей.
    # Если список пуст — не пройдёт никто (безопасный дефолт).
    dp.message.filter(F.from_user.id.in_(config.ALLOWED_TG_USERS))
    dp.callback_query.filter(F.from_user.id.in_(config.ALLOWED_TG_USERS))

    def tg_guild():
        if config.GUILD_ID:
            g = client.get_guild(int(config.GUILD_ID))
            if g:
                return g
        return client.guilds[0] if client.guilds else None

    async def tg_ensure_voice(guild):
        """Возвращает голосовой клиент, подключаясь к VOICE_CHANNEL_ID при нужде."""
        vc = guild.voice_client
        if vc is not None:
            return vc
        if config.VOICE_CHANNEL_ID is None:
            return None
        channel = client.get_channel(config.VOICE_CHANNEL_ID)
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
        "/remove <номер> — убрать из очереди\n"
        "/move <откуда> <куда> — переставить\n"
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
                from core import skip_requested
                skip_requested[guild.id] = True
                vc.stop()
                await call.answer("⏭ Пропущено")
            else:
                await call.answer("Ничего не играет.")
        elif data == "ctl:stop":
            if vc:
                from core import skip_requested
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
            from core import skip_requested
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
            from core import skip_requested
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
        await message.answer(queue_text(guild))

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

    @dp.message(Command("remove"))
    async def tg_remove(message: Message, command: CommandObject):
        arg = (command.args or "").strip()
        if not arg.isdigit():
            await message.answer("Использование: /remove <номер>")
            return
        guild = tg_guild()
        if guild is None:
            await message.answer("Сервер недоступен.")
            return
        if queue_remove_track(guild.id, int(arg) - 1):
            await message.answer(f"🗑 Трек #{arg} удалён из очереди")
        else:
            await message.answer("Неверный номер трека.")

    @dp.message(Command("move"))
    async def tg_move(message: Message, command: CommandObject):
        parts = (command.args or "").split()
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            await message.answer("Использование: /move <откуда> <куда>")
            return
        guild = tg_guild()
        if guild is None:
            await message.answer("Сервер недоступен.")
            return
        if move_track(guild.id, int(parts[0]) - 1, int(parts[1]) - 1):
            await message.answer(f"↕ #{parts[0]} → {parts[1]}")
        else:
            await message.answer("Неверные позиции.")

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
