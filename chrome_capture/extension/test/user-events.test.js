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

test("active capture records customer inputs and file metadata while redacting passwords", async () => {
  const harness = loadUserEvents();
  const { window } = harness;
  await sleep(0);
  harness.updateAuthority(true, 1);

  const textInput = window.document.createElement("input");
  textInput.type = "text";
  textInput.value = "customer reference 42";
  const passwordInput = window.document.createElement("input");
  passwordInput.type = "password";
  passwordInput.value = "never-record-this-secret";
  const fileInput = window.document.createElement("input");
  fileInput.type = "file";
  Object.defineProperty(fileInput, "files", {
    configurable: true,
    value: [{
      name: "customer-report.pdf",
      size: 2048,
      type: "application/pdf",
      lastModified: 1234,
    }],
  });
  window.document.body.append(textInput, passwordInput, fileInput);

  textInput.dispatchEvent(new window.Event("input", { bubbles: true }));
  passwordInput.dispatchEvent(new window.Event("input", { bubbles: true }));
  fileInput.dispatchEvent(new window.Event("input", { bubbles: true }));
  const passwordPaste = new window.Event("paste", { bubbles: true });
  Object.defineProperty(passwordPaste, "clipboardData", {
    value: { getData: () => "never-record-this-pasted-secret" },
  });
  passwordInput.dispatchEvent(passwordPaste);
  await sleep(400);

  const actions = harness.sentMessages.filter(
    (message) => message.type === "hcc-trace-action",
  );
  const typed = actions.find(
    (message) => message.event.kind === "type"
      && message.event.payload.input.value === "customer reference 42",
  );
  const redacted = actions.filter(
    (message) => ["type", "paste"].includes(message.event.kind)
      && message.event.payload.input.secure_input === true,
  );
  const upload = actions.find((message) => message.event.kind === "upload");

  assert.ok(typed);
  assert.equal(redacted.length, 2);
  assert.ok(redacted.every(
    (message) => message.event.payload.input.redacted === true,
  ));
  assert.deepEqual(
    JSON.parse(JSON.stringify(upload.event.payload.input.files)),
    [{
      name: "customer-report.pdf",
      size: 2048,
      mime: "application/pdf",
      last_modified: 1234,
    }],
  );
  assert.doesNotMatch(JSON.stringify(actions), /never-record-this/);

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
