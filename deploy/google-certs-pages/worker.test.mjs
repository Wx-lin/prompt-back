import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

const worker = await import("./_worker.js").catch(() => null);
const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

test("only relays Google's public certificates", async () => {
  assert.ok(worker, "Pages worker module must exist");
  const fetched = [];
  globalThis.fetch = async (url, options) => {
    fetched.push({ url, redirect: options.redirect });
    return new Response('{"key-id":"certificate"}', {
      status: 200,
      headers: { "content-type": "application/json", "cache-control": "public, max-age=3600" },
    });
  };

  const response = await worker.default.fetch(
    new Request("https://aigc-google-certs.pages.dev/oauth2/v1/certs")
  );

  assert.equal(response.status, 200);
  assert.equal(await response.text(), '{"key-id":"certificate"}');
  assert.deepEqual(fetched, [
    { url: "https://www.googleapis.com/oauth2/v1/certs", redirect: "manual" },
  ]);
  assert.equal(response.headers.get("cache-control"), "public, max-age=3600");
});

test("rejects requests on other paths without fetching upstream", async () => {
  assert.ok(worker, "Pages worker module must exist");
  globalThis.fetch = () => {
    throw new Error("upstream must not be called");
  };

  const response = await worker.default.fetch(
    new Request("https://aigc-google-certs.pages.dev/tokeninfo?token=secret")
  );

  assert.equal(response.status, 404);
});

test("reports upstream HTTP failure without exposing a token", async () => {
  assert.ok(worker, "Pages worker module must exist");
  globalThis.fetch = async () => new Response("upstream error", { status: 522 });

  const response = await worker.default.fetch(
    new Request("https://aigc-google-certs.pages.dev/oauth2/v1/certs")
  );

  assert.equal(response.status, 503);
  assert.equal(response.headers.get("x-upstream-status"), "522");
});
