import { promises as fs } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { randomInt } from "node:crypto";
import { v4 as uuidv4 } from "uuid";
import type {
  CreateOnOffDeviceInput,
  CreateRgbDeviceInput,
  DeviceRecord,
  OutputProtocol,
  UpdateDeviceInput,
} from "./types.js";
import { normalizeDevice } from "./types.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
export const DATA_DIR = path.resolve(
  process.env.DATA_DIR?.trim() || path.join(__dirname, "..", "data"),
);
export const DEVICES_FILE = path.join(DATA_DIR, "devices.json");
export const MATTER_STORAGE_DIR = path.join(DATA_DIR, "matter");

const MATTER_PORT_BASE = 5540;
const MATTER_PORT_MAX = 5640;

interface StoreFile {
  devices: DeviceRecord[];
}

async function ensureDataDir(): Promise<void> {
  await fs.mkdir(DATA_DIR, { recursive: true });
  await fs.mkdir(MATTER_STORAGE_DIR, { recursive: true });
}

async function readStore(): Promise<StoreFile> {
  await ensureDataDir();
  try {
    const raw = await fs.readFile(DEVICES_FILE, "utf8");
    const parsed = JSON.parse(raw) as StoreFile;
    if (!Array.isArray(parsed.devices)) {
      return { devices: [] };
    }
    return { devices: parsed.devices.map((d) => normalizeDevice(d)) };
  } catch (err) {
    const code = (err as NodeJS.ErrnoException).code;
    if (code === "ENOENT") {
      return { devices: [] };
    }
    throw err;
  }
}

async function writeStore(store: StoreFile): Promise<void> {
  await ensureDataDir();
  const tmp = `${DEVICES_FILE}.tmp`;
  await fs.writeFile(tmp, JSON.stringify(store, null, 2), "utf8");
  await fs.rename(tmp, DEVICES_FILE);
}

function allocatePort(devices: DeviceRecord[]): number {
  const used = new Set(devices.map((d) => d.matterPort));
  for (let port = MATTER_PORT_BASE; port <= MATTER_PORT_MAX; port++) {
    if (!used.has(port)) {
      return port;
    }
  }
  throw new Error(`No free Matter ports left in range ${MATTER_PORT_BASE}-${MATTER_PORT_MAX}`);
}

function randomPasscode(): number {
  for (let i = 0; i < 20; i++) {
    const code = randomInt(1, 99999998);
    const invalid = new Set([
      0, 11111111, 22222222, 33333333, 44444444, 55555555, 66666666, 77777777, 88888888, 99999999,
      12345678, 87654321,
    ]);
    if (!invalid.has(code)) {
      return code;
    }
  }
  return 20202021;
}

function randomDiscriminator(): number {
  return randomInt(0, 4096);
}

function validateChannel(value: number, label: string): number {
  if (!Number.isInteger(value) || value < 1 || value > 512) {
    throw new Error(`${label} must be an integer between 1 and 512`);
  }
  return value;
}

function validateProtocol(value: unknown): OutputProtocol {
  if (value === "sacn") {
    return "sacn";
  }
  return "artnet";
}

function validateUniverse(value: number, protocol: OutputProtocol): number {
  if (protocol === "sacn") {
    if (!Number.isInteger(value) || value < 1 || value > 63999) {
      throw new Error("sACN universe must be an integer between 1 and 63999");
    }
    return value;
  }
  if (!Number.isInteger(value) || value < 0 || value > 32767) {
    throw new Error("Art-Net universe must be an integer between 0 and 32767");
  }
  return value;
}

function validateHost(host: string, broadcast: boolean): string {
  if (broadcast) {
    return host.trim() || "255.255.255.255";
  }
  const trimmed = host.trim();
  if (!trimmed) {
    throw new Error("Destination IP is required unless broadcast is enabled");
  }
  return trimmed;
}

function validateName(name: string): string {
  const trimmed = name.trim();
  if (!trimmed) {
    throw new Error("name is required");
  }
  if (trimmed.length > 64) {
    throw new Error("name must be 64 characters or fewer");
  }
  return trimmed;
}

export async function listDevices(): Promise<DeviceRecord[]> {
  const store = await readStore();
  return store.devices;
}

export async function getDevice(id: string): Promise<DeviceRecord | undefined> {
  const store = await readStore();
  return store.devices.find((d) => d.id === id);
}

export async function createOnOffDevice(input: CreateOnOffDeviceInput): Promise<DeviceRecord> {
  const store = await readStore();
  const now = new Date().toISOString();
  const protocol = validateProtocol(input.protocol);
  const broadcast = Boolean(input.broadcast);
  const device: DeviceRecord = {
    id: uuidv4(),
    name: validateName(input.name),
    type: "onoff",
    protocol,
    broadcast,
    artnetHost: validateHost(input.artnetHost, broadcast),
    universe: validateUniverse(input.universe, protocol),
    channels: { channel: validateChannel(input.channel, "channel") },
    matterPort: allocatePort(store.devices),
    passcode: randomPasscode(),
    discriminator: randomDiscriminator(),
    uniqueId: uuidv4().replace(/-/g, "").slice(0, 32),
    createdAt: now,
    updatedAt: now,
  };
  store.devices.push(device);
  await writeStore(store);
  return device;
}

export async function createRgbDevice(input: CreateRgbDeviceInput): Promise<DeviceRecord> {
  const store = await readStore();
  const now = new Date().toISOString();
  const protocol = validateProtocol(input.protocol);
  const broadcast = Boolean(input.broadcast);
  const device: DeviceRecord = {
    id: uuidv4(),
    name: validateName(input.name),
    type: "rgb",
    protocol,
    broadcast,
    artnetHost: validateHost(input.artnetHost, broadcast),
    universe: validateUniverse(input.universe, protocol),
    channels: {
      intensity: validateChannel(input.intensityChannel, "intensityChannel"),
      red: validateChannel(input.redChannel, "redChannel"),
      green: validateChannel(input.greenChannel, "greenChannel"),
      blue: validateChannel(input.blueChannel, "blueChannel"),
    },
    matterPort: allocatePort(store.devices),
    passcode: randomPasscode(),
    discriminator: randomDiscriminator(),
    uniqueId: uuidv4().replace(/-/g, "").slice(0, 32),
    createdAt: now,
    updatedAt: now,
  };
  store.devices.push(device);
  await writeStore(store);
  return device;
}

export async function updateDevice(id: string, input: UpdateDeviceInput): Promise<DeviceRecord> {
  const store = await readStore();
  const index = store.devices.findIndex((d) => d.id === id);
  if (index < 0) {
    throw new Error(`Device not found: ${id}`);
  }

  const current = store.devices[index];
  const protocol =
    input.protocol !== undefined ? validateProtocol(input.protocol) : current.protocol ?? "artnet";
  const broadcast = input.broadcast !== undefined ? Boolean(input.broadcast) : Boolean(current.broadcast);
  const updated: DeviceRecord = {
    ...current,
    protocol,
    broadcast,
    name: input.name !== undefined ? validateName(input.name) : current.name,
    artnetHost:
      input.artnetHost !== undefined
        ? validateHost(input.artnetHost, broadcast)
        : validateHost(current.artnetHost, broadcast),
    universe:
      input.universe !== undefined
        ? validateUniverse(input.universe, protocol)
        : validateUniverse(current.universe, protocol),
    updatedAt: new Date().toISOString(),
  };

  if (updated.type === "onoff") {
    const channel =
      input.channel !== undefined
        ? validateChannel(input.channel, "channel")
        : (current.channels as { channel: number }).channel;
    updated.channels = { channel };
  } else {
    const prev = current.channels as {
      intensity: number;
      red: number;
      green: number;
      blue: number;
    };
    updated.channels = {
      intensity:
        input.intensityChannel !== undefined
          ? validateChannel(input.intensityChannel, "intensityChannel")
          : prev.intensity,
      red: input.redChannel !== undefined ? validateChannel(input.redChannel, "redChannel") : prev.red,
      green:
        input.greenChannel !== undefined ? validateChannel(input.greenChannel, "greenChannel") : prev.green,
      blue:
        input.blueChannel !== undefined ? validateChannel(input.blueChannel, "blueChannel") : prev.blue,
    };
  }

  store.devices[index] = updated;
  await writeStore(store);
  return updated;
}

export async function deleteDevice(id: string): Promise<boolean> {
  const store = await readStore();
  const next = store.devices.filter((d) => d.id !== id);
  if (next.length === store.devices.length) {
    return false;
  }
  store.devices = next;
  await writeStore(store);
  return true;
}

export function matterStoragePathForDevice(device: DeviceRecord): string {
  return path.join(MATTER_STORAGE_DIR, device.id);
}
