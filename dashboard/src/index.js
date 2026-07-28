/** Hermes-first recorder -> reviewed native cron job. */
(function () {
  "use strict";
  const SDK = window.__HERMES_PLUGIN_SDK__;
  const REGISTRY = window.__HERMES_PLUGINS__;
  if (!SDK || !REGISTRY) return;

  const h = SDK.React.createElement;
  const { useEffect, useState } = SDK.hooks;
  const { Button, Card, CardContent, CardHeader, Input, Label } = SDK.components;
  const schedule = window.__HERMES_CRUNCH_SCHEDULE__;
  if (!schedule) {
    throw new Error("Hermes Crunch schedule module did not load");
  }
  const {
    DEFAULT_SCHEDULE_STATE,
    buildScheduleString,
    createScheduleBuilder,
    describeScheduleBuilderState,
    parseScheduleString,
  } = schedule;
  const ScheduleBuilder = createScheduleBuilder(SDK);
  const API = "/api/plugins/hermes-crunch";
  const HERMES_BASE_PATH = (function () {
    const raw = String(window.__HERMES_BASE_PATH__ || "").trim();
    if (!raw) return "";
    const withLeadingSlash = raw.startsWith("/") ? raw : "/" + raw;
    return withLeadingSlash.replace(/\/+$/, "");
  })();
  const DASHBOARD_PROVIDER_SOURCE = "hermes-crunch-dashboard";
  const CHROME_PROVIDER_SOURCE = "hermes-chrome-provider";
  const CHROME_PROVIDER_REQUEST_TYPE = "hcc-provider-request";
  const CHROME_PROVIDER_RESPONSE_TYPE = "hcc-provider-response";
  const CHROME_PROVIDER_CONTENT_SCRIPT_VERSION = 4;
  const CHROME_PROVIDER_REQUEST_TIMEOUT_MS = 3000;
  const CHROME_PROVIDER_FINALIZE_TIMEOUT_MS = 6500;
  const WORKFLOW_PAGE_SESSIONS = new Map();

  function createWorkflowPageState() {
    return {
      name: "",
      instructions: "",
      scheduleState: Object.assign({}, DEFAULT_SCHEDULE_STATE, {
        weekdays: DEFAULT_SCHEDULE_STATE.weekdays.slice(),
      }),
      runMode: "schedule",
      recording: null,
      draft: null,
      compiled: null,
      installed: null,
      busy: "",
      dismissConfirmOpen: false,
      draftOperationId: "",
      cancelingDraft: false,
      notice: "",
      draftProgress: [],
      requirementsAttempted: false,
      boundaryEnabled: {},
      reviewConversation: null,
      reviewMessage: "",
      reviewLoading: false,
    };
  }

  function workflowPageSession(profile) {
    if (!WORKFLOW_PAGE_SESSIONS.has(profile)) {
      WORKFLOW_PAGE_SESSIONS.set(profile, {
        state: createWorkflowPageState(),
        listeners: new Set(),
      });
    }
    return WORKFLOW_PAGE_SESSIONS.get(profile);
  }

  function useWorkflowPageState(profile) {
    const [state, setReactState] = useState(function () {
      return workflowPageSession(profile).state;
    });

    useEffect(function () {
      const session = workflowPageSession(profile);
      session.listeners.add(setReactState);
      setReactState(session.state);
      return function () { session.listeners.delete(setReactState); };
    }, [profile]);

    function updateState(patchOrUpdater) {
      const session = workflowPageSession(profile);
      const patch = typeof patchOrUpdater === "function"
        ? patchOrUpdater(session.state)
        : patchOrUpdater;
      session.state = Object.assign({}, session.state, patch);
      session.listeners.forEach(function (listener) {
        listener(session.state);
      });
    }
    return [state, updateState];
  }

  function post(path, body) {
    return SDK.fetchJSON(API + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  function get(path) {
    return SDK.fetchJSON(API + path);
  }

  function captureAuthorizationNonce() {
    if (!globalThis.crypto || typeof globalThis.crypto.getRandomValues !== "function") {
      throw new Error("Secure browser randomness is unavailable for Chrome capture authorization");
    }
    const bytes = new Uint8Array(32);
    globalThis.crypto.getRandomValues(bytes);
    return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  }

  function requestChromePageRecorder(command, recordingId, captureAuthorization) {
    return new Promise(function (resolve) {
      const requestId = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
      let settled = false;
      let staleProviderSeen = false;
      let providerError = "";
      const finish = function (result) {
        if (settled) return;
        settled = true;
        window.clearTimeout(timer);
        window.removeEventListener("message", onMessage);
        resolve(result);
      };
      const onMessage = function (event) {
        const response = event.data;
        if (event.source !== window || event.origin !== window.location.origin) return;
        if (!response || response.source !== CHROME_PROVIDER_SOURCE || response.type !== CHROME_PROVIDER_RESPONSE_TYPE) return;
        if (response.requestId !== requestId) return;
        if (response.contentScriptVersion !== CHROME_PROVIDER_CONTENT_SCRIPT_VERSION) {
          staleProviderSeen = true;
          return;
        }
        if (response.ok !== true && response.error) providerError = response.error;
        finish({ ok: response.ok === true, error: response.error || "" });
      };
      const timeoutMs = command === "finalize"
        ? CHROME_PROVIDER_FINALIZE_TIMEOUT_MS
        : CHROME_PROVIDER_REQUEST_TIMEOUT_MS;
      const timer = window.setTimeout(function () {
        finish({
          ok: false,
          error: providerError || (
            staleProviderSeen
              ? "The loaded Hermes Chrome Provider is outdated. Reload or reinstall the current provider."
              : "Hermes Chrome provider page recorder did not respond"
          ),
        });
      }, timeoutMs);
      window.addEventListener("message", onMessage);
      window.postMessage({
        source: DASHBOARD_PROVIDER_SOURCE,
        type: CHROME_PROVIDER_REQUEST_TYPE,
        requestId,
        command,
        recordingId: recordingId || "",
        captureAuthorization: captureAuthorization || "",
      }, window.location.origin);
    });
  }

  function errorText(error) {
    return error instanceof Error ? error.message : String(error);
  }

  function GuidedStep(number, title, description, id, child, incomplete) {
    return h("div", { className: "hc-guided-step" + (incomplete ? " hc-guided-step-required" : "") },
      h(Label, { className: "hc-step-heading", htmlFor: id },
        h("span", { className: "hc-step-number", "aria-hidden": "true" }, number),
        h("span", { className: "hc-step-copy" },
          h("span", { className: "hc-step-title-row" },
            h("strong", null, title),
            incomplete ? h("span", { className: "hc-step-required" }, "Required") : null
          ),
          h("span", null, incomplete ? "Complete this before recording." : description)
        )
      ),
      child
    );
  }

  function SectionHeading(title, description) {
    return h("div", { className: "hc-section-heading" },
      h("h2", { className: "hc-card-title" }, title),
      h("p", { className: "hc-card-description" }, description)
    );
  }

  function boundaryDescription(boundary) {
    const effects = Array.isArray(boundary.effects) ? boundary.effects : [];
    const fallback = effects.length ? effects.join(" or ") + " action" : "this action";
    return String(boundary.description || fallback).trim();
  }

  function uniqueStringList(value) {
    if (!Array.isArray(value)) return null;
    const seen = new Set();
    const result = [];
    for (const item of value) {
      const normalized = typeof item === "string" ? item.trim() : "";
      if (!normalized || seen.has(normalized)) return null;
      seen.add(normalized);
      result.push(normalized);
    }
    return result;
  }

  function boundaryEnabledState(boundary, enabledByKey) {
    return Object.prototype.hasOwnProperty.call(enabledByKey, boundary.key)
      ? enabledByKey[boundary.key] !== false
      : boundary.enabled !== false;
  }

  function summarizeHumanChecks(boundaries, enabledByKey) {
    const inline = boundaries.filter((boundary) => boundary.steps.length > 0);
    if (!inline.length) return "None";
    const enabled = inline.filter((boundary) => boundaryEnabledState(boundary, enabledByKey)).length;
    const suggested = inline.filter((boundary) => boundary.enabled !== false).length;
    const affectedSteps = new Set();
    inline.forEach((boundary) => boundary.steps.forEach((step) => affectedSteps.add(step)));
    return inline.length + (inline.length === 1 ? " approval pause" : " approval pauses")
      + " · " + enabled + " on"
      + " · " + suggested + " suggested"
      + " · " + affectedSteps.size + (affectedSteps.size === 1 ? " step" : " steps");
  }

  function approvalTargetLabel(installReadiness) {
    if (!installReadiness || typeof installReadiness !== "object") return "";
    return String(
      installReadiness.approval_target_name
      || installReadiness.approval_target
      || installReadiness.approval_route && installReadiness.approval_route.platform
      || ""
    ).trim();
  }

  function profileScopedActionHref(href, profile) {
    const raw = String(href || "").trim();
    if (!raw) return "";
    if (!raw.startsWith("/")) return raw;
    const separator = raw.includes("?") ? "&" : "?";
    const scoped = /(?:^|[?&])profile=/.test(raw)
      ? raw
      : raw + separator + "profile=" + encodeURIComponent(profile);
    return HERMES_BASE_PATH + scoped;
  }

  function selectInstallReadinessBlockers(blockers, runMode) {
    return (Array.isArray(blockers) ? blockers : []).filter((item) => {
      if (!item || !item.code) return true;
      if (item.code === "webhook_receiver_unavailable") return runMode === "webhook";
      return true;
    });
  }

  function projectStepCaptureWarnings(review, stepCount) {
    const byStep = {};
    const warnings = [
      ...(Array.isArray(review.warnings) ? review.warnings : []),
      ...(Array.isArray(review.recording_warnings) ? review.recording_warnings : []),
    ];
    warnings.forEach((warning) => {
      if (!warning || warning.kind !== "capture_quality_gap" || !Number.isInteger(warning.step)
        || warning.step < 1 || warning.step > stepCount || byStep[warning.step]) return;
      const message = typeof warning.message === "string" ? warning.message.trim() : "";
      const verify = typeof warning.verify === "string" ? warning.verify.trim() : "";
      if (!message || !verify
        || /^(capture diagnostic|trace row|internal diagnostic)\b/i.test(message)
        || /\b(committed-side-effect|capture diagnostic|trace row|telemetry event|provider event)\b/i.test(verify)) return;
      // Recording diagnostics stay internal. The customer only needs the
      // concrete check Hermes will perform to confirm the step worked.
      byStep[warning.step] = { verify };
    });
    return byStep;
  }

  function recordingNeedsRerecord(blockers) {
    const integrityKinds = new Set(["capture_acceptance_missing", "capture_acceptance_invalid"]);
    return Array.isArray(blockers) && blockers.some((item) =>
      item && item.resolution === "rerecord"
      && integrityKinds.has(String(item.kind || item.code || ""))
    );
  }

  function summarizeRerecordBlockers(blockers) {
    if (!recordingNeedsRerecord(blockers)) return "";
    return "Part of the saved demonstration is missing or changed, so Hermes can’t safely build from it. "
      + "Nothing has been turned on. Please show the workflow again.";
  }

  function normalizeBoundaries(value) {
    if (value && typeof value.error === "string" && value.error.trim()) {
      return { boundaries: [], error: value.error.trim() };
    }
    if (!value || !Array.isArray(value.boundaries)) return { boundaries: [], error: "Hermes could not load the approval pauses for this draft. Reload the page before turning it on." };
    const seen = new Set();
    const boundaries = [];
    for (const boundary of value.boundaries) {
      if (!boundary || typeof boundary.key !== "string" || !boundary.key.trim()
        || !["operation", "script"].includes(boundary.kind)
        || boundary.enabled !== undefined && typeof boundary.enabled !== "boolean") {
        return { boundaries: [], error: "Hermes could not load an approval pause for this draft. Reload the page before turning it on." };
      }
      const key = boundary.key.trim();
      if (seen.has(key)) {
        return { boundaries: [], error: "Hermes could not load an approval pause for this draft. Reload the page before turning it on." };
      }
      const steps = Array.isArray(boundary.steps) ? boundary.steps : [];
      if (steps.some((step) => !Number.isInteger(step) || step < 1)) {
        return { boundaries: [], error: "Hermes could not place an approval pause in this draft. Reload the page before turning it on." };
      }
      let effects;
      if (boundary.kind === "operation") {
        const operationId = typeof boundary.operation_id === "string"
          ? boundary.operation_id.trim() : "";
        effects = uniqueStringList(boundary.effects);
        if (!operationId || key !== "operation:" + operationId
          || !effects || effects.length !== 1
          || typeof boundary.description !== "string" || !boundary.description.trim()) {
          return { boundaries: [], error: "Hermes could not load an approval pause for this draft. Reload the page before turning it on." };
        }
      } else {
        const effect = typeof boundary.effect === "string" ? boundary.effect.trim() : "";
        const scriptId = typeof boundary.script_id === "string" ? boundary.script_id.trim() : "";
        if (!effect || !scriptId || key !== "script:" + scriptId) {
          return { boundaries: [], error: "Hermes could not load an approval pause for this draft. Reload the page before turning it on." };
        }
        effects = [effect];
      }
      seen.add(key);
      boundaries.push(Object.assign({}, boundary, {
        key,
        effects,
        steps: Array.from(new Set(steps)).sort((left, right) => left - right),
      }));
    }
    return { boundaries, error: "" };
  }

  function WorkflowPage() {
    const { locale, t } = SDK.useI18n();
    const profile = new URLSearchParams(window.location.search).get("profile") || "default";
    const [pageState, updatePageState] = useWorkflowPageState(profile);
    const {
      name,
      instructions,
      scheduleState,
      runMode,
      recording,
      draft,
      compiled,
      installed,
      busy,
      dismissConfirmOpen,
      draftOperationId,
      cancelingDraft,
      notice,
      draftProgress,
      requirementsAttempted,
      boundaryEnabled,
      reviewConversation,
      reviewMessage,
      reviewLoading,
    } = pageState;
    function fieldSetter(field) {
      return function (nextValue) {
        updatePageState(function (current) {
          return {
            [field]: typeof nextValue === "function"
              ? nextValue(current[field])
              : nextValue,
          };
        });
      };
    }
    function setInstallInput(field, value) {
      updatePageState(function (current) {
        if (current.busy) return {};
        return {
          [field]: value,
          compiled: null,
        };
      });
    }
    const setName = fieldSetter("name");
    const setInstructions = fieldSetter("instructions");
    const setRecording = fieldSetter("recording");
    const setBusy = fieldSetter("busy");
    const setNotice = fieldSetter("notice");
    const setDraftProgress = fieldSetter("draftProgress");
    const setReviewMessage = fieldSetter("reviewMessage");

    const recordingKey = recording && (recording.recording_key || recording.recording_dir);
    const recordingSessionId = recording && (
      recording.metadata && recording.metadata.sessionID
      || recording.session && recording.session.sessionID
      || String(recordingKey || "").split("/").filter(Boolean).pop()
    );
    const schedule = buildScheduleString(scheduleState);
    const scheduleSummary = describeScheduleBuilderState(scheduleState, t.cron, locale);
    const weeklyScheduleIncomplete = scheduleState.mode === "weekly" && scheduleState.weekdays.length === 0;
    const runSummary = runMode === "webhook" ? "When a verified webhook event arrives" : scheduleSummary;

    function applyResumeResult(result) {
      const resumePatch = {};
      if (!result || !result.recording) {
        updatePageState(resumePatch);
        return;
      }
      const recovered = result.recording;
      resumePatch.recording = recovered;
      resumePatch.name = recovered.name || "";
      resumePatch.instructions = recovered.instructions || "";
      if (recovered.run_mode === "webhook") resumePatch.runMode = "webhook";
      if (recovered.schedule) {
        resumePatch.scheduleState = parseScheduleString(recovered.schedule);
      }
      updatePageState(resumePatch);
      if (result.draft) applyDraft(result.draft);
    }

    useEffect(() => {
      if (recording || draft || installed) return undefined;
      let canceled = false;
      get("/recordings/resume?profile=" + encodeURIComponent(profile)).then((result) => {
        if (canceled || !result) return;
        applyResumeResult(result);
      }).catch((error) => {
        if (!canceled) setNotice(errorText(error));
      });
      return () => { canceled = true; };
    }, [profile]);

    useEffect(() => {
      if (!recordingKey || !draft || !draft.draft_digest) return undefined;
      let canceled = false;
      updatePageState({ reviewLoading: true });
      (async function () {
        try {
          let result = await get("/recordings/" + encodeURIComponent(recordingKey)
            + "/review-conversation?profile=" + encodeURIComponent(profile)
            + "&expected_draft_digest=" + encodeURIComponent(draft.draft_digest));
          if (result.needs_initial_turn) {
            result = await post("/recordings/" + encodeURIComponent(recordingKey) + "/review-conversation", {
              profile,
              expected_draft_digest: draft.draft_digest,
              message: null,
            });
          }
          if (canceled) return;
          updatePageState({
            reviewConversation: result,
            reviewLoading: false,
          });
        } catch (error) {
          if (!canceled) {
            updatePageState({
              notice: errorText(error),
              reviewLoading: false,
            });
          }
        }
      })();
      return () => { canceled = true; };
    }, [profile, recordingKey, draft && draft.draft_digest]);

    function applyDraft(result) {
      const inferredTrigger = result && result.workflow && result.workflow.trigger;
      const normalized = normalizeBoundaries(result && result.effective_approval_boundaries);
      const enabled = {};
      normalized.boundaries.forEach((boundary) => {
        enabled[boundary.key] = boundary.enabled !== false;
      });
      updatePageState(function (current) {
        return {
          draft: result,
          compiled: null,
          runMode: inferredTrigger
            && inferredTrigger.requested === true
            && inferredTrigger.feasibility === "supported"
            ? "webhook"
            : current.runMode,
          reviewConversation: null,
          reviewMessage: "",
          boundaryEnabled: enabled,
        };
      });
    }

    async function start() {
      if (!name.trim() || !instructions.trim()
        || runMode === "schedule" && (!schedule || weeklyScheduleIncomplete)) {
        updatePageState({ requirementsAttempted: true });
        return;
      }
      updatePageState({
        requirementsAttempted: false,
        busy: "start",
        notice: "",
        draft: null,
        compiled: null,
        installed: null,
        boundaryEnabled: {},
        reviewConversation: null,
      });
      const captureAuthorization = captureAuthorizationNonce();
      try {
        const pageRecorder = await requestChromePageRecorder("prepare", "", captureAuthorization);
        if (!pageRecorder.ok) {
          throw new Error(pageRecorder.error || "Hermes Chrome provider page recorder is unavailable");
        }
        const result = await post("/recordings/start", {
          profile,
          name,
          instructions,
          schedule,
          run_mode: runMode,
          capture_authorization_nonce: captureAuthorization,
          capture_dashboard_origin: window.location.origin,
        });
        setRecording(result);
      } catch (error) {
        await requestChromePageRecorder("cancel", "", captureAuthorization);
        setNotice(errorText(error));
      }
      finally { setBusy(""); }
    }

    async function stopAndDraft() {
      if (!recordingKey) return;
      updatePageState({ busy: "stop", notice: "" });
      try {
        if (recording.status !== "stopped") {
          const pageRecorder = await requestChromePageRecorder("finalize", recordingSessionId);
          await post("/recordings/" + encodeURIComponent(recordingKey) + "/stop", {
            profile,
            chrome_finalize_ok: pageRecorder.ok === true,
            chrome_finalize_error: pageRecorder.ok === true ? "" : String(pageRecorder.error || "Chrome capture continuity was not verified"),
          });
          setRecording((current) => Object.assign({}, current, { status: "stopped" }));
        }
        await startDraftOperation();
      } catch (error) {
        setNotice(errorText(error));
      }
      finally { setBusy(""); }
    }

    async function waitForDraft(operationId) {
      while (true) {
        await new Promise((resolve) => window.setTimeout(resolve, 500));
        const operation = await get("/drafts/" + encodeURIComponent(operationId));
        setDraftProgress(Array.isArray(operation.progress) ? operation.progress : []);
        if (operation.status === "completed") return operation.result;
        if (operation.status === "canceled") return null;
        if (operation.status === "failed") throw new Error(operation.error || "Workflow drafting failed");
      }
    }

    async function startDraftOperation(reviewThreadId) {
      updatePageState({
        busy: "draft",
        notice: "",
        cancelingDraft: false,
        draftProgress: [],
      });
      try {
        const payload = { profile, name, instructions };
        if (draft && draft.draft_digest) {
          payload.expected_draft_digest = draft.draft_digest;
        }
        if (reviewThreadId) {
          payload.review_thread_id = reviewThreadId;
        }
        const operation = await post("/recordings/" + encodeURIComponent(recordingKey) + "/draft", payload);
        updatePageState({
          draftProgress: Array.isArray(operation.progress) ? operation.progress : [],
          draftOperationId: operation.operation_id,
        });
        const result = await waitForDraft(operation.operation_id);
        if (result) applyDraft(result);
      } finally {
        updatePageState({
          draftOperationId: "",
          cancelingDraft: false,
          busy: "",
        });
      }
    }

    async function cancelDraft() {
      if (!draftOperationId || cancelingDraft) return;
      updatePageState({ cancelingDraft: true, notice: "" });
      try {
        await post("/drafts/" + encodeURIComponent(draftOperationId) + "/cancel", {});
      } catch (error) {
        updatePageState({
          cancelingDraft: false,
          notice: errorText(error),
        });
      }
    }

    async function sendReview() {
      const message = reviewMessage.trim();
      if (!recordingKey || !draft || !message || busy) return;
      updatePageState({ busy: "review", notice: "" });
      try {
        const result = await post("/recordings/" + encodeURIComponent(recordingKey) + "/review-conversation", {
          profile,
          expected_draft_digest: draft.draft_digest,
          message,
        });
        updatePageState({
          reviewConversation: result,
          reviewMessage: "",
        });
      } catch (error) { setNotice(errorText(error)); }
      finally { setBusy(""); }
    }

    async function dismiss() {
      if (!recordingKey || busy) return;
      updatePageState({ busy: "dismiss", notice: "" });
      try {
        await post("/recordings/" + encodeURIComponent(recordingKey) + "/discard", { profile });
        updatePageState(Object.assign(createWorkflowPageState(), {
          busy: "dismiss",
        }));
      } catch (error) { setNotice(errorText(error)); }
      finally { setBusy(""); }
    }

    function requestDismiss() {
      if (!recordingKey || busy) return;
      updatePageState({ dismissConfirmOpen: true, notice: "" });
    }

    function cancelDismiss() {
      if (!busy) updatePageState({ dismissConfirmOpen: false });
    }

    async function install() {
      if (runMode === "schedule" && !schedule) {
        setNotice(weeklyScheduleIncomplete ? "Choose at least one day." : "Choose a valid schedule.");
        return;
      }
      updatePageState({ busy: "install", notice: "" });
      try {
        const result = await post("/recordings/schedule", {
          profile,
          recording_dir: draft.recording_dir,
          schedule,
          run_mode: runMode,
          expected_draft_digest: draft.draft_digest,
          authorization: selectedAuthorization,
          approve: !!compiled,
          expected_approval_envelope_sha256: compiled && compiled.approval_envelope_sha256,
        });
        if (result.status === "awaiting_approval") {
          updatePageState({ compiled: result });
          return;
        }
        updatePageState({
          installed: result,
          compiled: null,
          recording: null,
          draft: null,
          draftOperationId: "",
          draftProgress: [],
        });
      } catch (error) {
        updatePageState({
          notice: errorText(error),
          compiled: null,
        });
        await refreshInstallReadiness();
      }
      finally { setBusy(""); }
    }

    async function refreshInstallReadiness() {
      if (!recordingKey || !draft || !draft.draft_digest) return;
      updatePageState({ reviewLoading: true });
      try {
        const result = await get("/recordings/" + encodeURIComponent(recordingKey)
          + "/review-conversation?profile=" + encodeURIComponent(profile)
          + "&expected_draft_digest=" + encodeURIComponent(draft.draft_digest));
        updatePageState({
          reviewConversation: result,
          reviewLoading: false,
          compiled: null,
        });
      } catch (error) {
        updatePageState({
          notice: errorText(error),
          reviewLoading: false,
          compiled: null,
        });
      }
    }

    const workflow = draft && draft.workflow;
    const trigger = workflow && workflow.trigger || {};
    const triggerSupported = trigger.feasibility === "supported";
    const webhookRuntime = reviewConversation && reviewConversation.webhook_runtime || null;
    const webhookReady = !!(webhookRuntime && webhookRuntime.enabled);
    const normalizedBoundaries = workflow
      ? normalizeBoundaries(
        reviewConversation
          ? reviewConversation.effective_approval_boundaries
          : draft && draft.effective_approval_boundaries
      )
      : { boundaries: [], error: "" };
    const approvalBoundaries = normalizedBoundaries.boundaries;
    const approvalBoundaryError = normalizedBoundaries.error;
    const review = workflow && workflow.review || {};
    const execution = workflow && workflow.execution || {};
    const authorization = execution.authorization || {};
    const operations = Array.isArray(authorization.operations) ? authorization.operations : [];
    const scriptApproval = authorization.script && typeof authorization.script === "object"
      ? authorization.script : null;
    const draftScript = execution.script && typeof execution.script === "object"
      ? execution.script : null;
    const compiledEnvelope = compiled && compiled.approval_envelope
      && typeof compiled.approval_envelope === "object"
      ? compiled.approval_envelope : null;
    const exactScriptSource = compiledEnvelope
      && typeof compiledEnvelope.script_source === "string"
      ? compiledEnvelope.script_source
      : draftScript && typeof draftScript.body === "string"
        ? draftScript.body
        : "";
    const planSteps = Array.isArray(review.execution_steps) ? review.execution_steps : [];
    const stepCaptureWarnings = projectStepCaptureWarnings(review, planSteps.length);
    const boundaryByKey = approvalBoundaries.reduce((result, boundary) => {
      result[boundary.key] = boundary;
      return result;
    }, {});
    const boundaryMappingError = approvalBoundaries.some((boundary) =>
      boundary.steps.some((step) => step > planSteps.length)
    ) ? "Hermes could not place an approval pause in the workflow steps. Reload the page before turning it on."
      : approvalBoundaries.some((boundary) => boundary.steps.length === 0)
        ? "Hermes could not place every approval pause in the workflow steps. Reload the page before turning it on."
        : "";
    const boundaryRenderError = approvalBoundaryError || boundaryMappingError;
    const operationEnabledById = {};
    let scriptApprovalEnabled;
    approvalBoundaries.forEach((boundary) => {
      const enabled = boundaryEnabledState(boundary, boundaryEnabled);
      if (boundary.kind === "operation") {
        operationEnabledById[boundary.operation_id] = enabled;
      } else if (boundary.kind === "script") {
        scriptApprovalEnabled = enabled;
      }
    });
    const selectedOperations = operations.map((operation) => {
      const operationId = String(operation.id || "");
      return Object.assign({}, operation, {
        enabled: Object.prototype.hasOwnProperty.call(operationEnabledById, operationId)
          ? operationEnabledById[operationId]
          : operation.enabled !== false,
      });
    });
    const selectedScriptApproval = scriptApproval
      ? Object.assign({}, scriptApproval, {
        enabled: typeof scriptApprovalEnabled === "boolean"
          ? scriptApprovalEnabled
          : scriptApproval.enabled !== false,
      })
      : null;
    const selectedAuthorization = {
      mode: "human_in_loop",
      effects: [],
      operations: selectedOperations,
      checkpoints: [],
      script: selectedScriptApproval,
    };
    const hasRuntimeApproval = selectedOperations.some(
      (operation) => operation.enabled !== false
    )
      || !!(selectedScriptApproval && selectedScriptApproval.enabled !== false);
    const installReadiness = reviewConversation && reviewConversation.install_readiness
      || draft && draft.install_readiness
      || null;
    const approvalRouteReady = !!(installReadiness && installReadiness.approval_route_ready);
    const readinessBlockers = Array.isArray(installReadiness && installReadiness.blockers)
      ? installReadiness.blockers : [];
    const selectedReadinessBlockers = selectInstallReadinessBlockers(
      readinessBlockers,
      runMode,
    );
    if (!installReadiness) {
      selectedReadinessBlockers.push({
        code: "install_readiness_unavailable",
        message: "Hermes could not verify this profile’s home gateway and required tracing. Check again before turning on this workflow.",
      });
    } else if (installReadiness.ready !== true && selectedReadinessBlockers.length === 0
      && (hasRuntimeApproval || readinessBlockers.length === 0)) {
      selectedReadinessBlockers.push({
        code: "install_readiness_unavailable",
        message: "Hermes could not verify that this profile is ready to run the workflow. Check again before turning it on.",
      });
    }
    const installReadinessBlocker = selectedReadinessBlockers.find((item) =>
      item && item.message
    ) || {
      message: "Hermes could not verify that this profile is ready to run the workflow.",
    };
    const installReadinessBlocked = selectedReadinessBlockers.length > 0;
    const resolvedApprovalTarget = approvalTargetLabel(installReadiness);
    const approvalGatewayDispatchReady = !!(
      installReadiness && installReadiness.gateway_dispatch_ready
    );
    const approvalGatewayCopy = hasRuntimeApproval && approvalRouteReady && resolvedApprovalTarget
      ? selectedScriptApproval && selectedScriptApproval.enabled !== false
        ? "The exact reviewed script will pause before it starts and ask for your approval through the "
          + resolvedApprovalTarget + " home gateway for this profile."
        : "Each enabled protected action will pause before it runs and ask for your approval through the "
          + resolvedApprovalTarget + " home gateway for this profile."
      : "";
    const inlineBoundaries = approvalBoundaries.filter((boundary) => boundary.steps.length > 0);
    const boundariesByStep = {};
    inlineBoundaries.forEach((boundary) => {
      const owner = boundary.steps[0];
      boundary.steps.forEach((step) => {
        if (!boundariesByStep[step]) boundariesByStep[step] = [];
        boundariesByStep[step].push({ boundary, owner });
      });
    });
    const humanChecksSummary = summarizeHumanChecks(approvalBoundaries, boundaryEnabled);
    const enabledApprovalPauseCount = inlineBoundaries.filter((boundary) =>
      boundaryEnabledState(boundary, boundaryEnabled)
    ).length;
    const approvalPauseFinalCopy = selectedScriptApproval
      && selectedScriptApproval.enabled !== false
      ? "Hermes will ask for your approval before executing the exact reviewed script."
      : enabledApprovalPauseCount
        ? "Hermes will ask for your approval before "
        + enabledApprovalPauseCount + (enabledApprovalPauseCount === 1 ? " change." : " changes.")
        : "Hermes will enforce the reviewed policy without approval prompts.";
    const reviewState = reviewConversation && reviewConversation.state || "";
    const reviewBlockers = Array.isArray(reviewConversation && reviewConversation.blockers)
      ? reviewConversation.blockers
      : Array.isArray(draft && draft.review_blockers) ? draft.review_blockers : [];
    const requiresNewDemonstration = recordingNeedsRerecord(reviewBlockers);
    const rerecordBlockerText = summarizeRerecordBlockers(reviewBlockers);
    const activeReviewThreadId = reviewConversation && reviewConversation.thread_id;
    const isRecording = recording && recording.status !== "stopped";
    const nameMissing = !name.trim();
    const instructionsMissing = !instructions.trim();
    const scheduleMissing = runMode === "schedule" && (!schedule || weeklyScheduleIncomplete);
    const formComplete = !nameMissing && !instructionsMissing && !scheduleMissing;
    const showRequirements = requirementsAttempted && !formComplete;
    const recordingBlocker = busy === "start" ? "Preparing the recorder…" :
      !formComplete ? showRequirements ? "Finish the highlighted steps to start recording" : "Complete the details before recording" :
      "Ready to record";
    const recordingButtonLabel = busy === "start" ? "Starting…" : "Start recording";
    const latestProgress = draftProgress.length ? draftProgress[draftProgress.length - 1] : null;
    const installBlocker = busy ? "Hermes is finishing the current action." :
      runMode === "schedule" && !schedule ? weeklyScheduleIncomplete ? "Choose at least one day." : "Choose a valid schedule." :
      runMode === "webhook" && !triggerSupported ? "This event cannot be installed as a webhook trigger: " + (trigger.reason || "Hermes could not verify a compatible authenticated webhook.") :
      runMode === "webhook" && !webhookReady ? (webhookRuntime && webhookRuntime.reason || "Loading webhook setup…") :
      boundaryRenderError || reviewLoading ? boundaryRenderError || "Loading the workflow review…" :
      requiresNewDemonstration ? "Hermes needs a trustworthy demonstration before this workflow can be turned on." :
      reviewState === "needs_rerecord" ? "Hermes is updating this draft so it can confirm each result." :
      reviewState === "needs_input" ? "Answer Hermes’s question before turning on this workflow." :
      reviewState === "ready_to_redraft" ? "Your requested change is ready. Update the draft to continue." :
      reviewBlockers.length ? "Resolve the remaining draft detail before turning on this workflow." :
      !reviewState ? "Loading the workflow review…" :
      installReadinessBlocked ? installReadinessBlocker.message :
      compiled ? "Final setup ready. Turn it on when you’re comfortable." :
      "Ready for your final review";
    const scheduleDisabled = !!busy || runMode === "schedule" && !schedule
      || runMode === "webhook" && !triggerSupported || !!boundaryRenderError || reviewLoading
      || runMode === "webhook" && !webhookReady
      || reviewState !== "ready_to_schedule" || reviewBlockers.length > 0
      || installReadinessBlocked;

    function toggleBoundary(key) {
      if (busy) return;
      if (!boundaryByKey[key]) {
        setNotice("Hermes could not find that approval pause. Reload the page before turning on this workflow.");
        return;
      }
      updatePageState(function (current) {
        if (current.busy) return {};
        return {
          boundaryEnabled: Object.assign({}, current.boundaryEnabled, {
            [key]: !boundaryEnabledState(boundaryByKey[key], current.boundaryEnabled),
          }),
          compiled: null,
        };
      });
    }

    function renderHumanCheck(boundary, key) {
      const enabled = boundaryEnabledState(boundary, boundaryEnabled);
      const description = boundaryDescription(boundary);
      return h("div", { className: "hc-human-check", key: key || boundary.key },
        h("div", { className: "hc-human-check-copy" },
          h("span", { className: "hc-effect-badge" }, boundary.effects.join(" / ")),
          h("strong", null, "Approval pause"),
          h("span", null, description)
        ),
        h("label", { className: "hc-switch-control" },
          h("span", { className: "hc-switch-copy" },
            h("span", null, boundary.enabled !== false ? "Recommended" : "Optional"),
            h("strong", null, enabled ? "Will pause" : "Won’t pause")
          ),
          h("input", {
            type: "checkbox",
            role: "switch",
            checked: enabled,
            disabled: !!busy,
            onChange: () => toggleBoundary(boundary.key),
            "aria-label": "Ask for approval before: " + description,
            "aria-describedby": "hc-approval-pause-help",
          }),
          h("span", { className: "hc-switch-track", "aria-hidden": "true" })
        )
      );
    }

    function renderTranscript() {
      if (requiresNewDemonstration && rerecordBlockerText) {
        return h("div", { className: "hc-chat-bubble hc-chat-assistant" }, rerecordBlockerText);
      }
      if (reviewState === "needs_rerecord") {
        return h("div", { className: "hc-chat-bubble hc-chat-assistant" },
          "Hermes kept your draft. Review the success checks in the workflow steps, then ask Hermes to correct anything that is not what you want."
        );
      }
      const threads = Array.isArray(reviewConversation && reviewConversation.threads)
        ? reviewConversation.threads : [];
      const entries = [];
      threads.forEach((thread, index) => {
        if (index && threads[index - 1] && threads[index - 1].state === "applied") {
          entries.push(h("div", { className: "hc-draft-divider", key: "divider-" + index },
            "Draft updated to revision " + (Number(threads[index - 1].draft_revision || 0) + 1)
          ));
        }
        if (thread.state === "applied" && thread.feedback_summary) {
          entries.push(h("p", { className: "hc-feedback-applied", key: "feedback-" + thread.thread_id },
            "Feedback applied: “" + thread.feedback_summary + "”"
          ));
        }
        (Array.isArray(thread.messages) ? thread.messages : []).forEach((message, messageIndex) => {
          entries.push(h("div", {
            className: "hc-chat-bubble hc-chat-" + message.role,
            key: thread.thread_id + "-" + messageIndex,
          }, message.content));
        });
      });
      return entries.length
        ? entries
        : h("p", { className: "hc-chat-empty" }, "No unresolved details. Ask Hermes about this draft or request a change.");
    }

    function progressText(event) {
      if (!event) return "Hermes is drafting the workflow…";
      if (event.type === "assistant.message") return event.text;
      if (event.type === "tool.started") return event.text || (event.tool ? "Running " + event.tool : "Hermes is working…");
      if (event.type === "tool.completed") return event.tool ? event.tool + (event.is_error ? " failed" : " completed") : "Step completed";
      return event.text || event.tool || "Hermes is drafting the workflow…";
    }

    return h("div", { className: "hc-page" },
      h(Card, { className: "hc-card" },
        h(CardHeader, { className: "hc-card-header" },
          SectionHeading("Create a workflow", "Describe the result, choose when it should run, then do the task once. Work normally—you do not need to explain every click.")
        ),
        h(CardContent, { className: "hc-card-content hc-create-content" },
          h("div", { className: "hc-guided-grid" },
            h("div", { className: "hc-guided-main" },
              GuidedStep("1", "Name your workflow", "Use a short name you will recognize later in Cron.", "hc-workflow-name",
                h(Input, { id: "hc-workflow-name", value: name, disabled: !!busy || !!installed, onChange: (event) => setName(event.target.value), placeholder: "Daily customer activity brief", autoComplete: "off", "aria-invalid": showRequirements && nameMissing }),
                showRequirements && nameMissing
              ),
              GuidedStep("2", "Describe the result", "Say what should be finished and what success looks like. You do not need to list every click.", "hc-workflow-instructions",
                h("textarea", { id: "hc-workflow-instructions", className: "hc-textarea", value: instructions, disabled: !!busy || !!installed, onChange: (event) => setInstructions(event.target.value), placeholder: "Create a daily brief, save it to…, and confirm…", "aria-invalid": showRequirements && instructionsMissing }),
                showRequirements && instructionsMissing
              )
            ),
            h("section", { className: "hc-guided-step hc-schedule-step" + (showRequirements && scheduleMissing ? " hc-guided-step-required" : ""), "aria-labelledby": "hc-schedule-title" },
              h("div", { className: "hc-step-heading" },
                h("span", { className: "hc-step-number", "aria-hidden": "true" }, "3"),
                h("span", { className: "hc-step-copy" },
                  h("span", { className: "hc-step-title-row" },
                    h("strong", { id: "hc-schedule-title" }, "Choose how it starts"),
                    showRequirements && scheduleMissing ? h("span", { className: "hc-step-required" }, "Required") : null
                  ),
                  h("span", null, runMode === "webhook"
                    ? "Hermes will verify the event can securely trigger this workflow."
                    : showRequirements && scheduleMissing ? "Select a schedule before recording." : "Hermes can also detect an event trigger from your description.")
                )
              ),
              h("div", { className: "hc-run-mode", role: "group", "aria-label": "Workflow start mode" },
                h(Button, { outlined: runMode !== "schedule", onClick: () => setInstallInput("runMode", "schedule"), disabled: !!busy }, "On a schedule"),
                h(Button, { outlined: runMode !== "webhook", onClick: () => setInstallInput("runMode", "webhook"), disabled: !!busy }, "From an event")
              ),
              runMode === "schedule" ? h("fieldset", {
                className: "hc-schedule-controls",
                disabled: !!busy,
              }, h(ScheduleBuilder, {
                value: scheduleState,
                onChange: (value) => setInstallInput("scheduleState", value),
              })) :
                h("div", { className: "hc-trigger-hint" },
                  h("strong", null, "Describe the exact event in step 2"),
                  h("span", null, "Examples: when a GitHub issue opens, after an AgentMail message arrives, or when my service sends a signed event.")
                )
            )
          ),
          notice ? h("div", { className: "hc-notice hc-notice-error", role: "alert" }, h("strong", null, "Something needs attention"), h("span", null, notice)) : null,
          !recording ? h("div", { className: "hc-submit-row" },
            h("div", { className: "hc-submit-summary" },
              h("span", { id: "hc-recording-requirements", className: showRequirements ? "hc-submit-state hc-submit-state-locked" : "hc-submit-state" }, recordingBlocker),
              showRequirements ? h("ul", { className: "hc-recording-checklist", "aria-label": "Recording requirements" },
                h("li", { className: nameMissing ? "hc-requirement-missing" : "hc-requirement-complete" }, h("span", { "aria-hidden": "true" }, nameMissing ? "!" : "✓"), "Name"),
                h("li", { className: instructionsMissing ? "hc-requirement-missing" : "hc-requirement-complete" }, h("span", { "aria-hidden": "true" }, instructionsMissing ? "!" : "✓"), "Outcome"),
                h("li", { className: scheduleMissing ? "hc-requirement-missing" : "hc-requirement-complete" }, h("span", { "aria-hidden": "true" }, scheduleMissing ? "!" : "✓"), runMode === "webhook" ? "Event trigger" : "Schedule")
              ) : null,
              h("strong", null, runSummary)
            ),
            h(Button, {
              className: "hc-primary-action" + (!formComplete ? " hc-primary-action-needs-details" : ""),
              onClick: start,
              disabled: !!busy,
              title: formComplete ? "Start recording this workflow" : "Start recording and show required details",
              "aria-describedby": "hc-recording-requirements"
            }, recordingButtonLabel)
          ) : null,
          recording && !draft ? h("div", { className: "hc-recording", role: "status", "aria-live": "polite" },
            h("div", { className: "hc-recording-copy" },
              h("span", { className: "hc-recording-dot" + (isRecording ? " hc-recording-dot-active" : ""), "aria-hidden": "true" }),
              h("div", null,
                h("strong", null, isRecording ? "Recording your workflow" : busy === "draft" ? "Drafting your workflow" : "Recording saved"),
                h("p", null, isRecording ? "Do the task once as you normally would, then return here to create the draft. You do not need to narrate each click." : busy === "draft" ? progressText(latestProgress) : "Your demonstration is saved. Retry drafting when you are ready.")
              )
            ),
            h("div", { className: "hc-actions" },
              h(Button, { outlined: true, onClick: requestDismiss, disabled: !!busy }, busy === "dismiss" ? "Dismissing…" : "Dismiss and start over"),
              busy === "draft"
                ? h(Button, { outlined: true, onClick: cancelDraft, disabled: cancelingDraft }, cancelingDraft ? "Canceling…" : "Cancel drafting")
                : h(Button, { onClick: stopAndDraft, disabled: !!busy }, busy === "stop" ? "Stopping…" : recording.status === "stopped" ? "Retry draft" : "Stop and draft")
            )
          ) : null
        )
      ),
      workflow ? h(Card, { className: "hc-card hc-review-card" },
        h(CardHeader, { className: "hc-card-header" }, SectionHeading("Review your workflow", "Hermes drafted a reusable method from what you showed. Check the result; Hermes asks only about choices it cannot safely infer.")),
        h(CardContent, { className: "hc-card-content hc-review" },
          h("div", { className: "hc-review-facts", "aria-label": "Workflow summary" },
            h("div", { className: "hc-review-fact" }, h("span", null, "Starts"), h("strong", null, runSummary)),
            h("div", { className: "hc-review-fact" }, h("span", null, "Approval pauses"), h("strong", null, humanChecksSummary))
          ),
          trigger.requested ? h("div", { className: "hc-trigger-review " + (triggerSupported ? "hc-trigger-supported" : "hc-trigger-unsupported") },
            h("strong", null, triggerSupported ? "Webhook trigger suggested" : "This trigger cannot be created"),
            h("span", null, trigger.description || "Requested event trigger"),
            h("p", null, trigger.reason || "Hermes could not verify a compatible authenticated webhook."),
            triggerSupported ? h("span", { className: "hc-trigger-events" }, "Events: " + (trigger.events || []).join(", ")) : null,
            triggerSupported && webhookRuntime && !webhookReady ? h("span", { className: "hc-trigger-setup-needed" }, webhookRuntime.reason) : null
          ) : null,
          boundaryRenderError ? h("div", { className: "hc-notice hc-notice-error", role: "alert" }, boundaryRenderError) : null,
          installReadinessBlocked ? h("div", {
            className: "hc-notice hc-notice-error hc-channel-needed",
            role: "alert",
          },
            h("strong", null, "Before this workflow can turn on"),
            h("span", { className: "hc-readiness-profile" }, "Profile: " + profile),
            h("ul", { className: "hc-readiness-blockers" },
              selectedReadinessBlockers.map((item, index) => {
                const action = item && item.action && typeof item.action === "object"
                  ? item.action : null;
                const href = profileScopedActionHref(action && action.href, profile);
                return h("li", { key: String(item && item.code || "blocker") + "-" + index },
                  h("span", null, String(item && item.message || "Complete the required setup.")),
                  href ? h("a", {
                    href,
                  }, String(action && action.label || "Resolve setup")) : null
                );
              })
            ),
            h("button", {
              type: "button",
              className: "hc-check-readiness",
              disabled: !!busy || reviewLoading,
              onClick: () => { void refreshInstallReadiness(); },
            }, reviewLoading ? "Checking…" : "Check again")
          ) : null,
          approvalGatewayCopy ? h("div", {
            className: "hc-approval-route"
              + (approvalGatewayDispatchReady ? " hc-approval-route-ready" : ""),
            role: "status",
          },
            h("strong", null, resolvedApprovalTarget + " home gateway"),
            h("span", null, approvalGatewayCopy)
          ) : null,
          exactScriptSource ? h("section", {
            className: "hc-script-review",
            "aria-labelledby": "hc-exact-script-source",
          },
            h("div", { className: "hc-review-section-heading" },
              h("h3", { id: "hc-exact-script-source" }, "Exact script source"),
              h("p", null, selectedScriptApproval
                ? "Approval authorizes this complete source as one exact script before Hermes starts it."
                : "This complete source is the script Hermes will run.")
            ),
            compiledEnvelope && compiledEnvelope.script_sha256
              ? h("p", { className: "hc-script-digest" },
                "Script SHA-256: ",
                h("code", null, compiledEnvelope.script_sha256)
              )
              : null,
            h("pre", { className: "hc-script-source" },
              h("code", null, exactScriptSource)
            )
          ) : null,
          h("div", { className: "hc-review-workspace" },
            h("section", { className: "hc-review-timeline", "aria-labelledby": "hc-workflow-steps" },
              h("div", { className: "hc-review-section-heading" },
                h("h3", { id: "hc-workflow-steps" }, "Workflow steps"),
                h("p", { id: "hc-approval-pause-help" },
                  "Approval pauses let you authorize a change before Hermes makes it. They do not confirm success; the success checks below confirm the result."
                )
              ),
              planSteps.length ? h("ol", { className: "hc-plan-steps" }, planSteps.map((step, index) => {
                const placements = boundariesByStep[index + 1] || [];
                const captureWarning = stepCaptureWarnings[index + 1];
                return h("li", { key: index },
                  h("span", { className: "hc-plan-step-number", "aria-hidden": "true" }, index + 1),
                  h("div", { className: "hc-plan-step-copy" },
                    h("p", null, step),
                    captureWarning ? h("div", { className: "hc-step-success-check", role: "note" },
                      h("strong", null, "Success check"),
                      h("span", null, "Hermes will perform this check: " + captureWarning.verify)
                    ) : null,
                    placements.map((placement, placementIndex) =>
                      placement.owner === index + 1
                        ? renderHumanCheck(placement.boundary, placement.boundary.key + "-" + index)
                        : h("div", { className: "hc-covered-check", key: placement.boundary.key + "-" + placementIndex },
                          "Covered by the approval pause on step " + placement.owner + ": " + boundaryDescription(placement.boundary)
                        )
                    )
                  )
                );
              })) : h("div", { className: "hc-notice hc-notice-error" }, "Hermes could not produce a readable step list. Ask Hermes to rebuild the draft, or start over if the demonstration itself was incomplete."),
            ),
            h("aside", { className: "hc-review-conversation", "aria-labelledby": "hc-review-hermes-title" },
              h("div", { className: "hc-review-section-heading" },
                h("h3", { id: "hc-review-hermes-title" }, "Review with Hermes"),
                h("p", null, "Ask a question or tell Hermes what to change. Use the controls beside each step to choose approval pauses.")
              ),
              h("div", { className: "hc-chat-log", role: "log", "aria-live": "polite", "aria-label": "Review conversation" }, renderTranscript()),
              h(Label, { htmlFor: "hc-review-message", className: "hc-chat-label" }, "Ask Hermes or request a change"),
              h("textarea", {
                id: "hc-review-message",
                className: "hc-textarea hc-chat-input",
                value: reviewMessage,
                disabled: !!busy || reviewLoading,
                placeholder: "For example: save the note in my Billing folder",
                "aria-describedby": "hc-review-hermes-title",
                onChange: (event) => setReviewMessage(event.target.value),
                onKeyDown: (event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    void sendReview();
                  }
                },
              }),
              h("div", { className: "hc-chat-actions" },
                h(Button, { outlined: true, onClick: sendReview, disabled: !!busy || reviewLoading || !reviewMessage.trim() }, busy === "review" ? "Sending…" : "Send"),
                reviewState === "ready_to_redraft" && activeReviewThreadId
                  ? h(Button, {
                    onClick: () => { void startDraftOperation(activeReviewThreadId).catch((error) => { setNotice(errorText(error)); setBusy(""); }); },
                    disabled: !!busy,
                  }, busy === "draft" ? "Updating…" : "Update draft")
                  : null,
                busy === "draft" ? h(Button, { outlined: true, onClick: cancelDraft, disabled: cancelingDraft }, cancelingDraft ? "Canceling…" : "Cancel drafting") : null
              )
            )
          ),
          h("div", { className: "hc-review-footer" },
            h("div", { className: "hc-review-schedule" },
              h("span", {
                id: "hc-activation-status",
                className: requiresNewDemonstration ? "hc-blocked-state" : scheduleDisabled ? "hc-pending-state" : "hc-ready-state",
                role: "status",
                "aria-live": "polite",
              }, installBlocker),
              h("strong", null, runSummary)
            ),
            compiled ? h("div", { className: "hc-final-setup" },
              h("strong", null, "Final setup"),
              h("p", null, runSummary + ". " + approvalPauseFinalCopy),
              h("details", { className: "hc-approval-envelope" },
                h("summary", null, "Technical details"),
                h("p", null, "This identifier locks your approval to this exact setup."),
                h("code", null, compiled.approval_envelope_sha256),
                h("pre", null, JSON.stringify(compiled.approval_envelope, null, 2))
              )
            ) : null,
            h(Button, {
              outlined: !requiresNewDemonstration,
              onClick: requestDismiss,
              disabled: !!busy,
              "aria-describedby": "hc-activation-status",
            }, busy === "dismiss" ? "Starting over…" : requiresNewDemonstration ? "Show workflow again" : "Discard draft"),
            requiresNewDemonstration ? null
              : h(Button, {
                onClick: install,
                disabled: scheduleDisabled,
                "aria-describedby": "hc-activation-status",
              }, busy === "install"
                ? compiled ? "Turning on workflow…" : "Preparing final setup…"
                : compiled ? "Turn on workflow" : "Review final setup")
          )
        )
      ) : null,
      installed ? h(Card, { className: "hc-card hc-success-card" }, h(CardContent, { className: "hc-success-content" },
        h("span", { className: "hc-success-icon", "aria-hidden": "true" }, "✓"),
        h("div", { className: "hc-success-copy" },
          h("strong", null, "Workflow is on"),
          h("p", null, installed.run_mode === "webhook"
            ? "Hermes will start it when a matching event arrives."
            : "Hermes will run it " + runSummary.toLowerCase() + "."),
          installed.webhook ? h("div", { className: "hc-webhook-credentials" },
            h("span", null, "Webhook URL"),
            h("code", null, installed.webhook.url),
            h("span", null, "Signing secret (shown once)"),
            h("code", null, installed.webhook.secret)
          ) : null
        ),
        h("a", { className: "hc-cron-link", href: installed.cron_url || "/cron" }, "Manage in Cron", h("span", { "aria-hidden": "true" }, "→"))
      )) : null,
      dismissConfirmOpen ? h("div", {
        className: "hc-dismiss-backdrop",
        role: "presentation",
        onMouseDown: (event) => { if (event.target === event.currentTarget) cancelDismiss(); },
      }, h("section", {
        className: "hc-dismiss-dialog",
        role: "dialog",
        "aria-modal": "true",
        "aria-labelledby": "hc-dismiss-title",
        "aria-describedby": "hc-dismiss-description",
        onKeyDown: (event) => { if (event.key === "Escape") cancelDismiss(); },
      },
        h("h2", { id: "hc-dismiss-title" }, "Discard this recording?"),
        h("p", { id: "hc-dismiss-description" }, "Hermes will stop recording and close this unfinished workflow so you can start over."),
        h("div", { className: "hc-dismiss-actions" },
          h(Button, { outlined: true, onClick: cancelDismiss, disabled: !!busy, autoFocus: true }, "Keep recording"),
          h(Button, { onClick: dismiss, disabled: !!busy }, busy === "dismiss" ? "Discarding…" : "Discard and start over")
        )
      )) : null
    );
  }

  REGISTRY.register("hermes-crunch", WorkflowPage);
})();
