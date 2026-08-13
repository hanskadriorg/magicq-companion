export type DeviceType = "onoff" | "rgb";
export type OutputProtocol = "artnet" | "sacn";

export interface OnOffChannels {
  channel: number;
}

export interface RgbChannels {
  intensity: number;
  red: number;
  green: number;
  blue: number;
}

export interface DeviceRecord {
  id: string;
  name: string;
  type: DeviceType;
  /** artnet or sacn */
  protocol: OutputProtocol;
  /** Art-Net: UDP broadcast 255.255.255.255. sACN: universe multicast. */
  broadcast: boolean;
  /** Unicast destination IP (ignored when broadcast=true for Art-Net; optional override for sACN unicast). */
  artnetHost: string;
  universe: number;
  channels: OnOffChannels | RgbChannels;
  matterPort: number;
  passcode: number;
  discriminator: number;
  uniqueId: string;
  createdAt: string;
  updatedAt: string;
}

export interface OutputTarget {
  protocol: OutputProtocol;
  broadcast: boolean;
  host: string;
  universe: number;
}

export interface CreateOnOffDeviceInput {
  name: string;
  protocol?: OutputProtocol;
  broadcast?: boolean;
  artnetHost: string;
  universe: number;
  channel: number;
}

export interface CreateRgbDeviceInput {
  name: string;
  protocol?: OutputProtocol;
  broadcast?: boolean;
  artnetHost: string;
  universe: number;
  intensityChannel: number;
  redChannel: number;
  greenChannel: number;
  blueChannel: number;
}

export interface UpdateDeviceInput {
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
}

export interface DeviceStatus {
  commissioned: boolean;
  online: boolean;
  qrPairingCode?: string;
  manualPairingCode?: string;
  qrDataUrl?: string;
}

export interface DeviceView extends DeviceRecord {
  status: DeviceStatus;
}

export function isOnOffChannels(channels: OnOffChannels | RgbChannels): channels is OnOffChannels {
  return "channel" in channels;
}

export function isRgbChannels(channels: OnOffChannels | RgbChannels): channels is RgbChannels {
  return "red" in channels && "green" in channels && "blue" in channels && "intensity" in channels;
}

/** Normalize older saved devices that lack protocol/broadcast. */
export function normalizeDevice(raw: Partial<DeviceRecord> & Pick<DeviceRecord, "id" | "name" | "type">): DeviceRecord {
  return {
    protocol: raw.protocol === "sacn" ? "sacn" : "artnet",
    broadcast: Boolean(raw.broadcast),
    artnetHost: raw.artnetHost ?? "255.255.255.255",
    universe: raw.universe ?? 0,
    channels: raw.channels ?? { channel: 1 },
    matterPort: raw.matterPort ?? 5540,
    passcode: raw.passcode ?? 20202021,
    discriminator: raw.discriminator ?? 3840,
    uniqueId: raw.uniqueId ?? raw.id.replace(/-/g, "").slice(0, 32),
    createdAt: raw.createdAt ?? new Date().toISOString(),
    updatedAt: raw.updatedAt ?? new Date().toISOString(),
    id: raw.id,
    name: raw.name,
    type: raw.type,
  };
}
