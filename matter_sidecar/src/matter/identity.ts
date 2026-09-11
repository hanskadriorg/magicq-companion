import { VendorId } from "@matter/main";
import type { DeviceRecord } from "../types.js";

export const VENDOR_NAME = "Suvaline";
export const VENDOR_ID = 0xfff1;

export const PRODUCT_IDS = {
  onoff: 0x8001,
  dimmer: 0x8003,
  rgb: 0x8002,
  output: 0x8004,
} as const;

export function basicInformation(device: DeviceRecord, productId: number) {
  return {
    vendorName: VENDOR_NAME,
    vendorId: VendorId(VENDOR_ID),
    nodeLabel: device.name,
    productName: device.name,
    productLabel: device.name,
    productId,
    serialNumber: `maa-${device.id.slice(0, 16)}`,
    uniqueId: device.uniqueId,
    hardwareVersion: 1,
    hardwareVersionString: "1",
    softwareVersion: 1,
    softwareVersionString: "1.0.0",
  };
}
