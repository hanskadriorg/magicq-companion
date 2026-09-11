/**
 * A Matter plug that switches the companion's own Art-Net/sACN output
 * instead of DMX channels — the same thing as the "Output on" toggle in the
 * dashboard header, reachable from a smart-home app.
 */

import { DeviceTypeId, Endpoint, ServerNode } from "@matter/main";
import { OnOffPlugInUnitDevice } from "@matter/main/devices/on-off-plug-in-unit";
import { readCompanionOutput, setCompanionOutput } from "../dmxOutput.js";
import type { DeviceRecord } from "../types.js";
import { PRODUCT_IDS, basicInformation } from "./identity.js";

export async function createOutputSwitchServerNode(
  device: DeviceRecord,
): Promise<{ server: ServerNode; endpoint: Endpoint }> {
  const live = { device };

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
      deviceType: DeviceTypeId(OnOffPlugInUnitDevice.deviceType),
    },
    basicInformation: basicInformation(device, PRODUCT_IDS.output),
  });

  // Adopt the companion's current position rather than pushing a default,
  // otherwise restarting the sidecar would blackout a running show.
  const current = (await readCompanionOutput()) ?? true;

  const endpoint = new Endpoint(OnOffPlugInUnitDevice, {
    id: "output",
    onOff: {
      onOff: current,
    },
  });
  await server.add(endpoint);
  (endpoint as unknown as { __liveDevice?: { device: DeviceRecord } }).__liveDevice = live;

  endpoint.events.onOff.onOff$Changed.on((value) => {
    console.log(`[matter:${live.device.name}] companion output -> ${value ? "ON" : "OFF"}`);
    setCompanionOutput(value);
  });

  return { server, endpoint };
}

export function rebindOutputSwitch(device: DeviceRecord, endpoint: Endpoint): void {
  const live = (endpoint as unknown as { __liveDevice?: { device: DeviceRecord } }).__liveDevice;
  if (live) {
    live.device = device;
  }
}

/** Reflect a dashboard-side toggle back onto the Matter attribute. */
export async function pushOutputState(endpoint: Endpoint, enabled: boolean): Promise<void> {
  const state = endpoint.state as { onOff?: { onOff?: boolean } };
  if (Boolean(state.onOff?.onOff) === enabled) {
    return;
  }
  await (endpoint as Endpoint<typeof OnOffPlugInUnitDevice>).set({ onOff: { onOff: enabled } });
}
