const CERTS_PATH = "/oauth2/v1/certs";
const GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v1/certs";

export default {
  async fetch(request) {
    const url = new URL(request.url);
    if (request.method !== "GET" || url.pathname !== CERTS_PATH || url.search) {
      return new Response("Not found", { status: 404 });
    }

    try {
      const upstream = await fetch(GOOGLE_CERTS_URL, { redirect: "manual" });
      if (!upstream.ok) {
        return new Response("Google certificates unavailable", {
          status: 503,
          headers: { "x-upstream-status": String(upstream.status) },
        });
      }
      return new Response(upstream.body, {
        status: 200,
        headers: {
          "content-type": "application/json",
          "cache-control": upstream.headers.get("cache-control") || "no-store",
        },
      });
    } catch {
      return new Response("Google certificates unavailable", { status: 503 });
    }
  },
};
