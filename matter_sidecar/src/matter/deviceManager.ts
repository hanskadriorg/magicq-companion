import { Environment, ServerNode } from "@matter/main";
import type { Endpoint } from "@matter/main";
import { promises as fs } from "node:fs";
import QRCode from "qrcode";
import { startDmxOutput, stopDmxOutput, bindDeviceRecord, unbindDeviceRecord } from "../dmxOutput.js";
import { matterStoragePathForDevice, MATTER_STORAGE_DIR } from "../store.js";
import type { DeviceRecord, DeviceStatus } from "../types.js";
import { createColorServerNode, rebindColorOutput } from "./colorDevice.js";
import { createOnOffServerNode, rebindOnOffOutput } from "./onOffDevice.js";

interface RuntimeDevice {
  record: DeviceRecord;
  server: ServerNode;
  endpoint: Endpoint;
}

export class DeviceManager {
  private readonly devices = new Map<string, RuntimeDevice>();
  private storageConfigured = false;

  private configureStorage(): void {
    if (this.storageConfigured) {
      return;
    }
    Environment.default.vars.set("storage.path", MATTER_STORAGE_DIR);
    this.storageConfigured = true;
  }

  async startAll(records: DeviceRecord[]): Promise<void> {
    this.configureStorage();
    await startDmxOutput();
    for (const record of records) {
      try {
        await this.startDevice(record);
      } catch (err) {
        console.error(`[manager] Failed to start device ${record.name}:`, err);
      }
    }
  }

  async startDevice(record: DeviceRecord): Promise<void> {
    if (this.devices.has(record.id)) {
      return;
    }

    this.configureStorage();
    await fs.mkdir(matterStoragePathForDevice(record), { recursive: true });

    const created =
      record.type === "onoff"
        ? await createOnOffServerNode(record)
        : await createColorServerNode(record);

    await created.server.start();
    this.devices.set(record.id, {
      record,
      server: created.server,
      endpoint: created.endpoint,
    });

    const status = await this.getStatus(record.id);
    console.log(
      `[manager] Started "${record.name}" (${record.type}/${record.protocol ?? "artnet"}) on Matter port ${record.matterPort}` +
        (status.commissioned ? " [commissioned]" : ` [pairing ${status.manualPairingCode}]`),
    );
  }

  async stopDevice(id: string): Promise<void> {
    const runtime = this.devices.get(id);
    if (!runtime) {
      return;
    }
    try {
      await runtime.server.cancel();
    } catch (err) {
      console.error(`[manager] Error stopping device ${id}:`, err);
    }
    this.devices.delete(id);
    unbindDeviceRecord(id);
  }

  async deleteDevice(id: string): Promise<void> {
    const runtime = this.devices.get(id);
    const storagePath = runtime ? matterStoragePathForDevice(runtime.record) : undefined;

    await this.stopDevice(id);

    if (storagePath) {
      await fs.rm(storagePath, { recursive: true, force: true });
    }

    try {
      const entries = await fs.readdir(MATTER_STORAGE_DIR);
      for (const entry of entries) {
        if (entry.includes(id) || (runtime && entry.includes(runtime.record.uniqueId))) {
          await fs.rm(`${MATTER_STORAGE_DIR}/${entry}`, { recursive: true, force: true });
        }
      }
    } catch {
      // ignore cleanup errors
    }
  }

  updateRecord(record: DeviceRecord): void {
    const runtime = this.devices.get(record.id);
    if (!runtime) {
      return;
    }
    runtime.record = record;
    bindDeviceRecord(record);
    if (record.type === "onoff") {
      rebindOnOffOutput(record, runtime.endpoint);
    } else {
      rebindColorOutput(record, runtime.endpoint);
    }
  }

  async getStatus(id: string): Promise<DeviceStatus> {
    const runtime = this.devices.get(id);
    if (!runtime) {
      return { commissioned: false, online: false };
    }

    const commissioned = runtime.server.lifecycle.isCommissioned;
    const status: DeviceStatus = {
      commissioned,
      online: true,
    };

    if (!commissioned) {
      try {
        const { qrPairingCode, manualPairingCode } = runtime.server.state.commissioning.pairingCodes;
        status.qrPairingCode = qrPairingCode;
        status.manualPairingCode = manualPairingCode;
        status.qrDataUrl = await QRCode.toDataURL(qrPairingCode, {
          margin: 1,
          width: 280,
          errorCorrectionLevel: "M",
        });
      } catch (err) {
        console.error(`[manager] pairing codes unavailable for ${id}:`, err);
      }
    }

    return status;
  }

  async openCommissioningWindow(id: string): Promise<DeviceStatus> {
    const runtime = this.devices.get(id);
    if (!runtime) {
      throw new Error(`Device not running: ${id}`);
    }

    const record = runtime.record;
    try {
      await runtime.server.erase();
    } catch (err) {
      console.warn(`[manager] erase failed for ${id}, restarting node:`, err);
    }

    await this.stopDevice(id);
    await this.startDevice(record);
    return this.getStatus(id);
  }

  async shutdown(): Promise<void> {
    const ids = [...this.devices.keys()];
    for (const id of ids) {
      await this.stopDevice(id);
    }
    await stopDmxOutput();
  }
}

export const deviceManager = new DeviceManager();
