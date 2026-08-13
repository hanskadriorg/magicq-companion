/** Convert Matter HSV (hue/sat 0–254) to 8-bit RGB. */
export function matterHsvToRgb(hue: number, saturation: number): { r: number; g: number; b: number } {
  const h = ((hue % 254) / 254) * 360;
  const s = Math.max(0, Math.min(1, saturation / 254));
  const v = 1;

  const c = v * s;
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
  const m = v - c;

  let rp = 0;
  let gp = 0;
  let bp = 0;

  if (h < 60) {
    rp = c;
    gp = x;
  } else if (h < 120) {
    rp = x;
    gp = c;
  } else if (h < 180) {
    gp = c;
    bp = x;
  } else if (h < 240) {
    gp = x;
    bp = c;
  } else if (h < 300) {
    rp = x;
    bp = c;
  } else {
    rp = c;
    bp = x;
  }

  return {
    r: Math.round((rp + m) * 255),
    g: Math.round((gp + m) * 255),
    b: Math.round((bp + m) * 255),
  };
}

/** Map Matter currentLevel (1–254) to DMX 0–255. */
export function matterLevelToDmx(level: number): number {
  if (level <= 0) {
    return 0;
  }
  return Math.max(0, Math.min(255, Math.round((level / 254) * 255)));
}

function gammaCorrect(c: number): number {
  const clipped = Math.max(0, Math.min(1, c));
  return clipped <= 0.0031308 ? 12.92 * clipped : 1.055 * Math.pow(clipped, 1 / 2.4) - 0.055;
}

/** Convert Matter CIE XY (0–65279) to 8-bit sRGB. */
export function matterXyToRgb(currentX: number, currentY: number): { r: number; g: number; b: number } {
  const x = currentX / 65536;
  const y = Math.max(1e-6, currentY / 65536);
  const z = Math.max(0, 1 - x - y);
  const Y = 1;
  const X = (Y / y) * x;
  const Z = (Y / y) * z;

  const rLin = X * 3.2406 + Y * -1.5372 + Z * -0.4986;
  const gLin = X * -0.9689 + Y * 1.8758 + Z * 0.0415;
  const bLin = X * 0.0557 + Y * -0.204 + Z * 1.057;

  return {
    r: Math.round(gammaCorrect(rLin) * 255),
    g: Math.round(gammaCorrect(gLin) * 255),
    b: Math.round(gammaCorrect(bLin) * 255),
  };
}
