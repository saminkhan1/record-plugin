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

function createHarness(html = "<!doctype html><html><head><title>Test App</title></head><body></body></html>") {
  const runtimeListeners = [];
  const sentMessages = [];
  let queryAuthority = {
    ok: true,
    type: "hcc-capture-authority-state",
    revision: 1,
    active: true,
    recordingId: "rec-test-1",
  };

  const dom = new JSDOM(html, {
    runScripts: "outside-only",
    url: "https://app.example.test/workflows",
  });
  const { window } = dom;
  window.CSS ||= {};
  window.CSS.escape ||= (value) => String(value).replaceAll('"', '\\"');
  window.chrome = {
    runtime: {
      get lastError() { return undefined; },
      onMessage: {
        addListener(listener) { runtimeListeners.push(listener); },
      },
      connect() {
        return {
          name: "hcc-capture-authority",
          onMessage: { addListener() {} },
          onDisconnect: { addListener() {} },
          disconnect() {},
        };
      },
      sendMessage(message, callback) {
        sentMessages.push(message);
        queueMicrotask(() => {
          callback?.(
            message?.type === "hcc-capture-authority-query"
              ? queryAuthority
              : { ok: true },
          );
        });
      },
    },
  };

  window.eval(causalitySource);
  window.eval(userEventsSource);

  function updateAuthority(active, revision = 1) {
    const message = {
      action: "hcc-capture-authority-update",
      type: "hcc-capture-authority-state",
      revision,
      active,
      recordingId: active ? "rec-test-1" : "",
    };
    for (const listener of runtimeListeners) listener(message, {}, () => {});
  }

  return {
    window,
    sentMessages,
    updateAuthority,
    close() { dom.window.close(); },
  };
}

test("enhanced label resolution: aria-labelledby, associated label, and enclosing label", async () => {
  const html = `<!doctype html><html><head><title>App</title></head><body>
    <div id="section-hdr">Billing Information</div>
    <button id="hdr-btn" aria-labelledby="section-hdr">Edit</button>

    <label for="user-email">Work Email Address</label>
    <input id="user-email" type="email" value="jane@corp.test">

    <label>Department Name <input id="dept-input" type="text" value="Engineering"></label>
  </body></html>`;

  const harness = createHarness(html);
  const { window } = harness;
  await sleep(10);
  harness.updateAuthority(true, 1);

  const btn = window.document.getElementById("hdr-btn");
  btn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));

  const emailInput = window.document.getElementById("user-email");
  emailInput.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));

  const deptInput = window.document.getElementById("dept-input");
  deptInput.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));

  await sleep(50);

  const clicks = harness.sentMessages
    .filter((m) => m.type === "hcc-trace-action" && m.event.kind === "click");

  assert.equal(clicks.length, 3);
  assert.equal(clicks[0].event.payload.target.label, "Billing Information");
  assert.equal(clicks[1].event.payload.target.label, "Work Email Address");
  assert.equal(clicks[2].event.payload.target.label, "Department Name");

  harness.close();
});

test("semantic landmark and fieldset group extraction", async () => {
  const html = `<!doctype html><html><head><title>App</title></head><body>
    <dialog aria-label="Confirm Deletion">
      <fieldset>
        <legend>Danger Zone</legend>
        <button id="del-btn">Delete Account</button>
      </fieldset>
    </dialog>
  </body></html>`;

  const harness = createHarness(html);
  const { window } = harness;
  await sleep(10);
  harness.updateAuthority(true, 1);

  const btn = window.document.getElementById("del-btn");
  btn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await sleep(50);

  const clicks = harness.sentMessages
    .filter((m) => m.type === "hcc-trace-action" && m.event.kind === "click");

  assert.equal(clicks.length, 1);
  const target = clicks[0].event.payload.target;
  assert.equal(target.landmark, "dialog[Confirm Deletion]");
  assert.equal(target.group_label, "Danger Zone");

  harness.close();
});

test("stable selectors: form scoping and modern test attributes", async () => {
  const html = `<!doctype html><html><head><title>App</title></head><body>
    <form id="settings-form">
      <input name="api_key" placeholder="Enter API key" data-cy="api-key-input" value="123">
    </form>
  </body></html>`;

  const harness = createHarness(html);
  const { window } = harness;
  await sleep(10);
  harness.updateAuthority(true, 1);

  const input = window.document.querySelector("input");
  input.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await sleep(50);

  const clicks = harness.sentMessages
    .filter((m) => m.type === "hcc-trace-action" && m.event.kind === "click");

  assert.equal(clicks.length, 1);
  const selectors = clicks[0].event.payload.target.selectors;

  assert.ok(selectors.some((s) => s.includes('[data-cy="api-key-input"]')));
  assert.ok(selectors.some((s) => s.includes('form#settings-form [name="api_key"]')));
  assert.ok(selectors.some((s) => s.includes('[placeholder="Enter API key"]')));

  harness.close();
});

test("form submission extraction captures submitter, controls summary, and redacts passwords", async () => {
  const html = `<!doctype html><html><head><title>App</title></head><body>
    <form id="user-reg-form" action="/register" method="POST">
      <label for="f-name">Full Name</label>
      <input id="f-name" name="fullname" type="text" value="Alice Smith">

      <label for="f-role">Role</label>
      <select id="f-role" name="role">
        <option value="admin" selected>Administrator</option>
      </select>

      <label for="f-terms">Agree to terms</label>
      <input id="f-terms" name="terms" type="checkbox" checked>

      <label for="f-pwd">Password</label>
      <input id="f-pwd" name="password" type="password" value="SuperSecret123">

      <button type="submit" name="submit_action" value="create_admin">Create Admin Account</button>
    </form>
  </body></html>`;

  const harness = createHarness(html);
  const { window } = harness;
  await sleep(10);
  harness.updateAuthority(true, 1);

  const form = window.document.getElementById("user-reg-form");
  const submitBtn = form.querySelector("button");

  const submitEvent = new window.Event("submit", { bubbles: true, cancelable: true });
  submitEvent.submitter = submitBtn;
  form.dispatchEvent(submitEvent);
  await sleep(50);

  const submits = harness.sentMessages
    .filter((m) => m.type === "hcc-trace-action" && m.event.kind === "submit");

  assert.equal(submits.length, 1);
  const target = submits[0].event.payload.target;

  // Verify submitter extraction
  assert.ok(target.submitter);
  assert.equal(target.submitter.tag, "button");
  assert.equal(target.submitter.name, "submit_action");
  assert.equal(target.submitter.value, "create_admin");
  assert.equal(target.submitter.label, "Create Admin Account");

  // Verify form summary extraction
  assert.ok(target.form_summary);
  assert.equal(target.form_summary.id, "user-reg-form");
  assert.equal(target.form_summary.method, "POST");
  assert.equal(target.form_summary.field_count, 4);

  const pwdField = target.form_summary.fields.find((f) => f.name === "password");
  assert.ok(pwdField);
  assert.equal(pwdField.value, "[REDACTED]");

  const roleField = target.form_summary.fields.find((f) => f.name === "role");
  assert.ok(roleField);
  assert.equal(roleField.value, "admin");

  const termsField = target.form_summary.fields.find((f) => f.name === "terms");
  assert.ok(termsField);
  assert.equal(termsField.checked, true);

  harness.close();
});

test("select change captures human-readable options and labels", async () => {
  const html = `<!doctype html><html><head><title>App</title></head><body>
    <select id="shipping-method">
      <option value="std">Standard Shipping ($5)</option>
      <option value="exp" selected>Express Overnight ($25)</option>
    </select>
  </body></html>`;

  const harness = createHarness(html);
  const { window } = harness;
  await sleep(10);
  harness.updateAuthority(true, 1);

  const select = window.document.getElementById("shipping-method");
  select.dispatchEvent(new window.Event("change", { bubbles: true }));
  await sleep(50);

  const changes = harness.sentMessages
    .filter((m) => m.type === "hcc-trace-action" && m.event.kind === "change");

  assert.equal(changes.length, 1);
  const input = changes[0].event.payload.input;

  assert.deepEqual(JSON.parse(JSON.stringify(input.selected_items)), ["exp"]);
  assert.deepEqual(JSON.parse(JSON.stringify(input.selected_texts)), ["Express Overnight ($25)"]);
  assert.deepEqual(JSON.parse(JSON.stringify(input.selected_options)), [
    { value: "exp", label: "Express Overnight ($25)" },
  ]);

  harness.close();
});

test("page semantics: h1, meta description, og:title, and JSON-LD extraction", async () => {
  const html = `<!doctype html><html><head>
    <title>Order Confirmation</title>
    <meta name="description" content="Your order has been placed successfully.">
    <meta property="og:title" content="Order #12345 Confirmation">
    <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@type": "Order",
        "orderNumber": "12345"
      }
    </script>
  </head><body>
    <h1>Thank You for Your Order!</h1>
    <button id="view-order">View Order Details</button>
  </body></html>`;

  const harness = createHarness(html);
  const { window } = harness;
  await sleep(10);
  harness.updateAuthority(true, 1);

  const btn = window.document.getElementById("view-order");
  btn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await sleep(50);

  const clicks = harness.sentMessages
    .filter((m) => m.type === "hcc-trace-action" && m.event.kind === "click");

  assert.equal(clicks.length, 1);
  const browser = clicks[0].event.payload.browser;

  assert.ok(browser.semantics);
  assert.equal(browser.semantics.heading, "Thank You for Your Order!");
  assert.equal(browser.semantics.description, "Your order has been placed successfully.");
  assert.equal(browser.semantics.og_title, "Order #12345 Confirmation");
  assert.deepEqual(JSON.parse(JSON.stringify(browser.semantics.ld_types)), ["Order"]);

  harness.close();
});
