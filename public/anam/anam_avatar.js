import { createClient, AnamEvent } from "https://esm.sh/@anam-ai/js-sdk?bundle";

const TRANSCRIPT_STORAGE_KEY = "dria.showTranscript";

let config = null;
let anamClient = null;
let audioInputStream = null;
let audioSocket = null;
let cameraStream = null;
let started = false;
let starting = false;
let avatarEnabled = false;
let cameraEnabled = false;
let currentSessionId = null;
let resamplerState = { buffer: new Int16Array(0), position: 0, sourceRate: 0 };

function setStatus(message, state = "idle") {
  const node = document.getElementById("dria-anam-status");
  if (!node) return;
  node.textContent = message;
  node.dataset.state = state;
}

function setButtons() {
  const start = document.getElementById("dria-anam-start");
  const stop = document.getElementById("dria-anam-stop");
  if (start) {
    start.disabled = starting || started || !avatarEnabled || !config?.configured;
    start.textContent = starting ? "Starting" : "Start";
  }
  if (stop) stop.disabled = !(starting || started || anamClient || audioSocket);
}

function ensurePanel() {
  if (document.getElementById("dria-call-shell")) return;

  const panel = document.createElement("section");
  panel.id = "dria-call-shell";
  panel.hidden = true;
  panel.innerHTML = `
    <div class="dria-call-stage">
      <div class="dria-video-grid">
        <article class="dria-video-card dria-avatar-card" id="dria-avatar-card">
          <div class="dria-video-label">DRIA</div>
          <video id="dria-anam-video" autoplay playsinline></video>
          <div class="dria-video-placeholder" id="dria-avatar-placeholder">
            <img class="dria-avatar-still" src="/dria.png?v=20260528_dria_still" alt="" aria-hidden="true" />
            <div class="dria-avatar-off-state">
              <strong>DRIA</strong>
              <span>Cara 3 avatar off</span>
            </div>
          </div>
        </article>
        <article class="dria-video-card dria-user-card" id="dria-user-card">
          <div class="dria-video-label">You</div>
          <video id="dria-user-video" autoplay playsinline muted></video>
          <div class="dria-video-placeholder" id="dria-camera-placeholder">
            <strong>Camera off</strong>
            <span>Enable camera below</span>
          </div>
        </article>
      </div>
      <div class="dria-call-dock">
        <label class="dria-control-pill">
          <input type="checkbox" id="dria-anam-enabled" />
          <span>Avatar</span>
        </label>
        <div class="dria-avatar-actions">
          <button type="button" id="dria-anam-start">Start</button>
          <button type="button" id="dria-anam-stop" disabled>Stop</button>
        </div>
        <label class="dria-control-pill">
          <input type="checkbox" id="dria-camera-enabled" />
          <span>Camera</span>
        </label>
        <label class="dria-control-pill">
          <input type="checkbox" id="dria-transcript-enabled" />
          <span>Transcript</span>
        </label>
        <div class="dria-anam-status" id="dria-anam-status">Checking Anam configuration.</div>
      </div>
    </div>
  `;

  const host = document.querySelector("gradio-app") || document.body.firstElementChild || document.body;
  document.body.insertBefore(panel, host);

  document.getElementById("dria-anam-start")?.addEventListener("click", startAvatar);
  document.getElementById("dria-anam-stop")?.addEventListener("click", stopAvatar);
  document.getElementById("dria-anam-enabled")?.addEventListener("change", toggleAvatarEnabled);
  document.getElementById("dria-camera-enabled")?.addEventListener("change", toggleCameraEnabled);
  document.getElementById("dria-transcript-enabled")?.addEventListener("change", toggleTranscriptEnabled);
  initializeTranscriptVisibility();
}

async function loadConfig() {
  const response = await fetch("/anam/config", { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`Anam config failed with HTTP ${response.status}`);
  config = await response.json();

  const panel = document.getElementById("dria-call-shell");
  const avatarPlaceholder = document.getElementById("dria-avatar-placeholder");
  if (panel) panel.hidden = false;
  if (avatarPlaceholder) {
    avatarPlaceholder.querySelector("strong").textContent = config.personaName || "DRIA";
    avatarPlaceholder.querySelector("span").textContent = config.configured
      ? `${config.avatarModel || "cara-3"} avatar off`
      : "Anam key not configured";
  }

  if (!config.enabled) {
    setStatus("Anam avatar integration is disabled.");
  } else if (!config.configured) {
    setStatus("Set ANAM_API_KEY, then restart FastRTC to enable the avatar.", "error");
  } else {
    setStatus("Avatar off. Enable Avatar when you want to use Anam credits.");
  }
  setButtons();
}

async function startAvatar() {
  if (starting || started || !avatarEnabled) return;
  starting = true;
  setStatus("Creating Anam session.");
  setButtons();

  try {
    const tokenResponse = await fetch("/anam/session-token", {
      method: "POST",
      headers: { Accept: "application/json" },
    });
    const tokenData = await tokenResponse.json();
    if (!tokenResponse.ok || !tokenData.ok) {
      throw new Error(tokenData.error || `Session token failed with HTTP ${tokenResponse.status}`);
    }
    currentSessionId = tokenData.sessionId || null;

    anamClient = createClient(tokenData.sessionToken, { disableInputAudio: true });
    const sessionReady = waitForSessionReady(anamClient);
    wireAnamEvents(anamClient);

    enableAnamVideoAudio();
    await anamClient.streamToVideoElement("dria-anam-video");
    enableAnamVideoAudio();
    setStatus("Waiting for Anam session readiness.");
    await sessionReady;
    setAvatarLive(true);

    const passthroughSampleRate = Number(config.passthroughSampleRate || config.assistantAudioSampleRate || 24000);
    audioInputStream = anamClient.createAgentAudioInputStream({
      encoding: "pcm_s16le",
      sampleRate: passthroughSampleRate,
      channels: 1,
    });

    resetResampler();
    await postAnamState({ active: true, sessionId: currentSessionId });
    connectAudioSocket();
    started = true;
    starting = false;
    setStatus("Avatar connected. Anam is playing the synced TTS.", "ready");
  } catch (error) {
    await stopAvatar();
    setStatus(error instanceof Error ? error.message : String(error), "error");
  } finally {
    starting = false;
    setButtons();
  }
}

function waitForSessionReady(client) {
  if (!AnamEvent?.SESSION_READY || !client?.addListener) return Promise.resolve();

  return new Promise((resolve) => {
    let resolved = false;
    const finish = () => {
      if (resolved) return;
      resolved = true;
      resolve();
    };
    client.addListener(AnamEvent.SESSION_READY, finish);
    setTimeout(finish, 10000);
  });
}

async function toggleAvatarEnabled(event) {
  avatarEnabled = Boolean(event.target.checked);
  if (!avatarEnabled) {
    await stopAvatar();
    setStatus(config?.configured ? "Avatar off. Enable Avatar when you want to use Anam credits." : "Set ANAM_API_KEY, then restart FastRTC to enable the avatar.");
  } else if (config?.configured) {
    setStatus("Avatar enabled. Press Start.", "ready");
  }
  setButtons();
}

async function toggleCameraEnabled(event) {
  cameraEnabled = Boolean(event.target.checked);
  if (cameraEnabled) {
    await startCamera();
  } else {
    stopCamera();
  }
}

function toggleTranscriptEnabled(event) {
  setTranscriptVisible(Boolean(event.target.checked));
}

function initializeTranscriptVisibility() {
  const checkbox = document.getElementById("dria-transcript-enabled");
  const shouldShow = localStorage.getItem(TRANSCRIPT_STORAGE_KEY) === "true";
  if (checkbox) checkbox.checked = shouldShow;
  setTranscriptVisible(shouldShow);
}

function setTranscriptVisible(visible) {
  document.body.classList.toggle("dria-show-transcript", visible);
  localStorage.setItem(TRANSCRIPT_STORAGE_KEY, visible ? "true" : "false");
}

async function startCamera() {
  const video = document.getElementById("dria-user-video");
  const placeholder = document.getElementById("dria-camera-placeholder");
  const card = document.getElementById("dria-user-card");
  if (!video) return;

  try {
    cameraStream = await navigator.mediaDevices.getUserMedia({
      video: { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: "user" },
      audio: false,
    });
    video.srcObject = cameraStream;
    await video.play();
    card?.classList.add("dria-video-live");
    if (placeholder) placeholder.hidden = true;
  } catch (error) {
    const checkbox = document.getElementById("dria-camera-enabled");
    if (checkbox) checkbox.checked = false;
    cameraEnabled = false;
    card?.classList.remove("dria-video-live");
    if (placeholder) {
      placeholder.hidden = false;
      placeholder.querySelector("strong").textContent = "Camera unavailable";
      placeholder.querySelector("span").textContent = "Check browser permission";
    }
    setStatus(error instanceof Error ? error.message : String(error), "error");
  }
}

function stopCamera() {
  const video = document.getElementById("dria-user-video");
  const placeholder = document.getElementById("dria-camera-placeholder");
  const card = document.getElementById("dria-user-card");

  if (cameraStream) {
    for (const track of cameraStream.getTracks()) track.stop();
  }
  cameraStream = null;
  if (video) video.srcObject = null;
  card?.classList.remove("dria-video-live");
  if (placeholder) {
    placeholder.hidden = false;
    placeholder.querySelector("strong").textContent = "Camera off";
    placeholder.querySelector("span").textContent = "Enable camera below";
  }
}

async function postAnamState(state) {
  const payload = typeof state === "boolean" ? { active: state } : state;
  await fetch("/anam/state", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(payload),
  });
}

function wireAnamEvents(client) {
  if (!AnamEvent || !client?.addListener) return;
  const statusEvents = [
    ["CONNECTION_ESTABLISHED", "Anam connection established."],
    ["SESSION_READY", "Anam session ready."],
    ["VIDEO_PLAY_STARTED", "Avatar video started."],
  ];
  for (const [name, message] of statusEvents) {
    if (AnamEvent[name]) {
      client.addListener(AnamEvent[name], () => setStatus(message, "ready"));
    }
  }
  if (AnamEvent.CONNECTION_CLOSED) {
    client.addListener(AnamEvent.CONNECTION_CLOSED, (_reason, details) => {
      setStatus(details ? `Anam connection closed: ${details}` : "Anam connection closed.");
      started = false;
      starting = false;
      currentSessionId = null;
      setAvatarLive(false);
      postAnamState(false).catch(() => {});
      setButtons();
    });
  }
}

function connectAudioSocket() {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  audioSocket = new WebSocket(`${scheme}://${window.location.host}${config.audioWebsocket}`);
  audioSocket.addEventListener("message", (message) => {
    try {
      handleAudioEvent(JSON.parse(message.data));
    } catch (error) {
      console.warn("Invalid Anam audio event", error);
    }
  });
  audioSocket.addEventListener("close", () => {
    if (started) setStatus("Anam audio bridge disconnected.", "error");
  });
}

function handleAudioEvent(event) {
  if (!audioInputStream) return;

  if (event.type === "audio_delta" && event.audio) {
    const sourceRate = Number(event.sample_rate || config.assistantAudioSampleRate || 24000);
    const targetRate = Number(config.passthroughSampleRate || sourceRate);
    const pcm16 = base64ToInt16(event.audio);
    const output = sourceRate === targetRate ? pcm16 : resamplePcm16Streaming(pcm16, sourceRate, targetRate);
    if (output.length) audioInputStream.sendAudioChunk(int16ToBase64(output));
    return;
  }

  if (event.type === "audio_done" || event.type === "response_done") {
    audioInputStream.endSequence();
    resetResampler();
    return;
  }

  if (event.type === "interrupt") {
    if (anamClient?.interruptPersona) anamClient.interruptPersona();
    audioInputStream.endSequence();
    resetResampler();
  }
}

async function stopAvatar() {
  started = false;
  starting = false;
  if (audioSocket) {
    audioSocket.close();
    audioSocket = null;
  }
  if (audioInputStream) {
    try {
      audioInputStream.endSequence();
    } catch {
      // Stream may already be closed.
    }
    audioInputStream = null;
  }
  resetResampler();
  if (anamClient) {
    try {
      await anamClient.stopStreaming();
    } catch {
      // The SDK may already have closed the session.
    }
    anamClient = null;
  }
  currentSessionId = null;
  setAvatarLive(false);
  muteAnamVideoAudio();
  postAnamState(false).catch(() => {});
  setStatus(
    avatarEnabled && config?.configured
      ? "Avatar stopped. Press Start."
      : "Avatar off. Enable Avatar when you want to use Anam credits."
  );
  setButtons();
}

function stopAvatarOnUnload() {
  started = false;
  starting = false;
  currentSessionId = null;
  try {
    if (audioSocket) audioSocket.close();
    if (audioInputStream) audioInputStream.endSequence();
    if (anamClient?.stopStreaming) anamClient.stopStreaming();
  } catch {
    // The page is unloading; best effort cleanup only.
  }
  const payload = JSON.stringify({ active: false, starting: false });
  if (navigator.sendBeacon) {
    navigator.sendBeacon("/anam/state", new Blob([payload], { type: "application/json" }));
  }
}

function base64ToInt16(base64) {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return new Int16Array(bytes.buffer);
}

function int16ToBase64(samples) {
  const bytes = new Uint8Array(samples.buffer, samples.byteOffset, samples.byteLength);
  let binary = "";
  const chunkSize = 0x8000;
  for (let i = 0; i < bytes.length; i += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
  }
  return btoa(binary);
}

function enableAnamVideoAudio() {
  const video = document.getElementById("dria-anam-video");
  if (!video) return;
  video.muted = false;
  video.volume = 1;
  const playback = video.play?.();
  if (playback?.catch) playback.catch(() => {});
}

function muteAnamVideoAudio() {
  const video = document.getElementById("dria-anam-video");
  if (!video) return;
  video.muted = true;
  video.volume = 0;
}

function setAvatarLive(live) {
  const card = document.getElementById("dria-avatar-card");
  const placeholder = document.getElementById("dria-avatar-placeholder");
  card?.classList.toggle("dria-video-live", live);
  if (placeholder) placeholder.hidden = live;
}

function resetResampler() {
  resamplerState = { buffer: new Int16Array(0), position: 0, sourceRate: 0 };
}

function resamplePcm16Streaming(input, sourceRate, targetRate) {
  if (!input.length || sourceRate <= 0 || targetRate <= 0 || sourceRate === targetRate) return input;

  if (resamplerState.sourceRate !== sourceRate) {
    resetResampler();
    resamplerState.sourceRate = sourceRate;
  }

  const combined = new Int16Array(resamplerState.buffer.length + input.length);
  combined.set(resamplerState.buffer, 0);
  combined.set(input, resamplerState.buffer.length);

  const ratio = sourceRate / targetRate;
  let sourceIndex = resamplerState.position;
  const output = [];
  while (sourceIndex < combined.length - 1) {
    const low = Math.floor(sourceIndex);
    const high = low + 1;
    const fraction = sourceIndex - low;
    output.push(Math.round(combined[low] + (combined[high] - combined[low]) * fraction));
    sourceIndex += ratio;
  }

  const keepStart = Math.max(0, Math.floor(sourceIndex) - 1);
  resamplerState.buffer = combined.slice(keepStart);
  resamplerState.position = sourceIndex - keepStart;

  return Int16Array.from(output);
}

ensurePanel();
loadConfig().catch((error) => {
  setStatus(error instanceof Error ? error.message : String(error), "error");
  setButtons();
});
window.addEventListener("pagehide", stopAvatarOnUnload);
