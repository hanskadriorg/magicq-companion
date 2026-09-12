import { elementToLocal } from "./matrix.js";

/** @typedef {0 | 45 | 90 | 135 | 180 | 225 | 270 | 315} Rotation */

export const ROTATION_STEP = 45;

/** @type {Rotation[]} */
export const ROTATIONS = [0, 45, 90, 135, 180, 225, 270, 315];

/** @type {Record<Rotation, [number, number]>} */
const DIRECTIONS = {
  0: [1, 0],
  45: [1, 1],
  90: [0, 1],
  135: [-1, 1],
  180: [-1, 0],
  225: [-1, -1],
  270: [0, -1],
  315: [1, -1],
};

/**
 * @typedef {Object} Fixture
 * @property {string} id
 * @property {number} headNumber
 * @property {number} x
 * @property {number} y
 * @property {Rotation} rotation
 * @property {number} matrixWidth
 * @property {number} matrixHeight
 * @property {import("./matrix.js").PixelOrder} pixelOrder
 */

/** @param {Fixture} fixture */
export function pixelCount(fixture) {
  return fixture.matrixWidth * fixture.matrixHeight;
}

/** @param {Rotation} rotation */
export function getMatrixBasis(rotation) {
  const widthDir = /** @type {[number, number]} */ (DIRECTIONS[rotation] ?? DIRECTIONS[0]);
  const heightRotation = /** @type {Rotation} */ ((rotation + 90) % 360);
  const heightDir = /** @type {[number, number]} */ (DIRECTIONS[heightRotation] ?? DIRECTIONS[90]);
  return { widthDir, heightDir };
}

/**
 * @param {number} dx
 * @param {number} dy
 * @returns {Rotation | null}
 */
export function rotationFromDelta(dx, dy) {
  /** @type {[number, number, Rotation][]} */
  const pairs = [
    [1, 0, 0],
    [1, 1, 45],
    [0, 1, 90],
    [-1, 1, 135],
    [-1, 0, 180],
    [-1, -1, 225],
    [0, -1, 270],
    [1, -1, 315],
  ];
  for (const [x, y, rotation] of pairs) {
    if (dx === x && dy === y) return rotation;
  }
  return null;
}

/**
 * @param {number} rotation
 * @returns {Rotation}
 */
export function normalizeRotation(rotation) {
  const wrapped = ((rotation % 360) + 360) % 360;
  const nearest = ROTATIONS.reduce((best, candidate) =>
    Math.abs(candidate - wrapped) < Math.abs(best - wrapped) ||
    (Math.abs(candidate - wrapped) === Math.abs(best - wrapped) && candidate < best)
      ? candidate
      : best,
  );
  return nearest;
}

/**
 * @param {Fixture} fixture
 * @param {number} element
 */
export function cellForElement(fixture, element) {
  const local = elementToLocal(element, fixture.matrixWidth, fixture.matrixHeight, fixture.pixelOrder);
  if (!local) return { x: fixture.x, y: fixture.y };

  const { widthDir, heightDir } = getMatrixBasis(fixture.rotation);
  return {
    x: fixture.x + local.x * widthDir[0] + local.y * heightDir[0],
    y: fixture.y + local.x * widthDir[1] + local.y * heightDir[1],
  };
}

/** @param {Fixture} fixture @param {number} index */
export function cellForPixel(fixture, index) {
  return cellForElement(fixture, index + 1);
}

/** @param {Fixture} fixture */
export function getFixtureCells(fixture) {
  /** @type {{ x: number, y: number, value: string, element: number }[]} */
  const cells = [];
  const count = pixelCount(fixture);
  for (let element = 1; element <= count; element += 1) {
    const { x, y } = cellForElement(fixture, element);
    cells.push({
      x,
      y,
      element,
      value: `${fixture.headNumber}.${element}`,
    });
  }
  return cells;
}

/** @param {Fixture} fixture @param {number} gridWidth @param {number} gridHeight */
export function isFixtureInBounds(fixture, gridWidth, gridHeight) {
  return getFixtureCells(fixture).every(
    ({ x, y }) => x >= 0 && y >= 0 && x < gridWidth && y < gridHeight,
  );
}

/**
 * @param {Fixture} candidate
 * @param {Fixture[]} fixtures
 * @param {string} [ignoreId]
 */
export function fixtureOverlaps(candidate, fixtures, ignoreId) {
  const cells = new Set(getFixtureCells(candidate).map(({ x, y }) => `${x},${y}`));
  for (const other of fixtures) {
    if (other.id === ignoreId || other.id === candidate.id) continue;
    for (const { x, y } of getFixtureCells(other)) {
      if (cells.has(`${x},${y}`)) return true;
    }
  }
  return false;
}

/** @param {Fixture} fixture */
export function isFixtureValid(fixture, gridWidth, gridHeight, fixtures, ignoreId) {
  return (
    isFixtureInBounds(fixture, gridWidth, gridHeight) &&
    !fixtureOverlaps(fixture, fixtures, ignoreId)
  );
}

/** @param {Rotation} rotation */
export function nextRotation(rotation) {
  const idx = ROTATIONS.indexOf(rotation);
  if (idx === -1) return 0;
  return ROTATIONS[(idx + 1) % ROTATIONS.length];
}

/** @param {Fixture} fixture @param {number} cellSize */
export function fixtureBoundsPx(fixture, cellSize) {
  const cells = getFixtureCells(fixture);
  const xs = cells.map((c) => c.x);
  const ys = cells.map((c) => c.y);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  return {
    x: minX * cellSize,
    y: minY * cellSize,
    width: (maxX - minX + 1) * cellSize,
    height: (maxY - minY + 1) * cellSize,
  };
}

/** @param {Fixture} fixture @param {number} cellSize */
export function startPointPx(fixture, cellSize) {
  const { x, y } = cellForElement(fixture, 1);
  return {
    cx: x * cellSize + cellSize / 2,
    cy: y * cellSize + cellSize / 2,
  };
}

/**
 * @param {number} x
 * @param {number} y
 * @param {Fixture[]} fixtures
 * @param {string | null} ignoreId
 * @param {{ snapToGrid: boolean, snapToFixtures: boolean, snapDistance: number }} options
 */
export function snapAnchor(x, y, fixtures, ignoreId, options) {
  let sx = x;
  let sy = y;

  if (!options.snapToGrid) {
    return {
      x: Math.round(sx),
      y: Math.round(sy),
      guides: /** @type {{ type: "v" | "h", pos: number }[]} */ ([]),
    };
  }

  sx = Math.round(sx);
  sy = Math.round(sy);

  /** @type {{ type: "v" | "h", pos: number }[]} */
  const guides = [];
  if (!options.snapToFixtures) {
    return { x: sx, y: sy, guides };
  }

  const dist = options.snapDistance;

  for (const other of fixtures) {
    if (other.id === ignoreId) continue;

    const rowDist = Math.abs(sy - other.y);
    const colDist = Math.abs(sx - other.x);

    if (rowDist <= dist) {
      sy = other.y;
      guides.push({ type: "h", pos: other.y });
    } else if (colDist <= dist) {
      sx = other.x;
      guides.push({ type: "v", pos: other.x });
    }
  }

  return { x: sx, y: sy, guides };
}

/** @param {Fixture} fixture */
export function anchorAfterFixture(fixture) {
  const cells = getFixtureCells(fixture);
  const maxY = Math.max(...cells.map((c) => c.y));
  const minX = Math.min(...cells.map((c) => c.x));
  const rowBelow = cells.filter((c) => c.y === maxY);
  const anchorX = Math.min(...rowBelow.map((c) => c.x));
  return { x: anchorX, y: maxY + 1, rotation: fixture.rotation };
}

/**
 * @param {number} gridWidth
 * @param {number} gridHeight
 * @param {Fixture} prototype partial fixture without id/head/x/y
 * @param {Fixture[]} fixtures
 * @param {{ snapToFixtures: boolean }} [options]
 */
export function findPlacement(gridWidth, gridHeight, prototype, fixtures, options) {
  const probe = { ...prototype, id: "probe", headNumber: 0, x: 0, y: 0 };

  if (options?.snapToFixtures && fixtures.length > 0) {
    const last = fixtures[fixtures.length - 1];
    const after = anchorAfterFixture(last);

    /** @type {{ x: number, y: number, rotation: Rotation }[]} */
    const preferred = [
      { x: after.x, y: after.y, rotation: prototype.rotation },
      { x: last.x, y: after.y, rotation: prototype.rotation },
    ];

    for (const { x, y, rotation } of preferred) {
      const candidate = { ...probe, x, y, rotation };
      if (isFixtureValid(candidate, gridWidth, gridHeight, fixtures, "probe")) {
        return { x, y };
      }
    }
  }

  for (let y = 0; y < gridHeight; y += 1) {
    for (let x = 0; x < gridWidth; x += 1) {
      const candidate = { ...probe, x, y };
      if (isFixtureValid(candidate, gridWidth, gridHeight, fixtures, "probe")) {
        return { x, y };
      }
    }
  }
  return { x: 0, y: 0 };
}

/** @param {Fixture} fixture @param {number} cellSize */
export function directionEndPx(fixture, cellSize) {
  const start = cellForElement(fixture, 1);
  if (pixelCount(fixture) < 2) {
    const s = startPointPx(fixture, cellSize);
    return { x1: s.cx, y1: s.cy, x2: s.cx, y2: s.cy };
  }
  const end = cellForElement(fixture, 2);
  return {
    x1: start.x * cellSize + cellSize / 2,
    y1: start.y * cellSize + cellSize / 2,
    x2: end.x * cellSize + cellSize / 2,
    y2: end.y * cellSize + cellSize / 2,
  };
}

/** @param {Fixture} fixture */
export function fixtureSummary(fixture) {
  const count = pixelCount(fixture);
  if (fixture.matrixHeight === 1 && fixture.matrixWidth === count && fixture.pixelOrder === "row-lr") {
    return `${count}px line · ${fixture.rotation}°`;
  }
  const order = fixture.pixelOrder.replace("row-", "R:").replace("col-", "C:").replace(/-/g, " ");
  return `${fixture.matrixWidth}×${fixture.matrixHeight} ${order} · ${fixture.rotation}°`;
}

/** @param {number} matrixWidth @param {number} matrixHeight @param {import("./matrix.js").PixelOrder} pixelOrder */
export function createLineFixtureDefaults(matrixWidth) {
  return {
    matrixWidth,
    matrixHeight: 1,
    pixelOrder: /** @type {import("./matrix.js").PixelOrder} */ ("row-lr"),
  };
}
