import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { JSDOM } from "jsdom";

const extensionRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const causalitySource = readFileSync(path.join(extensionRoot, "src", "causality.js"), "utf8");
const userEventsSource = readFileSync(path.join(extensionRoot, "src", "user-events.js"), "utf8");
const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

function loadUserEvents() {
  const runtimeListeners = [];
  const sentMessages = [];
  const ports = [];
  let queryAuthority = {
    ok: true,
    type: "hcc-capture-authority-state",
    revision: 0,
    active: false,
    recordingId: "",
  };

  function extensionEvent() {
    const listeners = [];
    return {
      listeners,
      addListener(listener) {
        listeners.push(listener);
      },
    };
  }

  function createPort() {
    const onMessage = extensionEvent();
    const onDisconnect = extensionEvent();
    let disconnected = false;
    return {
      name: "hcc-capture-authority",
      onMessage,
      onDisconnect,
      disconnect() {
        if (disconnected) return;
        disconnected = true;
        for (const listener of onDisconnect.listeners) listener();
      },
      push(message) {
        for (const listener of onMessage.listeners) listener(message);
      },
    };
  }

  function createRuntime() {
    let invalidated = false;
    return {
      invalidate() {
        invalidated = true;
      },
      get lastError() {
        return undefined;
      },
      onMessage: {
        addListener(listener) {
          runtimeListeners.push(listener);
        },
      },
      connect() {
        if (invalidated) throw new Error("Extension context invalidated.");
        const port = createPort();
        ports.push(port);
        return port;
      },
      sendMessage(message, callback) {
        if (invalidated) throw new Error("Extension context invalidated.");
        sentMessages.push(message);
        queueMicrotask(() => {
          callback?.(
            message?.type === "hcc-capture-authority-query"
              ? queryAuthority
              : { ok: true },
          );
        });
      },
    };
  }

  const dom = new JSDOM(
    "<!doctype html><html><head><title>Private page</title></head><body></body></html>",
    {
      runScripts: "outside-only",
      url: "https://private.example.test/account",
    },
  );
  const { window } = dom;
  window.CSS ||= {};
  window.CSS.escape ||= (value) => String(value).replaceAll('"', '\\"');
  window.chrome = {
    runtime: createRuntime(),
  };
  window.eval(causalitySource);
  window.eval(userEventsSource);

  return {
    window,
    sentMessages,
    invalidateRuntime() {
      window.chrome.runtime.invalidate();
    },
    reloadExtensionContext() {
      window.chrome.runtime.invalidate();
      window.chrome.runtime = createRuntime();
      window.eval(userEventsSource);
    },
    reinjectCurrentContext() {
      window.eval(userEventsSource);
    },
    setQueryAuthority(value) {
      queryAuthority = value;
    },
    updateAuthority(active, revision) {
      const message = {
        action: "hcc-capture-authority-update",
        type: "hcc-capture-authority-state",
        revision,
        active,
        recordingId: active ? "recording-1" : "",
      };
      for (const listener of runtimeListeners) listener(message, {}, () => {});
    },
    disconnectLatestPort() {
      ports.at(-1)?.disconnect();
    },
    ping() {
      let response;
      for (const listener of runtimeListeners) {
        listener({ action: "hcc-ping" }, {}, (value) => {
          response = value;
        });
      }
      return response;
    },
    close() {
      dom.window.close();
    },
  };
}

test("dashboard bridge forwards capture authorization only through the explicit provider command", async () => {
  const harness = loadUserEvents();
  const { window } = harness;
  const captureAuthorization = "cd".repeat(32);
  await sleep(0);

  window.dispatchEvent(new window.MessageEvent("message", {
    source: window,
    origin: window.location.origin,
    data: {
      source: "hermes-crunch-dashboard",
      type: "hcc-provider-request",
      requestId: "prepare-1",
      command: "prepare",
      captureAuthorization,
    },
  }));
  await sleep(0);

  const command = harness.sentMessages.find((message) =>
    message.type === "hcc-provider-command" && message.requestId === "prepare-1"
  );
  assert.ok(command);
  assert.equal(command.captureAuthorization, captureAuthorization);
  assert.equal(Object.hasOwn(command, "url"), false);
  assert.equal(Object.hasOwn(command, "title"), false);
  harness.close();
});

test("dashboard bridge fails cleanly when its extension context is invalidated", async () => {
  const harness = loadUserEvents();
  const { window } = harness;
  await sleep(0);
  const responses = [];
  window.addEventListener("message", (event) => {
    if (event.data?.source === "hermes-chrome-provider") responses.push(event.data);
  });
  harness.invalidateRuntime();

  window.dispatchEvent(new window.MessageEvent("message", {
    source: window,
    origin: window.location.origin,
    data: {
      source: "hermes-crunch-dashboard",
      type: "hcc-provider-request",
      requestId: "invalidated-1",
      command: "prepare",
      captureAuthorization: "cd".repeat(32),
    },
  }));
  await sleep(0);

  assert.equal(responses.length, 1);
  assert.equal(responses[0].ok, false);
  assert.match(responses[0].error, /context invalidated/i);
  harness.close();
});

test("reinjection replaces a stale extension context without reloading the page", async () => {
  const harness = loadUserEvents();
  const { window } = harness;
  await sleep(0);
  harness.reinjectCurrentContext();
  harness.reloadExtensionContext();
  await sleep(0);

  window.dispatchEvent(new window.MessageEvent("message", {
    source: window,
    origin: window.location.origin,
    data: {
      source: "hermes-crunch-dashboard",
      type: "hcc-provider-request",
      requestId: "reloaded-1",
      command: "prepare",
      captureAuthorization: "cd".repeat(32),
    },
  }));
  await sleep(0);

  const commands = harness.sentMessages.filter((message) =>
    message.type === "hcc-provider-command" && message.requestId === "reloaded-1"
  );
  assert.equal(commands.length, 1);
  harness.close();
});

test("inactive content script does not read or emit page data", async () => {
  const harness = loadUserEvents();
  const { window } = harness;
  await sleep(0);

  let valueReads = 0;
  let fileReads = 0;
  let selectionReads = 0;
  let clipboardReads = 0;
  let transferReads = 0;
  let targetMetadataReads = 0;
  let titleReads = 0;

  const input = window.document.createElement("input");
  input.type = "text";
  Object.defineProperty(input, "value", {
    configurable: true,
    get() {
      valueReads += 1;
      return "private typed value";
    },
  });
  const fileInput = window.document.createElement("input");
  fileInput.type = "file";
  Object.defineProperty(fileInput, "files", {
    configurable: true,
    get() {
      fileReads += 1;
      return [{ name: "private.pdf", size: 42, type: "application/pdf", lastModified: 1 }];
    },
  });
  const button = window.document.createElement("button");
  const originalGetAttribute = button.getAttribute.bind(button);
  button.getAttribute = (...args) => {
    targetMetadataReads += 1;
    return originalGetAttribute(...args);
  };
  Object.defineProperty(window.document, "title", {
    configurable: true,
    get() {
      titleReads += 1;
      return "Private page";
    },
  });
  window.getSelection = () => {
    selectionReads += 1;
    return { toString: () => "private selection" };
  };
  window.document.body.append(input, fileInput, button);

  input.dispatchEvent(new window.Event("input", { bubbles: true }));
  fileInput.dispatchEvent(new window.Event("input", { bubbles: true }));
  button.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  window.document.dispatchEvent(new window.Event("selectionchange", { bubbles: true }));
  const copyEvent = new window.Event("copy", { bubbles: true });
  input.dispatchEvent(copyEvent);
  const pasteEvent = new window.Event("paste", { bubbles: true });
  Object.defineProperty(pasteEvent, "clipboardData", {
    configurable: true,
    get() {
      clipboardReads += 1;
      return { getData: () => "private clipboard" };
    },
  });
  input.dispatchEvent(pasteEvent);
  const dropEvent = new window.Event("drop", { bubbles: true });
  Object.defineProperty(dropEvent, "dataTransfer", {
    configurable: true,
    get() {
      transferReads += 1;
      return { files: [{ name: "private-drop.pdf" }] };
    },
  });
  input.dispatchEvent(dropEvent);
  await sleep(400);

  assert.equal(valueReads, 0);
  assert.equal(fileReads, 0);
  assert.equal(selectionReads, 0);
  assert.equal(clipboardReads, 0);
  assert.equal(transferReads, 0);
  assert.equal(targetMetadataReads, 0);
  assert.equal(titleReads, 0);
  assert.equal(
    harness.sentMessages.filter((message) => message.type?.startsWith("hcc-trace-")).length,
    0,
  );
  const ping = harness.ping();
  assert.equal(ping.ready, true);
  assert.equal(ping.version, 4);
  assert.equal(Object.hasOwn(ping, "url"), false);

  harness.close();
});

test("revocation clears queued reads and a disconnected authority fails closed", async () => {
  const harness = loadUserEvents();
  const { window } = harness;
  await sleep(0);

  const input = window.document.createElement("input");
  input.type = "text";
  let currentValue = "captured while active";
  let valueReads = 0;
  Object.defineProperty(input, "value", {
    configurable: true,
    get() {
      valueReads += 1;
      return currentValue;
    },
  });
  window.document.body.append(input);

  harness.updateAuthority(true, 1);
  input.dispatchEvent(new window.Event("input", { bubbles: true }));
  await sleep(400);
  assert.equal(valueReads, 1);
  assert.equal(
    harness.sentMessages.filter((message) => message.type === "hcc-trace-action").length,
    1,
  );

  currentValue = "must not be read after revoke";
  input.dispatchEvent(new window.Event("input", { bubbles: true }));
  harness.updateAuthority(false, 2);
  await sleep(400);
  assert.equal(valueReads, 1);
  assert.equal(
    harness.sentMessages.filter((message) => message.type === "hcc-trace-action").length,
    1,
  );

  harness.updateAuthority(true, 3);
  harness.setQueryAuthority({
    ok: true,
    type: "hcc-capture-authority-state",
    revision: 4,
    active: false,
    recordingId: "",
  });
  harness.disconnectLatestPort();
  await sleep(0);
  let clipboardReads = 0;
  const pasteEvent = new window.Event("paste", { bubbles: true });
  Object.defineProperty(pasteEvent, "clipboardData", {
    configurable: true,
    get() {
      clipboardReads += 1;
      return { getData: () => "must stay private" };
    },
  });
  input.dispatchEvent(pasteEvent);
  assert.equal(clipboardReads, 0);
  assert.equal(
    harness.sentMessages.filter((message) => message.type === "hcc-trace-action").length,
    1,
  );

  harness.close();
});
