import "@matter/nodejs";
import { Environment } from "@matter/main";
import Fastify from "fastify";
import { registerRoutes } from "./api/routes.js";
import { deviceManager } from "./matter/deviceManager.js";
import { listDevices, DATA_DIR } from "./store.js";

const HOST = process.env.HOST ?? "127.0.0.1";
const PORT = Number(process.env.PORT ?? 3081);

function configureMatterNetworking(): void {
  const iface = process.env.MATTER_MDNS_NETWORK_INTERFACE?.trim() || "";
  if (iface) {
    Environment.default.vars.set("mdns.networkInterface", iface);
    console.log(`[bridge] Matter mDNS bound to interface: ${iface}`);
  } else {
    console.log("[bridge] Matter mDNS using system default interface");
  }
  if (process.env.MATTER_MDNS_IPV4 !== "0") {
    Environment.default.vars.set("mdns.ipv4", true);
  }
}

async function main(): Promise<void> {
  configureMatterNetworking();
  console.log(`[bridge] data directory: ${DATA_DIR}`);

  const devices = await listDevices();
  await deviceManager.startAll(devices);

  const app = Fastify({ logger: false });
  app.addContentTypeParser("application/json", { parseAs: "string" }, (req, body, done) => {
    try {
      const text = typeof body === "string" ? body : body?.toString?.() ?? "";
      done(null, text.trim() ? JSON.parse(text) : {});
    } catch (err) {
      done(err as Error, undefined);
    }
  });
  await registerRoutes(app);

  const shutdown = async (signal: string) => {
    console.log(`[bridge] ${signal}, shutting down...`);
    await app.close();
    await deviceManager.shutdown();
    process.exit(0);
  };

  process.on("SIGINT", () => void shutdown("SIGINT"));
  process.on("SIGTERM", () => void shutdown("SIGTERM"));

  await app.listen({ host: HOST, port: PORT });
  console.log(`[bridge] Matter API listening on http://${HOST}:${PORT}`);
}

main().catch((err) => {
  console.error("[bridge] fatal:", err);
  process.exit(1);
});
