@echo off
setlocal

set "LLAMA_CPP_DIR=C:\Users\Dwain-Admin\Downloads\llama-b9070-bin-win-cuda-12.4-x64"
set "MODEL_PATH=C:\Users\Dwain-Admin\.cache\huggingface\hub\models--unsloth--gemma-4-E4B-it-GGUF\snapshots\653803f092503c04a65164346f3208a36e707693\gemma-4-E4B-it-Q8_0.gguf"
set "MMPROJ_PATH=C:\Users\Dwain-Admin\.cache\huggingface\hub\models--unsloth--gemma-4-E4B-it-GGUF\snapshots\653803f092503c04a65164346f3208a36e707693\mmproj-BF16.gguf"

cd /d "%LLAMA_CPP_DIR%"

llama-server.exe ^
  -m "%MODEL_PATH%" ^
  --mmproj "%MMPROJ_PATH%" ^
  --host 127.0.0.1 ^
  --port 8080 ^
  -c 32768 ^
  -ngl 99 ^
  -np 4 ^
  --jinja
