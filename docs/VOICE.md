# Voice bridge plugin

This is a source-tree integration. The existing RC1 wheel and `MANIFEST.sha256` still describe the earlier build and do not include these changes; build and verify a new distribution before deploying them.

Voice support is optional and disabled by default. It uses a small Node.js Discord voice receiver and sends completed speech segments to a configured ASR provider. Recognized text is submitted through `SayukiBot.submit_external_event()` as a normal 4.6 `Event` and `Turn`, so the existing queue, context, memory policy, and delivery pipeline remain in use. Voice transcripts are explicit plugin events and enter the conversation engine without the ordinary attention classifier. The existing tool registry supplies `VOICE_JOIN` and `VOICE_LEAVE`; it is static registration, not a runtime third-party plugin loader.

## Setup

1. Install Node.js 20 or later, then install the locked bridge dependencies from the project root:

   ```sh
   npm ci
   ```

2. In `config/runtime.toml`, enable the bundled plugin and set an ASR model. The example config uses an OpenAI compatible endpoint:

   ```toml
   [plugins]
   enabled = ["voice"]

   [plugins.voice]
   asr_provider = "openai-compatible"
   asr_model = "whisper-1"
   asr_api_key_env = "VOICE_ASR_API_KEY"
   ```

   For NVIDIA, set `asr_provider = "nvidia"` and the appropriate model name. For ElevenLabs, set `asr_provider = "elevenlabs"` and an ElevenLabs model ID. `asr_base_url` is optional and defaults according to the provider. Put the credential in `.env` under the configured environment variable name.

3. Restart Sayuki. Voice bridge startup is attempted only when enabled. The bridge binds to loopback and uses a random per-process secret for its local control and PCM endpoints.

4. Join a voice channel, then run `/sayuki voice-join` in the text channel where you want Sayuki's replies. This command is admin-only and joins the command user's current voice channel. Run `/sayuki voice-leave` to stop listening in that server. The `VOICE_JOIN` and `VOICE_LEAVE` registered tools use the same operations and are also admin-only.

Each completed speaker segment is transcribed and submitted to the ordinary 4.6 conversation engine. The output route is the text channel used by `voice-join`; it is held in memory and must be set again after a restart. The bridge supports one active voice channel per guild; joining another channel switches the session. Users must still be in the active voice channel when their segment is submitted. Voice control does not create a separate conversation engine or bypass channel authorization.

## Audio storage

Audio recording is off by default. Set `recordings_enabled = true` to save mono WAV copies under `recordings_directory` (default `data/voice-recordings`). The `data/` directory is excluded from Git. ASR audio is still sent to the configured provider for transcription when recording is disabled. Transcripts enter the same database and retention/privacy behavior as other events.

## Operational notes

- The bridge uses the same Discord bot token in a separate discord.js gateway client; Node.js dependencies and Discord voice permissions are required.
- The PCM callback and bridge controls bind to `127.0.0.1`. The random secret protects those local HTTP endpoints from unrelated local callers.
- A bridge startup failure leaves text chat running; check the Python and Node logs for the cause.
- Transcripts are marked as ASR text because speech recognition can be inaccurate. The source is not a Discord text message and cannot be fetched with `READ_MESSAGE`.
- The bridge handles inbound audio and transcription only; it does not synthesize speech or play audio into the voice channel.
- Let channel participants know that audio is being transcribed before starting a session.
