(function installHermesURLSanitizer(root) {
  const SECRET_QUERY_TERMS = new Set([
    "token",
    "auth",
    "authorization",
    "oauth",
    "oauth2",
    "code",
    "key",
    "secret",
    "session",
    "sid",
    "jwt",
    "password",
    "passwd",
    "pwd",
  ]);
  const SECRET_QUERY_COMPACT_KEYS = new Set([
    "accesstoken",
    "apikey",
    "apitoken",
    "authcode",
    "authtoken",
    "bearertoken",
    "clientsecret",
    "idtoken",
    "oauthcode",
    "passcode",
    "refreshtoken",
    "sessionid",
    "sessionkey",
    "sig",
    "signature",
    "xamzsecuritytoken",
    "xamzsignature",
    "xgoogsignature",
  ]);
  const SECRET_QUERY_COMPACT_SUFFIXES = ["password", "passwd", "secret", "token"];
  const URL_FIELD_NAMES = new Set([
    "url",
    "origin",
    "href",
    "beforeurl",
    "afterurl",
    "frameurl",
    "topurl",
    "finalurl",
    "activeurl",
    "referrer",
  ]);

  function decodeQueryComponent(value) {
    let decoded = String(value || "");
    for (let round = 0; round < 3; round += 1) {
      const plusDecoded = decoded.replace(/\+/g, " ");
      const nextValue = plusDecoded.replace(/%([0-9A-Fa-f]{2})/g, (escape, hex) => {
        const byte = Number.parseInt(hex, 16);
        return byte <= 0x7F ? String.fromCharCode(byte) : escape;
      });
      if (nextValue === decoded) break;
      decoded = nextValue;
    }
    return decoded;
  }

  function isSecretQueryKey(value) {
    const key = String(value || "").trim();
    if (!key) return false;
    const compact = key.replace(/[^A-Za-z0-9]+/g, "").toLowerCase();
    if (
      SECRET_QUERY_COMPACT_KEYS.has(compact)
      || SECRET_QUERY_COMPACT_SUFFIXES.some((suffix) => compact.endsWith(suffix))
    ) {
      return true;
    }
    const separated = key
      .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
      .replace(/([A-Z])([A-Z][a-z])/g, "$1 $2");
    const terms = separated
      .split(/[^A-Za-z0-9]+/)
      .filter(Boolean)
      .map((term) => term.toLowerCase());
    return terms.some((term) => SECRET_QUERY_TERMS.has(term));
  }

  function redactQueryString(value) {
    const parts = String(value).split(/([&;])/);
    if (!parts.some((part, index) => index % 2 === 0 && queryFieldHasSecretKey(part))) {
      return value;
    }
    return parts
      .map((part, index) => {
        if (index % 2 === 1) return part;
        const field = part;
        if (!queryFieldHasSecretKey(field)) return field;
        const separator = field.indexOf("=");
        const key = field.slice(0, separator);
        return `${key}=%5BREDACTED%5D`;
      })
      .join("");
  }

  function queryFieldHasSecretKey(field) {
    const separator = field.indexOf("=");
    if (separator < 0) return false;
    const key = field.slice(0, separator);
    return isSecretQueryKey(decodeQueryComponent(key));
  }

  function redactFragment(value) {
    if (!value) return value;
    const separator = value.indexOf("?");
    if (separator >= 0) {
      const route = value.slice(0, separator);
      const query = value.slice(separator + 1);
      const redacted = redactQueryString(query);
      return redacted === query ? value : `${route}?${redacted}`;
    }
    if (!value.includes("=")) return value;
    return redactQueryString(value);
  }

  function removeAuthorityCredentials(value) {
    const scheme = value.match(/^[A-Za-z][A-Za-z0-9+.-]*:/);
    const authorityStart = value.startsWith("//")
      ? 2
      : scheme && value.slice(scheme[0].length).startsWith("//")
        ? scheme[0].length + 2
        : -1;
    if (authorityStart < 0) return value;
    const pathStart = value.indexOf("/", authorityStart);
    const authorityEnd = pathStart < 0 ? value.length : pathStart;
    const authority = value.slice(authorityStart, authorityEnd);
    const credentialEnd = authority.lastIndexOf("@");
    if (credentialEnd < 0) return value;
    return `${value.slice(0, authorityStart)}${authority.slice(credentialEnd + 1)}${value.slice(authorityEnd)}`;
  }

  function sanitizeUrl(value) {
    const text = String(value || "").trim();
    if (!text) return "";
    const hasScheme = /^[A-Za-z][A-Za-z0-9+.-]*:/.test(text);
    if (!hasScheme && !text.startsWith("//")) return text;

    const fragmentStart = text.indexOf("#");
    const beforeFragment = fragmentStart < 0 ? text : text.slice(0, fragmentStart);
    const fragment = fragmentStart < 0 ? "" : text.slice(fragmentStart + 1);
    const queryStart = beforeFragment.indexOf("?");
    const base = queryStart < 0 ? beforeFragment : beforeFragment.slice(0, queryStart);
    const query = queryStart < 0 ? "" : beforeFragment.slice(queryStart + 1);
    return [
      removeAuthorityCredentials(base),
      queryStart < 0 ? "" : `?${redactQueryString(query)}`,
      fragmentStart < 0 ? "" : `#${redactFragment(fragment)}`,
    ].join("");
  }

  function isURLField(key) {
    const normalized = String(key || "").replace(/[^A-Za-z0-9]+/g, "").toLowerCase();
    return URL_FIELD_NAMES.has(normalized) || normalized.endsWith("url");
  }

  function sanitizeValue(value, key = "") {
    if (Array.isArray(value)) return value.map((item) => sanitizeValue(item, key));
    if (isURLField(key) && typeof value === "string") return sanitizeUrl(value);
    if (!value || typeof value !== "object") return value;
    return Object.fromEntries(
      Object.entries(value).map(([entryKey, entryValue]) => [
        entryKey,
        sanitizeValue(entryValue, entryKey),
      ]),
    );
  }

  root.HermesURLSanitizer = Object.freeze({ sanitizeUrl, sanitizeValue });
})(globalThis);
