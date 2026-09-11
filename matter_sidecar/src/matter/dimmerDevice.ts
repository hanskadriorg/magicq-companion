import { DeviceTypeId, Endpoint, ServerNode } from "@matter/main";
import { DimmableLightDevice } from "@matter/main/devices/dimmable-light";
import { matterLevelToDmx } from "../color.js";
import { bindDeviceRecord, setDeviceChannels } from "../dmxOutput.js";
import type { DeviceRecord, DimmerChannels } from "../types.js";
import { PRODUCT_IDS, basicInformation } from "./identity.js";

interface DimmerRuntimeState {
  onOff?: { onOff?: boolean };
  levelControl?: { currentLevel?: number };
}

function readState(endpoint: Endpoint): { on: boolean; level: number } {
  const state = endpoint.state as DimmerRuntimeState;
  return {
    on: Boolean(state.onOff?.onOff),
    level: Number(state.levelControl?.currentLevel ?? 254),
  };
}

function pushDimmer(device: DeviceRecord, endpoint: Endpoint): void {
  bindDeviceRecord(device);
  const channels = device.channels as DimmerChannels;
  const { on, level } = readState(endpoint);
  setDeviceChannels(device.id, {
    [channels.intensity]: on ? matterLevelToDmx(level) : 0,
  });
}

export async function createDimmerServerNode(
  device: DeviceRecord,
): Promise<{ server: ServerNode; endpoint: Endpoint }> {
  const live = { device };
  bindDeviceRecord(device);

  const server = await ServerNode.create({
    id: device.uniqueId,
    network: {
      port: device.matterPort,
    },
    commissioning: {
      passcode: device.passcode,
      discriminator: device.discriminator,
    },
    productDescription: {
      name: device.name,
      deviceType: DeviceTypeId(DimmableLightDevice.deviceType),
    },
    basicInformation: basicInformation(device, PRODUCT_IDS.dimmer),
  });

  const endpoint = new Endpoint(DimmableLightDevice, {
    id: "light",
    onOff: {
      onOff: false,
    },
    levelControl: {
      currentLevel: 254,
    },
  });
  await server.add(endpoint);
  (endpoint as unknown as { __liveDevice?: { device: DeviceRecord } }).__liveDevice = live;

  const events = endpoint.events as unknown as {
    onOff: { onOff$Changed: { on: (cb: (value: boolean) => void) => void } };
    levelControl: { currentLevel$Changed: { on: (cb: (value: number) => void) => void } };
  };
  const emit = () => {
    const { on, level } = readState(endpoint);
    console.log(`[matter:${live.device.name}] OnOff=${on ? "ON" : "OFF"} level=${level}`);
    pushDimmer(live.device, endpoint);
  };
  events.onOff.onOff$Changed.on(emit);
  events.levelControl.currentLevel$Changed.on(emit);

  pushDimmer(live.device, endpoint);
  return { server, endpoint };
}

export function rebindDimmerOutput(device: DeviceRecord, endpoint: Endpoint): void {
  const live = (endpoint as unknown as { __liveDevice?: { device: DeviceRecord } }).__liveDevice;
  if (live) {
    live.device = device;
  }
  pushDimmer(device, endpoint);
}
