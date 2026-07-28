importScripts("causality.js");

const actionCausality = globalThis.HermesCausality.createActionCausality();
const HOST_NAME = "com.hermes.chrome_capture";
const STATUS_ALARM = "hermes-chrome-capture-status";
const CHECKPOINT_KEY = "hermesChromeProviderCheckpoint";
const ACTION_OUTBOX_KEY = "hermesChromeActionOutbox";
const HEARTBEAT_MS = 3000;
const AUTO_SCREENSHOT_DELAY_MS = 350;
const PROTOCOL_VERSION = 3;
const CONTENT_SCRIPT_VERSION = 4;
const EXTENSION_VERSION = chrome.runtime.getManifest().version;
const NATIVE_SCHEMA_VERSION = "hermes-chrome-provider-v1";
const LIVE_JOIN_RETRY_INTERVAL_MS = 150;
const INACTIVE_CONTENT_PROBE_INTERVAL_MS = 1000;
const STATUS_ALARM_MINUTES = 0.5;

const state = {
  chromeSegmentActive: false,
  captureConsentArmed: false,
  captureConsentOwnerTabId: null,
  captureAuthorizationEpoch: 0,
  captureAuthorization: "",
  captureDashboardOrigin: "",
  authorizedRecordingId: "",
  captureAuthorityRevision: 0,
  recordingId: "",
  segmentId: "",
  activeTarget: null,
  contentScriptReady: false,
  lastNativeHeartbeatAt: 0,
  ensurePromise: null,
  heartbeatTimer: null,
  screenshotTimer: null,
  pendingScreenshot: null,
  screenshotPromise: Promise.resolve(),
  joinRetryTimer: null,
  joinRetryOwnerTabId: null,
  lastInactiveContentProbeAt: 0,
  providerSequence: 0,
};

let nativePort = null;
let nextNativeId = 1;
const nativePending = new Map();
const captureAuthorityPorts = new Set();
let actionOutboxChain = Promise.resolve();
let checkpointWriteChain = Promise.resolve();
let startupPromise = Promise.resolve();

function correlationId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function ensureActionCorrelation(row) {
  row.payload = row.payload || {};
  row.payload.meta = row.payload.meta || {};
  row.payload.meta.correlation_id = row.payload.meta.correlation_id || correlationId();
  return row.payload.meta.correlation_id;
}

function ensureProviderIdentity(row) {
  row.payload = row.payload || {};
  row.payload.meta = row.payload.meta || {};
  if (!row.payload.meta.provider_event_id) {
    row.payload.meta.provider_event_id = correlationId();
    state.providerSequence += 1;
    row.payload.meta.provider_sequence = state.providerSequence;
  }
  return row.payload.meta.provider_event_id;
}

function causalMeta(tabId, windowId, url, referrer = "") {
  return actionCausality.match(tabId, windowId, url, referrer);
}
function nowIso() {
  return new Date().toISOString();
}

function originFor(url) {
  try {
    return new URL(url || "").origin;
  } catch {
    return "";
  }
}

function resetSegmentFreshness() {
  state.lastNativeHeartbeatAt = 0;
}

function captureAuthorityActive() {
  return !!(
    state.captureConsentArmed
    && state.chromeSegmentActive
    && state.contentScriptReady
    && state.recordingId
    && state.segmentId
    && /^[0-9a-f]{64}$/.test(state.captureAuthorization)
    && isLoopbackDashboardOrigin(state.captureDashboardOrigin)
    && state.authorizedRecordingId === state.recordingId
  );
}

function captureConsentCurrent(epoch) {
  return state.captureConsentArmed && state.captureAuthorizationEpoch === epoch;
}

function nativeIdentityMatches(status) {
  return status?.schemaVersion === NATIVE_SCHEMA_VERSION
    && status?.protocolVersion === PROTOCOL_VERSION
    && status?.extensionId === chrome.runtime.id
    && status?.extensionVersion === EXTENSION_VERSION;
}

function captureAuthorityPayload(sender = null) {
  const senderTabId = sender?.tab?.id;
  const senderWindowId = sender?.tab?.windowId;
  const targetMatches = Number.isInteger(senderTabId)
    && senderTabId === state.activeTarget?.tabId
    && (!Number.isInteger(senderWindowId) || senderWindowId === state.activeTarget?.windowId);
  return {
    type: "hcc-capture-authority-state",
    revision: state.captureAuthorityRevision,
    active: captureAuthorityActive() && targetMatches,
    recordingId: captureAuthorityActive() && targetMatches ? state.recordingId : "",
  };
}

function publishCaptureAuthority() {
  state.captureAuthorityRevision += 1;
  for (const port of [...captureAuthorityPorts]) {
    try {
      port.postMessage(captureAuthorityPayload(port.sender));
    } catch {
      captureAuthorityPorts.delete(port);
    }
  }
  const tabIds = new Set(
    [...captureAuthorityPorts]
      .map((port) => port.sender?.tab?.id)
      .filter(Number.isInteger),
  );
  if (Number.isInteger(state.activeTarget?.tabId)) tabIds.add(state.activeTarget.tabId);
  for (const tabId of tabIds) {
    void chrome.tabs.sendMessage(tabId, {
      action: "hcc-capture-authority-update",
      ...captureAuthorityPayload({ tab: { id: tabId } }),
    }).catch(() => {});
  }
}

async function clearCaptureAuthorization({
  clearOutbox = false,
  preserveRecordingScope = false,
} = {}) {
  const recordingId = preserveRecordingScope ? state.recordingId : "";
  const authorizedRecordingId = preserveRecordingScope
    ? state.authorizedRecordingId || state.recordingId
    : "";
  state.captureAuthorizationEpoch += 1;
  state.captureConsentArmed = false;
  state.captureConsentOwnerTabId = null;
  state.captureAuthorization = "";
  state.captureDashboardOrigin = "";
  state.authorizedRecordingId = authorizedRecordingId;
  state.chromeSegmentActive = false;
  state.recordingId = recordingId;
  state.segmentId = "";
  state.activeTarget = null;
  state.contentScriptReady = false;
  resetSegmentFreshness();
  clearJoinRetry();
  clearInterval(state.heartbeatTimer);
  state.heartbeatTimer = null;
  clearTimeout(state.screenshotTimer);
  state.screenshotTimer = null;
  state.pendingScreenshot = null;
  await state.screenshotPromise.catch(() => {});
  actionCausality.clear();
  publishCaptureAuthority();
  await actionOutboxChain.catch(() => {});
  await checkpointWriteChain.catch(() => {});
  if (preserveRecordingScope && recordingId) {
    await chrome.storage.local.set({
      [CHECKPOINT_KEY]: {
        schemaVersion: 3,
        recordingId,
        segmentId: "",
        activeTarget: null,
        lastNativeHeartbeatAt: 0,
        providerSequence: state.providerSequence,
        authorizedRecordingId,
        captureConsentOwnerTabId: null,
        captureAuthorization: "",
        captureDashboardOrigin: "",
      },
    });
    if (clearOutbox) await chrome.storage.local.remove(ACTION_OUTBOX_KEY);
  } else {
    await chrome.storage.local.remove(
      clearOutbox ? [CHECKPOINT_KEY, ACTION_OUTBOX_KEY] : CHECKPOINT_KEY,
    );
  }
}

function captureAuthorizedForSender(sender) {
  return captureAuthorityPayload(sender).active === true;
}

async function beginRecordingScope(recordingId) {
  if (state.recordingId === recordingId) return;
  // A provider event can be retried across worker and native-host restarts, but
  // never across recording identities. Drain the old scope before replacing
  // it so an in-flight write cannot repopulate the new recording's outbox.
  await actionOutboxChain.catch(() => {});
  resetSegmentFreshness();
  state.providerSequence = 0;
  await chrome.storage.local.remove(ACTION_OUTBOX_KEY);
}

function connectNative() {
  if (nativePort) return nativePort;
  nativePort = chrome.runtime.connectNative(HOST_NAME);
  nativePort.onMessage.addListener((msg) => {
    const id = msg && msg.id;
    if (id == null || !nativePending.has(id)) return;
    const pending = nativePending.get(id);
    nativePending.delete(id);
    clearTimeout(pending.timer);
    if (msg.ok) pending.resolve(msg);
    else pending.reject(new Error(msg.error || "Native host request failed"));
  });
  nativePort.onDisconnect.addListener(() => {
    const err = new Error(chrome.runtime.lastError?.message || "Native host disconnected");
    for (const pending of nativePending.values()) {
      clearTimeout(pending.timer);
      pending.reject(err);
    }
    nativePending.clear();
    nativePort = null;
  });
  return nativePort;
}

function nativeRequest(type, payload = {}, timeoutMs = 5000) {
  const id = nextNativeId++;
  const message = {
    id,
    type,
    extensionId: chrome.runtime.id,
    extensionVersion: EXTENSION_VERSION,
    ...payload,
  };
  return new Promise((resolve, reject) => {
    try {
      const port = connectNative();
      const timer = setTimeout(() => {
        nativePending.delete(id);
        reject(new Error(`Native host timed out for ${type}`));
      }, timeoutMs);
      nativePending.set(id, { resolve, reject, timer });
      port.postMessage(message);
    } catch (err) {
      reject(err);
    }
  });
}

async function nativeOptional(type, payload = {}) {
  try {
    return await nativeRequest(type, payload);
  } catch (err) {
    console.warn("Hermes Chrome Provider native request skipped", type, err);
    return null;
  }
}

async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return tab || null;
}

function targetForTab(tab, attached = false) {
  if (!tab?.id) return null;
  const reuseTarget = state.chromeSegmentActive && state.activeTarget?.tabId === tab.id && state.activeTarget?.windowId === tab.windowId;
  return {
    activeTargetId: reuseTarget ? state.activeTarget.activeTargetId : "",
    tabId: tab.id,
    windowId: tab.windowId,
    url: tab.url || "",
    title: tab.title || "",
    attached,
    updatedAt: nowIso(),
  };
}

function activeTargetSummary(target = state.activeTarget) {
  return target ? {
    activeTargetId: target.activeTargetId,
    activeTabId: target.tabId,
    activeWindowId: target.windowId,
    activeUrl: target.url,
    activeTitle: target.title,
    activeTargetAttached: !!target.attached,
    activeTargetUpdatedAt: target.updatedAt,
  } : {};
}

function browserPayload(tab, frame = {}) {
  const url = frame.frameUrl || tab?.url || state.activeTarget?.url || "";
  return {
    url,
    origin: originFor(url),
    title: tab?.title || state.activeTarget?.title || "",
    tab_id: tab?.id || state.activeTarget?.tabId || 0,
    window_id: tab?.windowId || state.activeTarget?.windowId || 0,
    frame_id: frame.frameId ?? 0,
    parent_frame_id: frame.parentFrameId ?? 0,
    document_id: frame.documentId || "",
    frame_url: frame.frameUrl || url,
    top_url: frame.topUrl || tab?.url || url,
    active_target_id: state.activeTarget?.activeTargetId || "",
    profile_hint: "current-profile",
    viewport: frame.viewport || {},
  };
}

function segmentPayload(tab, reason = "heartbeat") {
  return {
    reason,
    protocolVersion: PROTOCOL_VERSION,
    recordingId: state.recordingId,
    segmentId: state.segmentId,
    userAgent: navigator.userAgent,
    platform: navigator.platform || "",
    language: navigator.language || "",
    languages: navigator.languages || [],
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "",
    online: navigator.onLine,
    tabId: tab?.id,
    windowId: tab?.windowId,
    url: tab?.url,
    title: tab?.title,
    contentScriptReady: state.contentScriptReady,
    contentScriptUpdatedAt: nowIso(),
    captureAuthorization: state.captureAuthorization,
    dashboardOrigin: state.captureDashboardOrigin,
    ...activeTargetSummary(),
  };
}

function writeCheckpoint() {
  const authorizationEpoch = state.captureAuthorizationEpoch;
  const checkpoint = {
    schemaVersion: 3,
    recordingId: state.recordingId,
    segmentId: state.segmentId,
    activeTarget: state.activeTarget,
    lastNativeHeartbeatAt: state.lastNativeHeartbeatAt,
    providerSequence: state.providerSequence,
    authorizedRecordingId: state.authorizedRecordingId,
    captureConsentOwnerTabId: state.captureConsentOwnerTabId,
    captureAuthorization: state.captureAuthorization,
    captureDashboardOrigin: state.captureDashboardOrigin,
  };
  checkpointWriteChain = checkpointWriteChain.catch(() => {}).then(async () => {
    if (!captureConsentCurrent(authorizationEpoch)) return;
    try {
      await chrome.storage.local.set({ [CHECKPOINT_KEY]: checkpoint });
    } catch {
      // Storage is best-effort.
    }
  });
  return checkpointWriteChain;
}

async function restoreCheckpoint() {
  try {
    const value = await chrome.storage.local.get([CHECKPOINT_KEY, ACTION_OUTBOX_KEY]);
    const checkpoint = value?.[CHECKPOINT_KEY]?.schemaVersion === 3
      ? value[CHECKPOINT_KEY]
      : null;
    if (checkpoint?.activeTarget) state.activeTarget = checkpoint.activeTarget;
    if (checkpoint?.recordingId) state.recordingId = checkpoint.recordingId;
    if (checkpoint?.segmentId) state.segmentId = checkpoint.segmentId;
    if (Number.isFinite(checkpoint?.lastNativeHeartbeatAt)) {
      state.lastNativeHeartbeatAt = checkpoint.lastNativeHeartbeatAt;
    }
    if (Number.isFinite(checkpoint?.providerSequence)) {
      state.providerSequence = checkpoint.providerSequence;
    }
    if (
      checkpoint?.authorizedRecordingId
      && checkpoint.authorizedRecordingId === checkpoint.recordingId
    ) {
      state.authorizedRecordingId = checkpoint.authorizedRecordingId;
    }
    if (Number.isInteger(checkpoint?.captureConsentOwnerTabId)) {
      state.captureConsentOwnerTabId = checkpoint.captureConsentOwnerTabId;
    }
    if (/^[0-9a-f]{64}$/.test(String(checkpoint?.captureAuthorization || ""))) {
      state.captureAuthorization = checkpoint.captureAuthorization;
    }
    if (isLoopbackDashboardOrigin(checkpoint?.captureDashboardOrigin)) {
      state.captureDashboardOrigin = checkpoint.captureDashboardOrigin;
    }
    const storedOutbox = value?.[ACTION_OUTBOX_KEY];
    const queued = storedOutbox?.recordingId === state.recordingId && Array.isArray(storedOutbox.rows)
      ? storedOutbox.rows
      : [];
    state.providerSequence = Math.max(
      state.providerSequence,
      ...queued.map((row) => Number(row?.payload?.meta?.provider_sequence) || 0),
    );
  } catch {
    // Storage is best-effort.
  }
}

async function restoreCaptureAuthorization() {
  if (
    !state.authorizedRecordingId
    || state.authorizedRecordingId !== state.recordingId
    || !Number.isInteger(state.captureConsentOwnerTabId)
    || !/^[0-9a-f]{64}$/.test(state.captureAuthorization)
    || !isLoopbackDashboardOrigin(state.captureDashboardOrigin)
  ) {
    const ownerlessRecoveryScope = !!state.recordingId
      && state.authorizedRecordingId === state.recordingId
      && !Number.isInteger(state.captureConsentOwnerTabId)
      && !state.captureAuthorization
      && !state.captureDashboardOrigin;
    await clearCaptureAuthorization({
      preserveRecordingScope: ownerlessRecoveryScope,
    });
    return;
  }
  const native = await nativeOptional("status");
  const captureState = native?.captureState || {};
  const nativeRecordingId = String(
    captureState.sessionID
    || native?.recordingId
    || "",
  );
  if (
    !nativeIdentityMatches(native)
    || native?.active !== true
    || captureState.sessionActive !== true
    || nativeRecordingId !== state.authorizedRecordingId
  ) {
    await clearCaptureAuthorization({ clearOutbox: true });
    return;
  }
  state.captureAuthorizationEpoch += 1;
  state.captureConsentArmed = true;
  await ensureSegment("authorized_worker_restore");
}

async function probeUserEvents(tabId) {
  try {
    const response = await chrome.tabs.sendMessage(tabId, { action: "hcc-ping" }, { frameId: 0 });
    return response?.ready === true && response?.version === CONTENT_SCRIPT_VERSION;
  } catch {
    return false;
  }
}

function sameTarget(left, right) {
  return !!left && !!right && left.tabId === right.tabId && left.windowId === right.windowId && left.url === right.url;
}

function isTrustedDashboardUrl(value) {
  try {
    const url = new URL(value || "");
    return url.protocol === "http:"
      && ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
      && url.pathname === "/workflows";
  } catch {
    return false;
  }
}

function dashboardOriginForSender(sender) {
  if (!isTrustedDashboardSender(sender)) return "";
  try {
    return new URL(sender.tab.url).origin;
  } catch {
    return "";
  }
}

function isLoopbackDashboardOrigin(value) {
  try {
    const url = new URL(String(value || ""));
    return url.protocol === "http:"
      && ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
      && !!url.port
      && !url.username
      && !url.password
      && String(value) === url.origin;
  } catch {
    return false;
  }
}

function isTrustedDashboardSender(sender) {
  return Number.isInteger(sender?.tab?.id) && isTrustedDashboardUrl(sender.tab.url);
}

function captureOwnedBySender(sender) {
  const origin = dashboardOriginForSender(sender);
  return !!origin
    && sender?.tab?.id === state.captureConsentOwnerTabId
    && isLoopbackDashboardOrigin(origin)
    && origin === state.captureDashboardOrigin;
}

function clearJoinRetry() {
  if (state.joinRetryTimer) clearTimeout(state.joinRetryTimer);
  state.joinRetryTimer = null;
  state.joinRetryOwnerTabId = null;
}

function resetSegmentState() {
  state.chromeSegmentActive = false;
  state.segmentId = "";
  state.contentScriptReady = false;
  resetSegmentFreshness();
  clearInterval(state.heartbeatTimer);
  state.heartbeatTimer = null;
  actionCausality.clear();
  if (state.joinRetryOwnerTabId == null) clearJoinRetry();
  publishCaptureAuthority();
  void writeCheckpoint();
}

async function syncSegment(reason = "ensure") {
  const authorizationEpoch = state.captureAuthorizationEpoch;
  if (!captureConsentCurrent(authorizationEpoch)) return null;
  const tab = await getActiveTab();
  if (!captureConsentCurrent(authorizationEpoch)) return null;
  if (!tab?.id) return null;
  const previousTarget = state.activeTarget;
  await injectUserEvents(tab.id);
  if (!captureConsentCurrent(authorizationEpoch)) return null;
  const contentScriptReady = await probeUserEvents(tab.id);
  if (!captureConsentCurrent(authorizationEpoch)) return null;
  state.contentScriptReady = contentScriptReady;
  state.activeTarget = targetForTab(tab, state.contentScriptReady);
  const targetChanged = !sameTarget(previousTarget, state.activeTarget);
  const heartbeatAgeMs = Date.now() - state.lastNativeHeartbeatAt;
  const heartbeatDue = heartbeatAgeMs >= HEARTBEAT_MS - 250;
  if (state.chromeSegmentActive && !targetChanged && !heartbeatDue) {
    scheduleHeartbeat();
    await writeCheckpoint();
    try {
      await flushStoredActions();
    } catch (error) {
      console.warn("Hermes Chrome Provider outbox flush failed", error);
    }
    return tab;
  }
  let requestType = state.chromeSegmentActive ? "segment.heartbeat" : "segment.start";
  let response = await nativeOptional(requestType, segmentPayload(tab, reason));
  if (!captureConsentCurrent(authorizationEpoch)) return null;
  if (!response && requestType === "segment.heartbeat") {
    // A restored MV3 checkpoint can contain a segment lease that the host no
    // longer recognizes. Rejoin through segment.start, whose authority is the
    // single active host recording, instead of leaving every future request
    // poisoned by the stale recording/segment identity.
    requestType = "segment.start";
    response = await nativeOptional(requestType, segmentPayload(tab, `${reason}_rejoin`));
    if (!captureConsentCurrent(authorizationEpoch)) return null;
  }
  if (response?.segmentActive) {
    const responseRecordingId = response.recordingId || "";
    const responseSegmentId = response.segmentId || state.segmentId;
    if (
      state.authorizedRecordingId
      && state.authorizedRecordingId !== responseRecordingId
    ) {
      await clearCaptureAuthorization({ clearOutbox: true });
      return null;
    }
    const sameSegment = !!state.recordingId
      && state.recordingId === responseRecordingId
      && !!state.segmentId
      && state.segmentId === responseSegmentId;
    // Provider segments are app-surface leases inside one recording. An
    // intentional Chrome -> native -> Chrome handoff creates a new segment,
    // but the already persisted business actions still belong to this capture.
    // Only a different recording identity starts a new durable outbox scope.
    await beginRecordingScope(responseRecordingId);
    if (!captureConsentCurrent(authorizationEpoch)) return null;
    if (!sameSegment) resetSegmentFreshness();
    state.chromeSegmentActive = true;
    state.recordingId = responseRecordingId;
    state.segmentId = responseSegmentId;
    state.authorizedRecordingId = responseRecordingId;
    if (state.activeTarget && !state.activeTarget.activeTargetId) {
      state.activeTarget.activeTargetId = `${state.segmentId}:${state.activeTarget.windowId}:${state.activeTarget.tabId}`;
    }
    state.lastNativeHeartbeatAt = Date.now();
    clearJoinRetry();
    scheduleHeartbeat();
    await writeCheckpoint();
    if (!captureConsentCurrent(authorizationEpoch)) return null;
    publishCaptureAuthority();
    try {
      await flushStoredActions();
    } catch (error) {
      console.warn("Hermes Chrome Provider outbox flush failed", error);
    }
  } else if (response) {
    // An inactive native response is authoritative, but a prepare request may
    // intentionally arrive just before the backend recording exists. Keep the
    // requested join alive across that ordering window.
    resetSegmentState();
  } else {
    // A transport failure is not proof that the recording ended. Revoke the
    // dead owner while retaining the recording/outbox scope so a visible
    // Workflows tab can authorize a fresh segment.start.
    await clearCaptureAuthorization({
      preserveRecordingScope: !!state.recordingId,
    });
  }
  return tab;
}

async function ensureSegment(reason = "ensure") {
  if (!state.chromeSegmentActive && String(reason).startsWith("content_")) {
    const now = Date.now();
    if (now - state.lastInactiveContentProbeAt < INACTIVE_CONTENT_PROBE_INTERVAL_MS) return null;
    state.lastInactiveContentProbeAt = now;
  }
  if (state.ensurePromise) return state.ensurePromise;
  state.ensurePromise = syncSegment(reason).finally(() => {
    state.ensurePromise = null;
  });
  return state.ensurePromise;
}

function runJoinRetry(ownerTabId) {
  const retry = async () => {
    if (state.joinRetryOwnerTabId !== ownerTabId) return;
    state.joinRetryTimer = null;
    try {
      await ensureSegment("recording_start_requested");
    } catch (error) {
      console.warn("Hermes Chrome Provider join retry failed", error);
    }
    if (state.chromeSegmentActive || state.joinRetryOwnerTabId !== ownerTabId) {
      if (state.joinRetryOwnerTabId === ownerTabId) clearJoinRetry();
      await writeCheckpoint();
      return;
    }
    state.joinRetryTimer = setTimeout(retry, LIVE_JOIN_RETRY_INTERVAL_MS);
  };
  void retry();
}

function armJoinRetry(ownerTabId, captureAuthorization, dashboardOrigin) {
  const normalizedAuthorization = String(captureAuthorization || "").trim().toLowerCase();
  if (
    !Number.isInteger(ownerTabId)
    || !/^[0-9a-f]{64}$/.test(normalizedAuthorization)
    || !isLoopbackDashboardOrigin(dashboardOrigin)
  ) {
    return false;
  }
  if (state.captureConsentArmed) {
    if (
      state.captureConsentOwnerTabId !== ownerTabId
      || state.captureAuthorization !== normalizedAuthorization
      || state.captureDashboardOrigin !== dashboardOrigin
    ) return false;
    if (!state.chromeSegmentActive && state.joinRetryOwnerTabId == null) {
      state.joinRetryOwnerTabId = ownerTabId;
      runJoinRetry(ownerTabId);
    }
    return true;
  }
  if (state.joinRetryOwnerTabId != null) {
    return state.joinRetryOwnerTabId === ownerTabId;
  }
  state.captureConsentArmed = true;
  state.captureAuthorizationEpoch += 1;
  state.captureConsentOwnerTabId = ownerTabId;
  state.captureAuthorization = normalizedAuthorization;
  state.captureDashboardOrigin = dashboardOrigin;
  state.authorizedRecordingId = "";
  state.joinRetryOwnerTabId = ownerTabId;
  runJoinRetry(ownerTabId);
  return true;
}

async function cancelCaptureConsent(sender, captureAuthorization = "") {
  const ownerTabId = sender?.tab?.id;
  const normalizedAuthorization = String(captureAuthorization || "").trim().toLowerCase();
  if (
    !captureOwnedBySender(sender)
    || (
      state.authorizedRecordingId
        ? normalizedAuthorization && normalizedAuthorization !== state.captureAuthorization
        : (
          state.captureConsentOwnerTabId !== ownerTabId
          || state.joinRetryOwnerTabId !== ownerTabId
          || normalizedAuthorization !== state.captureAuthorization
        )
    )
  ) {
    return false;
  }
  await clearCaptureAuthorization({ clearOutbox: true });
  return true;
}

function scheduleHeartbeat() {
  if (state.heartbeatTimer) return;
  state.heartbeatTimer = setInterval(async () => {
    await ensureSegment("timer");
  }, HEARTBEAT_MS);
}

async function injectUserEvents(tabId) {
  try {
    await chrome.scripting.executeScript({ target: { tabId, allFrames: true }, files: ["src/causality.js", "src/user-events.js"] });
  } catch {
    // Content scripts are also declared in the manifest; injection is best-effort for already-open pages.
  }
}

async function sendTraceContext(rows) {
  if (!rows.length) return null;
  if (!captureAuthorityActive()) return { ok: true, skipped: true, reason: "not_recording" };
  return nativeOptional("trace.context", { protocolVersion: PROTOCOL_VERSION, recordingId: state.recordingId, segmentId: state.segmentId, rows });
}

async function sendTraceActions(rows) {
  if (!rows.length) return { accepted: 0 };
  if (!captureAuthorityActive()) return { accepted: 0, queued: 0, reason: "not_recording" };
  const recordingId = state.recordingId;
  const segmentId = state.segmentId;
  if (!recordingId || !segmentId) return { accepted: 0, queued: 0, reason: "not_recording" };
  rows.forEach((row) => {
    ensureActionCorrelation(row);
    ensureProviderIdentity(row);
  });
  // The durable outbox is the recovery authority. A failed native send must
  // leave its rows queued, but it must not poison every later flush in this
  // worker lifetime.
  actionOutboxChain = actionOutboxChain.catch(() => {}).then(async () => {
    const stored = await chrome.storage.local.get(ACTION_OUTBOX_KEY);
    const storedOutbox = stored?.[ACTION_OUTBOX_KEY];
    const pending = storedOutbox?.recordingId === recordingId && Array.isArray(storedOutbox.rows)
      ? storedOutbox.rows
      : [];
    const byId = new Map();
    [...pending, ...rows].forEach((row) => {
      const id = row?.payload?.meta?.provider_event_id;
      if (id) byId.set(id, row);
    });
    await chrome.storage.local.set({
      [ACTION_OUTBOX_KEY]: { schemaVersion: 2, recordingId, rows: [...byId.values()] },
    });
    await writeCheckpoint();
    if (!state.chromeSegmentActive || state.recordingId !== recordingId || state.segmentId !== segmentId) {
      return { accepted: 0, queued: byId.size, reason: "recording_scope_changed" };
    }
    const batch = [...byId.values()];
    const response = await nativeRequest("trace.actions", { protocolVersion: PROTOCOL_VERSION, recordingId, segmentId, rows: batch });
    const acknowledged = new Set(Array.isArray(response?.acceptedEventIds) ? response.acceptedEventIds : []);
    const remaining = batch.filter((row) => !acknowledged.has(row?.payload?.meta?.provider_event_id));
    const current = await chrome.storage.local.get(ACTION_OUTBOX_KEY);
    if (current?.[ACTION_OUTBOX_KEY]?.recordingId === recordingId) {
      await chrome.storage.local.set({
        [ACTION_OUTBOX_KEY]: { schemaVersion: 2, recordingId, rows: remaining },
      });
    }
    return response;
  });
  return actionOutboxChain;
}

async function flushStoredActions() {
  const stored = await chrome.storage.local.get(ACTION_OUTBOX_KEY);
  const storedOutbox = stored?.[ACTION_OUTBOX_KEY];
  if (storedOutbox?.recordingId && storedOutbox.recordingId !== state.recordingId) {
    await chrome.storage.local.remove(ACTION_OUTBOX_KEY);
    return;
  }
  const pending = storedOutbox?.recordingId === state.recordingId && Array.isArray(storedOutbox.rows)
    ? storedOutbox.rows
    : [];
  if (pending.length) await sendTraceActions(pending);
}

async function sendTraceEvidence(rows) {
  if (!rows.length) return null;
  if (!captureAuthorityActive()) return { ok: true, skipped: true, reason: "not_recording" };
  return nativeOptional("trace.evidence", { protocolVersion: PROTOCOL_VERSION, recordingId: state.recordingId, segmentId: state.segmentId, rows });
}

async function finalizeContinuity(expectedRecordingId = "") {
  if (!expectedRecordingId) {
    return {
      ok: false,
      contentScriptVersion: CONTENT_SCRIPT_VERSION,
      continuity: { ok: false, reasons: ["recording_identity_required"] },
      error: "Chrome capture continuity failed: recording_identity_required",
    };
  }
  if (state.recordingId && state.recordingId !== expectedRecordingId) {
    // The dashboard binds finalization to the immutable recording revision it
    // is stopping. Force a host-authoritative rejoin after worker suspension;
    // never finalize whichever stale recording happened to be checkpointed.
    state.chromeSegmentActive = false;
    state.segmentId = "";
    resetSegmentFreshness();
  }
  await ensureSegment("finalize");
  await flushStoredActions();
  await actionOutboxChain;
  if (state.screenshotTimer && state.pendingScreenshot) {
    clearTimeout(state.screenshotTimer);
    state.screenshotTimer = null;
    const pending = state.pendingScreenshot;
    state.pendingScreenshot = null;
    state.screenshotPromise = captureScreenshot(
      pending.label,
      pending.afterActionId,
    );
  }
  await state.screenshotPromise;
  if (state.recordingId !== expectedRecordingId) {
    return {
      ok: false,
      contentScriptVersion: CONTENT_SCRIPT_VERSION,
      continuity: { ok: false, reasons: ["recording_identity_mismatch"] },
      error: "Chrome capture continuity failed: recording_identity_mismatch",
    };
  }
  const result = await nativeRequest("recording.finalize", {
    protocolVersion: PROTOCOL_VERSION,
    recordingId: expectedRecordingId,
    // Finalization is recording-scoped. A Chrome -> desktop handoff correctly
    // closes the active Chrome segment before the user returns to the review
    // page, so there may be no live segment lease left to present here.
    segmentId: state.segmentId || "",
  });
  const continuity = result.continuity || { ok: false, reasons: ["host_finalization_missing"] };
  return {
    ok: continuity.ok,
    contentScriptVersion: CONTENT_SCRIPT_VERSION,
    continuity,
    error: continuity.ok ? "" : `Chrome capture continuity failed: ${continuity.reasons.join(", ")}`,
  };
}

function rowFromContentEvent(event, sender) {
  const tab = sender.tab || state.activeTarget || {};
  const browser = browserPayload(tab, event.payload?.browser || event.browser || {});
  return {
    ts: event.ts || nowIso(),
    kind: event.kind || "click",
    payload: { ...(event.payload || {}), browser: { ...browser, ...(event.payload?.browser || {}) } },
    quality: event.quality || { confidence: "high", warnings: [] },
  };
}

async function captureScreenshot(label = "manual", afterActionId = "") {
  if (!captureAuthorityActive()) throw new Error("No authorized Chrome recording is active");
  const tab = await ensureSegment("screenshot");
  if (!captureAuthorityActive()) throw new Error("No authorized Chrome recording is active");
  if (!tab?.windowId) throw new Error("No active tab for screenshot");
  const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
  return nativeOptional("screenshot", { protocolVersion: PROTOCOL_VERSION, recordingId: state.recordingId, segmentId: state.segmentId, dataUrl, tabId: tab.id, windowId: tab.windowId, url: tab.url, title: tab.title, activeTargetId: state.activeTarget?.activeTargetId, label, afterActionId });
}

function scheduleEvidenceScreenshot(kind, afterActionId) {
  clearTimeout(state.screenshotTimer);
  state.pendingScreenshot = {
    label: `after-${kind}`,
    afterActionId,
  };
  state.screenshotTimer = setTimeout(() => {
    state.screenshotTimer = null;
    const pending = state.pendingScreenshot;
    state.pendingScreenshot = null;
    state.screenshotPromise = captureScreenshot(
      pending.label,
      pending.afterActionId,
    ).catch(() => {});
  }, AUTO_SCREENSHOT_DELAY_MS);
}

chrome.runtime.onConnect?.addListener((port) => {
  if (port?.name !== "hcc-capture-authority" || !Number.isInteger(port.sender?.tab?.id)) {
    port?.disconnect?.();
    return;
  }
  captureAuthorityPorts.add(port);
  port.onDisconnect.addListener(() => {
    captureAuthorityPorts.delete(port);
  });
  void startupPromise.then(() => {
    if (!captureAuthorityPorts.has(port)) return;
    try {
      port.postMessage(captureAuthorityPayload(port.sender));
    } catch {
      captureAuthorityPorts.delete(port);
    }
  });
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    // Do not allow a fresh service worker's checkpoint restore to race a new
    // user consent command and erase the authorization it just armed.
    await startupPromise;
    if (msg?.type === "hcc-capture-authority-query") {
      sendResponse({ ok: true, ...captureAuthorityPayload(sender) });
    } else if (msg?.type === "hcc-provider-command") {
      if (!isTrustedDashboardSender(sender)) {
        sendResponse({ ok: false, error: "Provider command rejected" });
        return;
      }
      if (msg.command === "probe") {
        sendResponse({ ok: true, contentScriptVersion: CONTENT_SCRIPT_VERSION });
      } else if (msg.command === "prepare") {
        let nativeStatus = null;
        try {
          nativeStatus = await nativeRequest("status", {}, 1500);
        } catch (error) {
          sendResponse({
            ok: false,
            contentScriptVersion: CONTENT_SCRIPT_VERSION,
            error: `Hermes Chrome Provider cannot reach its native host: ${String(error?.message || error)}`,
          });
          return;
        }
        if (!nativeIdentityMatches(nativeStatus)) {
          sendResponse({
            ok: false,
            contentScriptVersion: CONTENT_SCRIPT_VERSION,
            error: "Hermes Chrome Provider and its native host are from different versions. Reinstall the current provider.",
          });
          return;
        }
        const prepared = armJoinRetry(
          sender.tab.id,
          msg.captureAuthorization,
          dashboardOriginForSender(sender),
        );
        sendResponse(prepared
          ? { ok: true, contentScriptVersion: CONTENT_SCRIPT_VERSION }
          : { ok: false, error: "Provider join authorization was rejected or is owned by another Workflows tab" });
      } else if (msg.command === "cancel") {
        const cancelled = await cancelCaptureConsent(sender, msg.captureAuthorization);
        sendResponse(cancelled
          ? { ok: true, contentScriptVersion: CONTENT_SCRIPT_VERSION }
          : { ok: false, error: "Provider join is not owned by this Workflows tab" });
      } else if (msg.command === "finalize") {
        if (!captureOwnedBySender(sender)) {
          sendResponse({ ok: false, error: "Provider finalization was rejected" });
          return;
        }
        const finalized = await finalizeContinuity(String(msg.recordingId || ""));
        if (finalized.ok === true) {
          await clearCaptureAuthorization({ clearOutbox: true });
        }
        sendResponse(finalized);
      } else {
        sendResponse({ ok: false, error: "Unknown provider command" });
      }
    } else if (msg?.action === "status") {
      const native = await nativeOptional("status");
      sendResponse({
        chromeSegmentActive: captureAuthorityActive(),
        activeTarget: captureAuthorityActive() ? state.activeTarget : null,
        native: { active: native?.active === true },
      });
    } else if (msg?.action === "screenshot") {
      sendResponse(await captureScreenshot("manual"));
    } else if (msg?.type === "hcc-trace-action") {
      if (!captureAuthorizedForSender(sender)) {
        sendResponse({ ok: true, skipped: true, reason: "not_recording" });
        return;
      }
      await ensureSegment("content_event");
      if (!captureAuthorizedForSender(sender)) {
        const recovering = !!state.recordingId;
        sendResponse(recovering
          ? { ok: false, error: "Chrome recording continuity is unavailable; the action was not captured" }
          : { ok: true, skipped: true, reason: "not_recording" });
        return;
      }
      const row = rowFromContentEvent(msg.event || {}, sender);
      const actionId = ensureActionCorrelation(row);
      const providerEventId = ensureProviderIdentity(row);
      const stored = await actionCausality.rememberWhileStoring(row, () => sendTraceActions([row]));
      if (
        !Array.isArray(stored?.acceptedEventIds)
        || !stored.acceptedEventIds.includes(providerEventId)
      ) {
        throw new Error("Chrome action was not persisted to the active recording");
      }
      if (stored?.accepted && ["click", "double_click", "submit", "upload", "drop", "change", "navigate"].includes(row.kind)) {
        scheduleEvidenceScreenshot(row.kind, actionId);
      }
      sendResponse({ ok: true });
    } else if (msg?.type === "hcc-trace-context") {
      if (!captureAuthorizedForSender(sender)) {
        sendResponse({ ok: true, skipped: true, reason: "not_recording" });
        return;
      }
      await ensureSegment("content_context");
      if (!captureAuthorizedForSender(sender)) {
        sendResponse({ ok: true, skipped: true, reason: "not_recording" });
        return;
      }
      await sendTraceContext([rowFromContentEvent(msg.event || {}, sender)]);
      sendResponse({ ok: true });
    } else if (msg?.type === "hcc-trace-evidence") {
      if (!captureAuthorizedForSender(sender)) {
        sendResponse({ ok: true, skipped: true, reason: "not_recording" });
        return;
      }
      await ensureSegment("content_evidence");
      if (!captureAuthorizedForSender(sender)) {
        sendResponse({ ok: true, skipped: true, reason: "not_recording" });
        return;
      }
      await sendTraceEvidence([rowFromContentEvent(msg.event || {}, sender)]);
      sendResponse({ ok: true });
    } else {
      sendResponse({ ok: false, error: "unknown message" });
    }
  })().catch((err) => sendResponse({ ok: false, error: String(err?.message || err) }));
  return true;
});

chrome.tabs.onActivated.addListener(() => {
  if (state.captureConsentArmed) void ensureSegment("tab_activated");
});
chrome.windows.onFocusChanged.addListener((windowId) => {
  if (state.captureConsentArmed && windowId !== chrome.windows.WINDOW_ID_NONE) {
    void ensureSegment("window_focused");
  }
});
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (!state.captureConsentArmed) return;
  if (
    state.captureConsentOwnerTabId === tabId
    && !state.authorizedRecordingId
    && (
      changeInfo.status === "loading"
      || (
        changeInfo.url
        && (
          !isTrustedDashboardUrl(changeInfo.url)
          || new URL(changeInfo.url).origin !== state.captureDashboardOrigin
        )
      )
    )
  ) {
    void clearCaptureAuthorization({ clearOutbox: true });
    return;
  }
  if (!changeInfo.url && !changeInfo.title && changeInfo.status !== "complete") return;
  void (async () => {
    const beforeUrl = state.activeTarget?.url || "";
    await ensureSegment("tab_updated");
    if (
      changeInfo.url
      && captureAuthorityActive()
      && tabId === state.activeTarget?.tabId
    ) {
      const meta = {
        correlation_id: correlationId(),
        ...causalMeta(tabId, tab?.windowId, changeInfo.url),
      };
      await sendTraceActions([{ kind: "navigate", payload: { browser: browserPayload(tab), meta, effect: { before_url: beforeUrl, after_url: changeInfo.url } }, quality: { confidence: "high", warnings: [] } }]);
    }
  })().catch((error) => {
    console.warn("Hermes Chrome Provider did not persist top-level navigation", error);
  });
});
chrome.tabs.onRemoved.addListener((tabId) => {
  if (state.captureConsentOwnerTabId === tabId) {
    void clearCaptureAuthorization({
      clearOutbox: !state.recordingId,
      preserveRecordingScope: !!state.recordingId,
    });
  }
});
chrome.webNavigation.onHistoryStateUpdated.addListener((details) => {
  if (!state.captureConsentArmed) return;
  // Child-frame route changes are implementation details. Direct actions
  // inside those frames still arrive through the content script with full
  // frame metadata, but a passive iframe navigation must not become a
  // workflow phase or dependency.
  if (details.frameId !== 0) return;
  if (!captureAuthorityActive() || details.tabId !== state.activeTarget?.tabId) return;
  const windowId = state.activeTarget?.tabId === details.tabId ? state.activeTarget.windowId : undefined;
  const meta = {
    correlation_id: correlationId(),
    ...causalMeta(details.tabId, windowId, details.url),
  };
  void sendTraceActions([{ kind: "navigate", payload: { browser: browserPayload({ id: details.tabId, windowId, url: details.url }, { frameId: details.frameId, topUrl: details.url }), meta, effect: { after_url: details.url } }, quality: { confidence: "high", warnings: [] } }]).catch((error) => {
    console.warn("Hermes Chrome Provider did not persist navigation", error);
  });
});
chrome.downloads.onChanged.addListener(async (downloadDelta) => {
  if (!captureAuthorityActive()) return;
  if (!downloadDelta.id) return;
  const [download] = await chrome.downloads.search({ id: downloadDelta.id }).catch(() => []);
  if (!captureAuthorityActive()) return;
  if (!download) return;
  const tabId = state.activeTarget?.tabId;
  const windowId = state.activeTarget?.windowId;
  if (!Number.isInteger(tabId)) return;
  const causality = causalMeta(
    tabId,
    windowId,
    download.finalUrl || download.url,
    download.referrer || "",
  );
  if (!causality.after_action_id) return;
  const meta = {
    correlation_id: correlationId(),
    ...causality,
  };
  try {
    await sendTraceActions([{ kind: "download", payload: { browser: browserPayload(state.activeTarget || {}), meta, effect: { download: { id: download.id, url: download.url || "", final_url: download.finalUrl || "", referrer: download.referrer || "", filename: download.filename || "", mime: download.mime || "", state: download.state || "", error: download.error || "", total_bytes: download.totalBytes || 0, file_size: download.fileSize || 0 } } }, quality: { confidence: "medium", warnings: [] } }]);
  } catch (error) {
    console.warn("Hermes Chrome Provider did not persist download", error);
  }
});
chrome.alarms.onAlarm.addListener((alarm) => {
  if (state.captureConsentArmed && alarm.name === STATUS_ALARM) void ensureSegment("alarm");
});
chrome.alarms.create(STATUS_ALARM, { delayInMinutes: STATUS_ALARM_MINUTES, periodInMinutes: STATUS_ALARM_MINUTES });
chrome.runtime.onSuspend?.addListener(() => { void writeCheckpoint(); });

startupPromise = (async () => {
  await restoreCheckpoint();
  await restoreCaptureAuthorization();
})();
