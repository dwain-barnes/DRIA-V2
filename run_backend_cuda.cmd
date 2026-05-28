@echo off
cd /d "%~dp0"
set OPENAI_API_KEY=not-needed

C:\Users\Dwain-Admin\miniconda3\envs\s2s\python.exe C:\Users\Dwain-Admin\miniconda3\envs\s2s\Scripts\speech-to-speech.exe ^
  --mode realtime ^
  --stt parakeet-tdt ^
  --parakeet_tdt_device cuda ^
  --llm_backend responses-api ^
  --responses_api_base_url http://localhost:8080/v1 ^
  --model_name gemma-4-E4B-it ^
  --responses_api_stream true ^
  --stream_batch_sentences 1 ^
  --tts qwen3 ^
  --qwen3_tts_model_name Qwen/Qwen3-TTS-12Hz-1.7B-Base ^
  --qwen3_tts_device cuda ^
  --qwen3_tts_ref_audio "%~dp0main_voice.wav" ^
  --qwen3_tts_ref_text "If the red of the second ball falls upon the green of the first, the result is to give a ball with an abnormally wide yellow band since red and green light when mixed form yellow." ^
  --qwen3_tts_language auto ^
  --qwen3_tts_non_streaming_mode True ^
  --enable_live_transcription ^
  --min_silence_ms 550 ^
  --min_speech_ms 250 ^
  --speech_pad_ms 300 ^
  --thresh 0.45 ^
  --smart_turn ^
  --smart_turn_threshold 0.5 ^
  --smart_turn_max_seconds 8
