#!/usr/bin/env bash
set -euo pipefail

export OPENAI_API_KEY="${OPENAI_API_KEY:-not-needed}"

local_ref_audio="${S2S_LOCAL_REF_AUDIO:-/workspace/main_voice.wav}"
local_ref_text="${S2S_LOCAL_REF_TEXT:-If the red of the second ball falls upon the green of the first, the result is to give a ball with an abnormally wide yellow band since red and green light when mixed form yellow.}"
default_ref_audio_url="${S2S_DEFAULT_REF_AUDIO_URL:-https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen3-TTS-Repo/clone.wav}"
default_ref_text="${S2S_DEFAULT_REF_TEXT:-Okay. Yeah. I resent you. I love you. I respect you. But you know what? You blew it! And thanks to you.}"

if [[ -n "${S2S_QWEN3_REF_AUDIO:-}" ]]; then
  ref_audio="$S2S_QWEN3_REF_AUDIO"
  ref_text="${S2S_QWEN3_REF_TEXT:-$local_ref_text}"
elif [[ -s "$local_ref_audio" ]]; then
  echo "Using local TTS reference voice: $local_ref_audio" >&2
  ref_audio="$local_ref_audio"
  ref_text="${S2S_QWEN3_REF_TEXT:-$local_ref_text}"
else
  ref_audio="${S2S_DEFAULT_REF_AUDIO_PATH:-/models/qwen3-tts/clone.wav}"
  ref_text="${S2S_QWEN3_REF_TEXT:-$default_ref_text}"
  mkdir -p "$(dirname "$ref_audio")"

  if [[ ! -s "$ref_audio" ]]; then
    echo "No local TTS reference voice found. Downloading default Qwen3-TTS reference audio." >&2
    tmp_ref_audio="${ref_audio}.tmp"
    curl -fsSL "$default_ref_audio_url" -o "$tmp_ref_audio"
    mv "$tmp_ref_audio" "$ref_audio"
  fi
fi

args=(
  --mode realtime
  --ws_host "${S2S_WS_HOST:-0.0.0.0}"
  --ws_port "${S2S_WS_PORT:-8765}"
  --stt "${S2S_STT:-parakeet-tdt}"
  --parakeet_tdt_device "${S2S_PARAKEET_DEVICE:-cuda}"
  --llm_backend responses-api
  --responses_api_api_key "${S2S_RESPONSES_API_KEY:-not-needed}"
  --responses_api_base_url "${S2S_RESPONSES_API_BASE_URL:-http://llama-cpp:8080/v1}"
  --model_name "${S2S_MODEL_NAME:-gemma-4-E4B-it}"
  --responses_api_stream "${S2S_RESPONSES_API_STREAM:-true}"
  --stream_batch_sentences "${S2S_STREAM_BATCH_SENTENCES:-1}"
  --tts "${S2S_TTS:-qwen3}"
  --qwen3_tts_model_name "${S2S_QWEN3_TTS_MODEL_NAME:-Qwen/Qwen3-TTS-12Hz-1.7B-Base}"
  --qwen3_tts_device "${S2S_QWEN3_TTS_DEVICE:-cuda}"
  --qwen3_tts_ref_audio "$ref_audio"
  --qwen3_tts_ref_text "$ref_text"
  --qwen3_tts_language "${S2S_QWEN3_TTS_LANGUAGE:-auto}"
  --qwen3_tts_non_streaming_mode "${S2S_QWEN3_NON_STREAMING_MODE:-True}"
  --min_silence_ms "${S2S_MIN_SILENCE_MS:-550}"
  --min_speech_ms "${S2S_MIN_SPEECH_MS:-250}"
  --speech_pad_ms "${S2S_SPEECH_PAD_MS:-300}"
  --thresh "${S2S_VAD_THRESHOLD:-0.45}"
)

if [[ "${S2S_ENABLE_LIVE_TRANSCRIPTION:-true}" != "false" ]]; then
  args+=(--enable_live_transcription)
fi

if [[ "${S2S_SMART_TURN:-false}" == "true" ]]; then
  echo "S2S_SMART_TURN=true was requested, but the PyPI speech-to-speech package does not expose Smart Turn flags in this image. Continuing without Smart Turn." >&2
fi

exec speech-to-speech "${args[@]}" "$@"
