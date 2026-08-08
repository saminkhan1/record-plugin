import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const extensionRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const source = readFileSync(path.join(extensionRoot, "src", "url-sanitizer.js"), "utf8");
const fixture = JSON.parse(readFileSync(
  path.join(extensionRoot, "..", "..", "tests", "fixtures", "url_sanitization_vectors.json"),
  "utf8",
));
const context = vm.createContext({});
vm.runInContext(source, context);
const { sanitizeUrl, sanitizeValue } = context.HermesURLSanitizer;

test("URL sanitizer matches the shared Python and Swift fixture", () => {
  assert.equal(fixture.version, 1);
  for (const vector of fixture.vectors) {
    assert.equal(sanitizeUrl(vector.input), vector.expected, vector.name);
  }
});

test("URL sanitizer defensively sanitizes every nested capture URL field", () => {
  const secret = "https://user:password@example.test/callback?access_token=query-secret#id_token=fragment-secret";
  const safe = "https://example.test/callback?access_token=%5BREDACTED%5D#id_token=%5BREDACTED%5D";
  const signedReferrer = fixture.vectors.find(
    (vector) => vector.name === "signed_url_signatures_are_redacted"
  );
  assert.ok(signedReferrer);
  const sanitized = sanitizeValue({
    activeTarget: { url: secret },
    redirectUrl: [secret, "https://example.test/safe?q=a%20b"],
    rows: [{
      payload: {
        browser: { activeUrl: secret, redirectUrl: secret },
        effect: { download: { download_url: secret, referrer: [signedReferrer.input] } },
      },
    }],
    unrelated: secret,
  });

  assert.equal(sanitized.activeTarget.url, safe);
  assert.deepEqual(
    Array.from(sanitized.redirectUrl),
    [safe, "https://example.test/safe?q=a%20b"]
  );
  assert.equal(sanitized.rows[0].payload.browser.activeUrl, safe);
  assert.equal(sanitized.rows[0].payload.browser.redirectUrl, safe);
  assert.equal(sanitized.rows[0].payload.effect.download.download_url, safe);
  assert.deepEqual(
    Array.from(sanitized.rows[0].payload.effect.download.referrer),
    [signedReferrer.expected]
  );
  assert.equal(sanitized.unrelated, secret);
});
