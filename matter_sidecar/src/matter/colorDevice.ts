import { DeviceTypeId, Endpoint, ServerNode, VendorId } from "@matter/main";
import { ColorControlServer } from "@matter/main/behaviors/color-control";
import { ColorControl } from "@matter/main/clusters/color-control";
import { ExtendedColorLightDevice } from "@matter/main/devices/extended-color-light";
import { matterHsvToRgb, matterLevelToDmx, matterXyToRgb } from "../color.js";
import { bindDeviceRecord, setDeviceChannels } from "../dmxOutput.js";
import type { DeviceRecord, RgbChannels } from "../types.js";

const VENDOR_NAME = "Suvaline";
const VENDOR_ID = 0xfff1;
const PRODUCT_ID = 0x8002;

const ColorLight = ExtendedColorLightDevice.with(
  ColorControlServer.with("HueSaturation", "Xy", "ColorTemperature").alter({
    attributes: { remainingTime: { optional: false } },
  }),
);

interface LightRuntimeState {
  onOff?: { onOff?: boolean };
  levelControl?: { currentLevel?: number };
  colorControl?: {
    colorMode?: number;
    currentHue?: number;
    currentSaturation?: number;
    currentX?: number;
    currentY?: number;
  };
}

interface LightRuntimeEvents {
  onOff: { onOff$Changed: { on: (cb: (value: boolean) => void) => void } };
  levelControl: { currentLevel$Changed: { on: (cb: (value: number) => void) => void } };
  colorControl: {
    colorMode$Changed: { on: (cb: (value: number) => void) => void };
    currentHue$Changed: { on: (cb: (value: number) => void) => void };
    currentSaturation$Changed: { on: (cb: (value: number) => void) => void };
    currentX$Changed: { on: (cb: (value: number) => void) => void };
    currentY$Changed: { on: (cb: (value: number) => void) => void };
  };
}

function resolveRgb(state: LightRuntimeState): { r: number; g: number; b: number } {
  const cc = state.colorControl ?? {};
  const mode = cc.colorMode ?? ColorControl.ColorMode.CurrentHueAndCurrentSaturation;

  if (mode === ColorControl.ColorMode.CurrentXAndCurrentY) {
    return matterXyToRgb(Number(cc.currentX ?? 0), Number(cc.currentY ?? 0));
  }

  if (mode === ColorControl.ColorMode.ColorTemperatureMireds) {
    return { r: 255, g: 180, b: 80 };
  }

  return matterHsvToRgb(Number(cc.currentHue ?? 0), Number(cc.currentSaturation ?? 254));
}

function pushColorState(device: DeviceRecord, endpoint: Endpoint): void {
  bindDeviceRecord(device);
  const channels = device.channels as RgbChannels;
  const state = endpoint.state as LightRuntimeState;
  const on = Boolean(state.onOff?.onOff);
  const level = Number(state.levelControl?.currentLevel ?? 254);
  const { r, g, b } = resolveRgb(state);
  const intensity = on ? matterLevelToDmx(level) : 0;

  setDeviceChannels(device.id, {
    [channels.intensity]: intensity,
    [channels.red]: r,
    [channels.green]: g,
    [channels.blue]: b,
  });
}

export async function createColorServerNode(
  device: DeviceRecord,
): Promise<{ server: ServerNode; endpoint: Endpoint }> {
  bindDeviceRecord(device);
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
      deviceType: DeviceTypeId(ExtendedColorLightDevice.deviceType),
    },
    basicInformation: {
      vendorName: VENDOR_NAME,
      vendorId: VendorId(VENDOR_ID),
      nodeLabel: device.name,
      productName: device.name,
      productLabel: device.name,
      productId: PRODUCT_ID,
      serialNumber: `maa-${device.id.slice(0, 16)}`,
      uniqueId: device.uniqueId,
    },
  });

  const endpoint = new Endpoint(ColorLight, {
    id: "light",
    onOff: {
      onOff: false,
    },
    levelControl: {
      currentLevel: 254,
    },
    colorControl: {
      colorMode: ColorControl.ColorMode.CurrentHueAndCurrentSaturation,
      enhancedColorMode: ColorControl.EnhancedColorMode.CurrentHueAndCurrentSaturation,
      currentHue: 0,
      currentSaturation: 254,
      currentX: 0x8000,
      currentY: 0x8000,
      colorTempPhysicalMinMireds: 153,
      colorTempPhysicalMaxMireds: 454,
      colorTemperatureMireds: 250,
      coupleColorTempToLevelMinMireds: 153,
      startUpColorTemperatureMireds: 250,
    },
  });
  await server.add(endpoint);

  const events = endpoint.events as unknown as LightRuntimeEvents;
  const emit = () => {
    console.log(`[matter:${live.device.name}] color/level/onOff changed`);
    pushColorState(live.device, endpoint);
  };

  // Keep a mutable handle so rebind can refresh channel map without recreating listeners.
  (endpoint as unknown as { __liveDevice?: { device: DeviceRecord } }).__liveDevice = live;

  events.onOff.onOff$Changed.on(emit);
  events.levelControl.currentLevel$Changed.on(emit);
  events.colorControl.colorMode$Changed.on(emit);
  events.colorControl.currentHue$Changed.on(emit);
  events.colorControl.currentSaturation$Changed.on(emit);
  events.colorControl.currentX$Changed.on(emit);
  events.colorControl.currentY$Changed.on(emit);

  pushColorState(live.device, endpoint);
  return { server, endpoint };
}

export function rebindColorOutput(device: DeviceRecord, endpoint: Endpoint): void {
  const live = (endpoint as unknown as { __liveDevice?: { device: DeviceRecord } }).__liveDevice;
  if (live) {
    live.device = device;
  }
  pushColorState(device, endpoint);
}
