/** @typedef {'row-lr' | 'row-rl' | 'row-snake-lr' | 'row-snake-rl' | 'col-tb' | 'col-bt' | 'col-snake-tb' | 'col-snake-bt'} PixelOrder */

/** @typedef {{ id: PixelOrder, label: string, hint: string }} PixelOrderDef */

/** @type {PixelOrderDef[]} */
export const PIXEL_ORDERS = [
  { id: "row-lr", label: "Rows L→R", hint: "Each row left to right" },
  { id: "row-rl", label: "Rows R→L", hint: "Each row right to left" },
  { id: "row-snake-lr", label: "Snake rows", hint: "Zig-zag rows, start top-left" },
  { id: "row-snake-rl", label: "Snake rows ↩", hint: "Zig-zag rows, start top-right" },
  { id: "col-tb", label: "Cols T→B", hint: "Each column top to bottom" },
  { id: "col-bt", label: "Cols B→T", hint: "Each column bottom to top" },
  { id: "col-snake-tb", label: "Snake cols", hint: "Zig-zag columns, start top-left" },
  { id: "col-snake-bt", label: "Snake cols ↩", hint: "Zig-zag columns, start bottom-left" },
];

const PREVIEW_COLS = 4;
const PREVIEW_ROWS = 3;

/** @param {PixelOrder} order */
export function getOrderDef(order) {
  return PIXEL_ORDERS.find((o) => o.id === order) ?? PIXEL_ORDERS[0];
}

/**
 * @param {number} element
 * @param {number} width
 * @param {number} height
 * @param {PixelOrder} order
 */
export function elementToLocal(element, width, height, order) {
  const i = element - 1;
  if (element < 1 || element > width * height) return null;

  switch (order) {
    case "row-rl": {
      const row = Math.floor(i / width);
      return { x: width - 1 - (i % width), y: row };
    }
    case "row-snake-lr": {
      const row = Math.floor(i / width);
      const col = i % width;
      return { x: row % 2 === 0 ? col : width - 1 - col, y: row };
    }
    case "row-snake-rl": {
      const row = Math.floor(i / width);
      const col = i % width;
      return { x: row % 2 === 0 ? width - 1 - col : col, y: row };
    }
    case "col-tb": {
      const col = Math.floor(i / height);
      return { x: col, y: i % height };
    }
    case "col-bt": {
      const col = Math.floor(i / height);
      return { x: col, y: height - 1 - (i % height) };
    }
    case "col-snake-tb": {
      const col = Math.floor(i / height);
      const row = i % height;
      return { x: col, y: col % 2 === 0 ? row : height - 1 - row };
    }
    case "col-snake-bt": {
      const col = Math.floor(i / height);
      const row = i % height;
      return { x: col, y: col % 2 === 0 ? height - 1 - row : row };
    }
    case "row-lr":
    default:
      return { x: i % width, y: Math.floor(i / width) };
  }
}

/**
 * @param {number} localX
 * @param {number} localY
 * @param {number} width
 * @param {number} height
 * @param {PixelOrder} order
 */
export function localToElement(localX, localY, width, height, order) {
  for (let element = 1; element <= width * height; element += 1) {
    const local = elementToLocal(element, width, height, order);
    if (local && local.x === localX && local.y === localY) return element;
  }
  return null;
}

/** @param {PixelOrder} order */
export function previewCells(order) {
  /** @type {{ x: number, y: number, element: number }[]} */
  const cells = [];
  const count = PREVIEW_COLS * PREVIEW_ROWS;
  for (let element = 1; element <= count; element += 1) {
    const local = elementToLocal(element, PREVIEW_COLS, PREVIEW_ROWS, order);
    if (local) cells.push({ x: local.x, y: local.y, element });
  }
  return cells;
}

export function previewGridSize() {
  return { cols: PREVIEW_COLS, rows: PREVIEW_ROWS };
}

/**
 * @param {number} dx
 * @param {number} dy
 * @param {[number, number]} widthDir
 * @param {[number, number]} heightDir
 */
export function solveLocalOffset(dx, dy, widthDir, heightDir) {
  const det = widthDir[0] * heightDir[1] - widthDir[1] * heightDir[0];
  if (det === 0) return null;

  const lx = (dx * heightDir[1] - dy * heightDir[0]) / det;
  const ly = (dy * widthDir[0] - dx * widthDir[1]) / det;

  if (!Number.isInteger(lx) || !Number.isInteger(ly) || lx < 0 || ly < 0) return null;
  return { x: lx, y: ly };
}

/**
 * @param {{ x: number, y: number, element: number }[]} points
 * @param {import("./fixture.js").Rotation[]} rotations
 * @param {(rotation: import("./fixture.js").Rotation) => { widthDir: [number, number], heightDir: [number, number] }} getBasis
 */
export function inferMatrixFromPoints(points, rotations, getBasis) {
  if (points.length < 2) return null;

  const start = points.find((p) => p.element === 1) ?? points[0];

  for (const rotation of rotations) {
    const { widthDir, heightDir } = getBasis(rotation);
    /** @type {{ element: number, lx: number, ly: number }[]} */
    const mapped = [];

    for (const point of points) {
      const local = solveLocalOffset(point.x - start.x, point.y - start.y, widthDir, heightDir);
      if (!local) {
        mapped.length = 0;
        break;
      }
      mapped.push({ element: point.element, lx: local.x, ly: local.y });
    }

    if (mapped.length !== points.length) continue;

    const matrixWidth = Math.max(...mapped.map((m) => m.lx)) + 1;
    const matrixHeight = Math.max(...mapped.map((m) => m.ly)) + 1;
    if (matrixWidth * matrixHeight !== points.length) continue;

    for (const orderDef of PIXEL_ORDERS) {
      let matches = true;
      for (const m of mapped) {
        const expected = localToElement(m.lx, m.ly, matrixWidth, matrixHeight, orderDef.id);
        if (expected !== m.element) {
          matches = false;
          break;
        }
      }
      if (matches) {
        return {
          matrixWidth,
          matrixHeight,
          pixelOrder: orderDef.id,
          rotation,
          x: start.x,
          y: start.y,
        };
      }
    }
  }

  return null;
}
