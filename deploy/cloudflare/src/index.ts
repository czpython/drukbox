import { DurableObject } from "cloudflare:workers";

interface Env {
  DRUKBOX_TOKEN: string;
  SANDBOXES: DurableObjectNamespace<Sandbox>;
}

interface SandboxRequest {
  image: string;
  instance: "lite" | "standard-1" | "standard-2" | "standard-3" | "standard-4";
  script: string;
  label: string;
}

const INACTIVITY_MS = 5 * 60 * 1000;
const ALARM_MS = 60 * 1000;
const INSTANCES = new Set(["lite", "standard-1", "standard-2", "standard-3", "standard-4"]);

export class Sandbox extends DurableObject<Env> {
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    if (ctx.container?.running) {
      void ctx.blockConcurrencyWhile(() => ctx.container!.setInactivityTimeout(INACTIVITY_MS));
    }
  }

  async fetch(request: Request): Promise<Response> {
    const container = this.ctx.container!;
    if (request.method === "DELETE") {
      await this.ctx.storage.put("status", "deleted");
      await container.destroy();
      await this.ctx.storage.deleteAlarm();
      return Response.json({ status: "deleted" });
    }
    if (request.method === "GET") {
      const status = await this.ctx.storage.get<string>("status");
      return Response.json({ status: container.running ? status : "stopped" });
    }
    if (request.method !== "PUT") {
      return new Response(null, { status: 405 });
    }

    let body: SandboxRequest;
    try {
      body = await request.json<SandboxRequest>();
    } catch {
      return Response.json({ error: "Invalid JSON" }, { status: 400 });
    }
    if (
      !body || typeof body.image !== "string" || !Object.hasOwn(container.images, body.image) ||
      !INSTANCES.has(body.instance) || typeof body.script !== "string" ||
      body.script.length > 65536 || typeof body.label !== "string" ||
      body.label.length > 64
    ) {
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
      let timer: ReturnType<typeof setTimeout>;
      const deadline = new Promise<never>((_, reject) => {
        timer = setTimeout(() => {
          reject(new Error("Bootstrap timed out"));
          abort.abort();
        }, 120000);
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
        clearTimeout(timer!);
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

  async alarm(): Promise<void> {
    const status = await this.ctx.storage.get<string>("status");
    if (status === "active" || status === "creating") {
      if (this.ctx.container!.running) {
        await this.ctx.container!.setInactivityTimeout(INACTIVITY_MS);
        await this.ctx.storage.setAlarm(Date.now() + ALARM_MS);
      } else {
        await this.ctx.storage.put("status", "stopped");
      }
    }
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (!env.DRUKBOX_TOKEN || request.headers.get("Authorization") !== `Bearer ${env.DRUKBOX_TOKEN}`) {
      return new Response(null, { status: 401 });
    }
    const url = new URL(request.url);
    if (url.pathname === "/health" && request.method === "GET") {
      return Response.json({ status: "ok" });
    }
    const match = /^\/sandboxes\/([a-z0-9-]{1,100})$/.exec(url.pathname);
    if (!match) return new Response(null, { status: 404 });
    return env.SANDBOXES.getByName(match[1]).fetch(request);
  },
} satisfies ExportedHandler<Env>;
