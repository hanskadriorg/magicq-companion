/**
 * Forward Matter light state to the Python process, which owns Art-Net/sACN.
 * This sidecar does not send DMX itself.
 */

const PYTHON_DMX_URL =
  process.env.PYTHON_DMX_URL?.trim() || "http://127.0.0.1:8765/internal/matter/dmx";

export function bindDeviceRecord(_device: unknown): void {
  // Channel maps live in this process; Python only receives 1-based DMX slots.
}

export function unbindDeviceRecord(_id: string): void {
  // no-op
}

export async function startDmxOutput(): Promise<void> {
  console.log(`[dmx] forwarding channel updates to ${PYTHON_DMX_URL}`);
}

export async function stopDmxOutput(): Promise<void> {
  // no-op
}

export function setDeviceChannels(
  _deviceOrId: unknown,
  updates: Record<number, number>,
): void {
  const payload = { updates };
  void fetch(PYTHON_DMX_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }).catch((err: unknown) => {
    const message = err instanceof Error ? err.message : String(err);
    console.error(`[dmx] python forward failed: ${message}`);
  });
}
