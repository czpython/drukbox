const { test, expect } = require("@playwright/test");

const { expectJson, expectObject, expectStatus } = require("./helpers/assertions");
const { newPublicContext, newServiceContext } = require("./helpers/client");
const { hostEnv } = require("./helpers/data");
const { settings } = require("./helpers/env");
const { pollUntil } = require("./helpers/poll");

const EXPECTED_OPENAPI_OPERATIONS = [
  "DELETE /http-proxies/{name}",
  "DELETE /http-proxies/{name}/hosts/{host_id}",
  "DELETE /hosts/{host_id}",
  "DELETE /templates/{template_id}",
  "GET /doctor",
  "GET /hosts",
  "GET /hosts/{host_id}",
  "GET /templates",
  "GET /templates/{template_id}",
  "POST /http-proxies",
  "POST /http-proxies/{name}/hosts/{host_id}",
  "POST /hosts",
  "POST /hosts/{host_id}/renew",
  "POST /templates",
];

const HOST_KEYS = [
  "id",
  "name",
  "status",
  "provider",
  "image",
  "external_ssh_host",
  "external_ssh_port",
  "internal_ssh_host",
  "known_hosts",
  "tailscale_device_id",
  "last_error",
  "created_at",
  "updated_at",
  "activated_at",
  "expires_at",
];

const RESERVED_ENV_KEYS = ["TAILSCALE_AUTHKEY"];

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const HTTP_PROXY_TARGET = "https://httpbin.org";

// Account-bound HTTP proxies exist only on exe.dev. The lifecycle test skips
// for all other providers.
const HTTP_PROXY_PROVIDERS = new Set(["exe"]);

test.describe.configure({ mode: "serial" });

test.describe("Drukbox API", () => {
  let api;
  let badTokenApi;
  let publicApi;
  let createdHost;
  let createdProxyName;
  let createdTemplate;
  let config;

  test.beforeAll(async () => {
    config = settings();
    api = await newServiceContext();
    badTokenApi = await newServiceContext("wrong-service-token");
    publicApi = await newPublicContext();
  });

  test.afterAll(async () => {
    if (api && createdProxyName) {
      try {
        await api.delete(`/http-proxies/${createdProxyName}`);
      } catch {}
    }

    if (api && createdHost?.id) {
      try {
        await api.delete(`/hosts/${createdHost.id}`);
      } catch {}
    }

    if (api && createdTemplate?.id) {
      try {
        await api.delete(`/templates/${createdTemplate.id}`);
      } catch {}
    }

    await api?.dispose();
    await badTokenApi?.dispose();
    await publicApi?.dispose();
  });

  test("openapi exposes the covered API surface", async () => {
    const schema = await expectJson(await publicApi.get("/openapi.json"), 200);
    expectObject(schema, ["openapi", "paths"]);

    const operations = [];

    for (const [path, pathItem] of Object.entries(schema.paths)) {
      for (const method of Object.keys(pathItem)) {
        if (["delete", "get", "patch", "post", "put"].includes(method)) {
          operations.push(`${method.toUpperCase()} ${path}`);
        }
      }
    }

    expect(operations.sort()).toEqual([...EXPECTED_OPENAPI_OPERATIONS].sort());
  });

  test("GET /hosts requires auth and returns hosts with service auth", async () => {
    await expectStatus(await publicApi.get("/hosts"), 401);

    const hosts = await expectJson(await api.get("/hosts"), 200);
    expect(Array.isArray(hosts)).toBe(true);

    for (const host of hosts) {
      expectHost(host);
      expect(host).not.toHaveProperty("env");
    }
  });

  test("POST /hosts rejects missing and bad service auth", async () => {
    await expectStatus(await publicApi.post("/hosts", { data: { env: {} } }), 401);
    await expectStatus(await badTokenApi.post("/hosts", { data: { env: {} } }), 403);
  });

  test("POST /hosts rejects reserved environment keys", async () => {
    for (const key of RESERVED_ENV_KEYS) {
      const payload = await expectJson(
        await api.post("/hosts", {
          data: { env: { [key]: "caller-value" } },
        }),
        422,
      );

      expect(Array.isArray(payload.detail)).toBe(true);
      expect(payload.detail[0].loc).toEqual(["body", "env"]);
      expect(payload.detail[0].msg).toContain(`reserved env keys are not allowed: ${key}`);
    }
  });

  test("POST /hosts creates a host inline and returns it active", async () => {
    test.setTimeout(config.hostActiveTimeoutMs);
    const data = { env: hostEnv() };
    if (config.hostImage) {
      data.image = config.hostImage;
    }

    data.secrets = { anthropic: { value: "black-box-static-secret" } };
    createdHost = await expectJson(
      await api.post("/hosts", {
        data,
        timeout: config.hostActiveTimeoutMs,
      }),
      201,
    );

    expectHost(createdHost);
    expectConfiguredHostImage(createdHost, config);
    expect(createdHost.name).toMatch(/^sb-/);
    expect(createdHost.status).toBe("active");
    expect(createdHost.known_hosts.length).toBeGreaterThan(0);
    expect(createdHost.provider).toBe(config.expectedProvider);
    expect(createdHost.activated_at).not.toBeNull();
    expect(createdHost).not.toHaveProperty("env");
    expect(createdHost).not.toHaveProperty("secrets");
  });

  test("GET /hosts/{host_id} returns host details and 404 for missing hosts", async () => {
    const host = await expectJson(await api.get(`/hosts/${createdHost.id}`), 200);
    expectHost(host);
    expect(host.id).toBe(createdHost.id);
    expectConfiguredHostImage(host, config);
    expect(host).not.toHaveProperty("env");

    const missingId = "00000000-0000-0000-0000-000000000000";
    const missing = await expectJson(await api.get(`/hosts/${missingId}`), 404);
    expect(missing.detail).toBe("host not found");
  });

  test("created host is observably active", async () => {
    test.setTimeout(config.hostActiveTimeoutMs);

    createdHost = await pollUntil(
      async () => {
        const host = await expectJson(await api.get(`/hosts/${createdHost.id}`), 200);
        expectHost(host);

        if (host.status === "error") {
          throw new Error(`host entered error state: ${host.last_error || "no last_error"}`);
        }
        return host.status === "active" && host.activated_at ? host : null;
      },
      {
        timeoutMs: config.hostActiveTimeoutMs,
        intervalMs: config.hostPollIntervalMs,
        message: "created host did not become active",
      },
    );
  });

  test("http proxy lifecycle works against a real host", async () => {
    test.skip(
      !HTTP_PROXY_PROVIDERS.has(config.expectedProvider),
      `http proxies are not supported by provider "${config.expectedProvider}"`,
    );
    createdProxyName = `gmail-mcp-${Date.now().toString(36)}`;

    await expectStatus(
      await publicApi.post("/http-proxies", {
        data: {
          name: createdProxyName,
          target: HTTP_PROXY_TARGET,
          headers: { Authorization: "Bearer token" },
        },
      }),
      401,
    );
    await expectStatus(
      await badTokenApi.post("/http-proxies", {
        data: {
          name: createdProxyName,
          target: HTTP_PROXY_TARGET,
          headers: { Authorization: "Bearer token" },
        },
      }),
      403,
    );

    const createdProxy = await expectJson(
      await api.post("/http-proxies", {
        data: {
          name: createdProxyName,
          target: HTTP_PROXY_TARGET,
          headers: { Authorization: "Bearer token" },
        },
      }),
      201,
    );
    expectObject(createdProxy, ["name", "status"]);
    expect(createdProxy.name).toBe(createdProxyName);
    expect(createdProxy.status).toBe("created");

    const attachedProxy = await expectJson(
      await api.post(`/http-proxies/${createdProxyName}/hosts/${createdHost.id}`),
      200,
    );
    expectObject(attachedProxy, ["name", "host_id", "status"]);
    expect(attachedProxy.name).toBe(createdProxyName);
    expect(attachedProxy.host_id).toBe(createdHost.id);
    expect(attachedProxy.status).toBe("attached");

    await expectStatus(
      await api.delete(`/http-proxies/${createdProxyName}/hosts/${createdHost.id}`),
      204,
    );
    await expectStatus(await api.delete(`/http-proxies/${createdProxyName}`), 204);

    createdProxyName = null;
  });

  test("POST /hosts/{host_id}/renew extends the host lease", async () => {
    await expectStatus(await publicApi.post(`/hosts/${createdHost.id}/renew`), 401);
    await expectStatus(await badTokenApi.post(`/hosts/${createdHost.id}/renew`), 403);

    const renewed = await expectJson(await api.post(`/hosts/${createdHost.id}/renew`), 200);
    expectHost(renewed);
    expect(renewed.id).toBe(createdHost.id);
    expect(Date.parse(renewed.expires_at)).toBeGreaterThan(Date.now());

    const missingId = "00000000-0000-0000-0000-000000000000";
    const missing = await expectJson(await api.post(`/hosts/${missingId}/renew`), 404);
    expect(missing.detail).toBe("host not found");
  });

  test("DELETE /hosts/{host_id} tears down the created host", async () => {
    await expectStatus(await publicApi.delete(`/hosts/${createdHost.id}`), 401);
    await expectStatus(await badTokenApi.delete(`/hosts/${createdHost.id}`), 403);
    await expectStatus(await api.delete(`/hosts/${createdHost.id}`), 204);

    const missing = await expectJson(await api.get(`/hosts/${createdHost.id}`), 404);
    expect(missing.detail).toBe("host not found");
  });

  test("template lifecycle: build, fork a host, delete", async () => {
    test.setTimeout(config.hostActiveTimeoutMs * 2);

    createdTemplate = await expectJson(
      await api.post("/templates", {
        data: { setup_script: "printf drukbox > /drukbox-template-marker\n" },
      }),
      202,
    );
    expect(createdTemplate.id).toMatch(UUID_PATTERN);
    expect(createdTemplate.status).toBe("building");
    expect(createdTemplate.image).toBe("");
    expect(createdTemplate).not.toHaveProperty("setup_script");

    const built = await pollUntil(
      async () => {
        const template = await expectJson(
          await api.get(`/templates/${createdTemplate.id}`),
          200,
        );
        return template.status === "building" ? null : template;
      },
      { timeoutMs: config.hostActiveTimeoutMs, message: "template build did not finish" },
    );
    expect(built.status, built.last_error).toBe("available");
    expect(built.image).not.toBe("");

    const forked = await expectJson(
      await api.post("/hosts", {
        data: { template: createdTemplate.id },
        timeout: config.hostActiveTimeoutMs,
      }),
      201,
    );
    expect(forked.image).toBe(built.image);
    expect(forked.status).toBe("active");
    await expectStatus(await api.delete(`/hosts/${forked.id}`), 204);

    await expectStatus(await api.delete(`/templates/${createdTemplate.id}`), 204);
    await expectStatus(await api.get(`/templates/${createdTemplate.id}`), 404);
    createdTemplate = null;
  });
});

function expectHost(host) {
  expectObject(host, HOST_KEYS);
  expect(host.id).toMatch(UUID_PATTERN);
  expect(typeof host.name).toBe("string");
  expect(typeof host.status).toBe("string");
  expect(typeof host.provider).toBe("string");
  expect(typeof host.image).toBe("string");
  expect(typeof host.external_ssh_host).toBe("string");
  expect(typeof host.external_ssh_port).toBe("number");
  if (host.internal_ssh_host !== null) {
    expect(typeof host.internal_ssh_host).toBe("string");
  }
  expect(typeof host.known_hosts).toBe("string");
  if (host.tailscale_device_id !== null) {
    expect(typeof host.tailscale_device_id).toBe("string");
  }
  expect(typeof host.last_error).toBe("string");
  expect(Date.parse(host.created_at)).not.toBeNaN();
  expect(Date.parse(host.updated_at)).not.toBeNaN();

  if (host.activated_at !== null) {
    expect(Date.parse(host.activated_at)).not.toBeNaN();
  }
}

function expectConfiguredHostImage(host, config) {
  if (config.expectedHostImage) {
    expect(host.image).toBe(config.expectedHostImage);
  } else {
    expect(host.image.length).toBeGreaterThan(0);
  }
}
