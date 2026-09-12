import {
  ROTATIONS,
  createLineFixtureDefaults,
  getFixtureCells,
  getMatrixBasis,
  rotationFromDelta,
} from "./fixture.js";
import { inferMatrixFromPoints } from "./matrix.js";

/** @typedef {{ head: number, element: number }} ParsedCell */

/**
 * @param {string} raw
 * @returns {ParsedCell | null}
 */
export function parseCellValue(raw) {
  const value = raw.trim();
  if (!value || value === "0") return null;
  const dot = value.indexOf(".");
  if (dot === -1) {
    const head = Number(value);
    return Number.isFinite(head) && head > 0 ? { head, element: 1 } : null;
  }
  const head = Number(value.slice(0, dot));
  const element = Number(value.slice(dot + 1));
  if (!Number.isFinite(head) || !Number.isFinite(element) || head <= 0 || element <= 0) {
    return null;
  }
  return { head, element };
}

/**
 * Parse MagicQ grid CSV into a 2D cell matrix.
 * @param {string} content
 * @returns {{ width: number, height: number, cells: (ParsedCell | null)[][] }}
 */
export function parseGridCsv(content) {
  const normalized = content.replace(/\r\n/g, "\n").replace(/\r/g, "\n").trim();
  if (!normalized) {
    return { width: 0, height: 0, cells: [] };
  }

  const rows = normalized.split("\n").map((line) => {
    const sep = line.includes(";") && !line.includes(",") ? ";" : ",";
    return line.split(sep);
  });

  const height = rows.length;
  const width = Math.max(...rows.map((row) => row.length), 0);
  /** @type {(ParsedCell | null)[][]} */
  const cells = rows.map((row) =>
    Array.from({ length: width }, (_, x) => parseCellValue(row[x] ?? "")),
  );

  return { width, height, cells };
}

/**
 * @param {{ x: number, y: number, element: number }[]} points
 * @returns {boolean}
 */
function isCollinear(points) {
  if (points.length < 2) return true;
  const start = points.find((p) => p.element === 1) ?? points[0];
  const second = points.find((p) => p.element === 2);
  if (!second) return points.length === 1;
  const dx = second.x - start.x;
  const dy = second.y - start.y;
  if (dx === 0 && dy === 0) return false;

  for (const point of points) {
    const px = point.x - start.x;
    const py = point.y - start.y;
    if (px * dy !== py * dx) return false;
  }
  return true;
}

/**
 * Reconstruct fixtures from a parsed grid.
 * @param {(ParsedCell | null)[][]} cells
 * @param {number} width
 * @param {number} height
 * @returns {{ fixtures: import("./fixture.js").Fixture[], nextHead: number, warnings: string[] }}
 */
export function fixturesFromGrid(cells, width, height) {
  /** @type {Map<number, { x: number, y: number, element: number }[]>} */
  const byHead = new Map();

  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const cell = cells[y]?.[x];
      if (!cell) continue;
      const list = byHead.get(cell.head) ?? [];
      list.push({ x, y, element: cell.element });
      byHead.set(cell.head, list);
    }
  }

  /** @type {import("./fixture.js").Fixture[]} */
  const fixtures = [];
  /** @type {string[]} */
  const warnings = [];
  let nextId = 1;

  for (const [headNumber, points] of [...byHead.entries()].sort((a, b) => a[0] - b[0])) {
    points.sort((a, b) => a.element - b.element);
    const count = points.length;
    const expected = new Set(points.map((p) => p.element));
    for (let i = 1; i <= count; i += 1) {
      if (!expected.has(i)) {
        warnings.push(`Head ${headNumber}: missing element ${i}`);
      }
    }

    const matrix = inferMatrixFromPoints(points, ROTATIONS, getMatrixBasis);
    if (matrix) {
      fixtures.push({
        id: `fx-${nextId}`,
        headNumber,
        x: matrix.x,
        y: matrix.y,
        rotation: matrix.rotation,
        matrixWidth: matrix.matrixWidth,
        matrixHeight: matrix.matrixHeight,
        pixelOrder: matrix.pixelOrder,
      });
      nextId += 1;
      continue;
    }

    const start = points.find((p) => p.element === 1) ?? points[0];
    let rotation = /** @type {import("./fixture.js").Rotation} */ (0);
    const lineDefaults = createLineFixtureDefaults(count);

    if (count > 1) {
      if (!isCollinear(points)) {
        warnings.push(`Head ${headNumber}: could not infer matrix layout — imported as best-effort line`);
      }
      const second = points.find((p) => p.element === 2);
      if (!second) {
        warnings.push(`Head ${headNumber}: could not determine direction (no element 2)`);
      } else {
        const dx = second.x - start.x;
        const dy = second.y - start.y;
        const inferred = rotationFromDelta(dx, dy);
        if (inferred == null) {
          warnings.push(`Head ${headNumber}: not a straight line — imported as horizontal`);
        } else {
          rotation = inferred;
        }
      }
    }

    fixtures.push({
      id: `fx-${nextId}`,
      headNumber,
      x: start.x,
      y: start.y,
      rotation,
      ...lineDefaults,
    });
    nextId += 1;
  }

  const nextHead = fixtures.reduce((max, fx) => Math.max(max, fx.headNumber), 0) + 1;
  return { fixtures, nextHead, warnings };
}

/**
 * Build MagicQ grid CSV content.
 * @param {number} gridWidth
 * @param {number} gridHeight
 * @param {import("./fixture.js").Fixture[]} fixtures
 */
export function buildGridCsv(gridWidth, gridHeight, fixtures) {
  /** @type {string[][]} */
  const grid = Array.from({ length: gridHeight }, () =>
    Array.from({ length: gridWidth }, () => "0"),
  );

  for (const fixture of fixtures) {
    for (const { x, y, value } of getFixtureCells(fixture)) {
      if (y >= 0 && y < gridHeight && x >= 0 && x < gridWidth) {
        if (grid[y][x] !== "0" && grid[y][x] !== value) {
          console.warn(`Cell (${x}, ${y}) collision: ${grid[y][x]} vs ${value}`);
        }
        grid[y][x] = value;
      }
    }
  }

  return grid.map((row) => row.join(",")).join("\n");
}

/**
 * @param {string} filename
 * @param {string} content
 */
export function downloadCsv(filename, content) {
  const blob = new Blob([content], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename.endsWith(".csv") ? filename : `${filename}.csv`;
  link.click();
  URL.revokeObjectURL(url);
}
