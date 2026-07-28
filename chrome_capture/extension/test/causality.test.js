import assert from "node:assert/strict";
import test from "node:test";

await import("../src/causality.js");

const { createActionCausality, effectiveFormDestination } = globalThis.HermesCausality;

function deferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function clickRow(id = "action-1") {
  return {
    kind: "click",
    payload: {
      browser: {
        tab_id: 7,
        window_id: 3,
        url: "https://mail.example.test/inbox?view=all",
      },
      target: { href: "/message/42?mode=full" },
      meta: { correlation_id: id },
    },
  };
}

test("navigation can correlate while native action storage is pending", async () => {
  const clock = { value: 1000 };
  const tracker = createActionCausality({ now: () => clock.value });
  const nativeResponse = deferred();

  const storage = tracker.rememberWhileStoring(clickRow(), () => nativeResponse.promise);

  assert.deepEqual(
    tracker.match(7, 3, "https://mail.example.test/message/42?mode=full"),
    { after_action_id: "action-1" },
  );

  nativeResponse.resolve({ accepted: 1 });
  assert.deepEqual(await storage, { accepted: 1 });
  assert.deepEqual(tracker.match(7, 3, "https://mail.example.test/message/42?mode=full"), {});
});

test("rejected storage removes only the matching pending action", async () => {
  const tracker = createActionCausality();
  const nativeResponse = deferred();
  const storage = tracker.rememberWhileStoring(clickRow(), () => nativeResponse.promise);

  nativeResponse.resolve(null);
  assert.equal(await storage, null);
  assert.deepEqual(tracker.match(7, 3, "https://mail.example.test/message/42?mode=full"), {});
});

test("correlation requires exact target or referrer evidence and expires", async () => {
  const clock = { value: 1000 };
  const tracker = createActionCausality({ windowMs: 50, now: () => clock.value });
  await tracker.rememberWhileStoring(clickRow(), async () => ({ accepted: 1 }));

  assert.deepEqual(tracker.match(7, 3, "https://mail.example.test/unrelated"), {});
  assert.deepEqual(tracker.match(7, 4, "https://mail.example.test/message/42?mode=full"), {});

  clock.value = 1100;
  assert.deepEqual(tracker.match(7, 3, "https://mail.example.test/message/42?mode=full"), {});
});

test("referrer evidence correlates a download once", async () => {
  const tracker = createActionCausality();
  await tracker.rememberWhileStoring(clickRow(), async () => ({ accepted: 1 }));

  assert.deepEqual(
    tracker.match(
      7,
      3,
      "https://cdn.example.test/archive.zip",
      "https://mail.example.test/inbox?view=all",
    ),
    { after_action_id: "action-1" },
  );
  assert.deepEqual(
    tracker.match(
      7,
      3,
      "https://cdn.example.test/archive.zip",
      "https://mail.example.test/inbox?view=all",
    ),
    {},
  );
});

test("form destination drives submit navigation correlation", async () => {
  const pageUrl = "https://shop.example.test/cart?currency=usd";
  const destination = effectiveFormDestination({
    submitterAction: "/checkout/confirm?express=1",
    formAction: "/checkout",
    pageUrl,
  });
  assert.equal(destination, "https://shop.example.test/checkout/confirm?express=1");
  assert.equal(
    effectiveFormDestination({ formAction: "/checkout", pageUrl }),
    "https://shop.example.test/checkout",
  );

  const tracker = createActionCausality();
  const row = {
    kind: "submit",
    payload: {
      browser: { tab_id: 11, window_id: 4, url: pageUrl },
      target: { href: destination, form_method: "POST" },
      meta: { correlation_id: "submit-1" },
    },
  };
  await tracker.rememberWhileStoring(row, async () => ({ accepted: 1 }));

  assert.deepEqual(
    tracker.match(11, 4, "https://shop.example.test/checkout/confirm?express=1"),
    { after_action_id: "submit-1" },
  );
});

test("GET submit correlates when form controls change only the query", async () => {
  const tracker = createActionCausality();
  const row = {
    kind: "submit",
    payload: {
      browser: {
        tab_id: 12,
        window_id: 4,
        url: "https://search.example.test/find?query=old",
      },
      target: {
        href: "https://search.example.test/find",
        form_method: "GET",
      },
      meta: { correlation_id: "submit-get-1" },
    },
  };
  await tracker.rememberWhileStoring(row, async () => ({ accepted: 1 }));

  assert.deepEqual(
    tracker.match(12, 4, "https://search.example.test/find?query=new&scope=all"),
    { after_action_id: "submit-get-1" },
  );
});
