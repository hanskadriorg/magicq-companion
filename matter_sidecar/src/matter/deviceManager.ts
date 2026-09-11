import { Environment, ServerNode } from "@matter/main";
import type { Endpoint } from "@matter/main";
import { promises as fs } from "node:fs";
import QRCode from "qrcode";
import { startDmxOutput, stopDmxOutput, bindDeviceRecord, unbindDeviceRecord } from "../dmxOutput.js";
import { matterStoragePathForDevice, MATTER_STORAGE_DIR } from "../store.js";
import type { DeviceRecord, DeviceStatus } from "../types.js";
import { createColorServerNode, rebindColorOutput } from "./colorDevice.js";
import { createDimmerServerNode, rebindDimmerOutput } from "./dimmerDevice.js";
import { createOnOffServerNode, rebindOnOffOutput } from "./onOffDevice.js";
import {
  createOutputSwitchServerNode,
  pushOutputState,
  rebindOutputSwitch,
} from "./outputSwitchDevice.js";

interface RuntimeDevice {
  record: DeviceRecord;
  server: ServerNode;
  endpoint: Endpoint;
  advertiseTimer?: ReturnType<typeof setInterval>;
}

/** Matter.js stops operational mDNS after 15 minutes, and skips announce while a
 *  subscription exists. Force-announce often so hubs can rediscover after IP/UDP loss. */
const ADVERTISE_KEEPALIVE_MS = 2 * 60 * 1000;

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
      record.type === "output"
        ? await createOutputSwitchServerNode(record)
        : record.type === "onoff"
          ? await createOnOffServerNode(record)
          : record.type === "dimmer"
            ? await createDimmerServerNode(record)
            : await createColorServerNode(record);

    await created.server.start();
    const runtime: RuntimeDevice = {
      record,
      server: created.server,
      endpoint: created.endpoint,
    };
    this.devices.set(record.id, runtime);
    this.#watchNode(runtime);

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
    if (runtime.advertiseTimer) {
      clearInterval(runtime.advertiseTimer);
      runtime.advertiseTimer = undefined;
    }
    try {
      await runtime.server.cancel();
    } catch (err) {
      console.error(`[manager] Error stopping device ${id}:`, err);
    }
    this.devices.delete(id);
    unbindDeviceRecord(id);
  }

  #watchNode(runtime: RuntimeDevice): void {
    const { server, record } = runtime;
    const refresh = () => {
      void refreshAdvertisement(server, record.name);
    };
    runtime.advertiseTimer = setInterval(refresh, ADVERTISE_KEEPALIVE_MS);
    try {
      server.lifecycle.offline.on(() => {
        if (!this.devices.has(record.id)) {
          return;
        }
        console.warn(`[manager] "${record.name}" went offline; advertising again`);
        refresh();
      });
    } catch {
      // older matter.js without lifecycle.offline
    }
    refresh();
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
    if (record.type === "output") {
      rebindOutputSwitch(record, runtime.endpoint);
    } else if (record.type === "onoff") {
      rebindOnOffOutput(record, runtime.endpoint);
    } else if (record.type === "dimmer") {
      rebindDimmerOutput(record, runtime.endpoint);
    } else {
      rebindColorOutput(record, runtime.endpoint);
    }
  }

  /** Mirror a dashboard output toggle onto every companion-output switch. */
  async syncOutputSwitches(enabled: boolean): Promise<void> {
    for (const runtime of this.devices.values()) {
      if (runtime.record.type !== "output") {
        continue;
      }
      try {
        await pushOutputState(runtime.endpoint, enabled);
      } catch (err) {
        console.error(`[manager] could not update "${runtime.record.name}":`, err);
      }
    }
  }

  async getStatus(id: string): Promise<DeviceStatus> {
    const runtime = this.devices.get(id);
    if (!runtime) {
      return { commissioned: false, online: false };
    }

    const commissioned = runtime.server.lifecycle.isCommissioned;
    const online = Boolean(
      (runtime.server.lifecycle as { isOnline?: boolean }).isOnline ?? true,
    );
    const status: DeviceStatus = {
      commissioned,
      online,
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

async function refreshAdvertisement(server: ServerNode, name: string): Promise<void> {
  try {
    const env = (server as unknown as { env?: { get?: (ctor: unknown) => unknown } }).env;
    if (!env?.get) {
      return;
    }
    const protocol = await import("@matter/protocol");
    const DeviceAdvertiser = protocol.DeviceAdvertiser;
    const FabricManager = protocol.FabricManager;
    const advertiser = env.get(DeviceAdvertiser) as
      | {
          startAdvertising?: () => Promise<void>;
          advertiseFabrics?: (fabrics: unknown[], expire?: boolean) => Promise<void>;
        }
      | undefined;
    if (!advertiser) {
      return;
    }
    // Reset the 15-minute announcement window.
    if (advertiser.startAdvertising) {
      await advertiser.startAdvertising();
    }
    // startAdvertising() skips mDNS while a (possibly dead) subscription exists.
    // advertiseFabrics() always broadcasts the operational record.
    if (advertiser.advertiseFabrics && FabricManager) {
      const fabrics = env.get(FabricManager) as { fabrics?: unknown[] } | undefined;
      if (Array.isArray(fabrics?.fabrics) && fabrics.fabrics.length) {
        await advertiser.advertiseFabrics(fabrics.fabrics);
      }
    }
    console.log(`[manager] refreshed Matter advertisement for "${name}"`);
  } catch (err) {
    console.warn(`[manager] advertisement refresh failed for "${name}":`, err);
  }
}
