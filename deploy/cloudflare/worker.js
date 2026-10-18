import { DurableObject } from "cloudflare:workers";

const INSTANCES = ["lite", "standard-1", "standard-2", "standard-3", "standard-4"];
const INACTIVITY_MS = 5 * 60 * 1000;
const ALARM_MS = 60 * 1000;
const BOOTSTRAP_MS = 120 * 1000;

export class Sandbox extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    if (ctx.container.running) {
      void ctx.blockConcurrencyWhile(() => ctx.container.setInactivityTimeout(INACTIVITY_MS));
    }
  }

  async fetch(request) {
    const container = this.ctx.container;
    if (request.method === "DELETE") {
      await this.ctx.storage.put("status", "deleted");
      await container.destroy();
      await this.ctx.storage.deleteAlarm();
      return Response.json({ status: "deleted" });
    }
    if (request.method !== "PUT") {
      return new Response(null, { status: 405 });
    }

    const body = await request.json();
    if (!Object.hasOwn(container.images, body.image) || !INSTANCES.includes(body.instance)) {
      return Response.json({ error: "Invalid sandbox configuration" }, { status: 400 });
    }

    const claimed = await this.ctx.blockConcurrencyWhile(async () => {
      if (await this.ctx.storage.get("status")) return false;
      await this.ctx.storage.put("status", "creating");
      return true;
    });
    if (!claimed) return Response.json({ error: "Sandbox name already exists" }, { status: 409 });

    try {
      container.start({
        image: container.images[body.image],
        instance: body.instance,
        entrypoint: ["sleep", "infinity"],
        enableInternet: true,
        labels: { "managed-by": body.label },
      });
      await container.setInactivityTimeout(INACTIVITY_MS);
      await this.ctx.storage.setAlarm(Date.now() + ALARM_MS);
      const abort = new AbortController();
      let timer;
      const deadline = new Promise((_, reject) => {
        timer = setTimeout(() => {
          reject(new Error("Bootstrap timed out"));
          abort.abort();
        }, BOOTSTRAP_MS);
      });
      try {
        const exitCode = await Promise.race([
          container.exec(["bash", "-e", "-c", body.script], {
            signal: abort.signal, stdout: "ignore", stderr: "ignore",
          }).then(process => process.exitCode),
          deadline,
        ]);
        if (exitCode !== 0) throw new Error("Bootstrap failed");
      } finally {
        clearTimeout(timer);
      }
      if (await this.ctx.storage.get("status") !== "creating") {
        await container.destroy();
        return Response.json({ error: "Sandbox was deleted" }, { status: 409 });
      }
      await this.ctx.storage.put("status", "active");
      return Response.json({ status: "active" }, { status: 201 });
    } catch {
      if (await this.ctx.storage.get("status") === "creating") {
        await this.ctx.storage.put("status", "error");
      }
      await container.destroy();
      await this.ctx.storage.deleteAlarm();
      return Response.json({ error: "Sandbox provisioning failed" }, { status: 502 });
    }
  }

  async alarm() {
    const status = await this.ctx.storage.get("status");
    if (this.ctx.container.running && (status === "active" || status === "creating")) {
      await this.ctx.container.setInactivityTimeout(INACTIVITY_MS);
      await this.ctx.storage.setAlarm(Date.now() + ALARM_MS);
    }
  }
}

export default {
  async fetch(request, env) {
    const encoder = new TextEncoder();
    const supplied = encoder.encode(request.headers.get("Authorization") ?? "");
    const expected = encoder.encode(`Bearer ${env.DRUKBOX_TOKEN}`);
    if (
      !env.DRUKBOX_TOKEN || supplied.byteLength !== expected.byteLength ||
      !crypto.subtle.timingSafeEqual(supplied, expected)
    ) {
      return new Response(null, { status: 401 });
    }
    const url = new URL(request.url);
    if (url.pathname === "/health" && request.method === "GET") {
      return Response.json({ status: "ok" });
    }
    const match = /^\/sandboxes\/([a-z0-9-]{1,100})$/.exec(url.pathname);
    if (match) return env.SANDBOXES.getByName(match[1]).fetch(request);
    return new Response(null, { status: 404 });
  },
};
