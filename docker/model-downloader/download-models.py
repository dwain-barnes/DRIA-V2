import os
from pathlib import Path

from huggingface_hub import snapshot_download


def env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


repo_id = env("LLM_MODEL_REPO", "unsloth/gemma-4-E4B-it-GGUF")
model_file = env("LLAMA_CPP_MODEL_FILE", "gemma-4-E4B-it-Q8_0.gguf")
mmproj_file = env("LLAMA_CPP_MMPROJ_FILE", "mmproj-BF16.gguf")
model_dir = Path(env("MODEL_DIR", "/models"))
token = os.getenv("HF_TOKEN") or os.getenv("HUGGING_FACE_HUB_TOKEN") or None

required_files = [model_file, mmproj_file]
missing_files = [name for name in required_files if not (model_dir / name).is_file()]

if not missing_files:
    print(f"LLM model files already present in {model_dir}.")
else:
    print(f"Downloading {repo_id}: {', '.join(missing_files)}")
    model_dir.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=repo_id,
        local_dir=str(model_dir),
        allow_patterns=required_files,
        token=token,
    )

missing_after = [name for name in required_files if not (model_dir / name).is_file()]
if missing_after:
    raise RuntimeError(f"Missing required model files after download: {', '.join(missing_after)}")

print("LLM model download check complete.")
