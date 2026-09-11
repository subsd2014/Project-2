import os
import json
import base64
import asyncio
import queue
import re
import threading

import sounddevice as sd
import websockets


# ============================================================
# CONFIGURATION
# ============================================================

MODEL = "gpt-realtime-2.1"
VOICE = "marin"

SAMPLE_RATE = 24000
CHANNELS = 1

MIC_DEVICE = 2
SPEAKER_DEVICE = 4

CHUNK_MS = 10
CHUNK_SAMPLES = int(
    SAMPLE_RATE * CHUNK_MS / 1000
)

# Faster response
SILENCE_DURATION_MS = 200


# ============================================================
# OPENAI API KEY
# ============================================================

OPENAI_API_KEY = os.getenv(
    "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"
)

if not OPENAI_API_KEY:

    raise RuntimeError(
        "OPENAI_API_KEY পাওয়া যায়নি।"
    )


WS_URL = (
    f"wss://api.openai.com/v1/realtime"
    f"?model={MODEL}"
)


# ============================================================
# SYSTEM INSTRUCTIONS
# ============================================================

SYSTEM_INSTRUCTIONS = """
You are Pamela, a natural female AI voice customer-care assistant.

Your context is real estate and customer care.

============================================================
SUPPORTED LANGUAGES
============================================================

Supported languages:

1. Bengali
2. Hindi
3. English
4. Banglish
5. Hinglish

The application will tell you the customer's selected language.

============================================================
LANGUAGE LOCK
============================================================

Once the customer selects a language:

- The selected language is LOCKED.
- Never change the language automatically.
- Never ask for the language again during the same conversation.
- If the customer mixes languages, continue using the locked language.

Bengali:
Speak natural Bengali.

Hindi:
Speak natural Hindi.

English:
Speak natural English.

Banglish:
Speak Bengali naturally with normal English words.

Hinglish:
Speak Hindi naturally with normal English words.

============================================================
VOICE STYLE
============================================================

- Natural human female customer-care voice.
- Warm, polite and professional.
- Never robotic.
- Short and conversational.
- Normally one sentence.
- Maximum two short sentences.
- Do not unnecessarily repeat the question.
- Do not mention that you are an AI.

============================================================
CONVERSATION
============================================================

- Listen to the complete customer question.
- Answer only when the application asks you to answer.
- Continue the conversation naturally.
- Do not start speaking by yourself.
- If the customer interrupts you, stop speaking immediately.
"""


# ============================================================
# STATE
# ============================================================

# Possible states:
#
# sleeping
# active
# responding
# paused
# stopped

pamela_state = "sleeping"

# True while Pamela is actually speaking
ai_is_speaking = False

# True only while an OpenAI response is active
response_active = False

singing_mode = False


# ============================================================
# CUSTOMER INFORMATION
# ============================================================

customer_name = ""

selected_language = ""

conversation_initialized = False


# ============================================================
# CONTROL FLAGS
# ============================================================

# Used when a completely new conversation is requested
new_conversation_requested = False

# Used to stop current conversation
stop_requested = False


# ============================================================
# MICROPHONE QUEUE
# ============================================================

mic_queue = queue.Queue()


# ============================================================
# MICROPHONE CALLBACK
# ============================================================

def mic_callback(
    indata,
    frames,
    time_info,
    status
):

    try:

        mic_queue.put(
            bytes(indata)
        )

    except Exception:

        pass


# ============================================================
# STREAMING SPEAKER
# ============================================================

class StreamingSpeaker:

    def __init__(self, sample_rate, device):
        self.sample_rate = sample_rate
        self.device = device
        self.audio_queue = queue.Queue()
        self.stream = None
        self.running = False
        self.thread = None
        self.interrupt_event = threading.Event()

    def start(self):
        self.running = True
        self.interrupt_event.clear()
        self.stream = sd.RawOutputStream(
            samplerate=self.sample_rate, blocksize=CHUNK_SAMPLES,
            device=self.device, channels=CHANNELS, dtype="int16"
        )
        self.stream.start()
        self.thread = threading.Thread(target=self._playback_loop, daemon=True)
        self.thread.start()

    def _playback_loop(self):
        while self.running:
            try:
                pcm_bytes = self.audio_queue.get(timeout=0.05)
                try:
                    if self.interrupt_event.is_set():
                        continue
                    if self.stream is not None and pcm_bytes and self.running:
                        if not self.stream.active:
                            try: self.stream.start()
                            except Exception: pass
                        if not self.interrupt_event.is_set():
                            self.stream.write(pcm_bytes)
                finally:
                    self.audio_queue.task_done()
            except queue.Empty:
                continue
            except Exception:
                if not self.running: break

    def write(self, pcm_bytes):
        if self.running and pcm_bytes and not self.interrupt_event.is_set():
            self.audio_queue.put(pcm_bytes)

    def wait_until_empty(self):
        try: self.audio_queue.join()
        except Exception: pass

    def clear(self):
        while True:
            try:
                self.audio_queue.get_nowait(); self.audio_queue.task_done()
            except queue.Empty: break

    def hard_stop(self):
        self.interrupt_event.set()
        self.clear()
        try:
            if self.stream is not None: self.stream.abort()
        except Exception: pass

    def resume(self):
        self.interrupt_event.clear()

    def stop(self):
        self.running = False
        self.interrupt_event.set()
        self.clear()
        try:
            if self.stream: self.stream.abort()
        except Exception: pass
        if self.thread:
            self.thread.join(timeout=2); self.thread=None
        if self.stream:
            try: self.stream.stop(); self.stream.close()
            except Exception: pass
            self.stream=None


# ============================================================
# TEXT NORMALIZATION
# ============================================================

def normalize_text(text):

    if not text:

        return ""

    text = text.strip().lower()

    text = re.sub(
        r"[.,!?;:'\"“”‘’()\[\]{}<>/\\|]+",
        " ",
        text
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    ).strip()

    return text


# ============================================================
# COMMAND DETECTION
# ============================================================

def is_pamela_command(
    text,
    command
):

    normalized = normalize_text(
        text
    )

    command = normalize_text(
        command
    )

    return normalized == command


# ============================================================
# ONLY PAMELA WAKE COMMAND
# ============================================================

def is_pamela_only(
    text
):

    return is_pamela_command(
        text,
        "pamela"
    )


# ============================================================
# PAMELA PAUSE
# ============================================================

def is_pamela_pause(
    text
):

    return is_pamela_command(
        text,
        "pamela pause"
    )


# ============================================================
# PAMELA STOP
# ============================================================

def is_pamela_stop(
    text
):

    return is_pamela_command(
        text,
        "pamela stop"
    )


# ============================================================
# SINGING REQUEST DETECTION
# ============================================================

def is_singing_request(text):
    normalized = normalize_text(text)
    patterns = [
        r"\bsing\b", r"\bsing a song\b", r"\bsing me a song\b",
        r"\bgana gao\b", r"\bekta gaan gao\b", r"\bekta gan gao\b",
        r"\bgaan gao\b", r"\bgan gao\b", r"\bgaan sunao\b",
        r"\bgan sunao\b", r"\bgana sunao\b", r"গান গাও", r"গান শোনাও",
        r"গান গেয়ে শোনাও", r"গান গাইবে", r"गाना गाओ", r"गाना सुनाओ",
        r"एक गाना गाओ"
    ]
    return any(re.search(x, normalized, re.IGNORECASE) for x in patterns)


# ============================================================
# CLEAR MICROPHONE QUEUE
# ============================================================

def clear_mic_queue():

    while True:

        try:

            mic_queue.get_nowait()

            mic_queue.task_done()

        except queue.Empty:

            break


# ============================================================
# CREATE RESPONSE
# ============================================================

async def create_response(
    ws,
    instructions
):

    global response_active

    event = {

        "type":
            "response.create",

        "response": {

            "output_modalities": [
                "audio"
            ],

            "instructions":
                instructions,

        }

    }

    response_active = True

    await ws.send(
        json.dumps(event)
    )


# ============================================================
# CANCEL CURRENT RESPONSE
# ============================================================

async def cancel_current_response(
    ws,
    speaker
):

    global ai_is_speaking
    global response_active
    global singing_mode
    singing_mode = False

    # --------------------------------------------------------
    # Immediately stop speaker
    # --------------------------------------------------------

    ai_is_speaking = False

    try:

        speaker.clear()

    except Exception:

        pass

    # --------------------------------------------------------
    # Only cancel if response is really active
    # --------------------------------------------------------

    if not response_active:

        return

    # Mark inactive first
    response_active = False

    try:

        await ws.send(
            json.dumps(
                {
                    "type":
                        "response.cancel"
                }
            )
        )

    except Exception:

        pass


# ============================================================
# RESET CUSTOMER INFORMATION
# ============================================================

def reset_customer_information():

    global customer_name
    global selected_language
    global conversation_initialized
    global singing_mode

    singing_mode = False
    customer_name = ""

    selected_language = ""

    conversation_initialized = False


# ============================================================
# INITIAL GREETING
# ============================================================

async def start_initial_greeting(
    ws
):

    global pamela_state
    global ai_is_speaking

    pamela_state = "responding"

    ai_is_speaking = True

    print()

    print(
        "🔊 Pamela: Initial greeting..."
    )

    await create_response(
        ws,
        """
Start a new customer conversation.

Speak naturally in Bengali.

Say:

"নমস্কার, আমি Pamela। আপনার নাম কী? আর আপনি কোন ভাষায় কথা বলতে চান ?"

Wait for the customer's answer.

Do not ask anything else.

Do not mention AI.

Keep it natural and friendly.
"""
    )


# ============================================================
# START NEW CONVERSATION
# ============================================================

async def start_new_conversation(
    ws,
    speaker
):

    global pamela_state
    global new_conversation_requested

    global customer_name
    global selected_language
    global conversation_initialized

    # --------------------------------------------------------
    # Stop any current Pamela response
    # --------------------------------------------------------

    await cancel_current_response(
        ws,
        speaker
    )

    # --------------------------------------------------------
    # Reset customer information
    # --------------------------------------------------------

    reset_customer_information()

    clear_mic_queue()

    # --------------------------------------------------------
    # New conversation
    # --------------------------------------------------------

    pamela_state = "active"

    new_conversation_requested = False

    print()

    print(
        "🆕 NEW CONVERSATION"
    )

    print(
        "🔔 Pamela activated"
    )

    print(
        "👂 Listening..."
    )

    # --------------------------------------------------------
    # Greeting
    # --------------------------------------------------------

    await start_initial_greeting(
        ws
    )


# ============================================================
# PAUSE PAMELA
# ============================================================

async def pause_pamela(
    ws,
    speaker
):

    global pamela_state

    # Stop current audio immediately
    await cancel_current_response(
        ws,
        speaker
    )

    clear_mic_queue()

    pamela_state = "paused"

    print()

    print(
        "⏸️ Pamela paused."
    )

    print(
        "👉 Say: Pamela"
    )


# ============================================================
# STOP PAMELA
# ============================================================

async def stop_pamela(
    ws,
    speaker
):

    global pamela_state
    global stop_requested

    # Stop current response
    await cancel_current_response(
        ws,
        speaker
    )

    clear_mic_queue()

    # Reset everything
    reset_customer_information()

    pamela_state = "stopped"

    stop_requested = True

    print()

    print(
        "🛑 Pamela conversation stopped."
    )

    print(
        "👉 Say: Pamela for a new conversation."
    )


# ============================================================
# LANGUAGE DETECTION
# ============================================================

def detect_language(
    text
):

    normalized = normalize_text(
        text
    )

    if not normalized:

        return ""

    if (
        "banglish" in normalized
        or "bangla english" in normalized
    ):

        return "Banglish"

    if (
        "hinglish" in normalized
        or "hindi english" in normalized
    ):

        return "Hinglish"

    if (
        "bengali" in normalized
        or "bangla" in normalized
    ):

        return "Bengali"

    if "hindi" in normalized:

        return "Hindi"

    if "english" in normalized:

        return "English"

    return ""


# ============================================================
# EXTRACT CUSTOMER NAME
# ============================================================

def extract_customer_name(
    text
):

    if not text:

        return ""

    original = text.strip()

    normalized = normalize_text(
        original
    )

    # Remove language selection
    language_patterns = (
        r"\b(?:bengali|bangla|hindi|"
        r"english|banglish|hinglish)\b"
    )

    parts = re.split(
        language_patterns,
        normalized,
        flags=re.IGNORECASE
    )

    if parts:

        cleaned = parts[0].strip()

    else:

        cleaned = normalized

    # Common phrases
    patterns = [

        r"\bmy name is\b",

        r"\bi am\b",

        r"\bi'm\b",

        r"\bthis is\b",

        r"\bmera naam hai\b",

        r"\bmera naam\b",

        r"\bmera name\b",

        r"\bnaam hai\b",

        r"\bamar naam\b",

        r"\bamar nam\b",

        r"\bami\b",

    ]

    for pattern in patterns:

        cleaned = re.sub(
            pattern,
            "",
            cleaned,
            flags=re.IGNORECASE
        )

    # Bengali/Hindi filler
    cleaned = re.sub(
        r"^(আমি|আমার নাম|নাম|मेरा नाम|नाम)\s*",
        "",
        cleaned,
        flags=re.IGNORECASE
    )

    cleaned = cleaned.strip()

    words = cleaned.split()

    if not words:

        return ""

    # Avoid taking a full sentence as a name
    if len(words) > 5:

        return ""

    bad_values = {

        "yes",
        "no",
        "okay",
        "ok",
        "haan",
        "ji",
        "yes i am",
        "হ্যাঁ",
        "জি",

    }

    if cleaned.lower() in bad_values:

        return ""

    return cleaned


# ============================================================
# CUSTOMER SETUP
# ============================================================

async def handle_customer_setup(
    ws,
    text
):

    global customer_name
    global selected_language
    global conversation_initialized

    global pamela_state
    global ai_is_speaking

    # --------------------------------------------------------
    # Detect language
    # --------------------------------------------------------

    detected_language = detect_language(
        text
    )

    # --------------------------------------------------------
    # Language is selected only once
    # --------------------------------------------------------

    if (
        not selected_language
        and detected_language
    ):

        selected_language = (
            detected_language
        )

        print()

        print(
            f"🌐 Language selected: "
            f"{selected_language}"
        )

    # --------------------------------------------------------
    # Detect name
    # --------------------------------------------------------

    if not customer_name:

        detected_name = (
            extract_customer_name(
                text
            )
        )

        if detected_name:

            customer_name = (
                detected_name.title()
            )

            print(
                f"👤 Customer: "
                f"{customer_name}"
            )

    # --------------------------------------------------------
    # Both available
    # --------------------------------------------------------

    if (
        customer_name
        and selected_language
    ):

        conversation_initialized = True

        pamela_state = "responding"

        ai_is_speaking = True

        await create_response(
            ws,
            f"""
Customer setup is complete.

Customer name:
{customer_name}

LOCKED LANGUAGE:
{selected_language}

The language is now permanently locked
for this conversation.

Do NOT ask for the language again.

Do NOT change the language automatically.

Use only:

{selected_language}

Language behavior:

Bengali:
Natural Bengali.

Hindi:
Natural Hindi.

English:
Natural English.

Banglish:
Natural Bengali with English words.

Hinglish:
Natural Hindi with English words.

Say a short confirmation.

Example:

"ধন্যবাদ {customer_name}, তাহলে আমরা {selected_language}-এ কথা বলব। বলুন, কীভাবে সাহায্য করতে পারি?"

Keep it short and natural.
"""
        )

        return

    # --------------------------------------------------------
    # Name missing
    # --------------------------------------------------------

    if not customer_name:

        pamela_state = "responding"

        ai_is_speaking = True

        await create_response(
            ws,
            """
Ask only for the customer's name.

Speak naturally in Bengali:

"আপনার নামটা বলবেন?"

Do not ask the language again
if it has already been selected.
"""
        )

        return

    # --------------------------------------------------------
    # Language missing
    # --------------------------------------------------------

    if not selected_language:

        pamela_state = "responding"

        ai_is_speaking = True

        await create_response(
            ws,
            f"""
The customer name is:

{customer_name}

The language has not been selected.

Ask naturally in Bengali:

"ধন্যবাদ {customer_name}। আপনি কোন ভাষায় কথা বলতে চান ?"

Do not continue normal conversation
until a supported language is selected.
"""
        )

        return


# ============================================================
# SING A SONG
# ============================================================

async def sing_song(ws, song_request):
    global pamela_state, ai_is_speaking, singing_mode
    if pamela_state in ("paused", "stopped", "sleeping"): return
    singing_mode = True
    pamela_state = "responding"
    ai_is_speaking = True
    language = selected_language or "Bengali"
    print()
    print("🎵 Pamela singing...")
    await create_response(ws, f"""
The customer explicitly requested a song.
Customer request: {song_request}
Selected language: {language}

SINGING MODE:
- Sing a short, pleasant ORIGINAL song.
- Do not reproduce or quote lyrics of a known copyrighted song.
- Do not imitate a specific recording artist.
- Use the customer's locked language/style.
- Bengali: natural Bengali singing.
- Hindi: natural Hindi singing.
- English: natural English singing.
- Banglish: Bengali with natural English words.
- Hinglish: Hindi with natural English words.
- Make it musical and expressive, not normal speech.
- Keep it around 20-30 seconds.
- Start singing directly.
- If the customer interrupts, stop immediately.
""")


# ============================================================
# ANSWER CUSTOMER
# ============================================================

async def answer_customer(
    ws,
    question
):

    global pamela_state
    global ai_is_speaking

    if not question:

        return

    # --------------------------------------------------------
    # Safety: don't answer while paused/stopped
    # --------------------------------------------------------

    if pamela_state in (
        "paused",
        "stopped",
        "sleeping"
    ):

        return

    pamela_state = "responding"

    ai_is_speaking = True

    print()

    print(
        "🔊 Pamela speaking..."
    )

    await create_response(
        ws,
        f"""
Answer the customer's question.

Customer name:
{customer_name}

LOCKED LANGUAGE:
{selected_language}

IMPORTANT:

The selected language is LOCKED.

Never change it automatically.

If the customer uses another language,
continue answering in the locked language.

Language rules:

Bengali:
Speak Bengali.

Hindi:
Speak Hindi.

English:
Speak English.

Banglish:
Speak Bengali naturally with English words.

Hinglish:
Speak Hindi naturally with English words.

Customer question:

{question}

Rules:

- Natural and conversational.
- Polite and helpful.
- Short answer.
- Normally one sentence.
- Maximum two short sentences.
- Do not repeat the question.
- Do not mention AI.
"""
    )


# ============================================================
# HANDLE TRANSCRIPT
# ============================================================

async def handle_transcript(
    ws,
    speaker,
    transcript
):

    global pamela_state

    if not transcript:

        return

    transcript = transcript.strip()

    if not transcript:

        return

    normalized = normalize_text(
        transcript
    )

    # ========================================================
    # PAMELA STOP
    # Highest priority
    # ========================================================

    if is_pamela_stop(
        transcript
    ):

        await stop_pamela(
            ws,
            speaker
        )

        return

    # ========================================================
    # PAMELA PAUSE
    # ========================================================

    if is_pamela_pause(
        transcript
    ):

        await pause_pamela(
            ws,
            speaker
        )

        return

    # ========================================================
    # PAMELA ONLY
    #
    # Always starts a completely NEW conversation.
    # ========================================================

    if is_pamela_only(
        transcript
    ):

        await start_new_conversation(
            ws,
            speaker
        )

        return

    # ========================================================
    # PAUSED
    #
    # No response to any speech.
    # ========================================================

    if pamela_state == "paused":

        return

    # ========================================================
    # STOPPED
    #
    # No response until Pamela.
    # ========================================================

    if pamela_state == "stopped":

        return

    # ========================================================
    # SLEEPING
    #
    # No automatic response.
    # Only exact Pamela activates.
    # ========================================================

    if pamela_state == "sleeping":

        return

    if pamela_state == "active" and conversation_initialized:
        if is_singing_request(transcript):
            await sing_song(ws, transcript)
            return

    # ========================================================
    # ACTIVE
    # ========================================================

    if pamela_state == "active":

        # ----------------------------------------------------
        # Initial customer setup
        # ----------------------------------------------------

        if not conversation_initialized:

            await handle_customer_setup(
                ws,
                transcript
            )

            return

        # ----------------------------------------------------
        # Normal conversation
        # ----------------------------------------------------

        await answer_customer(
            ws,
            transcript
        )

        return

    # ========================================================
    # RESPONDING
    # ========================================================

    if pamela_state == "responding":

        return


# ============================================================
# MICROPHONE SENDER
# ============================================================

async def microphone_sender(
    ws
):

    while True:

        pcm_bytes = await asyncio.to_thread(
            mic_queue.get
        )

        try:

            audio_base64 = (
                base64.b64encode(
                    pcm_bytes
                ).decode(
                    "ascii"
                )
            )

            event = {

                "type":
                    "input_audio_buffer.append",

                "audio":
                    audio_base64,

            }

            await ws.send(
                json.dumps(event)
            )

        except asyncio.CancelledError:

            raise

        except Exception as e:

            print(
                f"🎤 Microphone error: {e}"
            )

            break


# ============================================================
# RECEIVE REALTIME EVENTS
# ============================================================

async def receive_events(
    ws,
    speaker
):

    global pamela_state
    global ai_is_speaking
    global response_active
    global singing_mode

    while True:

        raw_message = await ws.recv()

        event = json.loads(
            raw_message
        )

        event_type = event.get(
            "type"
        )

        # ====================================================
        # USER STARTED SPEAKING
        #
        # MOST IMPORTANT:
        # Immediately stop Pamela.
        # ====================================================

        if (
            event_type
            == "input_audio_buffer.speech_started"
        ):

            if ai_is_speaking:

                print()

                print(
                    "🛑 Customer interrupted Pamela"
                )

                await cancel_current_response(
                    ws,
                    speaker
                )

                if pamela_state not in (
                    "paused",
                    "stopped"
                ):

                    pamela_state = "active"

                clear_mic_queue()

        # ====================================================
        # AUDIO OUTPUT
        # ====================================================

        elif (
            event_type
            == "response.output_audio.delta"
        ):

            audio_base64 = event.get(
                "delta"
            )

            if audio_base64:

                pcm_bytes = (
                    base64.b64decode(
                        audio_base64
                    )
                )

                # Only play if still speaking
                if ai_is_speaking:

                    speaker.write(
                        pcm_bytes
                    )

        # ====================================================
        # AUDIO OUTPUT DONE
        # ====================================================

        elif (
            event_type
            == "response.output_audio.done"
        ):

            await asyncio.to_thread(
                speaker.wait_until_empty
            )

            ai_is_speaking = False

        # ====================================================
        # CUSTOMER TRANSCRIPTION
        # ====================================================

        elif (
            event_type
            == "conversation.item.input_audio_transcription.completed"
        ):

            transcript = event.get(
                "transcript",
                ""
            )

            if transcript:

                await handle_transcript(
                    ws,
                    speaker,
                    transcript
                )

        # ====================================================
        # RESPONSE CREATED
        # ====================================================

        elif (
            event_type
            == "response.created"
        ):

            response_active = True

        # ====================================================
        # RESPONSE CANCELLED
        # ====================================================

        elif (
            event_type
            == "response.cancelled"
        ):

            response_active = False

            ai_is_speaking = False
            singing_mode = False

            try:

                speaker.clear()

            except Exception:

                pass

            if pamela_state not in (
                "paused",
                "stopped"
            ):

                pamela_state = "active"

        # ====================================================
        # RESPONSE DONE
        # ====================================================

        elif (
            event_type
            == "response.done"
        ):

            response_active = False

            ai_is_speaking = False
            singing_mode = False

            try:

                speaker.clear()

            except Exception:

                pass

            if pamela_state not in (
                "paused",
                "stopped"
            ):

                pamela_state = "active"

                print()

                print(
                    "✅ Response completed"
                )

                print(
                    "👂 Pamela is listening..."
                )

        # ====================================================
        # SESSION CREATED
        # ====================================================

        elif (
            event_type
            == "session.created"
        ):

            print(
                "✅ Realtime session created"
            )

        # ====================================================
        # SESSION UPDATED
        # ====================================================

        elif (
            event_type
            == "session.updated"
        ):

            print(
                "⚙️ Session updated"
            )

        # ====================================================
        # ERROR
        # ====================================================

        elif event_type == "error":

            error = event.get(
                "error",
                {}
            )

            # Ignore cancellation race errors
            if (
                error.get("code")
                == "response_cancel_not_active"
            ):

                response_active = False

                continue

            print()

            print(
                "❌ OpenAI Realtime error:"
            )

            print(
                json.dumps(
                    error,
                    ensure_ascii=False,
                    indent=2
                )
            )


# ============================================================
# MAIN
# ============================================================

async def main():

    global pamela_state
    global ai_is_speaking
    global response_active
    global singing_mode

    print()

    print(
        "=" * 70
    )

    print(
        "             PAMELA AI VOICE CUSTOMER CARE"
    )

    print(
        "=" * 70
    )

    print()

    print(
        f"Model        : {MODEL}"
    )

    print(
        f"Voice        : {VOICE}"
    )

    print(
        f"Sample Rate  : {SAMPLE_RATE}"
    )

    print(
        f"Mic Device   : {MIC_DEVICE}"
    )

    print(
        f"Speaker      : {SPEAKER_DEVICE}"
    )

    print(
        "Wake Command : Pamela"
    )

    print(
        "Pause Command: Pamela Pause"
    )

    print(
        "Stop Command : Pamela Stop"
    )

    print(
        "Languages    : Bengali / Hindi / English / "
        "Banglish / Hinglish"
    )

    print()

    print(
        "Rules:"
    )

    print(
        "1. Pamela = NEW conversation."
    )

    print(
        "2. Pamela Pause = no response."
    )

    print(
        "3. Pamela Stop = stop conversation."
    )

    print(
        "4. Selected language stays locked."
    )

    print(
        "5. Customer can interrupt Pamela immediately."
    )

    print(
        "6. No 1-minute waiting timer."
    )

    print(
        "7. Customer song request = Singing Mode."
    )

    print()

    print(
        "=" * 70
    )

    headers = {

        "Authorization":
            f"Bearer {OPENAI_API_KEY}"

    }

    speaker = None
    mic_stream = None

    try:

        # ====================================================
        # CONNECT
        # ====================================================

        async with websockets.connect(

            WS_URL,

            additional_headers=headers,

            max_size=None,

            open_timeout=30,

            ping_interval=20,

            ping_timeout=20,

        ) as ws:

            print()

            print(
                "🌐 Connected to OpenAI Realtime API"
            )

            # =================================================
            # SESSION CONFIGURATION
            # =================================================

            session_update = {

                "type":
                    "session.update",

                "session": {

                    "type":
                        "realtime",

                    "model":
                        MODEL,

                    "instructions":
                        SYSTEM_INSTRUCTIONS,

                    "output_modalities": [
                        "audio"
                    ],

                    "audio": {

                        # -------------------------------------
                        # INPUT
                        # -------------------------------------

                        "input": {

                            "format": {

                                "type":
                                    "audio/pcm",

                                "rate":
                                    SAMPLE_RATE,

                            },

                            "noise_reduction": {

                                "type":
                                    "near_field"

                            },

                            "transcription": {

                                "model":
                                    "gpt-4o-mini-transcribe"

                            },

                            "turn_detection": {

                                "type":
                                    "server_vad",

                                "threshold":
                                    0.35,

                                "prefix_padding_ms":
                                    100,

                                "silence_duration_ms":
                                    SILENCE_DURATION_MS,

                                "create_response":
                                    False,

                                "interrupt_response":
                                    True,

                            },

                        },

                        # -------------------------------------
                        # OUTPUT
                        # -------------------------------------

                        "output": {

                            "format": {

                                "type":
                                    "audio/pcm",

                                "rate":
                                    SAMPLE_RATE,

                            },

                            "voice":
                                VOICE,

                            "speed":
                                1.0,

                        },

                    },

                },

            }

            await ws.send(
                json.dumps(
                    session_update
                )
            )

            print(
                "⚙️ Session configuration sent"
            )

            # =================================================
            # START SPEAKER
            # =================================================

            speaker = StreamingSpeaker(
                SAMPLE_RATE,
                SPEAKER_DEVICE
            )

            speaker.start()

            print(
                "🔊 Speaker started"
            )

            # =================================================
            # START MICROPHONE
            # =================================================

            mic_stream = sd.RawInputStream(

                samplerate=
                    SAMPLE_RATE,

                blocksize=
                    CHUNK_SAMPLES,

                device=
                    MIC_DEVICE,

                channels=
                    CHANNELS,

                dtype=
                    "int16",

                callback=
                    mic_callback,

            )

            mic_stream.start()

            print(
                "🎤 Microphone started"
            )

            # =================================================
            # INITIAL STATE
            # =================================================

            pamela_state = "sleeping"

            ai_is_speaking = False

            response_active = False
            singing_mode = False

            reset_customer_information()

            print()

            print(
                "😴 Pamela is sleeping."
            )

            print()

            print(
                "👉 Say: Pamela"
            )

            print(
                "👉 Say: Pamela Pause"
            )

            print(
                "👉 Say: Pamela Stop"
            )

            print()

            # =================================================
            # RUN
            # =================================================

            await asyncio.gather(

                microphone_sender(
                    ws
                ),

                receive_events(
                    ws,
                    speaker
                ),

            )

    except KeyboardInterrupt:

        print()

        print(
            "🛑 Stopped by user"
        )

    except Exception as e:

        print()

        print(
            "❌ Fatal error:"
        )

        print(
            e
        )

    finally:

        print()

        print(
            "🧹 Cleaning up..."
        )

        if mic_stream:

            try:

                mic_stream.stop()

                mic_stream.close()

            except Exception:

                pass

        if speaker:

            try:

                speaker.stop()

            except Exception:

                pass

        print(
            "✅ Pamela stopped."
        )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        print()

        print(
            "🛑 Pamela stopped."
        )