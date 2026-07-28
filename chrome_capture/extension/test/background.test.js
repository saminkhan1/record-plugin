import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import { webcrypto } from "node:crypto";

const extensionRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const sourcePath = path.join(extensionRoot, "src", "background.js");
const extensionVersion = JSON.parse(
  readFileSync(path.join(extensionRoot, "manifest.json"), "utf8"),
).version;
const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
const CAPTURE_AUTHORIZATION = "ab".repeat(32);
const DASHBOARD_ORIGIN = "http://127.0.0.1:54821";

function loadBackground({ traceActionsAccepted = 1, nativeResponder = null, localStorage = {}, hostLedger = [] } = {}) {
  const tab = {
    id: 7,
    windowId: 3,
    url: "https://mail.example.test/inbox",
    title: "Inbox",
  };
  const nativeMessages = [];
  const messageListeners = [];
  const tabUpdatedListeners = [];
  const tabRemovedListeners = [];
  const alarmListeners = [];
  let nativeMessageListener = null;
  let nativeDisconnectListener = null;
  let segmentRequestCount = 0;

  const nativePort = {
    onMessage: {
      addListener(listener) {
        nativeMessageListener = listener;
      },
    },
    onDisconnect: {
      addListener(listener) {
        nativeDisconnectListener = listener;
      },
    },
    postMessage(message) {
      nativeMessages.push(message);
      hostLedger.push(message);
      let payload;
      if (nativeResponder) payload = nativeResponder(message, segmentRequestCount);
      if (message.type === "segment.start" || message.type === "segment.heartbeat") {
        segmentRequestCount += 1;
      }
      const defaultPayload = message.type === "segment.start" || message.type === "segment.heartbeat"
        ? { segmentActive: true, recordingId: "recording-1", segmentId: "segment-1" }
        : message.type === "trace.actions"
          ? {
              accepted: traceActionsAccepted,
              acceptedEventIds: (message.rows || [])
                .slice(0, traceActionsAccepted)
                .map((row) => row?.payload?.meta?.provider_event_id)
                .filter(Boolean),
            }
          : message.type === "trace.context" || message.type === "trace.evidence"
            ? { accepted: message.rows?.length || 0 }
            : message.type === "recording.finalize"
              ? (() => {
                  const actions = hostLedger
                    .filter((item) => item.type === "trace.actions")
                    .flatMap((item) => item.rows || []);
                  const origins = [...new Set(actions.map((row) => row?.payload?.browser?.origin).filter((origin) =>
                    /^https?:\/\//.test(origin || "") && !/127\.0\.0\.1.*\/workflows/.test(origin || "")
                  ))];
                  const artifacts = hostLedger.filter((item) => item.type === "screenshot").length;
                  const reasons = [];
                  if (!actions.length) reasons.push("missing_chrome_actions");
                  if (!origins.length) reasons.push("missing_chrome_origin");
                  if (!artifacts) reasons.push("missing_chrome_artifact");
                  return { continuity: { ok: reasons.length === 0, reasons, action_count: actions.length, origins, artifact_count: artifacts } };
                })()
            : message.type === "status"
              ? {
                  active: true,
                  schemaVersion: "hermes-chrome-provider-v1",
                  protocolVersion: 3,
                  extensionId: "extension-id",
                  extensionVersion,
                  recordingId: "recording-1",
                  captureState: {
                    sessionActive: true,
                    sessionID: "recording-1",
                  },
                }
              : {};
      const { __delayMs = 0, ...nativePayload } = payload || {};
      const response = {
        id: message.id,
        ok: true,
        ...defaultPayload,
        ...nativePayload,
      };
      if (__delayMs > 0) {
        setTimeout(() => nativeMessageListener?.(response), __delayMs);
      } else {
        queueMicrotask(() => nativeMessageListener?.(response));
      }
    },
  };

  const chrome = {
    runtime: {
      id: "extension-id",
      lastError: undefined,
      getManifest() {
        return { version: extensionVersion };
      },
      connectNative() {
        return nativePort;
      },
      onMessage: {
        addListener(listener) {
          messageListeners.push(listener);
        },
      },
      onSuspend: { addListener() {} },
    },
    tabs: {
      async query() {
        return [tab];
      },
      async sendMessage() {
        return { ready: true, version: 4 };
      },
      async captureVisibleTab(windowId, options) {
        assert.equal(windowId, tab.windowId);
        assert.equal(options.format, "png");
        return "data:image/png;base64,iVBORw0KGgo=";
      },
      onActivated: { addListener() {} },
      onUpdated: { addListener(listener) { tabUpdatedListeners.push(listener); } },
      onRemoved: { addListener(listener) { tabRemovedListeners.push(listener); } },
    },
    windows: {
      WINDOW_ID_NONE: -1,
      onFocusChanged: { addListener() {} },
    },
    scripting: {
      async executeScript() {},
    },
    storage: {
      local: {
        async get(keys) {
          if (typeof keys === "string") return { [keys]: localStorage[keys] };
          if (Array.isArray(keys)) {
            return Object.fromEntries(keys.map((key) => [key, localStorage[key]]));
          }
          return { ...localStorage };
        },
        async set(values) {
          Object.assign(localStorage, JSON.parse(JSON.stringify(values)));
        },
        async remove(keys) {
          for (const key of Array.isArray(keys) ? keys : [keys]) delete localStorage[key];
        },
      },
    },
    webNavigation: {
      onHistoryStateUpdated: { addListener() {} },
    },
    downloads: {
      onChanged: { addListener() {} },
      async search() { return []; },
    },
    alarms: {
      onAlarm: { addListener(listener) { alarmListeners.push(listener); } },
      create() {},
    },
  };

  const context = vm.createContext({
    chrome,
    console,
    crypto: webcrypto,
    navigator: {
      userAgent: "Hermes test browser",
      platform: "test",
      language: "en-US",
      languages: ["en-US"],
      onLine: true,
    },
    URL,
    Intl,
    queueMicrotask,
    setTimeout,
    clearTimeout,
    setInterval: () => 1,
    clearInterval() {},
  });
  context.importScripts = (...paths) => {
    for (const relativePath of paths) {
      vm.runInContext(
        readFileSync(path.join(extensionRoot, "src", relativePath), "utf8"),
        context,
        { filename: relativePath },
      );
    }
  };
  const source = readFileSync(sourcePath, "utf8")
    .replace("const AUTO_SCREENSHOT_DELAY_MS = 350;", "const AUTO_SCREENSHOT_DELAY_MS = 1;")
    .replace("const LIVE_JOIN_RETRY_INTERVAL_MS = 150;", "const LIVE_JOIN_RETRY_INTERVAL_MS = 5;");
  vm.runInContext(source, context, { filename: sourcePath });

  const dashboardSender = {
    tab: { ...tab, url: "http://127.0.0.1:54821/workflows", title: "Agent - Dashboard" },
  };
  async function dispatch(message, sender = { tab }) {
    const listener = messageListeners.at(-1);
    assert.ok(listener, "background message listener must be registered");
    const effectiveMessage = message?.type === "hcc-provider-command"
      && ["prepare", "cancel"].includes(message.command)
      && !Object.hasOwn(message, "captureAuthorization")
      ? { ...message, captureAuthorization: CAPTURE_AUTHORIZATION }
      : message;
    return await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("background message did not respond")), 100);
      listener(effectiveMessage, sender, (response) => {
        clearTimeout(timer);
        resolve(response);
      });
    });
  }

  return {
    nativeMessages,
    dashboardSender,
    dispatch,
    async authorize() {
      return await dispatch(
        {
          type: "hcc-provider-command",
          command: "prepare",
          captureAuthorization: CAPTURE_AUTHORIZATION,
        },
        dashboardSender,
      );
    },
    disconnect() {
      nativeDisconnectListener?.();
    },
    alarm() {
      for (const listener of alarmListeners) listener({ name: "hermes-chrome-capture-status" });
    },
    updateTab(url, title = "Updated") {
      tab.url = url;
      tab.title = title;
      for (const listener of tabUpdatedListeners) listener(tab.id, { url }, tab);
    },
    reloadTab() {
      for (const listener of tabUpdatedListeners) listener(tab.id, { status: "loading" }, tab);
    },
    removeTab() {
      for (const listener of tabRemovedListeners) listener(tab.id);
    },
  };
}

test("accepted click stores a causally linked delayed screenshot", async () => {
  const background = loadBackground();
  await background.authorize();
  await sleep(5);

  const response = await background.dispatch({
    type: "hcc-trace-action",
    event: {
      kind: "click",
      payload: {
        browser: { frameId: 0, frameUrl: "https://mail.example.test/inbox" },
        target: { selector: "#archive", label: "Archive" },
        input: { button: "left" },
        meta: { correlation_id: "action-1" },
      },
    },
  });
  await sleep(20);

  assert.equal(response.ok, true);
  const action = background.nativeMessages.find((message) => message.type === "trace.actions");
  const screenshot = background.nativeMessages.find((message) => message.type === "screenshot");
  assert.equal(action.rows[0].payload.meta.correlation_id, "action-1");
  assert.ok(screenshot, `an accepted click must schedule visual evidence after the action: ${JSON.stringify(background.nativeMessages)}`);
  assert.equal(screenshot.afterActionId, "action-1");
  assert.equal(screenshot.label, "after-click");
  assert.match(screenshot.dataUrl, /^data:image\/png;base64,/);

  background.disconnect();
});

test("unpersisted Chrome action is reported as a capture failure", async () => {
  const localStorage = {};
  const background = loadBackground({ traceActionsAccepted: 0, localStorage });
  await background.authorize();
  await sleep(5);

  const response = await background.dispatch({
    type: "hcc-trace-action",
    event: {
      kind: "click",
      payload: {
        browser: { frameId: 0, frameUrl: "https://mail.example.test/inbox" },
        target: { selector: "#archive", label: "Archive" },
        input: { button: "left" },
        meta: { correlation_id: "action-rejected" },
      },
    },
  });
  await sleep(20);

  assert.equal(response.ok, false);
  assert.match(response.error, /not persisted/);
  assert.equal(background.nativeMessages.filter((message) => message.type === "screenshot").length, 0);
  assert.equal(localStorage.hermesChromeActionOutbox.rows.length, 1);

  background.disconnect();
});

test("top-level navigation is stored after the destination segment joins", async () => {
  const background = loadBackground();
  await background.authorize();
  await sleep(5);

  background.updateTab("https://example.com/", "Example Domain");
  await sleep(20);

  const navigation = background.nativeMessages.find((message) =>
    message.type === "trace.actions"
      && message.rows?.some((row) => row.kind === "navigate" && row.payload?.effect?.after_url === "https://example.com/"),
  );
  assert.ok(navigation, `navigation must be sent after the Chrome segment is live: ${JSON.stringify(background.nativeMessages)}`);
  assert.equal(navigation.rows[0].payload.effect.before_url, "https://mail.example.test/inbox");

  background.disconnect();
});

test("prepare keeps retrying across inactive native probes until the recording exists", async () => {
  let recordingActive = false;
  const localStorage = {};
  const background = loadBackground({
    localStorage,
    nativeResponder(message) {
      if (message.type !== "segment.start" && message.type !== "segment.heartbeat") return null;
      return recordingActive
        ? { segmentActive: true, recordingId: "recording-race", segmentId: "segment-race" }
        : { segmentActive: false, recordingId: "", segmentId: "" };
    },
  });
  await sleep(5);

  const prepared = await background.dispatch({ type: "hcc-provider-command", command: "prepare" }, background.dashboardSender);
  assert.equal(prepared.ok, true);
  await sleep(130);
  recordingActive = true;
  await sleep(30);

  const status = await background.dispatch({ action: "status" });
  assert.equal(status.chromeSegmentActive, true);
  assert.equal(localStorage.hermesChromeProviderCheckpoint.recordingId, "recording-race");
  assert.ok(
    background.nativeMessages.filter((message) => message.type === "segment.start").length >= 3,
    `inactive probes must not cancel the armed join: ${JSON.stringify(background.nativeMessages)}`,
  );

  background.disconnect();
});

test("cancel ends a prepared join when recording startup fails", async () => {
  const localStorage = {};
  const background = loadBackground({
    localStorage,
    nativeResponder(message) {
      if (message.type === "segment.start" || message.type === "segment.heartbeat") {
        return { segmentActive: false, recordingId: "", segmentId: "" };
      }
      return null;
    },
  });
  await sleep(5);

  const prepared = await background.dispatch({ type: "hcc-provider-command", command: "prepare" }, background.dashboardSender);
  assert.equal(prepared.ok, true);
  await sleep(20);
  assert.equal(localStorage.hermesChromeProviderCheckpoint?.joinRetryOwnerTabId, undefined);
  const beforeCancel = background.nativeMessages.filter((message) => message.type === "segment.start").length;

  const cancelled = await background.dispatch({ type: "hcc-provider-command", command: "cancel" }, background.dashboardSender);
  assert.equal(cancelled.ok, true);
  await sleep(20);

  assert.equal(
    background.nativeMessages.filter((message) => message.type === "segment.start").length,
    beforeCancel,
  );
  background.disconnect();
});

test("cancel cannot be undone by an in-flight native segment response", async () => {
  const localStorage = {};
  const background = loadBackground({
    localStorage,
    nativeResponder(message) {
      if (message.type === "segment.start") {
        return {
          __delayMs: 30,
          segmentActive: true,
          recordingId: "recording-race",
          segmentId: "segment-race",
        };
      }
      return null;
    },
  });
  await sleep(5);

  const prepared = await background.dispatch(
    { type: "hcc-provider-command", command: "prepare" },
    background.dashboardSender,
  );
  assert.equal(prepared.ok, true);
  await sleep(5);
  assert.ok(background.nativeMessages.some((message) => message.type === "segment.start"));

  const cancelled = await background.dispatch(
    { type: "hcc-provider-command", command: "cancel" },
    background.dashboardSender,
  );
  assert.equal(cancelled.ok, true);
  await sleep(40);

  const status = await background.dispatch({ action: "status" });
  assert.equal(status.chromeSegmentActive, false);
  assert.equal(localStorage.hermesChromeProviderCheckpoint, undefined);
  const ignored = await background.dispatch({
    type: "hcc-trace-action",
    event: {
      kind: "click",
      payload: {
        browser: { url: "https://private.example.test/" },
        target: { selector: "#private" },
      },
    },
  });
  assert.equal(ignored.skipped, true);
  assert.equal(
    background.nativeMessages.some((message) => message.type === "trace.actions"),
    false,
  );

  background.disconnect();
});

for (const [ending, endDashboard] of [
  ["navigation", (background) => background.updateTab("http://127.0.0.1:54821/cron")],
  ["reload", (background) => background.reloadTab()],
  ["tab closure", (background) => background.removeTab()],
]) {
  test(`${ending} ends a prepared join with its Workflows tab`, async () => {
    const background = loadBackground({
      nativeResponder(message) {
        if (message.type === "segment.start" || message.type === "segment.heartbeat") {
          return { segmentActive: false, recordingId: "", segmentId: "" };
        }
        return null;
      },
    });
    await sleep(5);
    await background.dispatch({ type: "hcc-provider-command", command: "prepare" }, background.dashboardSender);
    await sleep(20);
    const beforeEnding = background.nativeMessages.filter((message) => message.type === "segment.start").length;

    endDashboard(background);
    await sleep(20);

    assert.equal(
      background.nativeMessages.filter((message) => message.type === "segment.start").length,
      beforeEnding,
    );
    background.disconnect();
  });
}

test("only one Workflows tab can own and cancel a prepared join", async () => {
  const background = loadBackground({
    nativeResponder(message) {
      if (message.type === "segment.start" || message.type === "segment.heartbeat") {
        return { segmentActive: false, recordingId: "", segmentId: "" };
      }
      return null;
    },
  });
  const otherDashboardSender = {
    tab: { ...background.dashboardSender.tab, id: 8 },
  };
  await sleep(5);

  const prepared = await background.dispatch(
    { type: "hcc-provider-command", command: "prepare" },
    background.dashboardSender,
  );
  assert.equal(prepared.ok, true);
  const competingPrepare = await background.dispatch(
    { type: "hcc-provider-command", command: "prepare" },
    otherDashboardSender,
  );
  assert.equal(competingPrepare.ok, false);
  assert.match(competingPrepare.error, /owned by another/);

  const beforeCompetingCancel = background.nativeMessages
    .filter((message) => message.type === "segment.start").length;
  const competingCancel = await background.dispatch(
    { type: "hcc-provider-command", command: "cancel" },
    otherDashboardSender,
  );
  assert.equal(competingCancel.ok, false);
  await sleep(20);
  assert.ok(
    background.nativeMessages.filter((message) => message.type === "segment.start").length
      > beforeCompetingCancel,
  );

  const cancelled = await background.dispatch(
    { type: "hcc-provider-command", command: "cancel" },
    background.dashboardSender,
  );
  assert.equal(cancelled.ok, true);
  const afterOwnerCancel = background.nativeMessages
    .filter((message) => message.type === "segment.start").length;
  await sleep(20);
  assert.equal(
    background.nativeMessages.filter((message) => message.type === "segment.start").length,
    afterOwnerCancel,
  );
  background.disconnect();
});

test("parallel prepare commands own one retry loop", async () => {
  const background = loadBackground({
    nativeResponder(message) {
      if (message.type === "segment.start" || message.type === "segment.heartbeat") {
        return { segmentActive: false, recordingId: "", segmentId: "" };
      }
      return null;
    },
  });
  await sleep(5);
  await Promise.all([
    background.dispatch({ type: "hcc-provider-command", command: "prepare" }, background.dashboardSender),
    background.dispatch({ type: "hcc-provider-command", command: "prepare" }, background.dashboardSender),
  ]);
  await sleep(20);
  const beforeCancel = background.nativeMessages.filter((message) => message.type === "segment.start").length;

  await background.dispatch({ type: "hcc-provider-command", command: "cancel" }, background.dashboardSender);
  await sleep(20);

  assert.equal(
    background.nativeMessages.filter((message) => message.type === "segment.start").length,
    beforeCancel,
  );
  background.disconnect();
});

test("prepare requires a strong authorization and binds the native join to its dashboard origin", async () => {
  const background = loadBackground();
  await sleep(5);

  const missing = await background.dispatch(
    {
      type: "hcc-provider-command",
      command: "prepare",
      captureAuthorization: "",
    },
    background.dashboardSender,
  );
  assert.equal(missing.ok, false);
  assert.equal(
    background.nativeMessages.some((message) => message.type === "segment.start"),
    false,
  );

  const prepared = await background.authorize();
  assert.equal(prepared.ok, true);
  await sleep(5);

  const joined = background.nativeMessages.find((message) => message.type === "segment.start");
  assert.ok(joined);
  assert.equal(joined.captureAuthorization, CAPTURE_AUTHORIZATION);
  assert.equal(joined.dashboardOrigin, DASHBOARD_ORIGIN);
  background.disconnect();
});

test("another loopback origin cannot cancel or finalize an authorized recording", async () => {
  const background = loadBackground();
  await background.authorize();
  await sleep(5);
  const forgedSender = {
    tab: {
      ...background.dashboardSender.tab,
      id: 8,
      url: "http://127.0.0.1:54822/workflows",
    },
  };

  const cancelled = await background.dispatch(
    {
      type: "hcc-provider-command",
      command: "cancel",
      captureAuthorization: CAPTURE_AUTHORIZATION,
    },
    forgedSender,
  );
  assert.equal(cancelled.ok, false);

  const finalized = await background.dispatch(
    {
      type: "hcc-provider-command",
      command: "finalize",
      recordingId: "recording-1",
    },
    forgedSender,
  );
  assert.equal(finalized.ok, false);
  assert.equal(
    background.nativeMessages.some((message) => message.type === "recording.finalize"),
    false,
  );
  assert.equal((await background.dispatch({ action: "status" })).chromeSegmentActive, true);
  background.disconnect();
});

test("MV3 worker restart restores recording identity and provider sequence", async () => {
  const localStorage = {};
  const firstWorker = loadBackground({ localStorage });
  await firstWorker.authorize();
  await sleep(5);
  const stored = await firstWorker.dispatch({
    type: "hcc-trace-action",
    event: {
      kind: "copy",
      payload: {
        browser: { origin: "https://example.com", url: "https://example.com/" },
        target: { selector: "body", label: "Example Domain" },
        input: { selected_text: "Example Domain" },
        meta: { correlation_id: "copy-example" },
      },
    },
  });
  assert.equal(stored.ok, true);
  await sleep(5);

  const secondWorker = loadBackground({ localStorage });
  await sleep(15);
  const resumedStart = secondWorker.nativeMessages.find((message) => message.type === "segment.start");
  assert.equal(resumedStart.recordingId, "recording-1");
  assert.equal(resumedStart.segmentId, "segment-1");

  const resumed = await secondWorker.dispatch({
    type: "hcc-trace-action",
    event: {
      kind: "copy",
      payload: {
        browser: { origin: "https://example.com", url: "https://example.com/" },
        target: { selector: "body", label: "Example Domain" },
        input: { selected_text: "Example Domain" },
        meta: { correlation_id: "copy-after-restart" },
      },
    },
  });
  assert.equal(resumed.ok, true);
  const latestAction = secondWorker.nativeMessages
    .filter((message) => message.type === "trace.actions")
    .at(-1);
  assert.equal(latestAction.rows.at(-1).payload.meta.provider_sequence, 2);

  firstWorker.disconnect();
  secondWorker.disconnect();
});

test("one durable recording scope spans provider segments and resets for a new recording", async () => {
  let providerPhase = "chrome-first";
  const localStorage = {};
  const background = loadBackground({
    localStorage,
    nativeResponder(message) {
      if (message.type !== "segment.start" && message.type !== "segment.heartbeat") return null;
      if (providerPhase === "native") {
        return {
          segmentActive: false,
          recordingId: "recording-handoff",
          segmentId: "segment-1",
          captureState: { sessionID: "recording-handoff", desiredProvider: "native" },
        };
      }
      if (providerPhase === "different-recording") {
        return {
          segmentActive: true,
          recordingId: "recording-after-handoff",
          segmentId: "segment-new-recording",
        };
      }
      return {
        segmentActive: true,
        recordingId: "recording-handoff",
        segmentId: providerPhase === "chrome-second" ? "segment-2" : "segment-1",
      };
    },
  });
  await background.authorize();
  await sleep(5);
  const stored = await background.dispatch({
    type: "hcc-trace-action",
    event: {
      kind: "click",
      payload: {
        browser: { origin: "https://example.com", url: "https://example.com/" },
        target: { selector: "body", label: "Example Domain" },
        input: { button: "left" },
        meta: { correlation_id: "copy-before-native-handoff" },
      },
    },
  });
  assert.equal(stored.ok, true);
  await sleep(20);

  providerPhase = "native";
  background.updateTab("https://example.com/after-copy", "Example Domain");
  await sleep(15);
  let status = await background.dispatch({ action: "status" });
  assert.equal(status.chromeSegmentActive, false);
  assert.equal(localStorage.hermesChromeProviderCheckpoint.recordingId, "recording-handoff");
  assert.equal(localStorage.hermesChromeProviderCheckpoint.providerSequence, 1);

  providerPhase = "chrome-second";
  background.alarm();
  await sleep(15);
  status = await background.dispatch({ action: "status" });
  assert.equal(status.chromeSegmentActive, true);
  assert.equal(localStorage.hermesChromeProviderCheckpoint.segmentId, "segment-2");
  assert.equal(localStorage.hermesChromeProviderCheckpoint.providerSequence, 1);

  providerPhase = "native";
  background.updateTab("http://127.0.0.1:9119/workflows", "Agent - Dashboard");
  await sleep(15);
  status = await background.dispatch({ action: "status" });
  assert.equal(status.chromeSegmentActive, false);
  assert.equal(localStorage.hermesChromeProviderCheckpoint.recordingId, "recording-handoff");

  const finalized = await background.dispatch({
    type: "hcc-provider-command",
    command: "finalize",
    recordingId: "recording-handoff",
  }, background.dashboardSender);
  assert.equal(finalized.ok, true, JSON.stringify(finalized));
  await sleep(5);

  background.updateTab("https://example.com/recording-ended", "Example Domain");
  providerPhase = "different-recording";
  background.alarm();
  await sleep(15);
  assert.equal(localStorage.hermesChromeProviderCheckpoint, undefined);
  await background.authorize();
  await sleep(15);
  assert.equal(localStorage.hermesChromeProviderCheckpoint.recordingId, "recording-after-handoff");
  assert.equal(localStorage.hermesChromeProviderCheckpoint.providerSequence, 0);
  assert.equal(localStorage.hermesChromeActionOutbox, undefined);

  background.disconnect();
});

test("finalize requires recording identity and drains the final dashboard action", async () => {
  const background = loadBackground();
  await background.authorize();
  await sleep(5);
  const missingIdentity = await background.dispatch({
    type: "hcc-provider-command",
    command: "finalize",
  }, background.dashboardSender);
  assert.equal(missingIdentity.ok, false);
  assert.deepEqual(Array.from(missingIdentity.continuity.reasons), ["recording_identity_required"]);
  assert.equal(
    background.nativeMessages.some((message) => message.type === "recording.finalize"),
    false,
  );
  await background.authorize();
  await sleep(5);
  const dashboardAction = await background.dispatch({
    type: "hcc-trace-action",
    event: {
      kind: "click",
      payload: {
        browser: {
          origin: "http://127.0.0.1:54821",
          url: "http://127.0.0.1:54821/workflows/review?profile=default#capture",
        },
        target: { selector: "button.stop", label: "Stop" },
        meta: { correlation_id: "dashboard-control" },
      },
    },
  }, background.dashboardSender);
  assert.equal(dashboardAction.ok, true);

  const finalized = await background.dispatch({
    type: "hcc-provider-command",
    command: "finalize",
    recordingId: "recording-1",
  }, background.dashboardSender);
  assert.equal(finalized.ok, true, JSON.stringify(finalized));

  background.disconnect();
});

test("finalize drains a pending evidence screenshot before continuity", async () => {
  const background = loadBackground();
  await background.authorize();
  await sleep(5);
  const stored = await background.dispatch({
    type: "hcc-trace-action",
    event: {
      kind: "click",
      payload: {
        browser: {
          origin: "https://example.com",
          url: "https://example.com/",
        },
        target: { selector: "button.export", label: "Export" },
        meta: { correlation_id: "final-action" },
      },
    },
  });
  assert.equal(stored.ok, true);

  const finalized = await background.dispatch({
    type: "hcc-provider-command",
    command: "finalize",
    recordingId: "recording-1",
  }, background.dashboardSender);

  assert.equal(finalized.ok, true, JSON.stringify(finalized));
  const screenshotIndex = background.nativeMessages.findIndex(
    (message) => message.type === "screenshot",
  );
  const finalizeIndex = background.nativeMessages.findIndex(
    (message) => message.type === "recording.finalize",
  );
  assert.ok(screenshotIndex >= 0);
  assert.ok(finalizeIndex > screenshotIndex);
  background.disconnect();
});

test("localhost business apps remain business-origin evidence", async () => {
  const background = loadBackground();
  await background.authorize();
  await sleep(5);
  const stored = await background.dispatch({
    type: "hcc-trace-action",
    event: {
      kind: "click",
      payload: {
        browser: {
          origin: "http://localhost:3000",
          url: "http://localhost:3000/operations/workflows",
        },
        target: { selector: "button.export", label: "Export quarter" },
        meta: { correlation_id: "local-business-action" },
      },
    },
  });
  assert.equal(stored.ok, true);
  await sleep(5);
  const finalized = await background.dispatch({
    type: "hcc-provider-command",
    command: "finalize",
    recordingId: "recording-1",
  }, background.dashboardSender);
  assert.equal(finalized.ok, true, JSON.stringify(finalized));
  assert.deepEqual(Array.from(finalized.continuity.origins), ["http://localhost:3000"]);

  background.disconnect();
});

test("a stale MV3 authorization fails closed until the user prepares capture again", async () => {
  const localStorage = {};
  const hostLedger = [];
  const firstWorker = loadBackground({ localStorage, hostLedger });
  await firstWorker.authorize();
  await sleep(5);
  await firstWorker.dispatch({
    type: "hcc-trace-action",
    event: {
      kind: "navigate",
      payload: {
        browser: { origin: "https://example.com", url: "https://example.com/" },
        effect: { after_url: "https://example.com/" },
        meta: { correlation_id: "navigate-before-suspend" },
      },
    },
  });
  await sleep(5);
  const checkpoint = localStorage.hermesChromeProviderCheckpoint;
  checkpoint.lastNativeHeartbeatAt = Date.now() - 20_000;
  checkpoint.recordingId = "stale-recording";
  checkpoint.segmentId = "stale-segment";

  const resumedWorker = loadBackground({
    localStorage,
    hostLedger,
    nativeResponder(message) {
      if (message.type === "segment.heartbeat" && message.recordingId === "stale-recording") {
        return { ok: false, error: "Chrome message belongs to a different recording" };
      }
      return null;
    },
  });
  await sleep(15);
  let status = await resumedWorker.dispatch({ action: "status" });
  assert.equal(status.chromeSegmentActive, false);
  assert.equal(localStorage.hermesChromeProviderCheckpoint, undefined);
  assert.equal(
    resumedWorker.nativeMessages.some((message) =>
      ["segment.start", "segment.heartbeat", "trace.actions"].includes(message.type)
    ),
    false,
  );
  const evidence = resumedWorker.nativeMessages.find((message) =>
    message.type === "trace.evidence"
      && message.rows?.some((row) => row.kind === "chrome.provider.continuity"),
  );
  assert.equal(evidence, undefined);

  await resumedWorker.authorize();
  await sleep(15);
  status = await resumedWorker.dispatch({ action: "status" });
  assert.equal(status.chromeSegmentActive, true);
  const finalized = await resumedWorker.dispatch({
    type: "hcc-provider-command",
    command: "finalize",
    recordingId: "recording-1",
  }, resumedWorker.dashboardSender);
  assert.equal(finalized.ok, true, JSON.stringify(finalized));
  assert.equal(
    resumedWorker.nativeMessages.find((message) => message.type === "recording.finalize")?.recordingId,
    "recording-1",
  );

  firstWorker.disconnect();
  resumedWorker.disconnect();
});

test("finalize succeeds after accepted business-origin evidence", async () => {
  const background = loadBackground();
  await background.authorize();
  await sleep(5);
  const stored = await background.dispatch({
    type: "hcc-trace-action",
    event: {
      kind: "navigate",
      payload: {
        browser: { origin: "https://example.com", url: "https://example.com/" },
        effect: { after_url: "https://example.com/" },
        meta: { correlation_id: "navigate-example" },
      },
    },
  });
  assert.equal(stored.ok, true);
  await sleep(20);

  const finalized = await background.dispatch({
    type: "hcc-provider-command",
    command: "finalize",
    recordingId: "recording-1",
  }, background.dashboardSender);
  assert.equal(finalized.ok, true, JSON.stringify(finalized));
  assert.deepEqual(Array.from(finalized.continuity.origins), ["https://example.com"]);
  assert.ok(background.nativeMessages.some((message) => message.type === "recording.finalize"));

  background.disconnect();
});

test("finalize surfaces a host-owned continuity failure", async () => {
  const background = loadBackground({
    nativeResponder(message) {
      if (message.type === "recording.finalize") {
        return { continuity: { ok: false, reasons: ["host_finalization_failed"] } };
      }
      return null;
    },
  });
  await background.authorize();
  await sleep(5);
  await background.dispatch({
    type: "hcc-trace-action",
    event: {
      kind: "navigate",
      payload: {
        browser: { origin: "https://example.com", url: "https://example.com/" },
        effect: { after_url: "https://example.com/" },
        meta: { correlation_id: "navigate-before-finalize" },
      },
    },
  });

  const finalized = await background.dispatch({
    type: "hcc-provider-command",
    command: "finalize",
    recordingId: "recording-1",
  }, background.dashboardSender);
  assert.equal(finalized.ok, false);
  assert.ok(finalized.continuity.reasons.includes("host_finalization_failed"));

  background.disconnect();
});
