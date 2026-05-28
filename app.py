import asyncio
import ast
import base64
import datetime as dt
import json
import math
import os
import operator
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import gradio as gr
import numpy as np
import websockets
from dotenv import dotenv_values, load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastrtc import AdditionalOutputs, AsyncStreamHandler, Stream, wait_for_item

load_dotenv()

SAMPLE_RATE = 24_000
OUTPUT_FRAME_SIZE = int(os.getenv("FASTRTC_OUTPUT_FRAME_SIZE", "480"))
DEFAULT_REALTIME_WS_URL = "ws://0.0.0.0:8765/v1/realtime"
BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = BASE_DIR / "public"
AGENT_SKILLS_DIR = BASE_DIR / "agent_skills"
SOUL_PATH = BASE_DIR / "SOUL.md"
MEMORY_PATH = Path(os.getenv("AGENT_MEMORY_FILE", str(BASE_DIR / "MEMORY.md"))).resolve()
DRIA_IMAGE_PATH = BASE_DIR / "dria.png"
VISION_CONTEXT_SKILL_PATH = AGENT_SKILLS_DIR / "vision-context" / "SKILL.md"
SUPPORTED_AGENT_SKILL_ACTIONS = {
    "markdown",
    "calculate",
    "time",
    "searxng",
    "vision",
    "deep_research",
}
DRIA_RESEARCH_SCRIPT_PATH = BASE_DIR / "dria-stack" / "dria-research.ts"
RESEARCH_RESULTS_DIR = BASE_DIR / "research_results"
ANAM_DEFAULT_CARA_AVATAR_ID = "30fa96d0-26c4-4e55-94a0-517025942e18"
ANAM_HEAD = """
<!-- dria-anam-avatar-assets -->
<link rel="stylesheet" href="/public/anam/anam_avatar.css?v=20260528_anam_guard">
<script type="module" src="/public/anam/anam_avatar.js?v=20260528_anam_guard"></script>
"""
DEFAULT_INSTRUCTIONS = (
    "You are in a live spoken conversation. Reply in natural spoken English, "
    "with no markdown, bullet points, emojis, stage directions, or formatting. "
    "Treat transcripts as imperfect, and ask a concise clarifying question only "
    "when needed."
)

PUBLIC_DIR.mkdir(exist_ok=True)
AGENT_SKILLS_DIR.mkdir(exist_ok=True)
MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
RESEARCH_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
ACTIVE_REALTIME_HANDLERS: set[Any] = set()
MEMORY_LOCK = threading.Lock()
MEMORY_TURN_COUNT = 0
MEMORY_LAST_UPDATED_AT: float | None = None
ANAM_STATE_LOCK = threading.Lock()
ANAM_RUNTIME_STATE = {
    "active": False,
    "starting": False,
    "sessionId": None,
    "updatedAt": None,
}
try:
    DEEP_RESEARCH_MAX_JOBS = max(1, int(os.getenv("DRIA_DEEP_RESEARCH_MAX_JOBS", "2")))
except ValueError:
    DEEP_RESEARCH_MAX_JOBS = 2
DEEP_RESEARCH_JOBS: dict[str, dict[str, Any]] = {}
DEEP_RESEARCH_JOBS_LOCK = threading.Lock()
DEEP_RESEARCH_JOB_SEMAPHORE = threading.BoundedSemaphore(DEEP_RESEARCH_MAX_JOBS)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _local_env_value(name: str, default: str = "") -> str:
    try:
        value = dotenv_values(BASE_DIR / ".env").get(name)
    except OSError:
        value = None
    if value is not None and str(value).strip():
        return str(value).strip()
    return os.getenv(name, default).strip()


def _local_env_bool(name: str, default: bool = False) -> bool:
    value = _local_env_value(name)
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AgentSkill:
    name: str
    description: str
    action: str
    path: Path
    body: str

    @property
    def enabled(self) -> bool:
        return self.action in SUPPORTED_AGENT_SKILL_ACTIONS

    def public_dict(self, include_body: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "action": self.action,
            "path": str(self.path),
            "enabled": self.enabled,
        }
        if include_body:
            data["body"] = self.body
        return data


class AssistantAudioHub:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=240)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: dict[str, Any]) -> None:
        stale: list[asyncio.Queue[dict[str, Any]]] = []
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except Exception:
                    stale.append(queue)

        for queue in stale:
            self.unsubscribe(queue)


assistant_audio_hub = AssistantAudioHub()


def _load_instructions() -> str:
    for env_name in ("REALTIME_SOUL_FILE", "REALTIME_INSTRUCTIONS_FILE"):
        instructions_file = os.getenv(env_name)
        if not instructions_file:
            continue
        try:
            text = Path(instructions_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"Could not read {env_name}={instructions_file}: {exc}")
        else:
            if text:
                return text

    if SOUL_PATH.exists():
        try:
            text = SOUL_PATH.read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"Could not read {SOUL_PATH}: {exc}")
        else:
            if text:
                return text

    return os.getenv("REALTIME_INSTRUCTIONS", DEFAULT_INSTRUCTIONS)


def _memory_enabled() -> bool:
    return _env_bool("AGENT_MEMORY_ENABLED", False)


def _anam_enabled() -> bool:
    return _local_env_bool("ANAM_ENABLED", True)


def _anam_api_base_url() -> str:
    return _local_env_value("ANAM_API_BASE_URL", "https://api.anam.ai/v1").rstrip("/")


def _anam_avatar_id() -> str:
    return _local_env_value("ANAM_AVATAR_ID", ANAM_DEFAULT_CARA_AVATAR_ID)


def _anam_avatar_model() -> str:
    return _local_env_value("ANAM_AVATAR_MODEL", "cara-3")


def _anam_persona_name() -> str:
    return _local_env_value("ANAM_PERSONA_NAME", "DRIA") or "DRIA"


def _anam_passthrough_sample_rate() -> int:
    raw_value = _local_env_value("ANAM_PASSTHROUGH_SAMPLE_RATE", "16000")
    try:
        sample_rate = int(raw_value)
    except ValueError:
        return 16_000
    if sample_rate < 16_000 or sample_rate > 48_000:
        return 16_000
    return sample_rate


def _anam_runtime_active() -> bool:
    if not _anam_enabled():
        return False
    with ANAM_STATE_LOCK:
        return bool(ANAM_RUNTIME_STATE["active"])


def _anam_config() -> dict[str, Any]:
    api_key = _local_env_value("ANAM_API_KEY")
    with ANAM_STATE_LOCK:
        active = bool(ANAM_RUNTIME_STATE["active"])
        starting = bool(ANAM_RUNTIME_STATE["starting"])
        session_id = ANAM_RUNTIME_STATE["sessionId"]
    return {
        "enabled": _anam_enabled(),
        "configured": bool(api_key),
        "active": active,
        "starting": starting,
        "sessionId": session_id,
        "avatarId": _anam_avatar_id(),
        "avatarModel": _anam_avatar_model(),
        "personaName": _anam_persona_name(),
        "audioWebsocket": "/anam/audio",
        "assistantAudioSampleRate": SAMPLE_RATE,
        "passthroughSampleRate": _anam_passthrough_sample_rate(),
        "apiBaseUrl": _anam_api_base_url(),
    }


def _set_anam_runtime_state(
    active: bool | None = None,
    starting: bool | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    with ANAM_STATE_LOCK:
        if active is not None:
            ANAM_RUNTIME_STATE["active"] = bool(active)
            if active:
                ANAM_RUNTIME_STATE["starting"] = False
            else:
                ANAM_RUNTIME_STATE["starting"] = False
                ANAM_RUNTIME_STATE["sessionId"] = None
        if starting is not None:
            ANAM_RUNTIME_STATE["starting"] = bool(starting)
        if session_id is not None:
            ANAM_RUNTIME_STATE["sessionId"] = session_id
        ANAM_RUNTIME_STATE["updatedAt"] = time.time()
        return {
            "active": bool(ANAM_RUNTIME_STATE["active"]),
            "starting": bool(ANAM_RUNTIME_STATE["starting"]),
            "sessionId": ANAM_RUNTIME_STATE["sessionId"],
        }


def _create_anam_session_token() -> dict[str, Any]:
    if not _anam_enabled():
        raise RuntimeError("Anam avatar integration is disabled")

    api_key = _local_env_value("ANAM_API_KEY")
    if not api_key:
        raise RuntimeError("ANAM_API_KEY is not set")

    now = time.time()
    with ANAM_STATE_LOCK:
        updated_at = ANAM_RUNTIME_STATE.get("updatedAt")
        starting_is_stale = (
            bool(ANAM_RUNTIME_STATE["starting"])
            and isinstance(updated_at, (int, float))
            and now - float(updated_at) > 90
        )
        if bool(ANAM_RUNTIME_STATE["active"]):
            raise RuntimeError("An Anam avatar session is already active. Press Stop before starting another.")
        if bool(ANAM_RUNTIME_STATE["starting"]) and not starting_is_stale:
            raise RuntimeError("An Anam avatar session is already starting. Wait for it to connect or press Stop.")
        ANAM_RUNTIME_STATE["starting"] = True
        ANAM_RUNTIME_STATE["updatedAt"] = now

    persona_config: dict[str, Any] = {
        "name": _anam_persona_name(),
        "avatarId": _anam_avatar_id(),
        "avatarModel": _anam_avatar_model(),
        "enableAudioPassthrough": True,
        "skipGreeting": True,
    }

    max_session_seconds = int(
        _safe_float(_local_env_value("ANAM_MAX_SESSION_LENGTH_SECONDS", "600"), 600)
    )
    if max_session_seconds > 0:
        persona_config["maxSessionLengthSeconds"] = max_session_seconds

    payload: dict[str, Any] = {
        "clientLabel": _local_env_value("ANAM_CLIENT_LABEL", "dria-local-avatar"),
        "personaConfig": persona_config,
    }
    video_quality = _local_env_value("ANAM_VIDEO_QUALITY", "auto").lower()
    if video_quality in {"auto", "high"}:
        payload["sessionOptions"] = {"videoQuality": video_quality}

    request = urllib.request.Request(
        f"{_anam_api_base_url()}/auth/session-token",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    timeout = float(_local_env_value("ANAM_TIMEOUT", "20"))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        _set_anam_runtime_state(active=False)
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Anam API returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        _set_anam_runtime_state(active=False)
        raise RuntimeError(f"Anam API connection failed: {exc.reason}") from exc

    if "sessionToken" not in data:
        _set_anam_runtime_state(active=False)
        raise RuntimeError(f"Anam API response did not include sessionToken: {data}")
    if data.get("sessionId"):
        _set_anam_runtime_state(starting=True, session_id=data["sessionId"])
    return data


def _memory_model() -> str | None:
    return os.getenv("AGENT_MEMORY_MODEL") or os.getenv("VISION_MODEL") or os.getenv("REALTIME_MODEL")


def _memory_api_base_url() -> str:
    return (
        os.getenv("AGENT_MEMORY_API_BASE_URL")
        or os.getenv("VISION_API_BASE_URL")
        or "http://localhost:8080/v1"
    ).rstrip("/")


def _read_memory_text() -> str:
    if not MEMORY_PATH.exists():
        return "# Memory\n"
    try:
        return MEMORY_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Could not read {MEMORY_PATH}: {exc}")
        return "# Memory\n"


def _memory_context_text() -> str:
    if not _memory_enabled():
        return "Runtime memory: disabled."

    memory = _read_memory_text().strip()
    if not memory or memory == "# Memory":
        return (
            "Runtime memory: enabled, but no durable user facts have been saved yet. "
            "Do not invent remembered details."
        )

    max_chars = int(_safe_float(os.getenv("AGENT_MEMORY_CONTEXT_MAX_CHARS"), 1800))
    return f"Runtime memory:\n{memory[:max_chars]}"


def _call_memory_model(prompt: str, max_tokens: int, temperature: float) -> str:
    model = _memory_model()
    if not model:
        raise RuntimeError("AGENT_MEMORY_MODEL, VISION_MODEL, or REALTIME_MODEL is not set")

    api_kind = os.getenv("AGENT_MEMORY_API_KIND", "chat").strip().lower()
    base_url = _memory_api_base_url()
    timeout = float(os.getenv("AGENT_MEMORY_TIMEOUT", "8"))
    api_key = os.getenv("AGENT_MEMORY_API_KEY") or os.getenv("OPENAI_API_KEY") or "not-needed"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    system_prompt = (
        "You maintain private long-term memory for a local spoken voice agent. "
        "Only keep durable facts, preferences, relationships, identity details, "
        "location, and ongoing projects. Do not invent facts."
    )

    if api_kind == "responses":
        url = f"{base_url}/responses"
        payload: dict[str, Any] = {
            "model": model,
            "input": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
    else:
        url = f"{base_url}/chat/completions"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"memory API returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"memory API connection failed: {exc.reason}") from exc

    return _extract_model_text(data)


def _update_memory_for_turn(user_text: str, assistant_text: str) -> dict[str, Any]:
    global MEMORY_LAST_UPDATED_AT, MEMORY_TURN_COUNT

    if not _memory_enabled():
        return {"ok": True, "status": "disabled"}

    user_text = user_text.strip()
    assistant_text = assistant_text.strip()
    if not user_text or not assistant_text:
        return {"ok": True, "status": "empty_turn"}

    prompt = (
        f"Current memory:\n{_read_memory_text()}\n\n"
        f"User said: {user_text}\n\n"
        f"Assistant replied: {assistant_text}\n\n"
        "Did the user state a new durable fact about themselves or ongoing work? "
        "If yes, output one short fact per line starting with '- '. "
        "If no, output ONLY: NONE. Do not invent facts."
    )
    result = _call_memory_model(prompt, max_tokens=80, temperature=0.2).strip()
    if not result or "NONE" in result.upper():
        return {"ok": True, "status": "no_new_memory"}

    lines = [line.strip() for line in result.splitlines() if line.strip().startswith("-")]
    if not lines:
        return {"ok": True, "status": "no_memory_lines", "raw": result[:300]}

    with MEMORY_LOCK:
        existing = _read_memory_text()
        existing_lines = {line.strip().lower() for line in existing.splitlines()}
        new_lines = [line for line in lines if line.lower() not in existing_lines]
        if not new_lines:
            return {"ok": True, "status": "duplicate_memory"}
        with MEMORY_PATH.open("a", encoding="utf-8") as memory_file:
            if not existing.endswith("\n"):
                memory_file.write("\n")
            memory_file.write("\n".join(new_lines) + "\n")
        MEMORY_TURN_COUNT += 1
        MEMORY_LAST_UPDATED_AT = time.time()

    consolidate_every = int(_safe_float(os.getenv("AGENT_MEMORY_CONSOLIDATE_EVERY"), 5))
    consolidated = False
    if consolidate_every > 0 and MEMORY_TURN_COUNT % consolidate_every == 0:
        consolidated = _consolidate_memory().get("ok", False)

    return {
        "ok": True,
        "status": "updated",
        "added": new_lines,
        "consolidated": consolidated,
    }


def _consolidate_memory() -> dict[str, Any]:
    if not MEMORY_PATH.exists():
        return {"ok": True, "status": "no_memory_file"}

    prompt = (
        f"Here is a memory file about a user:\n\n{_read_memory_text()}\n\n"
        "Rewrite it: merge duplicates, remove transient or session-specific items, "
        "and keep only durable facts such as identity, preferences, relationships, "
        "location, and ongoing projects. Output the cleaned file, starting with "
        "'# Memory' followed by bullets starting with '- '. No explanation."
    )
    result = _call_memory_model(prompt, max_tokens=350, temperature=0.2).strip()
    if not result.startswith("# Memory"):
        return {"ok": False, "status": "invalid_model_output", "raw": result[:300]}

    with MEMORY_LOCK:
        MEMORY_PATH.write_text(result.rstrip() + "\n", encoding="utf-8")
    return {"ok": True, "status": "consolidated"}


async def _record_agent_turn(user_text: str, assistant_text: str) -> None:
    try:
        result = await asyncio.to_thread(_update_memory_for_turn, user_text, assistant_text)
    except Exception as exc:
        print(f"Memory update failed: {exc}")
        return

    if result.get("status") == "updated":
        print(f"Memory updated with {len(result.get('added', []))} fact(s)")
        await _broadcast_runtime_context_update()


def _agent_skills_enabled() -> bool:
    return _env_bool("AGENT_SKILLS_ENABLED", True)


def _agent_skill_tools_enabled() -> bool:
    return _env_bool("AGENT_SKILLS_REGISTER_TOOLS", True)


def _load_agent_skills() -> list[AgentSkill]:
    if not _agent_skills_enabled():
        return []

    skills: list[AgentSkill] = []
    try:
        skill_dirs = sorted(
            child for child in AGENT_SKILLS_DIR.iterdir() if child.is_dir()
        )
    except OSError as exc:
        print(f"Could not read agent skills directory {AGENT_SKILLS_DIR}: {exc}")
        return []

    for skill_dir in skill_dirs:
        skill_path = skill_dir / "SKILL.md"
        if not skill_path.exists():
            continue
        try:
            skills.append(_parse_agent_skill(skill_path))
        except Exception as exc:
            print(f"Could not load agent skill {skill_path}: {exc}")

    return skills


def _read_skill_markdown(path: Path) -> tuple[dict[str, str], str]:
    text = path.read_text(encoding="utf-8")
    metadata: dict[str, str] = {}
    body = text.strip()
    lines = text.splitlines()

    if lines and lines[0].strip() == "---":
        frontmatter_end = None
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                frontmatter_end = index
                break

        if frontmatter_end is not None:
            for line in lines[1:frontmatter_end]:
                key, separator, value = line.partition(":")
                if separator:
                    metadata[key.strip().lower()] = value.strip().strip("'\"")
            body = "\n".join(lines[frontmatter_end + 1 :]).strip()

    return metadata, body


def _parse_agent_skill(path: Path) -> AgentSkill:
    metadata, body = _read_skill_markdown(path)
    return AgentSkill(
        name=metadata.get("name") or path.parent.name.replace("-", "_"),
        description=metadata.get("description", ""),
        action=metadata.get("action", "markdown").strip().lower(),
        path=path,
        body=body,
    )


def _find_agent_skill(name: str) -> AgentSkill | None:
    normalized = name.strip().lower().replace("-", "_")
    for skill in _load_agent_skills():
        aliases = {skill.name.lower(), skill.path.parent.name.lower().replace("-", "_")}
        if normalized in aliases:
            return skill
    return None


def _agent_skill_context_text() -> str:
    if not _agent_skills_enabled():
        return "Runtime agent skills: disabled."

    skills = _load_agent_skills()
    if not skills:
        return "Runtime agent skills: no Markdown skills are currently loaded."

    lines = [
        "Runtime agent skills:",
        "Use these local skills only when the user's request matches the description. "
        "For executable skills, prefer the registered tool call when the backend supports it; "
        "do not invent exact calculation, time, web-search, or deep-research results.",
    ]
    for skill in skills:
        status = "enabled" if skill.enabled else "unsupported"
        lines.append(
            f"- {skill.name} ({skill.action}, {status}): "
            f"{skill.description or 'No description provided.'}"
        )

    markdown_skills = [skill for skill in skills if skill.action == "markdown" and skill.body]
    for skill in markdown_skills:
        lines.append(f"\nSkill instructions for {skill.name}:\n{skill.body}")

    return "\n".join(lines)


def _agent_skill_tool_definitions() -> list[dict[str, Any]]:
    if not _agent_skill_tools_enabled():
        return []

    tools: list[dict[str, Any]] = []
    for skill in _load_agent_skills():
        parameters = _agent_skill_parameters_schema(skill)
        if parameters is None:
            continue
        tools.append(
            {
                "type": "function",
                "name": skill.name,
                "description": skill.description,
                "parameters": parameters,
            }
        )
    return tools


def _agent_skill_parameters_schema(skill: AgentSkill) -> dict[str, Any] | None:
    if skill.action == "calculate":
        return {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "Plain arithmetic expression, for example '(12.5 * 4) + 6'.",
                }
            },
            "required": ["expression"],
            "additionalProperties": False,
        }

    if skill.action == "time":
        return {
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "Optional IANA timezone such as Europe/London or America/New_York.",
                }
            },
            "additionalProperties": False,
        }

    if skill.action == "searxng":
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Focused web search query."},
                "category": {
                    "type": "string",
                    "enum": ["general", "news"],
                    "description": "Use news for recent events; otherwise use general.",
                },
                "time_range": {
                    "type": "string",
                    "enum": ["", "day", "week", "month", "year"],
                    "description": "Optional recency filter.",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 8,
                    "description": "Number of search results to return.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    if skill.action == "deep_research":
        return {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["start", "status", "result", "list"],
                    "description": "Use start for new research, status/result with a job_id, or list for recent jobs.",
                },
                "query": {
                    "type": "string",
                    "description": "The research question to investigate when operation is start.",
                },
                "job_id": {
                    "type": "string",
                    "description": "Background research job id for status or result lookups.",
                },
                "breadth": {
                    "type": "integer",
                    "minimum": 3,
                    "maximum": 5,
                    "description": "Distinct research angles to investigate. Defaults to 4.",
                },
                "depth": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 8,
                    "description": "Search and scrape iterations per angle. Defaults to 5.",
                },
                "max_sources": {
                    "type": "integer",
                    "minimum": 3,
                    "maximum": 25,
                    "description": "Maximum unique URLs to scrape across the run. Defaults to 15.",
                },
                "recency": {
                    "type": "string",
                    "description": "Optional recency instruction, such as 'past month' or '2026'.",
                },
                "include_result": {
                    "type": "boolean",
                    "description": "Include the full JSON result when checking a completed job.",
                },
            },
            "additionalProperties": False,
        }

    return None


@dataclass(frozen=True)
class RealtimeConfig:
    ws_url: str = os.getenv("REALTIME_WS_URL", DEFAULT_REALTIME_WS_URL)
    api_key: str | None = os.getenv("REALTIME_API_KEY") or os.getenv("OPENAI_API_KEY")
    model: str | None = os.getenv("REALTIME_MODEL") or os.getenv("MODEL_NAME") or None
    normalize_any_host: bool = _env_bool("REALTIME_NORMALIZE_ANY_HOST", True)
    send_session_update: bool = _env_bool("REALTIME_SEND_SESSION_UPDATE", True)
    send_openai_beta_header: bool = _env_bool("REALTIME_OPENAI_BETA_HEADER", False)
    livekit_aec: bool = _env_bool("FASTRTC_LIVEKIT_AEC", False)
    transcription_model: str = os.getenv("REALTIME_TRANSCRIPTION_MODEL", "whisper-1")
    interrupt_response: bool = _env_bool("REALTIME_INTERRUPT_RESPONSE", True)

    @property
    def connect_url(self) -> str:
        ws_url = self._client_ws_url
        if not self.model or "model=" in ws_url:
            return ws_url
        separator = "&" if "?" in ws_url else "?"
        return f"{ws_url}{separator}model={quote(self.model)}"

    @property
    def _client_ws_url(self) -> str:
        parsed = urlparse(self.ws_url)
        if not self.normalize_any_host or parsed.hostname != "0.0.0.0":
            return self.ws_url

        netloc = "127.0.0.1"
        if parsed.port:
            netloc += f":{parsed.port}"
        return urlunparse(parsed._replace(netloc=netloc))

    @property
    def headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.send_openai_beta_header:
            headers["OpenAI-Beta"] = "realtime=v1"
        return headers

    @property
    def session(self) -> dict[str, Any]:
        session: dict[str, Any] = {
            "type": "realtime",
            "instructions": _instructions_with_runtime_context(_load_instructions()),
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                    "turn_detection": {
                        "type": "server_vad",
                        "interrupt_response": self.interrupt_response,
                    },
                    "transcription": {
                        "model": self.transcription_model,
                        "language": "en",
                    },
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                },
            },
        }
        tools = _agent_skill_tool_definitions()
        if tools:
            session["tools"] = tools
            session["tool_choice"] = "auto"
        return session


class OpenAICompatibleRealtimeHandler(AsyncStreamHandler):
    def __init__(self, config: RealtimeConfig | None = None) -> None:
        super().__init__(
            expected_layout="mono",
            input_sample_rate=SAMPLE_RATE,
            output_frame_size=OUTPUT_FRAME_SIZE,
            output_sample_rate=SAMPLE_RATE,
        )
        self.config = config or RealtimeConfig()
        self.websocket: Any | None = None
        self.output_queue: asyncio.Queue[Any] = asyncio.Queue()
        self.response_active = False
        self.aec = LiveKitAecProcessor(SAMPLE_RATE) if self.config.livekit_aec else None
        self.handled_tool_calls: set[str] = set()
        self.last_user_transcript: str | None = None
        self.last_recorded_memory_key: str | None = None
        self.pending_user_transcript_parts: list[str] = []
        self.user_transcript_committed = False
        self.pending_assistant_transcript_parts: list[str] = []
        self.assistant_transcript_committed = False
        self.last_assistant_transcript: str | None = None

    def copy(self) -> "OpenAICompatibleRealtimeHandler":
        return OpenAICompatibleRealtimeHandler(self.config)

    async def start_up(self) -> None:
        try:
            async with websockets.connect(
                self.config.connect_url,
                additional_headers=self.config.headers,
                max_size=None,
                proxy=None,
            ) as websocket:
                self.websocket = websocket
                ACTIVE_REALTIME_HANDLERS.add(self)
                if self.config.send_session_update:
                    await self._send(
                        {"type": "session.update", "session": self.config.session}
                    )

                async for raw_message in websocket:
                    await self._handle_event(raw_message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.output_queue.put(
                AdditionalOutputs(
                    {"role": "assistant", "content": f"Realtime connection failed: {exc}"}
                )
            )
        finally:
            ACTIVE_REALTIME_HANDLERS.discard(self)
            self.websocket = None

    async def receive(self, frame: tuple[int, np.ndarray]) -> None:
        if self.websocket is None:
            return

        _, audio = frame
        if self.aec is not None:
            audio = self.aec.process_mic(audio)
        audio_message = base64.b64encode(_to_pcm16_bytes(audio)).decode("ascii")
        await self._send({"type": "input_audio_buffer.append", "audio": audio_message})

    async def emit(self) -> tuple[int, np.ndarray] | AdditionalOutputs | None:
        item = await wait_for_item(self.output_queue)
        if self.aec is not None and _is_audio_output(item):
            self.aec.add_reference(item[1])
        return item

    async def shutdown(self) -> None:
        if self.websocket is not None:
            await self.websocket.close()
            self.websocket = None

    async def _send(self, event: dict[str, Any]) -> None:
        if self.websocket is not None:
            await self.websocket.send(json.dumps(event))

    async def update_session_context(self) -> None:
        if self.config.send_session_update and self.websocket is not None:
            await self._send({"type": "session.update", "session": self.config.session})

    async def _commit_user_transcript(self, transcript: str | None) -> None:
        text = _clean_transcript_text(transcript)
        if not text:
            return

        replace_last = False
        if self.user_transcript_committed:
            if len(text) <= len(self.last_user_transcript or ""):
                return
            replace_last = True

        self.last_user_transcript = text
        self.user_transcript_committed = True
        await self.output_queue.put(
            AdditionalOutputs(
                {"role": "user", "content": text, "_replace_last": replace_last}
            )
        )

    async def _commit_assistant_transcript(self, transcript: str | None) -> None:
        text = _clean_transcript_text(transcript)
        if not text:
            return

        replace_last = False
        if self.assistant_transcript_committed:
            text = _merge_transcript_text(self.last_assistant_transcript or "", text)
            if text == self.last_assistant_transcript:
                return
            replace_last = True

        self.last_assistant_transcript = text
        self.assistant_transcript_committed = True

        memory_key = f"{self.last_user_transcript or ''}\n---\n{text}"
        if self.last_user_transcript and memory_key != self.last_recorded_memory_key:
            self.last_recorded_memory_key = memory_key
            asyncio.create_task(_record_agent_turn(self.last_user_transcript, text))

        await self.output_queue.put(
            AdditionalOutputs(
                {"role": "assistant", "content": text, "_replace_last": replace_last}
            )
        )

    async def _handle_event(self, raw_message: str | bytes) -> None:
        event = _parse_event(raw_message)
        if event is None:
            return

        event_type = event.get("type", "")

        if event_type == "input_audio_buffer.speech_started":
            self.clear_queue()
            _drain_queue(self.output_queue)
            self.response_active = False
            self.pending_user_transcript_parts = []
            self.user_transcript_committed = False
            self.pending_assistant_transcript_parts = []
            self.assistant_transcript_committed = False
            self.last_assistant_transcript = None
            assistant_audio_hub.publish({"type": "interrupt"})
            await self.output_queue.put((SAMPLE_RATE, _silence_frame()))
            return

        if event_type == "response.created":
            self.response_active = True
            if self.pending_user_transcript_parts and not self.user_transcript_committed:
                await self._commit_user_transcript("".join(self.pending_user_transcript_parts))
            self.pending_assistant_transcript_parts = []
            self.assistant_transcript_committed = False
            self.last_assistant_transcript = None
            assistant_audio_hub.publish(
                {"type": "audio_start", "sample_rate": SAMPLE_RATE}
            )
            return

        if event_type in {
            "conversation.item.input_audio_transcription.delta",
            "input_audio_transcription.delta",
        }:
            delta = _event_text(event)
            if delta:
                self.pending_user_transcript_parts.append(delta)
            return

        if event_type in {
            "conversation.item.input_audio_transcription.completed",
            "input_audio_transcription.completed",
        }:
            transcript = _event_text(event) or "".join(self.pending_user_transcript_parts)
            await self._commit_user_transcript(transcript)
            self.pending_user_transcript_parts = []
            return

        if event_type in {
            "response.audio_transcript.delta",
            "response.output_audio_transcript.delta",
            "response.text.delta",
            "response.output_text.delta",
        }:
            delta = _event_text(event)
            if delta:
                self.pending_assistant_transcript_parts.append(delta)
            return

        if event_type in {
            "response.audio_transcript.done",
            "response.output_audio_transcript.done",
            "response.text.done",
        }:
            transcript = _event_text(event) or "".join(self.pending_assistant_transcript_parts)
            await self._commit_assistant_transcript(transcript)
            return

        if event_type in {"response.audio.delta", "response.output_audio.delta"}:
            delta = event.get("delta")
            if delta:
                self.response_active = True
                assistant_audio_hub.publish(
                    {
                        "type": "audio_delta",
                        "audio": delta,
                        "sample_rate": SAMPLE_RATE,
                    }
                )
                audio = np.frombuffer(base64.b64decode(delta), dtype=np.int16).reshape(
                    1, -1
                )
                if not _anam_runtime_active():
                    await self.output_queue.put((SAMPLE_RATE, audio))
            return

        if event_type in {"response.audio.done", "response.output_audio.done"}:
            self.response_active = False
            assistant_audio_hub.publish({"type": "audio_done"})
            await self.output_queue.put((SAMPLE_RATE, _silence_frame()))
            return

        if event_type == "response.done":
            self.response_active = False
            assistant_audio_hub.publish({"type": "response_done"})
            transcript = _extract_response_transcript(event.get("response"))
            if not transcript and self.pending_assistant_transcript_parts:
                transcript = "".join(self.pending_assistant_transcript_parts)
            await self._commit_assistant_transcript(transcript)
            return

        if event_type == "response.function_call_arguments.done":
            await self._handle_agent_skill_tool_call(event)
            return

        if event_type == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                await self._handle_agent_skill_tool_call(item)
            elif isinstance(item, dict):
                role = item.get("role")
                transcript = _extract_item_transcript(item)
                if role == "user":
                    await self._commit_user_transcript(transcript)
                elif role == "assistant":
                    await self._commit_assistant_transcript(transcript)
            return

        if event_type == "error":
            error = event.get("error", {})
            message = error.get("message") if isinstance(error, dict) else str(error)
            await self.output_queue.put(
                AdditionalOutputs({"role": "assistant", "content": f"Realtime error: {message}"})
            )

    async def _handle_agent_skill_tool_call(self, payload: dict[str, Any]) -> None:
        name = payload.get("name")
        call_id = payload.get("call_id") or payload.get("id")
        if not isinstance(name, str) or not isinstance(call_id, str):
            return

        call_key = f"{call_id}:{name}"
        if call_key in self.handled_tool_calls:
            return
        self.handled_tool_calls.add(call_key)

        result = await asyncio.to_thread(_run_agent_skill, name, payload.get("arguments"))
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(result, ensure_ascii=False),
                },
            }
        )
        await self._send({"type": "response.create"})


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _run_agent_skill(skill_name: str, arguments: Any = None) -> dict[str, Any]:
    skill = _find_agent_skill(skill_name)
    if skill is None:
        return {"ok": False, "error": f"Unknown agent skill: {skill_name}"}
    if not skill.enabled:
        return {
            "ok": False,
            "error": f"Unsupported action for agent skill {skill.name}: {skill.action}",
        }

    args = _parse_skill_arguments(arguments)
    try:
        if skill.action == "calculate":
            result = _run_calculate_skill(args)
        elif skill.action == "time":
            result = _run_time_skill(args)
        elif skill.action == "searxng":
            result = _run_searxng_skill(args)
        elif skill.action == "deep_research":
            result = _run_deep_research_skill(args)
        else:
            result = {"ok": True, "result": skill.body}
    except Exception as exc:
        return {"ok": False, "skill": skill.name, "action": skill.action, "error": str(exc)}

    result.setdefault("ok", True)
    result.setdefault("skill", skill.name)
    result.setdefault("action", skill.action)
    return result


def _parse_skill_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    if arguments is None:
        return {}
    if isinstance(arguments, str):
        text = arguments.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"input": text}
        if isinstance(parsed, dict):
            return parsed
        return {"input": parsed}
    return {"input": arguments}


def _run_calculate_skill(args: dict[str, Any]) -> dict[str, Any]:
    expression = str(args.get("expression") or args.get("input") or "").strip()
    if not expression:
        return {"ok": False, "error": "Missing arithmetic expression"}
    if len(expression) > 500:
        return {"ok": False, "error": "Arithmetic expression is too long"}

    result = _safe_eval_arithmetic(expression)
    if isinstance(result, float) and result.is_integer():
        display_result = str(int(result))
    else:
        display_result = str(result)
    return {
        "ok": True,
        "expression": expression,
        "result": display_result,
        "spoken": f"The answer is {display_result}.",
    }


def _safe_eval_arithmetic(expression: str) -> int | float:
    operators = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
        ast.USub: operator.neg,
        ast.UAdd: operator.pos,
    }

    def validate_number(value: int | float) -> int | float:
        if isinstance(value, int) and value.bit_length() > 4096:
            raise ValueError("Calculation result is too large")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Calculation result is not finite")
        return value

    def evaluate(node: ast.AST) -> int | float:
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return validate_number(node.value)
        if isinstance(node, ast.UnaryOp) and type(node.op) in operators:
            return validate_number(operators[type(node.op)](evaluate(node.operand)))
        if isinstance(node, ast.BinOp) and type(node.op) in operators:
            left = evaluate(node.left)
            right = evaluate(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 1000:
                raise ValueError("Exponent is too large")
            result = operators[type(node.op)](left, right)
            if isinstance(result, (int, float)):
                return validate_number(result)
        raise ValueError("Only plain arithmetic expressions are supported")

    return evaluate(ast.parse(expression, mode="eval"))


def _run_time_skill(args: dict[str, Any]) -> dict[str, Any]:
    timezone_name = str(
        args.get("timezone")
        or args.get("tz")
        or os.getenv("LOCAL_TIMEZONE")
        or "Europe/London"
    ).strip()
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown IANA timezone: {timezone_name}") from exc

    now = dt.datetime.now(timezone)
    spoken = now.strftime("%A, %d %B %Y at %H:%M %Z")
    return {
        "ok": True,
        "timezone": timezone_name,
        "iso": now.isoformat(),
        "date": now.date().isoformat(),
        "time": now.strftime("%H:%M:%S"),
        "spoken": spoken,
    }


def _run_searxng_skill(args: dict[str, Any]) -> dict[str, Any]:
    base_url = os.getenv("SEARXNG_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        return {"ok": False, "error": "SEARXNG_BASE_URL is not set"}

    query = str(args.get("query") or args.get("q") or args.get("input") or "").strip()
    if not query:
        return {"ok": False, "error": "Missing search query"}

    category = str(args.get("category") or args.get("categories") or "general").strip()
    if category not in {"general", "news"}:
        category = "general"

    time_range = str(args.get("time_range") or "").strip().lower()
    if time_range not in {"", "day", "week", "month", "year"}:
        time_range = ""

    max_results = int(_safe_float(args.get("max_results"), 5))
    max_results = max(1, min(max_results, 8))

    params: dict[str, str] = {
        "q": query,
        "format": "json",
        "categories": category,
    }
    if time_range:
        params["time_range"] = time_range

    timeout = float(os.getenv("SEARXNG_TIMEOUT", "8"))
    url = f"{base_url}/search?{urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "DRIA-agent-skills/1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "error": f"SearXNG returned HTTP {exc.code}: {detail}"}
    except urllib.error.URLError as exc:
        return {"ok": False, "error": f"SearXNG connection failed: {exc.reason}"}

    results = []
    for item in data.get("results", [])[:max_results]:
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "content": item.get("content"),
                "engine": item.get("engine"),
                "publishedDate": item.get("publishedDate"),
            }
        )

    return {
        "ok": True,
        "query": query,
        "category": category,
        "time_range": time_range or None,
        "result_count": len(results),
        "results": results,
    }


def _clamped_int_arg(
    args: dict[str, Any],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw_value = args.get(name)
    if raw_value in (None, ""):
        return default
    try:
        value = int(float(str(raw_value).strip()))
    except ValueError:
        return default
    return max(minimum, min(value, maximum))


def _deep_research_command() -> list[str]:
    local_tsx = BASE_DIR / "node_modules" / ".bin" / (
        "tsx.cmd" if os.name == "nt" else "tsx"
    )
    if local_tsx.exists():
        return [str(local_tsx), str(DRIA_RESEARCH_SCRIPT_PATH)]

    npx = shutil.which("npx.cmd") or shutil.which("npx") or shutil.which("npx.exe")
    if npx:
        return [npx, "--yes", "tsx", str(DRIA_RESEARCH_SCRIPT_PATH)]

    raise RuntimeError("Node npx was not found. Install Node.js or use the Docker image.")


def _parse_deep_research_stdout(stdout: str) -> Any:
    text = stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _make_research_job_id(query: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in query)
    while "__" in slug:
        slug = slug.replace("__", "_")
    slug = slug.strip("_")[:32] or "research"
    return f"{slug}_{uuid.uuid4().hex[:8]}"


def _tail_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    return text.strip()[-limit:]


def _public_deep_research_job(
    job: dict[str, Any],
    include_result: bool = False,
) -> dict[str, Any]:
    public = {
        key: value
        for key, value in job.items()
        if key not in {"thread", "env", "command"}
    }
    if not include_result:
        public.pop("result", None)
    return public


def _deep_research_job_status(
    job_id: str,
    include_result: bool = False,
) -> dict[str, Any]:
    with DEEP_RESEARCH_JOBS_LOCK:
        job = DEEP_RESEARCH_JOBS.get(job_id)
        if job is None:
            return {"ok": False, "error": f"Unknown deep research job: {job_id}"}
        return {
            "ok": True,
            "job": _public_deep_research_job(job, include_result=include_result),
        }


def _deep_research_job_list() -> dict[str, Any]:
    with DEEP_RESEARCH_JOBS_LOCK:
        jobs = [
            _public_deep_research_job(job, include_result=False)
            for job in DEEP_RESEARCH_JOBS.values()
        ]
    jobs.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return {"ok": True, "jobs": jobs, "job_count": len(jobs)}


def _update_deep_research_job(job_id: str, **fields: Any) -> None:
    with DEEP_RESEARCH_JOBS_LOCK:
        job = DEEP_RESEARCH_JOBS.get(job_id)
        if job is not None:
            job.update(fields)


def _prepare_deep_research_run(args: dict[str, Any]) -> dict[str, Any]:
    if not _env_bool("DRIA_DEEP_RESEARCH_ENABLED", True):
        return {"ok": False, "error": "DRIA_DEEP_RESEARCH_ENABLED is disabled"}
    if not DRIA_RESEARCH_SCRIPT_PATH.exists():
        return {
            "ok": False,
            "error": f"Deep research runner not found: {DRIA_RESEARCH_SCRIPT_PATH}",
        }

    query = str(args.get("query") or args.get("input") or "").strip()
    if not query:
        return {"ok": False, "error": "Missing research query"}
    if len(query) > 4000:
        return {"ok": False, "error": "Research query is too long"}

    breadth = _clamped_int_arg(args, "breadth", 4, 3, 5)
    depth = _clamped_int_arg(args, "depth", 5, 1, 8)
    max_sources = _clamped_int_arg(args, "max_sources", 15, 3, 25)
    recency = str(args.get("recency") or "").strip()

    RESEARCH_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.setdefault("OPENAI_API_KEY", "sk-noauth")
    env.setdefault("LLAMACPP_BASE_URL", "http://localhost:8080/v1")
    env.setdefault("LOCAL_MODEL", os.getenv("REALTIME_MODEL") or "qwen3.5-35b-a3b")
    env.setdefault("FIRECRAWL_API_URL", "http://localhost:3002")
    env["SKILLS_DIR"] = str(AGENT_SKILLS_DIR)
    env["DRIA_RESEARCH_RESULTS_DIR"] = str(RESEARCH_RESULTS_DIR)
    env["DRIA_RESEARCH_BREADTH"] = str(breadth)
    env["DRIA_RESEARCH_DEPTH"] = str(depth)
    env["DRIA_RESEARCH_MAX_SOURCES"] = str(max_sources)
    if recency:
        env["DRIA_RESEARCH_RECENCY"] = recency

    timeout = float(os.getenv("DRIA_DEEP_RESEARCH_TIMEOUT", "900"))
    command = _deep_research_command() + [query]
    return {
        "ok": True,
        "query": query,
        "breadth": breadth,
        "depth": depth,
        "max_sources": max_sources,
        "recency": recency or None,
        "env": env,
        "command": command,
        "timeout": timeout,
    }


def _deep_research_job_worker(job_id: str) -> None:
    stderr_limit = int(_safe_float(os.getenv("DRIA_DEEP_RESEARCH_STDERR_CHARS"), 4000))
    acquired = False
    try:
        DEEP_RESEARCH_JOB_SEMAPHORE.acquire()
        acquired = True
        with DEEP_RESEARCH_JOBS_LOCK:
            job = DEEP_RESEARCH_JOBS[job_id]
            command = job["command"]
            env = job["env"]
            timeout = job["timeout"]
            job.update(status="running", started_at=_now_iso())

        completed = subprocess.run(
            command,
            cwd=BASE_DIR,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )

        stderr = _tail_text(completed.stderr, stderr_limit)
        stdout = _tail_text(completed.stdout, stderr_limit)
        if completed.returncode != 0:
            _update_deep_research_job(
                job_id,
                status="error",
                completed_at=_now_iso(),
                returncode=completed.returncode,
                error=f"Deep research runner exited with {completed.returncode}",
                stderr=stderr,
                stdout=stdout,
            )
            return

        try:
            data = _parse_deep_research_stdout(completed.stdout)
        except Exception as exc:
            _update_deep_research_job(
                job_id,
                status="error",
                completed_at=_now_iso(),
                returncode=completed.returncode,
                error=f"Deep research runner did not return JSON: {exc}",
                stderr=stderr,
                stdout=stdout,
            )
            return

        result_path = data.get("result_path") if isinstance(data, dict) else None
        _update_deep_research_job(
            job_id,
            status="completed",
            completed_at=_now_iso(),
            returncode=completed.returncode,
            result=data,
            result_path=result_path,
            stderr=stderr or None,
        )
    except subprocess.TimeoutExpired as exc:
        _update_deep_research_job(
            job_id,
            status="timeout",
            completed_at=_now_iso(),
            error=f"Deep research exceeded {exc.timeout} seconds",
            stderr=_tail_text(exc.stderr, stderr_limit),
            stdout=_tail_text(exc.stdout, stderr_limit),
        )
    except Exception as exc:
        _update_deep_research_job(
            job_id,
            status="error",
            completed_at=_now_iso(),
            error=str(exc),
        )
    finally:
        if acquired:
            DEEP_RESEARCH_JOB_SEMAPHORE.release()


def _start_deep_research_job(args: dict[str, Any]) -> dict[str, Any]:
    prepared = _prepare_deep_research_run(args)
    if not prepared.get("ok"):
        return prepared

    max_queue = max(
        DEEP_RESEARCH_MAX_JOBS,
        int(_safe_float(os.getenv("DRIA_DEEP_RESEARCH_MAX_QUEUE"), 8)),
    )
    with DEEP_RESEARCH_JOBS_LOCK:
        active_jobs = sum(
            1
            for job in DEEP_RESEARCH_JOBS.values()
            if job.get("status") in {"queued", "running"}
        )
        if active_jobs >= max_queue:
            return {
                "ok": False,
                "error": f"Deep research queue is full ({active_jobs}/{max_queue})",
            }

        job_id = _make_research_job_id(str(prepared["query"]))
        prepared["env"]["DRIA_RESEARCH_JOB_ID"] = job_id
        job = {
            "job_id": job_id,
            "query": prepared["query"],
            "breadth": prepared["breadth"],
            "depth": prepared["depth"],
            "max_sources": prepared["max_sources"],
            "recency": prepared["recency"],
            "status": "queued",
            "created_at": _now_iso(),
            "started_at": None,
            "completed_at": None,
            "result_path": None,
            "error": None,
            "returncode": None,
            "command": prepared["command"],
            "env": prepared["env"],
            "timeout": prepared["timeout"],
        }
        DEEP_RESEARCH_JOBS[job_id] = job

    thread = threading.Thread(
        target=_deep_research_job_worker,
        args=(job_id,),
        name=f"dria-research-{job_id}",
        daemon=True,
    )
    with DEEP_RESEARCH_JOBS_LOCK:
        DEEP_RESEARCH_JOBS[job_id]["thread"] = thread
    thread.start()

    return {
        "ok": True,
        "status": "started",
        "background": True,
        "job": _public_deep_research_job(job, include_result=False),
        "message": (
            "Deep research has started in the background. "
            "You can keep talking to DRIA while the research agents work."
        ),
    }


def _run_deep_research_skill(args: dict[str, Any]) -> dict[str, Any]:
    operation = str(args.get("operation") or args.get("mode") or "").strip().lower()
    job_id = str(args.get("job_id") or "").strip()
    if operation == "list":
        return _deep_research_job_list()
    if operation in {"status", "result"} or (job_id and not args.get("query")):
        if not job_id:
            return {"ok": False, "error": "Missing job_id"}
        include_result = operation == "result" or bool(args.get("include_result"))
        return _deep_research_job_status(job_id, include_result=include_result)
    return _start_deep_research_job(args)


def _parse_event(raw_message: str | bytes) -> dict[str, Any] | None:
    if isinstance(raw_message, bytes):
        raw_message = raw_message.decode("utf-8")
    try:
        return json.loads(raw_message)
    except json.JSONDecodeError:
        return None


def _to_pcm16_bytes(audio: np.ndarray) -> bytes:
    pcm = np.asarray(audio).squeeze()
    if pcm.dtype == np.int16:
        return np.ascontiguousarray(pcm).tobytes()
    if np.issubdtype(pcm.dtype, np.floating):
        pcm = np.clip(pcm, -1.0, 1.0)
        pcm = (pcm * 32767).astype(np.int16)
    else:
        pcm = pcm.astype(np.int16)
    return np.ascontiguousarray(pcm).tobytes()


class LiveKitAecProcessor:
    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = sample_rate
        self.frame_size = sample_rate // 100
        self.reference = np.empty(0, dtype=np.float32)
        self.max_reference_samples = sample_rate * 2
        self.enabled = False
        try:
            from livekit.rtc import AudioFrame
            from livekit.rtc.apm import AudioProcessingModule
        except Exception as exc:
            print(f"LiveKit APM unavailable, AEC disabled: {exc}")
            return

        self.AudioFrame = AudioFrame
        self.apm = AudioProcessingModule(echo_cancellation=True, noise_suppression=True)
        self.enabled = True
        print("LiveKit APM AEC3 enabled for microphone cleanup")

    def add_reference(self, audio: np.ndarray) -> None:
        if not self.enabled:
            return
        reference = _to_float_mono(audio)
        if reference.size == 0:
            return
        self.reference = np.concatenate((self.reference, reference))
        if self.reference.size > self.max_reference_samples:
            self.reference = self.reference[-self.max_reference_samples :]

    def process_mic(self, audio: np.ndarray) -> np.ndarray:
        if not self.enabled:
            return audio
        mic = _to_float_mono(audio)
        if mic.size == 0:
            return audio

        processed = np.empty_like(mic)
        try:
            for start in range(0, mic.size, self.frame_size):
                chunk = mic[start : start + self.frame_size]
                chunk_len = chunk.size
                mic_frame = self._frame(_pad_to_frame(_float_to_int16(chunk), self.frame_size))
                ref_frame = self._frame(_pad_to_frame(_float_to_int16(self._take_reference(chunk_len)), self.frame_size))
                self.apm.process_reverse_stream(ref_frame)
                self.apm.process_stream(mic_frame)
                cleaned = np.frombuffer(bytes(mic_frame.data), dtype=np.int16)[:chunk_len]
                processed[start : start + chunk_len] = cleaned.astype(np.float32) / 32768.0
        except Exception as exc:
            self.enabled = False
            print(f"LiveKit APM failed, AEC disabled: {exc}")
            return audio

        return _float_to_int16(processed).reshape(1, -1)

    def _take_reference(self, size: int) -> np.ndarray:
        if self.reference.size >= size:
            chunk = self.reference[:size]
            self.reference = self.reference[size:]
            return chunk
        if self.reference.size == 0:
            return np.zeros(size, dtype=np.float32)
        chunk = np.concatenate((self.reference, np.zeros(size - self.reference.size, dtype=np.float32)))
        self.reference = np.empty(0, dtype=np.float32)
        return chunk

    def _frame(self, audio_i16: np.ndarray):
        return self.AudioFrame(
            audio_i16.tobytes(),
            sample_rate=self.sample_rate,
            num_channels=1,
            samples_per_channel=self.frame_size,
        )


def _to_float_mono(audio: np.ndarray) -> np.ndarray:
    mono = np.asarray(audio).squeeze()
    if mono.dtype == np.int16:
        return mono.astype(np.float32) / 32768.0
    if np.issubdtype(mono.dtype, np.floating):
        return np.clip(mono.astype(np.float32), -1.0, 1.0)
    return mono.astype(np.float32) / 32768.0


def _float_to_int16(audio: np.ndarray) -> np.ndarray:
    return (np.clip(audio, -1.0, 1.0).astype(np.float32) * 32767).astype(np.int16)


def _pad_to_frame(audio: np.ndarray, frame_size: int) -> np.ndarray:
    if audio.size == frame_size:
        return audio
    if audio.size > frame_size:
        return audio[:frame_size]
    return np.pad(audio, (0, frame_size - audio.size))


def _is_audio_output(item: Any) -> bool:
    return (
        isinstance(item, tuple)
        and len(item) == 2
        and isinstance(item[0], int)
        and isinstance(item[1], np.ndarray)
    )


def _drain_queue(queue: asyncio.Queue[Any]) -> None:
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return


def _silence_frame() -> np.ndarray:
    return np.zeros((1, OUTPUT_FRAME_SIZE), dtype=np.int16)


latest_vision_frame: dict[str, Any] = {}


def _markdown_section(body: str, heading: str) -> str:
    target = heading.strip().lower()
    collected: list[str] = []
    capturing = False

    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            current = stripped.lstrip("#").strip().lower()
            if capturing:
                break
            if current == target:
                capturing = True
            continue
        if capturing:
            collected.append(line)

    return "\n".join(collected).strip()


def _load_vision_context_skill() -> dict[str, Any]:
    try:
        metadata, body = _read_skill_markdown(VISION_CONTEXT_SKILL_PATH)
    except OSError:
        return {}

    analysis_prompt = metadata.get("analysis_prompt") or _markdown_section(
        body, "Analysis Prompt"
    )
    return {
        "name": metadata.get("name") or "vision_context",
        "version": metadata.get("version", ""),
        "description": metadata.get("description", ""),
        "action": metadata.get("action", "vision"),
        "path": str(VISION_CONTEXT_SKILL_PATH),
        "analysis_prompt": analysis_prompt,
        "body": body,
    }


def _default_vision_prompt() -> str:
    skill = _load_vision_context_skill()
    prompt = skill.get("analysis_prompt")
    if isinstance(prompt, str) and prompt.strip():
        return prompt.strip()
    return (
        "Briefly describe what is visible for a live voice assistant. "
        "Focus on people, objects, screen text, and anything the user might ask about. "
        "Return one concise paragraph with no markdown."
    )


def _latest_vision_context_text() -> str:
    if not _vision_enabled():
        return (
            "Vision skill status: disabled. If the user asks what you can see, "
            "say the camera vision skill is currently disabled."
        )

    analysis = latest_vision_frame.get("analysis")
    captured_at = _safe_float(latest_vision_frame.get("captured_at"), 0.0)
    max_age = float(os.getenv("VISION_CONTEXT_MAX_AGE_SECONDS", "20"))
    if not analysis or not captured_at:
        return (
            "Vision skill status: enabled, but no recent camera frame has been analyzed yet. "
            "If the user asks what you can see, ask them to turn on the Camera button or wait "
            "for the camera frame to update."
        )

    age = max(0.0, time.time() - captured_at)
    stale_note = ""
    if age > max_age:
        stale_note = " This visual context may be stale."

    return (
        f"Vision skill status: enabled. Latest camera frame was analyzed {age:.0f} seconds ago."
        f"{stale_note} Visual context: {analysis}"
    )


def _instructions_with_runtime_context(base_instructions: str) -> str:
    return (
        f"{base_instructions.rstrip()}\n\n"
        f"{_memory_context_text()}\n\n"
        f"{_agent_skill_context_text()}\n\n"
        "Runtime vision skill:\n"
        "You can use the live camera vision context below when answering questions about "
        "what you can see. Do not claim you are only a voice agent. If the context says no "
        "recent camera frame is available, say that plainly and ask the user to enable the camera.\n"
        f"{_latest_vision_context_text()}"
    )


async def _broadcast_runtime_context_update() -> None:
    handlers = list(ACTIVE_REALTIME_HANDLERS)
    if not handlers:
        return

    for handler in handlers:
        try:
            await handler.update_session_context()
        except Exception as exc:
            print(f"Could not update realtime runtime context: {exc}")


def _vision_enabled() -> bool:
    return _env_bool("VISION_ENABLED", False)


def _vision_api_base_url() -> str:
    return os.getenv("VISION_API_BASE_URL", "http://localhost:8080/v1").rstrip("/")


def _vision_model() -> str | None:
    return os.getenv("VISION_MODEL") or os.getenv("REALTIME_MODEL")


def _call_vision_model(image_url: str, prompt: str) -> str:
    model = _vision_model()
    if not model:
        raise RuntimeError("VISION_MODEL or REALTIME_MODEL is not set")

    api_kind = os.getenv("VISION_API_KIND", "responses").strip().lower()
    base_url = _vision_api_base_url()
    api_key = os.getenv("VISION_API_KEY") or os.getenv("OPENAI_API_KEY") or "not-needed"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    if api_kind == "chat":
        url = f"{base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
            "max_tokens": 160,
            # Keep local reasoning-capable llama.cpp models from returning only
            # hidden reasoning while the visible vision answer stays empty.
            "chat_template_kwargs": {"enable_thinking": False},
        }
    else:
        url = f"{base_url}/responses"
        payload = {
            "model": model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": image_url},
                    ],
                }
            ],
            "max_output_tokens": 160,
        }

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"vision API returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"vision API connection failed: {exc.reason}") from exc

    return _extract_model_text(data)


def _extract_model_text(data: dict[str, Any]) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"].strip()

    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message", {})
        content = message.get("content", "")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("text")
            ]
            return " ".join(parts).strip()

    output = data.get("output")
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        text = block.get("text") or block.get("content")
                        if isinstance(text, str):
                            parts.append(text)
        if parts:
            return " ".join(parts).strip()

    return "Model response received, but no text field was found."


def _clean_transcript_text(text: str | None) -> str:
    if not isinstance(text, str):
        return ""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _merge_transcript_text(previous: str, incoming: str) -> str:
    previous = _clean_transcript_text(previous)
    incoming = _clean_transcript_text(incoming)
    if not previous:
        return incoming
    if not incoming:
        return previous

    previous_folded = " ".join(previous.split()).casefold()
    incoming_folded = " ".join(incoming.split()).casefold()
    if incoming_folded == previous_folded or incoming_folded in previous_folded:
        return previous
    if incoming_folded.startswith(previous_folded):
        return incoming
    if previous_folded.startswith(incoming_folded):
        return previous

    separator = "" if previous.endswith((" ", "\n")) else " "
    return f"{previous}{separator}{incoming}"


def _event_text(event: dict[str, Any]) -> str:
    for key in ("transcript", "text", "delta"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _extract_content_transcript(content: Any) -> str:
    parts: list[str] = []

    if isinstance(content, str):
        return content

    if not isinstance(content, list):
        return ""

    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        for key in ("transcript", "text", "content"):
            value = block.get(key)
            if isinstance(value, str) and value:
                parts.append(value)
                break
        audio = block.get("audio")
        if isinstance(audio, dict):
            transcript = audio.get("transcript")
            if isinstance(transcript, str) and transcript:
                parts.append(transcript)

    return "\n".join(part.strip() for part in parts if part.strip())


def _extract_item_transcript(item: dict[str, Any]) -> str:
    content_text = _extract_content_transcript(item.get("content"))
    if content_text:
        return content_text
    for key in ("transcript", "text"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _extract_response_transcript(response: Any) -> str:
    if not isinstance(response, dict):
        return ""

    direct_text = _extract_content_transcript(response.get("content"))
    if direct_text:
        return direct_text

    output = response.get("output")
    if not isinstance(output, list):
        return ""

    parts: list[str] = []
    for item in output:
        if isinstance(item, dict) and item.get("role") == "assistant":
            text = _extract_item_transcript(item)
            if text:
                parts.append(text)
    return "\n".join(parts)


def update_chatbot(chatbot: list[dict[str, str]] | None, response: dict[str, str]):
    chatbot = chatbot or []
    replace_last = bool(response.pop("_replace_last", False))
    if replace_last:
        role = response.get("role")
        for index in range(len(chatbot) - 1, -1, -1):
            if chatbot[index].get("role") == role:
                chatbot[index] = response
                return chatbot
    chatbot.append(response)
    return chatbot


chatbot_component = gr.Chatbot(type="messages", label="Transcript")
stream = Stream(
    OpenAICompatibleRealtimeHandler(),
    mode="send-receive",
    modality="audio",
    additional_inputs=[chatbot_component],
    additional_outputs=[chatbot_component],
    additional_outputs_handler=update_chatbot,
    ui_args={"title": "FastRTC Realtime Voice"},
)

app = FastAPI(title="FastRTC OpenAI-Compatible Realtime Bridge")
app.mount("/public", StaticFiles(directory=PUBLIC_DIR), name="public")
stream.mount(app)


@app.middleware("http")
async def inject_anam_assets(request, call_next):
    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if not request.url.path.startswith("/ui") or "text/html" not in content_type:
        return response

    body = b""
    async for chunk in response.body_iterator:
        body += chunk

    html = body.decode("utf-8", errors="replace")
    if "dria-anam-avatar-assets" not in html and "</head>" in html:
        html = html.replace("</head>", f"{ANAM_HEAD}\n</head>", 1)

    headers = dict(response.headers)
    headers.pop("content-length", None)
    headers.pop("content-encoding", None)
    return Response(
        content=html,
        status_code=response.status_code,
        headers=headers,
        media_type="text/html",
    )


@app.get("/health")
async def health() -> JSONResponse:
    config = RealtimeConfig()
    markdown_skills = _load_agent_skills()
    tool_definitions = _agent_skill_tool_definitions()
    with DEEP_RESEARCH_JOBS_LOCK:
        deep_research_job_count = len(DEEP_RESEARCH_JOBS)
        deep_research_running_jobs = sum(
            1 for job in DEEP_RESEARCH_JOBS.values() if job.get("status") == "running"
        )
        deep_research_queued_jobs = sum(
            1 for job in DEEP_RESEARCH_JOBS.values() if job.get("status") == "queued"
        )
    return JSONResponse(
        {
            "status": "ok",
            "realtime_ws_url": config.connect_url,
            "configured_realtime_ws_url": config.ws_url,
            "send_session_update": config.send_session_update,
            "sample_rate": SAMPLE_RATE,
            "output_frame_size": OUTPUT_FRAME_SIZE,
            "livekit_aec": config.livekit_aec,
            "soul_path": str(SOUL_PATH),
            "soul_loaded": bool(_load_instructions().strip()),
            "memory_enabled": _memory_enabled(),
            "memory_path": str(MEMORY_PATH),
            "memory_file_exists": MEMORY_PATH.exists(),
            "memory_model": _memory_model(),
            "memory_api_base_url": _memory_api_base_url(),
            "memory_last_updated_at": MEMORY_LAST_UPDATED_AT,
            "anam": _anam_config(),
            "agent_skills_enabled": _agent_skills_enabled(),
            "agent_skill_names": [skill.name for skill in markdown_skills],
            "agent_skill_tools_enabled": _agent_skill_tools_enabled(),
            "agent_skill_tool_names": [tool["name"] for tool in tool_definitions],
            "deep_research_enabled": _env_bool("DRIA_DEEP_RESEARCH_ENABLED", True),
            "deep_research_runner": str(DRIA_RESEARCH_SCRIPT_PATH),
            "deep_research_runner_exists": DRIA_RESEARCH_SCRIPT_PATH.exists(),
            "deep_research_results_dir": str(RESEARCH_RESULTS_DIR),
            "deep_research_max_background_jobs": DEEP_RESEARCH_MAX_JOBS,
            "deep_research_job_count": deep_research_job_count,
            "deep_research_running_jobs": deep_research_running_jobs,
            "deep_research_queued_jobs": deep_research_queued_jobs,
            "firecrawl_api_url": os.getenv("FIRECRAWL_API_URL", "http://localhost:3002"),
            "llamacpp_base_url": os.getenv("LLAMACPP_BASE_URL", "http://localhost:8080/v1"),
            "searxng_configured": bool(os.getenv("SEARXNG_BASE_URL", "").strip()),
            "vision_enabled": _vision_enabled(),
            "vision_api_base_url": _vision_api_base_url(),
            "vision_model": _vision_model(),
            "vision_skill_path": str(VISION_CONTEXT_SKILL_PATH),
            "vision_latest_age_seconds": (
                round(time.time() - float(latest_vision_frame["captured_at"]), 1)
                if latest_vision_frame.get("captured_at")
                else None
            ),
            "vision_latest_analysis": latest_vision_frame.get("analysis"),
        }
    )


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse("/ui")


@app.get("/dria.png")
async def dria_image() -> FileResponse:
    return FileResponse(DRIA_IMAGE_PATH, media_type="image/png")


@app.get("/agent/soul")
async def agent_soul() -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "path": str(SOUL_PATH),
            "content": _load_instructions(),
        }
    )


@app.get("/agent/memory")
async def agent_memory() -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "enabled": _memory_enabled(),
            "path": str(MEMORY_PATH),
            "exists": MEMORY_PATH.exists(),
            "last_updated_at": MEMORY_LAST_UPDATED_AT,
            "content": _read_memory_text(),
        }
    )


@app.get("/agent/research_jobs")
async def agent_research_jobs() -> JSONResponse:
    return JSONResponse(_deep_research_job_list())


@app.get("/agent/research_jobs/{job_id}")
async def agent_research_job(job_id: str, include_result: bool = True) -> JSONResponse:
    result = _deep_research_job_status(job_id, include_result=include_result)
    return JSONResponse(result, status_code=200 if result.get("ok") else 404)


@app.get("/anam/config")
async def anam_config() -> JSONResponse:
    return JSONResponse({"ok": True, **_anam_config()})


@app.post("/anam/state")
async def anam_state(payload: dict[str, Any]) -> JSONResponse:
    state = _set_anam_runtime_state(
        active=payload.get("active") if "active" in payload else None,
        starting=payload.get("starting") if "starting" in payload else None,
        session_id=payload.get("sessionId") if "sessionId" in payload else None,
    )
    return JSONResponse({"ok": True, **state})


@app.get("/anam/state")
async def anam_state_get() -> JSONResponse:
    with ANAM_STATE_LOCK:
        return JSONResponse(
            {
                "ok": True,
                "active": bool(ANAM_RUNTIME_STATE["active"]),
                "starting": bool(ANAM_RUNTIME_STATE["starting"]),
                "sessionId": ANAM_RUNTIME_STATE["sessionId"],
                "updatedAt": ANAM_RUNTIME_STATE["updatedAt"],
            }
        )


@app.post("/anam/session-token")
async def anam_session_token() -> JSONResponse:
    try:
        data = await asyncio.to_thread(_create_anam_session_token)
    except Exception as exc:
        message = str(exc)
        status_code = 409 if "already" in message else 502
        return JSONResponse({"ok": False, "error": message}, status_code=status_code)
    return JSONResponse(
        {
            "ok": True,
            "sessionToken": data["sessionToken"],
            "expiresAt": data.get("expiresAt"),
            "sessionId": data.get("sessionId"),
            "avatarId": _anam_avatar_id(),
            "avatarModel": _anam_avatar_model(),
        }
    )


@app.websocket("/anam/audio")
async def anam_audio(websocket: WebSocket) -> None:
    await websocket.accept()
    queue = assistant_audio_hub.subscribe()
    try:
        await websocket.send_json(
            {
                "type": "connected",
                "sample_rate": SAMPLE_RATE,
                "target_sample_rate": _anam_passthrough_sample_rate(),
                "encoding": "pcm_s16le",
                "channels": 1,
            }
        )
        while True:
            await websocket.send_json(await queue.get())
    except WebSocketDisconnect:
        pass
    finally:
        assistant_audio_hub.unsubscribe(queue)


@app.post("/agent/memory/consolidate")
async def agent_memory_consolidate() -> JSONResponse:
    try:
        result = await asyncio.to_thread(_consolidate_memory)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    return JSONResponse(result, status_code=200 if result.get("ok") else 502)


@app.get("/agent/skills")
async def agent_skills() -> JSONResponse:
    skills = []
    for skill in _load_agent_skills():
        entry = {"kind": "markdown", **skill.public_dict()}
        if skill.name == "vision_context":
            entry["enabled"] = skill.enabled and _vision_enabled()
            entry["runtime_enabled"] = _vision_enabled()
            entry["latest_analysis"] = latest_vision_frame.get("analysis")
        skills.append(entry)
    return JSONResponse(
        {
            "ok": True,
            "skills": skills,
        }
    )


@app.get("/agent/skills/vision_context")
async def vision_context_skill() -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "skill": _load_vision_context_skill(),
            "runtime_context": _latest_vision_context_text(),
            "latest": latest_vision_frame,
        }
    )


@app.get("/agent/skills/{skill_name}")
async def agent_skill_detail(skill_name: str) -> JSONResponse:
    skill = _find_agent_skill(skill_name)
    if skill is None:
        return JSONResponse(
            {"ok": False, "error": f"Unknown agent skill: {skill_name}"},
            status_code=404,
        )
    return JSONResponse({"ok": True, "skill": skill.public_dict(include_body=True)})


@app.post("/agent/skills/{skill_name}/run")
async def agent_skill_run(skill_name: str, payload: dict[str, Any]) -> JSONResponse:
    result = await asyncio.to_thread(_run_agent_skill, skill_name, payload)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.post("/vision/analyze")
async def vision_analyze(payload: dict[str, Any]) -> JSONResponse:
    image = payload.get("image")
    if not isinstance(image, str) or not image:
        return JSONResponse({"ok": False, "error": "Missing image"}, status_code=400)

    if not image.startswith("data:image/"):
        image = f"data:image/jpeg;base64,{image}"

    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        prompt = _default_vision_prompt()

    latest_vision_frame.clear()
    latest_vision_frame.update(
        {
            "captured_at": time.time(),
            "image_bytes_estimate": int(len(image) * 0.75),
            "prompt": prompt,
        }
    )

    if not _vision_enabled():
        return JSONResponse(
            {
                "ok": True,
                "status": "cached",
                "message": (
                    "Vision frame captured. Set VISION_ENABLED=1 and configure "
                    "VISION_API_BASE_URL/VISION_MODEL to analyze snapshots."
                ),
            }
        )

    try:
        text = await asyncio.to_thread(_call_vision_model, image, prompt)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    latest_vision_frame["analysis"] = text
    latest_vision_frame["analyzed_at"] = time.time()
    await _broadcast_runtime_context_update()
    return JSONResponse({"ok": True, "status": "analyzed", "analysis": text})


@app.get("/vision/latest")
async def vision_latest() -> JSONResponse:
    return JSONResponse({"ok": True, "latest": latest_vision_frame})


if __name__ == "__main__":
    host = os.getenv("FASTRTC_HOST", "0.0.0.0")
    port = int(os.getenv("FASTRTC_PORT", "7860"))
    mode = os.getenv("FASTRTC_MODE", "ui").strip().lower()

    import uvicorn

    if mode == "api":
        uvicorn.run(app, host=host, port=port)
    else:
        gr.mount_gradio_app(
            app,
            stream.ui,
            path="/ui",
            server_name=host,
            server_port=port,
        )
        uvicorn.run(app, host=host, port=port)
