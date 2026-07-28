(function installHermesCausality(root) {
  const DEFAULT_ACTION_KINDS = new Set(["click", "double_click", "submit", "upload", "drop", "change"]);

  function canonicalUrl(value, base = "") {
    if (!value) return "";
    try {
      const url = new URL(value, base || undefined);
      url.hash = "";
      return url.href;
    } catch {
      return "";
    }
  }

  function canonicalOriginPath(value, base = "") {
    if (!value) return "";
    try {
      const url = new URL(value, base || undefined);
      return `${url.origin}${url.pathname}`;
    } catch {
      return "";
    }
  }

  function effectiveFormDestination({ submitterAction = "", formAction = "", pageUrl = "" } = {}) {
    return canonicalUrl(submitterAction || formAction || pageUrl, pageUrl);
  }

  function createActionCausality({ windowMs = 5000, now = () => Date.now() } = {}) {
    const recentActions = new Map();

    function rowIdentity(row) {
      const payload = row?.payload || {};
      const browser = payload.browser || {};
      return {
        id: payload.meta?.correlation_id || "",
        providerEventId: payload.meta?.provider_event_id || "",
        tabId: browser.tab_id,
      };
    }

    function remember(row) {
      if (!DEFAULT_ACTION_KINDS.has(row?.kind)) return;
      const payload = row.payload || {};
      const browser = payload.browser || {};
      const tabId = browser.tab_id;
      if (tabId == null) return;
      recentActions.set(String(tabId), {
        id: payload.meta?.correlation_id || "",
        kind: row.kind,
        formMethod: String(payload.target?.form_method || "").toUpperCase(),
        tabId,
        windowId: browser.window_id,
        pageUrl: browser.url || browser.top_url || "",
        targetHref: payload.target?.href || "",
        at: now(),
      });
    }

    function forget(row) {
      const { id, tabId } = rowIdentity(row);
      if (tabId == null) return;
      const key = String(tabId);
      if (recentActions.get(key)?.id === id) recentActions.delete(key);
    }

    async function rememberWhileStoring(row, store) {
      remember(row);
      try {
        const stored = await store();
        const providerEventId = rowIdentity(row).providerEventId;
        const accepted = providerEventId && Array.isArray(stored?.acceptedEventIds)
          ? stored.acceptedEventIds.includes(providerEventId)
          : !!stored?.accepted;
        if (!accepted) forget(row);
        return stored;
      } catch (error) {
        forget(row);
        throw error;
      }
    }

    function match(tabId, windowId, url, referrer = "") {
      if (tabId == null) return {};
      const key = String(tabId);
      const candidate = recentActions.get(key);
      if (!candidate) return {};
      if (now() - candidate.at > windowMs) {
        recentActions.delete(key);
        return {};
      }
      if (candidate.windowId != null && windowId != null && candidate.windowId !== windowId) return {};
      const destination = canonicalUrl(url);
      const targetHref = canonicalUrl(candidate.targetHref, candidate.pageUrl);
      const source = canonicalUrl(candidate.pageUrl);
      const sourceReferrer = canonicalUrl(referrer);
      const submitGetPathMatches = candidate.kind === "submit"
        && candidate.formMethod === "GET"
        && !!destination
        && canonicalOriginPath(destination) === canonicalOriginPath(targetHref);
      if (destination !== targetHref && !submitGetPathMatches && source !== sourceReferrer) return {};
      if (!destination && !sourceReferrer) return {};
      recentActions.delete(key);
      return candidate.id ? { after_action_id: candidate.id } : {};
    }

    return {
      clear: () => recentActions.clear(),
      match,
      rememberWhileStoring,
    };
  }

  root.HermesCausality = Object.freeze({ createActionCausality, effectiveFormDestination });
})(globalThis);
