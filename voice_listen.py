import os
import json
import asyncio

import config

# ----------------------------------------------------------------------------
# Голосовое прослушивание — опционально и СЕЙЧАС НЕ РАБОТАЕТ из-за обязательного
# сквозного шифрования голоса в Discord (DAVE, с марта 2026). Код оставлен.
# Если расширение/зависимости не установлены — бот всё равно запустится.
# ----------------------------------------------------------------------------

try:
    from discord.ext import voice_recv
    VOICE_RECV_AVAILABLE = True
except Exception:
    voice_recv = None
    VOICE_RECV_AVAILABLE = False

_vosk_model = None


def get_vosk_model():
    """Грузит модель Vosk один раз. Бросает понятную ошибку, если её нет."""
    global _vosk_model
    if _vosk_model is None:
        from vosk import Model  # импорт здесь, чтобы не требовать vosk при старте
        if not os.path.isdir(config.VOSK_MODEL_PATH):
            raise FileNotFoundError(
                f"Не найдена модель Vosk в '{config.VOSK_MODEL_PATH}'. "
                f"Скачай русскую модель и распакуй её туда."
            )
        _vosk_model = Model(config.VOSK_MODEL_PATH)
    return _vosk_model


if VOICE_RECV_AVAILABLE:

    class KeywordSink(voice_recv.AudioSink):
        """Распознаёт речь каждого участника и зовёт on_trigger(member) на слове.

        on_trigger — корутина, выполняемая в переданном event loop.
        """

        def __init__(self, guild, loop, on_trigger):
            super().__init__()
            self.guild = guild
            self.loop = loop
            self.on_trigger = on_trigger
            self.recognizers = {}  # user_id -> KaldiRecognizer

        def wants_opus(self):
            return False  # нам нужен раскодированный PCM

        def _get_recognizer(self, user_id):
            from vosk import KaldiRecognizer
            rec = self.recognizers.get(user_id)
            if rec is None:
                rec = KaldiRecognizer(get_vosk_model(), 16000)
                self.recognizers[user_id] = rec
            return rec

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

            if text and any(t in text for t in config.VOICE_TRIGGERS):
                from vosk import KaldiRecognizer
                self.recognizers[user.id] = KaldiRecognizer(
                    get_vosk_model(), 16000
                )
                member = self.guild.get_member(user.id)
                if member:
                    asyncio.run_coroutine_threadsafe(
                        self.on_trigger(member), self.loop
                    )

        def cleanup(self):
            self.recognizers.clear()
