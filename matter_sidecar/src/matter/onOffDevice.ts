import { DeviceTypeId, Endpoint, ServerNode } from "@matter/main";
import { OnOffLightDevice } from "@matter/main/devices/on-off-light";
import { bindDeviceRecord, setDeviceChannels } from "../dmxOutput.js";
import type { DeviceRecord, OnOffChannels } from "../types.js";
import { PRODUCT_IDS, basicInformation } from "./identity.js";

function readOnOff(endpoint: Endpoint): boolean {
  const state = endpoint.state as { onOff?: { onOff?: boolean } };
  return Boolean(state.onOff?.onOff);
}

function pushOnOff(device: DeviceRecord, on: boolean): void {
  bindDeviceRecord(device);
  const channels = device.channels as OnOffChannels;
  setDeviceChannels(device.id, {
    [channels.channel]: on ? 255 : 0,
  });
}

export async function createOnOffServerNode(
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
      deviceType: DeviceTypeId(OnOffLightDevice.deviceType),
    },
    basicInformation: basicInformation(device, PRODUCT_IDS.onoff),
  });

  const endpoint = new Endpoint(OnOffLightDevice, {
    id: "light",
    onOff: {
      onOff: false,
    },
  });
  await server.add(endpoint);
  (endpoint as unknown as { __liveDevice?: { device: DeviceRecord } }).__liveDevice = live;

  endpoint.events.onOff.onOff$Changed.on((value) => {
    console.log(`[matter:${live.device.name}] OnOff -> ${value ? "ON" : "OFF"}`);
    pushOnOff(live.device, value);
  });

  pushOnOff(live.device, false);
  return { server, endpoint };
}

export function rebindOnOffOutput(device: DeviceRecord, endpoint: Endpoint): void {
  const live = (endpoint as unknown as { __liveDevice?: { device: DeviceRecord } }).__liveDevice;
  if (live) {
    live.device = device;
  }
  pushOnOff(device, readOnOff(endpoint));
}
