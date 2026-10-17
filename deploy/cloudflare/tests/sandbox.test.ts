import { beforeEach, expect, test, vi } from "vitest";
import worker, { Sandbox } from "../src/index";

const values = new Map<string, unknown>();
const storage = {
  get: vi.fn(async (key: string) => values.get(key)),
  put: vi.fn(async (key: string, value: unknown) => { values.set(key, value); }),
  setAlarm: vi.fn(),
  deleteAlarm: vi.fn(),
};
const container = {
  images: { base: "registry/base@sha256:123" },
  running: false,
  start: vi.fn(() => { container.running = true; }),
  destroy: vi.fn(async () => { container.running = false; }),
  exec: vi.fn(async () => ({ exitCode: Promise.resolve(0) })),
  setInactivityTimeout: vi.fn(),
};
const context = { storage, container, blockConcurrencyWhile: async (callback: () => unknown) => callback() };
const getByName = vi.fn();
const env = { DRUKBOX_TOKEN: "test-token", SANDBOXES: { getByName } };
const configuration = { image: "base", instance: "standard-1", script: "echo ready", label: "drukbox" };

function request(method = "PUT", body: unknown = configuration): Request {
  return new Request("https://worker.test/sandboxes/sb-test", {
    method,
    headers: { Authorization: "Bearer test-token" },
    ...(method === "PUT" ? { body: JSON.stringify(body) } : {}),
  });
}

function sandbox(): Sandbox {
  return new Sandbox(context as unknown as DurableObjectState, env as never);
}

beforeEach(() => {
  vi.clearAllMocks();
  values.clear();
  container.running = false;
  container.exec.mockImplementation(async () => ({ exitCode: Promise.resolve(0) }));
});

test("authentication precedes access to a sandbox", async () => {
  const response = await worker.fetch(new Request("https://worker.test/sandboxes/sb-test"), env as never);
  expect(response.status).toBe(401);
  expect(getByName).not.toHaveBeenCalled();
});

test("health does not create a container", async () => {
  const response = await worker.fetch(new Request("https://worker.test/health", {
    headers: { Authorization: "Bearer test-token" },
  }), env as never);
  expect(response.status).toBe(200);
  expect(getByName).not.toHaveBeenCalled();
});

test("routes stable names to their Durable Object", async () => {
  const fetch = vi.fn(async () => new Response(null, { status: 204 }));
  getByName.mockReturnValue({ fetch });
  expect((await worker.fetch(request("DELETE"), env as never)).status).toBe(204);
  expect(getByName).toHaveBeenCalledWith("sb-test");
  expect(fetch).toHaveBeenCalledOnce();
});

test.each([null, {}, { ...configuration, image: "untrusted/image" }, { ...configuration, instance: "huge" }])(
  "rejects invalid configuration before starting a container: %j", async body => {
    expect((await sandbox().fetch(request("PUT", body))).status).toBe(400);
    expect(container.start).not.toHaveBeenCalled();
  },
);

test("creates once, executes bootstrap, and keeps the running VM alive", async () => {
  const instance = sandbox();
  expect((await instance.fetch(request())).status).toBe(201);
  expect(values.get("status")).toBe("active");
  expect(container.start).toHaveBeenCalledWith({
    image: container.images.base, instance: "standard-1", entrypoint: ["sleep", "infinity"],
    enableInternet: true, labels: { "managed-by": "drukbox" },
  });
  expect(container.exec).toHaveBeenCalledWith(["bash", "-e", "-c", "echo ready"], expect.objectContaining({
    stdout: "ignore", stderr: "ignore",
  }));
  expect((await instance.fetch(request())).status).toBe(409);
  expect(container.start).toHaveBeenCalledOnce();
  await instance.alarm();
  expect(storage.setAlarm).toHaveBeenCalledTimes(2);
});

test("bootstrap failure destroys the VM", async () => {
  container.exec.mockResolvedValue({ exitCode: Promise.resolve(1) });
  expect((await sandbox().fetch(request())).status).toBe(502);
  expect(container.destroy).toHaveBeenCalledOnce();
  expect(storage.deleteAlarm).toHaveBeenCalledOnce();
  expect(values.get("status")).toBe("error");
});

test("delete during bootstrap prevents activation", async () => {
  let finish!: (code: number) => void;
  container.exec.mockResolvedValue({ exitCode: new Promise(resolve => { finish = resolve; }) });
  const instance = sandbox();
  const creation = instance.fetch(request());
  await vi.waitFor(() => expect(container.exec).toHaveBeenCalledOnce());
  expect((await instance.fetch(request("DELETE"))).status).toBe(200);
  finish(0);
  expect((await creation).status).toBe(409);
  expect(values.get("status")).toBe("deleted");
  expect(container.running).toBe(false);
});

test("delete is repeatable and a later alarm cannot restart the VM", async () => {
  const instance = sandbox();
  await instance.fetch(request());
  await instance.fetch(request("DELETE"));
  await instance.fetch(request("DELETE"));
  await instance.alarm();
  expect(container.start).toHaveBeenCalledOnce();
  expect(values.get("status")).toBe("deleted");
  expect((await instance.fetch(request())).status).toBe(409);
});

test("a stopped VM is not silently recreated", async () => {
  values.set("status", "active");
  await sandbox().alarm();
  expect(values.get("status")).toBe("stopped");
  expect(container.start).not.toHaveBeenCalled();
});
