import type { FastifyInstance } from "fastify";
import { deviceManager } from "../matter/deviceManager.js";
import {
  createOnOffDevice,
  createRgbDevice,
  deleteDevice,
  getDevice,
  listDevices,
  updateDevice,
} from "../store.js";
import type { DeviceView, OutputProtocol } from "../types.js";

async function toView(id: string): Promise<DeviceView | undefined> {
  const device = await getDevice(id);
  if (!device) {
    return undefined;
  }
  const status = await deviceManager.getStatus(id);
  return { ...device, status };
}

export async function registerRoutes(app: FastifyInstance): Promise<void> {
  app.get("/api/health", async () => ({ ok: true }));

  app.get("/api/devices", async () => {
    const devices = await listDevices();
    const views: DeviceView[] = [];
    for (const device of devices) {
      views.push({
        ...device,
        status: await deviceManager.getStatus(device.id),
      });
    }
    return { devices: views };
  });

  app.get<{ Params: { id: string } }>("/api/devices/:id", async (req, reply) => {
    const view = await toView(req.params.id);
    if (!view) {
      return reply.code(404).send({ error: "Device not found" });
    }
    return { device: view };
  });

  app.post<{
    Body: {
      type: "onoff" | "rgb";
      name: string;
      protocol?: OutputProtocol;
      broadcast?: boolean;
      artnetHost?: string;
      universe: number;
      channel?: number;
      intensityChannel?: number;
      redChannel?: number;
      greenChannel?: number;
      blueChannel?: number;
    };
  }>("/api/devices", async (req, reply) => {
    try {
      const body = req.body;
      if (!body?.type || !body.name) {
        return reply.code(400).send({ error: "type and name are required" });
      }

      // DMX destination is owned by the Python process (shared NIC, per-segment
      // protocol/universe). Keep placeholder fields so stored records stay valid.
      const common = {
        name: body.name,
        protocol: "artnet" as const,
        broadcast: true,
        artnetHost: "127.0.0.1",
        universe: Number.isFinite(Number(body.universe)) ? Number(body.universe) : 0,
      };

      let device;
      if (body.type === "onoff") {
        if (body.channel === undefined) {
          return reply.code(400).send({ error: "channel is required for onoff devices" });
        }
        device = await createOnOffDevice({
          ...common,
          channel: Number(body.channel),
        });
      } else if (body.type === "rgb") {
        const { intensityChannel, redChannel, greenChannel, blueChannel } = body;
        if (
          intensityChannel === undefined ||
          redChannel === undefined ||
          greenChannel === undefined ||
          blueChannel === undefined
        ) {
          return reply
            .code(400)
            .send({ error: "intensityChannel, redChannel, greenChannel, blueChannel are required" });
        }
        device = await createRgbDevice({
          ...common,
          intensityChannel: Number(intensityChannel),
          redChannel: Number(redChannel),
          greenChannel: Number(greenChannel),
          blueChannel: Number(blueChannel),
        });
      } else {
        return reply.code(400).send({ error: "type must be onoff or rgb" });
      }

      try {
        await deviceManager.startDevice(device);
      } catch (startErr) {
        await deviceManager.deleteDevice(device.id);
        await deleteDevice(device.id);
        throw startErr;
      }

      const view = await toView(device.id);
      return reply.code(201).send({ device: view });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      return reply.code(400).send({ error: message });
    }
  });

  app.patch<{
    Params: { id: string };
    Body: {
      name?: string;
      protocol?: OutputProtocol;
      broadcast?: boolean;
      artnetHost?: string;
      universe?: number;
      channel?: number;
      intensityChannel?: number;
      redChannel?: number;
      greenChannel?: number;
      blueChannel?: number;
    };
  }>("/api/devices/:id", async (req, reply) => {
    try {
      const existing = await getDevice(req.params.id);
      if (!existing) {
        return reply.code(404).send({ error: "Device not found" });
      }

      const updated = await updateDevice(req.params.id, {
        name: req.body.name,
        channel: req.body.channel !== undefined ? Number(req.body.channel) : undefined,
        intensityChannel:
          req.body.intensityChannel !== undefined ? Number(req.body.intensityChannel) : undefined,
        redChannel: req.body.redChannel !== undefined ? Number(req.body.redChannel) : undefined,
        greenChannel: req.body.greenChannel !== undefined ? Number(req.body.greenChannel) : undefined,
        blueChannel: req.body.blueChannel !== undefined ? Number(req.body.blueChannel) : undefined,
      });

      deviceManager.updateRecord(updated);
      return { device: await toView(updated.id) };
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      return reply.code(400).send({ error: message });
    }
  });

  app.delete<{ Params: { id: string } }>("/api/devices/:id", async (req, reply) => {
    try {
      const existing = await getDevice(req.params.id);
      if (!existing) {
        return reply.code(404).send({ error: "Device not found" });
      }
      await deviceManager.deleteDevice(req.params.id);
      await deleteDevice(req.params.id);
      return { ok: true };
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      return reply.code(400).send({ error: message });
    }
  });

  app.get<{ Params: { id: string } }>("/api/devices/:id/pairing", async (req, reply) => {
    const existing = await getDevice(req.params.id);
    if (!existing) {
      return reply.code(404).send({ error: "Device not found" });
    }
    const status = await deviceManager.getStatus(req.params.id);
    return { status };
  });

  app.post<{ Params: { id: string } }>("/api/devices/:id/pairing/reset", async (req, reply) => {
    try {
      const existing = await getDevice(req.params.id);
      if (!existing) {
        return reply.code(404).send({ error: "Device not found" });
      }
      const status = await deviceManager.openCommissioningWindow(req.params.id);
      return { status };
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      return reply.code(400).send({ error: message });
    }
  });
}
