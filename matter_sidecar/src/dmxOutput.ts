/**
 * Forward Matter light state to the Python process, which owns Art-Net/sACN.
 * This sidecar does not send DMX itself.
 */

const PYTHON_DMX_URL =
  process.env.PYTHON_DMX_URL?.trim() || "http://127.0.0.1:8765/internal/matter/dmx";
/** Same server, sibling route: the companion's own output on/off switch. */
const PYTHON_OUTPUT_URL = PYTHON_DMX_URL.replace(/\/dmx$/, "/output");

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

/** Read the companion's current output state, so a restarting switch does
 *  not report (or force) the wrong position. */
export async function readCompanionOutput(): Promise<boolean | undefined> {
  try {
    const res = await fetch(PYTHON_OUTPUT_URL, { method: "GET" });
    if (!res.ok) {
      return undefined;
    }
    const data = (await res.json()) as { enabled?: boolean };
    return typeof data.enabled === "boolean" ? data.enabled : undefined;
  } catch {
    return undefined;
  }
}

export function setCompanionOutput(enabled: boolean): void {
  void fetch(PYTHON_OUTPUT_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  }).catch((err: unknown) => {
    const message = err instanceof Error ? err.message : String(err);
    console.error(`[output] python toggle failed: ${message}`);
  });
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
