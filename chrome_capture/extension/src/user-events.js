(() => {
  const extensionRuntime = chrome.runtime;
  const previousController = window.__hccUserEventsController;
  if (previousController?.runtime === extensionRuntime) return;
  try { previousController?.dispose(); } catch {}
  const lifecycleController = new AbortController();
  const captureListenerOptions = { capture: true, signal: lifecycleController.signal };
  const CONTENT_SCRIPT_VERSION = 4;
  const DASHBOARD_SOURCE = "hermes-crunch-dashboard";
  const PROVIDER_SOURCE = "hermes-chrome-provider";
  const PROVIDER_REQUEST_TYPE = "hcc-provider-request";
  const PROVIDER_RESPONSE_TYPE = "hcc-provider-response";
  const INTERACTIVE_SELECTOR = [
    "a[href]", "button", "input", "textarea", "select", "option", "summary", "label",
    "[role]", "[contenteditable]", "[draggable='true']", "[data-testid]", "[aria-label]",
  ].join(",");
  const SCROLL_DEBOUNCE_MS = 200;
  const SELECTION_DEBOUNCE_MS = 250;
  const TYPE_DEBOUNCE_MS = 350;
  const MODIFIER_KEYS = new Set(["Meta", "Control", "Alt", "Shift", "OS", "Fn", "CapsLock"]);
  let scrollTimer = null;
  let selectionTimer = null;
  const pendingTypes = new Map();
  let pastedTargets = new WeakSet();
  let captureAuthorityActive = false;
  let captureAuthorityRevision = -1;
  let captureAuthorityPort = null;

  function clearPendingCapture() {
    clearTimeout(scrollTimer);
    scrollTimer = null;
    clearTimeout(selectionTimer);
    selectionTimer = null;
    for (const pending of pendingTypes.values()) clearTimeout(pending.timer);
    pendingTypes.clear();
    pastedTargets = new WeakSet();
  }

  function disconnectAuthorityPort() {
    const port = captureAuthorityPort;
    captureAuthorityPort = null;
    try { port?.disconnect(); } catch {}
  }

  function setCaptureAuthority(message) {
    const revision = Number(message?.revision);
    if (!Number.isInteger(revision) || revision < captureAuthorityRevision) return;
    captureAuthorityRevision = revision;
    captureAuthorityActive = message?.active === true;
    if (!captureAuthorityActive) {
      clearPendingCapture();
      disconnectAuthorityPort();
      return;
    }
    connectAuthorityPort();
  }

  function connectAuthorityPort() {
    if (!captureAuthorityActive || captureAuthorityPort) return;
    try {
      const port = extensionRuntime.connect({ name: "hcc-capture-authority" });
      captureAuthorityPort = port;
      port.onMessage.addListener((message) => {
        if (message?.type === "hcc-capture-authority-state") setCaptureAuthority(message);
      });
      port.onDisconnect.addListener(() => {
        if (captureAuthorityPort !== port) return;
        captureAuthorityPort = null;
        captureAuthorityActive = false;
        captureAuthorityRevision = -1;
        clearPendingCapture();
        refreshCaptureAuthority();
      });
    } catch {
      captureAuthorityPort = null;
      captureAuthorityActive = false;
      clearPendingCapture();
    }
  }

  function refreshCaptureAuthority() {
    captureAuthorityActive = false;
    clearPendingCapture();
    try {
      extensionRuntime.sendMessage({ type: "hcc-capture-authority-query" }, (response) => {
        const runtimeError = extensionRuntime.lastError;
        if (runtimeError || response?.ok !== true) {
          captureAuthorityActive = false;
          clearPendingCapture();
          return;
        }
        setCaptureAuthority(response);
      });
    } catch {
      captureAuthorityActive = false;
      clearPendingCapture();
    }
  }

  extensionRuntime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.action === "hcc-capture-authority-update") {
      setCaptureAuthority(message);
      return false;
    }
    if (message?.action === "hcc-ping") {
      sendResponse({ ready: true, version: CONTENT_SCRIPT_VERSION });
      return false;
    }
    return undefined;
  });

  window.addEventListener("message", (event) => {
    const request = event.data;
    if (event.source !== window) return;
    if (!request || request.source !== DASHBOARD_SOURCE || request.type !== PROVIDER_REQUEST_TYPE) return;
    if (!request.requestId || !["probe", "prepare", "cancel", "finalize"].includes(request.command)) return;
    if (event.origin !== window.location.origin) return;

    const postResponse = (response, runtimeError = null) => {
      window.postMessage({
        source: PROVIDER_SOURCE,
        type: PROVIDER_RESPONSE_TYPE,
        requestId: request.requestId,
        ok: !runtimeError && response?.ok === true,
        contentScriptVersion: response?.contentScriptVersion,
        error: runtimeError?.message || response?.error || "",
      }, window.location.origin);
    };
    try {
      extensionRuntime.sendMessage({
        type: "hcc-provider-command",
        command: request.command,
        requestId: request.requestId,
        recordingId: request.recordingId || "",
        captureAuthorization: request.captureAuthorization || "",
      }, (response) => {
        postResponse(response, extensionRuntime.lastError);
      });
    } catch (error) {
      postResponse(null, error);
    }
  }, { signal: lifecycleController.signal });

  refreshCaptureAuthority();

  function ts() { return new Date().toISOString(); }
  function correlationId() {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  }
  function cap(text, limit = 160) { return String(text || "").replace(/\s+/g, " ").trim().slice(0, limit); }
  function modifiers(event) {
    return [event.metaKey && "meta", event.ctrlKey && "control", event.altKey && "alt", event.shiftKey && "shift"].filter(Boolean);
  }
  function originFor(url) { try { return new URL(url || "").origin; } catch { return ""; } }
  function framePayload() {
    let topUrl = "";
    try { topUrl = window.top === window ? location.href : window.top.location.href; } catch { topUrl = ""; }
    return {
      url: topUrl || location.href,
      origin: originFor(topUrl || location.href),
      title: document.title || "",
      frame_url: location.href,
      top_url: topUrl || location.href,
      viewport: { width: window.innerWidth, height: window.innerHeight, dpr: window.devicePixelRatio || 1 },
    };
  }
  function nearestTarget(node) {
    if (!(node instanceof Element)) return null;
    return node.closest(INTERACTIVE_SELECTOR) || node;
  }
  function selectorLiteral(value) { return CSS.escape(String(value || "")); }
  function stableSelectors(el) {
    if (!(el instanceof Element)) return [];
    const selectors = [];
    if (el.id) selectors.push(`#${selectorLiteral(el.id)}`);
    const testId = el.getAttribute("data-testid");
    if (testId) selectors.push(`[data-testid="${selectorLiteral(testId)}"]`);
    const name = el.getAttribute("name");
    if (name) selectors.push(`${el.tagName.toLowerCase()}[name="${selectorLiteral(name)}"]`);
    const aria = el.getAttribute("aria-label");
    if (aria) selectors.push(`${el.tagName.toLowerCase()}[aria-label="${selectorLiteral(aria)}"]`);
    const role = el.getAttribute("role");
    const label = readableText(el);
    if (role && label) selectors.push(`[role="${selectorLiteral(role)}"][aria-label="${selectorLiteral(label)}"]`);
    const parts = [];
    let cur = el;
    while (cur && cur instanceof Element && parts.length < 5) {
      let part = cur.tagName.toLowerCase();
      const parent = cur.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter((child) => child.tagName === cur.tagName);
        if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(cur) + 1})`;
      }
      parts.unshift(part);
      cur = parent;
    }
    if (parts.length) selectors.push(parts.join(" > "));
    return [...new Set(selectors.filter(Boolean))];
  }
  function shadowPath(event) {
    return (event.composedPath?.() || []).filter((node) => node instanceof Element).slice(0, 12).map((node) => {
      const el = node;
      return { tag: el.tagName.toLowerCase(), id: el.id || "", role: el.getAttribute("role") || "", label: cap(el.getAttribute("aria-label") || el.getAttribute("title") || "", 80) };
    });
  }
  function readableText(el) {
    if (!(el instanceof Element)) return "";
    return cap(el.getAttribute("aria-label") || el.getAttribute("title") || el.getAttribute("placeholder") || el.getAttribute("alt") || el.innerText || el.textContent || "");
  }
  function targetPayload(node, event = null) {
    const el = nearestTarget(node);
    if (!el) return {};
    const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
    const selectors = stableSelectors(el);
    return {
      selector: selectors[0] || "",
      selectors,
      role: el.getAttribute("role") || implicitRole(el),
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute("type") || "",
      label: readableText(el),
      name: el.getAttribute("name") || "",
      id: el.id || "",
      test_id: el.getAttribute("data-testid") || "",
      href: el instanceof HTMLAnchorElement ? el.href : "",
      aria_controls: el.getAttribute("aria-controls") || "",
      aria_expanded: el.getAttribute("aria-expanded") || "",
      content_editable: el instanceof HTMLElement ? !!el.isContentEditable : false,
      rect: rect ? { x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height) } : {},
      shadow_path: event ? shadowPath(event) : [],
    };
  }
  function implicitRole(el) {
    if (el instanceof HTMLButtonElement) return "button";
    if (el instanceof HTMLAnchorElement) return "link";
    if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement) return "form-control";
    return "";
  }
  function fieldValue(el) {
    if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement) return el.value;
    if (el instanceof HTMLElement && el.isContentEditable) return el.innerText || el.textContent || "";
    return "";
  }
  function filePayload(el) {
    if (!(el instanceof HTMLInputElement) || el.type !== "file") return [];
    return Array.from(el.files || []).map((file) => ({ name: file.name, size: file.size, mime: file.type, last_modified: file.lastModified }));
  }
  function secureField(el) {
    return el instanceof HTMLInputElement && el.type === "password";
  }
  function selectedText() { try { return String(window.getSelection()?.toString() || ""); } catch { return ""; } }
  function selectionTarget() {
    const selection = window.getSelection?.();
    const anchor = selection?.anchorNode;
    const node = anchor instanceof Element ? anchor : anchor?.parentElement;
    return targetPayload(node || document.activeElement);
  }
  function emit(type, event) {
    if (!captureAuthorityActive) return;
    try {
      extensionRuntime.sendMessage({ type, event }, (response) => {
        const runtimeError = extensionRuntime.lastError;
        if (runtimeError || response?.ok !== true) {
          console.warn("Hermes Chrome Provider did not persist capture event", type, runtimeError?.message || response?.error || "unknown error");
        }
      });
    } catch {
      // Extension context vanished.
    }
  }
  function action(kind, event, extra = {}) {
    if (!captureAuthorityActive) return;
    if (event.target instanceof Element && event.target.closest("[data-hermes-recorder-controls]")) return;
    const target = { ...targetPayload(event.target, event), ...(extra.target || {}) };
    const input = { modifiers: modifiers(event), ...extra.input };
    const meta = { ...(extra.meta || {}), correlation_id: correlationId() };
    emit("hcc-trace-action", {
      ts: ts(),
      kind,
      payload: { browser: framePayload(), target, input, meta, ...(extra.effect ? { effect: extra.effect } : {}) },
      quality: extra.quality || { confidence: target.selector || target.id || target.label ? "high" : "low", warnings: target.selector || target.id || target.label ? [] : ["missing_target"] },
    });
  }

  function flushType(target) {
    const pending = pendingTypes.get(target);
    if (!pending) return;
    clearTimeout(pending.timer);
    pendingTypes.delete(target);
    if (!captureAuthorityActive) return;
    if (secureField(target)) action("type", pending.event, { input: { redacted: true, secure_input: true } });
    else action("type", pending.event, { input: { value: fieldValue(target), coalesced: true } });
  }
  function flushAllTypes() {
    for (const target of Array.from(pendingTypes.keys())) flushType(target);
  }
  function queueType(event) {
    if (!captureAuthorityActive) return;
    const target = event.target;
    const previous = pendingTypes.get(target);
    if (previous) clearTimeout(previous.timer);
    pendingTypes.set(target, {
      event,
      timer: setTimeout(() => flushType(target), TYPE_DEBOUNCE_MS),
    });
  }
  function isTextField(target) {
    return target instanceof HTMLTextAreaElement
      || (target instanceof HTMLInputElement && !["checkbox", "radio", "range", "color", "file", "button", "submit", "reset"].includes(target.type))
      || (target instanceof HTMLElement && target.isContentEditable);
  }
  document.addEventListener("click", (event) => {
    if (!captureAuthorityActive) return;
    flushAllTypes();
    action("click", event, { input: { button: "left", x: event.clientX, y: event.clientY, page_x: event.pageX, page_y: event.pageY } });
  }, captureListenerOptions);
  document.addEventListener("dblclick", (event) => {
    if (!captureAuthorityActive) return;
    flushAllTypes();
    action("double_click", event, { input: { button: "left", x: event.clientX, y: event.clientY, page_x: event.pageX, page_y: event.pageY } });
  }, captureListenerOptions);
  document.addEventListener("contextmenu", (event) => {
    if (!captureAuthorityActive) return;
    flushAllTypes();
    action("context_menu", event, { input: { button: "right", x: event.clientX, y: event.clientY, page_x: event.pageX, page_y: event.pageY } });
  }, captureListenerOptions);
  document.addEventListener("input", (event) => {
    if (!captureAuthorityActive) return;
    const files = filePayload(event.target);
    if (files.length) {
      action("upload", event, { input: { files } });
      return;
    }
    if (!isTextField(event.target)) return;
    if (pastedTargets.has(event.target)) {
      pastedTargets.delete(event.target);
      return;
    }
    queueType(event);
  }, captureListenerOptions);
  document.addEventListener("change", (event) => {
    if (!captureAuthorityActive) return;
    if (isTextField(event.target)) {
      flushType(event.target);
      return;
    }
    if (secureField(event.target)) action("change", event, { input: { redacted: true, secure_input: true } });
    else action("change", event, { input: {
      value: fieldValue(event.target),
      checked: event.target instanceof HTMLInputElement && ["checkbox", "radio"].includes(event.target.type) ? event.target.checked : undefined,
      selected_items: event.target instanceof HTMLSelectElement ? Array.from(event.target.selectedOptions).map((item) => item.value) : [],
    } });
  }, captureListenerOptions);
  document.addEventListener("submit", (event) => {
    if (!captureAuthorityActive) return;
    flushAllTypes();
    const form = event.target instanceof HTMLFormElement ? event.target : event.target?.form;
    const submitterOverride = event.submitter instanceof Element
      ? event.submitter.getAttribute("formaction") || ""
      : "";
    const submitterAction = submitterOverride ? event.submitter.formAction : "";
    const pageUrl = location.href;
    const destination = globalThis.HermesCausality?.effectiveFormDestination({
      submitterAction,
      formAction: form?.action || "",
      pageUrl,
    }) || form?.action || pageUrl;
    action("submit", event, {
      target: { href: destination, form_method: String(form?.method || "get").toUpperCase() },
      effect: { side_effect: true, before_url: pageUrl },
    });
  }, captureListenerOptions);
  document.addEventListener("reset", (event) => {
    if (!captureAuthorityActive) return;
    action("reset", event);
  }, captureListenerOptions);
  document.addEventListener("keydown", (event) => {
    if (!captureAuthorityActive) return;
    if (MODIFIER_KEYS.has(event.key)) return;
    if (!event.metaKey && !event.ctrlKey && !event.altKey && event.key.length === 1) return;
    const lower = String(event.key || "").toLowerCase();
    if ((event.metaKey || event.ctrlKey) && ["c", "x", "v"].includes(lower)) return;
    action("shortcut", event, { input: { key: event.key, code: event.code, repeat: event.repeat } });
  }, captureListenerOptions);
  document.addEventListener("scroll", (event) => {
    if (!captureAuthorityActive) return;
    clearTimeout(scrollTimer);
    scrollTimer = setTimeout(() => {
      if (!captureAuthorityActive) return;
      action("scroll", event, { input: { scroll_x: window.scrollX, scroll_y: window.scrollY } });
    }, SCROLL_DEBOUNCE_MS);
  }, captureListenerOptions);
  document.addEventListener("selectionchange", () => {
    if (!captureAuthorityActive) return;
    clearTimeout(selectionTimer);
    selectionTimer = setTimeout(() => {
      if (!captureAuthorityActive) return;
      const selection = selectedText();
      if (selection) emit("hcc-trace-action", { ts: ts(), kind: "select", payload: { browser: framePayload(), target: selectionTarget(), input: { selected_text: cap(selection) }, meta: { correlation_id: correlationId() } }, quality: { confidence: "medium", warnings: [] } });
    }, SELECTION_DEBOUNCE_MS);
  }, captureListenerOptions);
  document.addEventListener("copy", (event) => {
    if (!captureAuthorityActive) return;
    action("copy", event, { input: { selected_text: cap(selectedText()) } });
  }, captureListenerOptions);
  document.addEventListener("cut", (event) => {
    if (!captureAuthorityActive) return;
    action("cut", event, { input: { selected_text: cap(selectedText()) } });
  }, captureListenerOptions);
  document.addEventListener("paste", (event) => {
    if (!captureAuthorityActive) return;
    pastedTargets.add(event.target);
    flushType(event.target);
    action("paste", event, { input: { redacted: secureField(event.target), secure_input: secureField(event.target), text: secureField(event.target) ? "" : String(event.clipboardData?.getData("text") || "").slice(0, 100_000) } });
  }, captureListenerOptions);
  document.addEventListener("dragstart", (event) => {
    if (!captureAuthorityActive) return;
    action("drag", event, { input: { x: event.clientX, y: event.clientY } });
  }, captureListenerOptions);
  document.addEventListener("drop", (event) => {
    if (!captureAuthorityActive) return;
    action("drop", event, { input: { x: event.clientX, y: event.clientY, files: Array.from(event.dataTransfer?.files || []).map((file) => ({ name: file.name, size: file.size, mime: file.type, last_modified: file.lastModified })) } });
  }, captureListenerOptions);
  document.addEventListener("focusin", (event) => {
    if (!captureAuthorityActive) return;
    emit("hcc-trace-context", { ts: ts(), kind: "chrome.active_target.changed", payload: { browser: framePayload(), target: targetPayload(event.target, event) }, quality: { confidence: "medium", warnings: [] } });
  }, captureListenerOptions);

  function emitNavigate(before, after, reason) {
    if (!captureAuthorityActive) return;
    emit("hcc-trace-action", { ts: ts(), kind: "navigate", payload: { browser: framePayload(), effect: { before_url: before || "", after_url: after || location.href }, meta: { reason, correlation_id: correlationId() } }, quality: { confidence: "high", warnings: [] } });
  }
  const originalPushState = history.pushState;
  const originalReplaceState = history.replaceState;
  function pushState(...args) {
    if (!captureAuthorityActive) return originalPushState.apply(this, args);
    const before = location.href;
    const result = originalPushState.apply(this, args);
    emitNavigate(before, location.href, "pushState");
    return result;
  }
  function replaceState(...args) {
    if (!captureAuthorityActive) return originalReplaceState.apply(this, args);
    const before = location.href;
    const result = originalReplaceState.apply(this, args);
    emitNavigate(before, location.href, "replaceState");
    return result;
  }
  history.pushState = pushState;
  history.replaceState = replaceState;
  window.addEventListener("popstate", () => {
    if (!captureAuthorityActive) return;
    emitNavigate("", location.href, "popstate");
  }, captureListenerOptions);
  window.addEventListener("pagehide", flushAllTypes, captureListenerOptions);
  window.__hccUserEventsController = {
    runtime: extensionRuntime,
    dispose() {
      captureAuthorityActive = false;
      clearPendingCapture();
      disconnectAuthorityPort();
      lifecycleController.abort();
      if (history.pushState === pushState) history.pushState = originalPushState;
      if (history.replaceState === replaceState) history.replaceState = originalReplaceState;
    },
  };
})();
