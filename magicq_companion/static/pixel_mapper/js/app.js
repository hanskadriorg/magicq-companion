import {
  cellForElement,
  createLineFixtureDefaults,
  directionEndPx,
  findPlacement,
  fixtureBoundsPx,
  fixtureOverlaps,
  fixtureSummary,
  getFixtureCells,
  isFixtureInBounds,
  isFixtureValid,
  nextRotation,
  pixelCount,
  snapAnchor,
  startPointPx,
} from "./fixture.js";
import { buildGridCsv, fixturesFromGrid, parseGridCsv } from "./csv.js";
import {
  loadExportFilename,
  openCsvForImport,
  saveCsvWithDialog,
  saveExportFilename,
} from "./export-settings.js";
import { getMatrixDraft, initMatrixPanel } from "./matrix-panel.js";

const CELL_SIZE = 36;
const MIN_ZOOM = 0.05;
const MAX_ZOOM = 4;
const PAN_STEP = 120;

/** @type {{ gridWidth: number, gridHeight: number, fixtures: import("./fixture.js").Fixture[], selectedIds: Set<string>, zoom: number, panX: number, panY: number, nextId: number, snapToGrid: boolean, snapToFixtures: boolean, snapDistance: number, snapGuides: { type: "v" | "h", pos: number }[] }} */
const state = {
  gridWidth: 40,
  gridHeight: 20,
  fixtures: [],
  selectedIds: new Set(),
  zoom: 1,
  panX: 40,
  panY: 40,
  nextId: 1,
  snapToGrid: true,
  snapToFixtures: true,
  snapDistance: 1,
  snapGuides: [],
};

/** @type {{ dragging: boolean, panning: boolean, marquee: boolean, moved: boolean, fixtureId: string | null, offsetX: number, offsetY: number, lastX: number, lastY: number, spaceHeld: boolean, marqueeStart: { x: number, y: number }, marqueeEnd: { x: number, y: number }, dragAnchorStart: { x: number, y: number }, dragOrigins: Map<string, { x: number, y: number }>, lastListClickId: string | null, lastAKeyTime: number }} */
const interaction = {
  dragging: false,
  panning: false,
  marquee: false,
  moved: false,
  fixtureId: null,
  offsetX: 0,
  offsetY: 0,
  lastX: 0,
  lastY: 0,
  spaceHeld: false,
  marqueeStart: { x: 0, y: 0 },
  marqueeEnd: { x: 0, y: 0 },
  dragAnchorStart: { x: 0, y: 0 },
  dragOrigins: new Map(),
  lastListClickId: null,
  lastAKeyTime: 0,
  evenSpacingPending: false,
  resizing: false,
  resizeAnchorGx: 0,
  resizeAnchorGy: 0,
  resizeFreeOrigGx: 0,
  resizeFreeOrigGy: 0,
  /** @type {"x" | "y" | "xy"} */
  resizeAxes: "xy",
  /** @type {Map<string, {x: number, y: number}>} */
  resizeOrigins: new Map(),
};

const DOUBLE_A_MS = 400;
const MARQUEE_MIN_PX = 4;

const svg = /** @type {SVGSVGElement} */ (document.getElementById("grid-svg"));
const canvasWrap = /** @type {HTMLElement} */ (document.getElementById("canvas-wrap"));
const zoomLabel = /** @type {HTMLElement} */ (document.getElementById("zoom-label"));
const selectedPanel = /** @type {HTMLElement} */ (document.getElementById("selected-panel"));
const selectedMeta = /** @type {HTMLElement} */ (document.getElementById("selected-meta"));
const editHeadNumber = /** @type {HTMLInputElement} */ (document.getElementById("edit-head-number"));
const importInput = /** @type {HTMLInputElement} */ (document.getElementById("import-csv-input"));
const fixtureListEl = /** @type {HTMLUListElement} */ (document.getElementById("fixture-list"));
const fixtureCountEl = /** @type {HTMLElement} */ (document.getElementById("fixture-count"));

/** @type {SVGGElement | null} */
let viewportGroup = null;

function uid() {
  const id = `fx-${state.nextId}`;
  state.nextId += 1;
  return id;
}

function isSelected(id) {
  return state.selectedIds.has(id);
}

function selectedFixtures() {
  return state.fixtures.filter((f) => state.selectedIds.has(f.id));
}

function selectedFixture() {
  const selected = selectedFixtures();
  return selected.length === 1 ? selected[0] : null;
}

function clearSelection() {
  state.selectedIds.clear();
  updateSelectedPanel();
  render();
}

function selectAllFixtures() {
  state.selectedIds = new Set(state.fixtures.map((f) => f.id));
  updateSelectedPanel();
  render();
}

/** @param {string[]} ids @param {{ focus?: boolean, append?: boolean, toggle?: boolean }} [options] */
function setSelection(ids, options = {}) {
  const validIds = ids.filter((id) => state.fixtures.some((f) => f.id === id));
  if (options.toggle) {
    for (const id of validIds) {
      if (state.selectedIds.has(id)) state.selectedIds.delete(id);
      else state.selectedIds.add(id);
    }
  } else if (options.append) {
    for (const id of validIds) state.selectedIds.add(id);
  } else {
    state.selectedIds = new Set(validIds);
  }
  updateSelectedPanel();
  render();
  if (options.focus && state.selectedIds.size === 1) focusSelectedFixture();
}

/** @param {string} id @param {MouseEvent} [event] */
function handleListSelection(id, event) {
  const sorted = [...state.fixtures].sort((a, b) => a.headNumber - b.headNumber);
  const index = sorted.findIndex((f) => f.id === id);
  if (index === -1) return;

  if (event?.shiftKey && interaction.lastListClickId) {
    const anchorIndex = sorted.findIndex((f) => f.id === interaction.lastListClickId);
    if (anchorIndex !== -1) {
      const lo = Math.min(anchorIndex, index);
      const hi = Math.max(anchorIndex, index);
      const rangeIds = sorted.slice(lo, hi + 1).map((f) => f.id);
      if (event.ctrlKey || event.metaKey) setSelection(rangeIds, { append: true });
      else setSelection(rangeIds);
      interaction.lastListClickId = id;
      return;
    }
  }

  if (event?.ctrlKey || event?.metaKey) {
    setSelection([id], { toggle: true });
  } else {
    setSelection([id], { focus: true });
  }
  interaction.lastListClickId = id;
}

/** @param {number} x1 @param {number} y1 @param {number} x2 @param {number} y2 */
function fixturesInRect(x1, y1, x2, y2) {
  const rx1 = Math.min(x1, x2);
  const ry1 = Math.min(y1, y2);
  const rx2 = Math.max(x1, x2);
  const ry2 = Math.max(y1, y2);
  /** @type {string[]} */
  const ids = [];

  for (const fixture of state.fixtures) {
    for (const { x, y } of getFixtureCells(fixture)) {
      const px = x * CELL_SIZE;
      const py = y * CELL_SIZE;
      if (px + CELL_SIZE > rx1 && px < rx2 && py + CELL_SIZE > ry1 && py < ry2) {
        ids.push(fixture.id);
        break;
      }
    }
  }
  return ids;
}

/** @param {Map<string, { x: number, y: number }>} positions */
function canPlaceAll(positions) {
  const ignoreIds = new Set(positions.keys());
  for (const [id, pos] of positions) {
    const fixture = state.fixtures.find((f) => f.id === id);
    if (!fixture) return false;
    const candidate = { ...fixture, x: pos.x, y: pos.y };
    for (const other of state.fixtures) {
      if (ignoreIds.has(other.id)) continue;
      if (fixtureOverlaps(candidate, [other], other.id)) return false;
    }
  }
  return true;
}

function updateSelectedPanel() {
  const selected = selectedFixtures();
  const panelTitle = /** @type {HTMLElement} */ (document.querySelector("#selected-panel h2"));
  const headField = /** @type {HTMLElement} */ (editHeadNumber.closest("label"));

  if (selected.length === 0) {
    selectedPanel.hidden = true;
    updateFixtureList();
    return;
  }

  selectedPanel.hidden = false;
  if (selected.length === 1) {
    const fx = selected[0];
    panelTitle.textContent = "Selected fixture";
    selectedMeta.textContent = `${fixtureSummary(fx)} · anchor (${fx.x}, ${fx.y})`;
    editHeadNumber.value = String(fx.headNumber);
    headField.hidden = false;
  } else {
    panelTitle.textContent = "Selected fixtures";
    const heads = selected.map((f) => f.headNumber).sort((a, b) => a - b);
    selectedMeta.textContent = `${selected.length} fixtures · heads ${heads.join(", ")}`;
    headField.hidden = true;
  }
  updateFixtureList();
}

function updateFixtureList() {
  const sorted = [...state.fixtures].sort((a, b) => a.headNumber - b.headNumber);
  fixtureCountEl.textContent = String(sorted.length);
  fixtureListEl.replaceChildren();

  if (sorted.length === 0) {
    const empty = document.createElement("li");
    empty.className = "fixture-list-empty";
    empty.textContent = "No fixtures yet";
    fixtureListEl.appendChild(empty);
    return;
  }

  for (const fx of sorted) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = `fixture-list-item${isSelected(fx.id) ? " selected" : ""}`;
    item.dataset.fixtureId = fx.id;

    const headNo = document.createElement("span");
    headNo.className = "head-no";
    headNo.textContent = `Head ${fx.headNumber}`;

    const meta = document.createElement("span");
    meta.className = "head-meta";
    meta.textContent = fixtureSummary(fx);

    item.append(headNo, meta);
    item.addEventListener("click", (event) => handleListSelection(fx.id, event));
    fixtureListEl.appendChild(item);
  }
}

/** @param {string} id @param {{ focus?: boolean }} [options] */
function selectFixture(id, options = {}) {
  setSelection([id], options);
}

function focusSelectedFixture() {
  const selected = selectedFixtures();
  if (selected.length === 0) return;

  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;

  for (const fx of selected) {
    const bounds = fixtureBoundsPx(fx, CELL_SIZE);
    minX = Math.min(minX, bounds.x);
    minY = Math.min(minY, bounds.y);
    maxX = Math.max(maxX, bounds.x + bounds.width);
    maxY = Math.max(maxY, bounds.y + bounds.height);
  }

  const boundsW = maxX - minX;
  const boundsH = maxY - minY;
  const rect = canvasWrap.getBoundingClientRect();
  const pad = 64;
  const zoomX = (rect.width - pad) / Math.max(boundsW, CELL_SIZE);
  const zoomY = (rect.height - pad) / Math.max(boundsH, CELL_SIZE);
  const targetZoom = Math.min(2.5, Math.max(MIN_ZOOM, Math.min(zoomX, zoomY)));

  const centerX = minX + boundsW / 2;
  const centerY = minY + boundsH / 2;

  state.zoom = targetZoom;
  state.panX = rect.width / 2 - centerX * targetZoom;
  state.panY = rect.height / 2 - centerY * targetZoom;
  zoomLabel.textContent = `${Math.round(targetZoom * 100)}%`;
  render();
}

function startNewGrid() {
  if (state.fixtures.length > 0) {
    const ok = window.confirm("Clear all fixtures and start a new grid?");
    if (!ok) return;
  }

  const widthInput = /** @type {HTMLInputElement} */ (document.getElementById("grid-width"));
  const heightInput = /** @type {HTMLInputElement} */ (document.getElementById("grid-height"));
  const headInput = /** @type {HTMLInputElement} */ (document.getElementById("head-number"));

  state.gridWidth = Math.max(1, Math.min(512, Number(widthInput.value) || 40));
  state.gridHeight = Math.max(1, Math.min(512, Number(heightInput.value) || 20));
  widthInput.value = String(state.gridWidth);
  heightInput.value = String(state.gridHeight);

  state.fixtures = [];
  state.selectedIds.clear();
  state.nextId = 1;
  state.snapGuides = [];
  headInput.value = "1";

  fitToView();
  updateSelectedPanel();
}

function setZoom(value, anchorX, anchorY) {
  const prev = state.zoom;
  const next = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, value));
  if (anchorX != null && anchorY != null) {
    const worldX = (anchorX - state.panX) / prev;
    const worldY = (anchorY - state.panY) / prev;
    state.panX = anchorX - worldX * next;
    state.panY = anchorY - worldY * next;
  }
  state.zoom = next;
  zoomLabel.textContent = `${Math.round(next * 100)}%`;
  render();
}

function fitToView() {
  const rect = canvasWrap.getBoundingClientRect();
  const contentW = state.gridWidth * CELL_SIZE;
  const contentH = state.gridHeight * CELL_SIZE;
  const pad = 48;
  const scaleX = (rect.width - pad) / contentW;
  const scaleY = (rect.height - pad) / contentH;
  state.zoom = Math.min(1.5, Math.max(MIN_ZOOM, Math.min(scaleX, scaleY)));
  state.panX = (rect.width - contentW * state.zoom) / 2;
  state.panY = (rect.height - contentH * state.zoom) / 2;
  zoomLabel.textContent = `${Math.round(state.zoom * 100)}%`;
  render();
}

function svgPoint(clientX, clientY) {
  const rect = svg.getBoundingClientRect();
  return {
    x: (clientX - rect.left - state.panX) / state.zoom,
    y: (clientY - rect.top - state.panY) / state.zoom,
  };
}

function gridSnap(worldX, worldY, ignoreId, fixture) {
  const rawX = Math.round(worldX / CELL_SIZE);
  const rawY = Math.round(worldY / CELL_SIZE);

  const snapped = snapAnchor(
    rawX,
    rawY,
    state.fixtures,
    ignoreId,
    {
      snapToGrid: state.snapToGrid,
      snapToFixtures: state.snapToFixtures,
      snapDistance: state.snapDistance,
    },
  );

  if (!fixture) {
    state.snapGuides = snapped.guides;
    return { x: rawX, y: rawY };
  }

  /** @param {number} x @param {number} y */
  const isFree = (x, y) => {
    const candidate = { ...fixture, x, y };
    return !fixtureOverlaps(candidate, state.fixtures, ignoreId);
  };

  if (isFree(snapped.x, snapped.y)) {
    state.snapGuides = snapped.guides;
    return { x: snapped.x, y: snapped.y };
  }

  if (isFree(rawX, rawY)) {
    state.snapGuides = [];
    return { x: rawX, y: rawY };
  }

  // Allow out-of-bounds positioning; only block when overlapping another fixture.
  state.snapGuides = [];
  return { x: rawX, y: rawY };
}

function render() {
  const contentW = state.gridWidth * CELL_SIZE;
  const contentH = state.gridHeight * CELL_SIZE;

  svg.setAttribute("viewBox", `0 0 ${canvasWrap.clientWidth} ${canvasWrap.clientHeight}`);

  while (svg.firstChild) svg.removeChild(svg.firstChild);

  viewportGroup = document.createElementNS("http://www.w3.org/2000/svg", "g");
  viewportGroup.setAttribute(
    "transform",
    `translate(${state.panX}, ${state.panY}) scale(${state.zoom})`,
  );
  svg.appendChild(viewportGroup);

  const bg = document.createElementNS("http://www.w3.org/2000/svg", "rect");
  bg.setAttribute("class", "grid-bg");
  bg.setAttribute("width", String(contentW));
  bg.setAttribute("height", String(contentH));
  viewportGroup.appendChild(bg);

  for (let x = 0; x <= state.gridWidth; x += 1) {
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("class", "grid-line");
    line.setAttribute("x1", String(x * CELL_SIZE));
    line.setAttribute("y1", "0");
    line.setAttribute("x2", String(x * CELL_SIZE));
    line.setAttribute("y2", String(contentH));
    viewportGroup.appendChild(line);
  }

  for (let y = 0; y <= state.gridHeight; y += 1) {
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("class", "grid-line");
    line.setAttribute("x1", "0");
    line.setAttribute("y1", String(y * CELL_SIZE));
    line.setAttribute("x2", String(contentW));
    line.setAttribute("y2", String(y * CELL_SIZE));
    viewportGroup.appendChild(line);
  }

  for (const guide of state.snapGuides) {
    const guideLine = document.createElementNS("http://www.w3.org/2000/svg", "line");
    guideLine.setAttribute("class", "snap-guide");
    if (guide.type === "v") {
      const px = guide.pos * CELL_SIZE;
      guideLine.setAttribute("x1", String(px));
      guideLine.setAttribute("y1", "0");
      guideLine.setAttribute("x2", String(px));
      guideLine.setAttribute("y2", String(contentH));
    } else {
      const py = guide.pos * CELL_SIZE;
      guideLine.setAttribute("x1", "0");
      guideLine.setAttribute("y1", String(py));
      guideLine.setAttribute("x2", String(contentW));
      guideLine.setAttribute("y2", String(py));
    }
    viewportGroup.appendChild(guideLine);
  }

  for (const fixture of state.fixtures) {
    renderFixture(fixture);
  }

  // Group bounding box + resize handles (2+ selected, not while doing a marquee)
  if (state.selectedIds.size >= 2 && !interaction.marquee) {
    const sel = selectedFixtures();
    const SVG_NS_LOCAL = "http://www.w3.org/2000/svg";

    // Visual outline: encompass all cells of all selected fixtures
    const allCells = sel.flatMap((f) => getFixtureCells(f));
    const vcMinX = Math.min(...allCells.map((c) => c.x)) * CELL_SIZE;
    const vcMinY = Math.min(...allCells.map((c) => c.y)) * CELL_SIZE;
    const vcMaxX = (Math.max(...allCells.map((c) => c.x)) + 1) * CELL_SIZE;
    const vcMaxY = (Math.max(...allCells.map((c) => c.y)) + 1) * CELL_SIZE;

    const pad = 3 / state.zoom;
    const bbox = document.createElementNS(SVG_NS_LOCAL, "rect");
    bbox.setAttribute("class", "selection-bbox");
    bbox.setAttribute("x", String(vcMinX - pad));
    bbox.setAttribute("y", String(vcMinY - pad));
    bbox.setAttribute("width", String(vcMaxX - vcMinX + pad * 2));
    bbox.setAttribute("height", String(vcMaxY - vcMinY + pad * 2));
    bbox.setAttribute("stroke-width", String(1.5 / state.zoom));
    bbox.setAttribute("stroke-dasharray", `${6 / state.zoom} ${3 / state.zoom}`);
    viewportGroup.appendChild(bbox);

    // Anchor grid bounds (used for handle positions and resize maths)
    const acMinGx = Math.min(...sel.map((f) => f.x));
    const acMaxGx = Math.max(...sel.map((f) => f.x));
    const acMinGy = Math.min(...sel.map((f) => f.y));
    const acMaxGy = Math.max(...sel.map((f) => f.y));
    const acMidGx = (acMinGx + acMaxGx) / 2;
    const acMidGy = (acMinGy + acMaxGy) / 2;

    // Half-size of each handle in world pixels (constant screen size ~8 px)
    const hs = 5 / state.zoom;

    // [id, worldX, worldY, cursor, anchorGx, anchorGy, freeOrigGx, freeOrigGy, axes]
    const handleDefs = [
      ["nw", acMinGx, acMinGy, "nw-resize", acMaxGx, acMaxGy, acMinGx, acMinGy, "xy"],
      ["n",  acMidGx, acMinGy, "n-resize",  0,       acMaxGy, 0,       acMinGy, "y"],
      ["ne", acMaxGx, acMinGy, "ne-resize", acMinGx, acMaxGy, acMaxGx, acMinGy, "xy"],
      ["e",  acMaxGx, acMidGy, "e-resize",  acMinGx, 0,       acMaxGx, 0,       "x"],
      ["se", acMaxGx, acMaxGy, "se-resize", acMinGx, acMinGy, acMaxGx, acMaxGy, "xy"],
      ["s",  acMidGx, acMaxGy, "s-resize",  0,       acMinGy, 0,       acMaxGy, "y"],
      ["sw", acMinGx, acMaxGy, "sw-resize", acMaxGx, acMinGy, acMinGx, acMaxGy, "xy"],
      ["w",  acMinGx, acMidGy, "w-resize",  acMaxGx, 0,       acMinGx, 0,       "x"],
    ];

    for (const [, hgx, hgy, cursor, ancX, ancY, freeX, freeY, axes] of handleDefs) {
      const h = document.createElementNS(SVG_NS_LOCAL, "rect");
      h.setAttribute("class", "resize-handle");
      h.setAttribute("x", String(hgx * CELL_SIZE - hs));
      h.setAttribute("y", String(hgy * CELL_SIZE - hs));
      h.setAttribute("width", String(hs * 2));
      h.setAttribute("height", String(hs * 2));
      h.setAttribute("stroke-width", String(1 / state.zoom));
      h.style.cursor = cursor;
      h.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;
        e.stopPropagation();
        e.preventDefault();
        interaction.resizing = true;
        interaction.resizeAnchorGx = ancX;
        interaction.resizeAnchorGy = ancY;
        interaction.resizeFreeOrigGx = freeX;
        interaction.resizeFreeOrigGy = freeY;
        interaction.resizeAxes = /** @type {"x" | "y" | "xy"} */ (axes);
        interaction.resizeOrigins = new Map(
          selectedFixtures().map((f) => [f.id, { x: f.x, y: f.y }]),
        );
      });
      viewportGroup.appendChild(h);
    }
  }

  if (interaction.marquee) {
    const x = Math.min(interaction.marqueeStart.x, interaction.marqueeEnd.x);
    const y = Math.min(interaction.marqueeStart.y, interaction.marqueeEnd.y);
    const w = Math.abs(interaction.marqueeEnd.x - interaction.marqueeStart.x);
    const h = Math.abs(interaction.marqueeEnd.y - interaction.marqueeStart.y);
    const marquee = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    marquee.setAttribute("class", "selection-marquee");
    marquee.setAttribute("x", String(x));
    marquee.setAttribute("y", String(y));
    marquee.setAttribute("width", String(w));
    marquee.setAttribute("height", String(h));
    viewportGroup.appendChild(marquee);
  }
}

/** @param {import("./fixture.js").Fixture} fixture */
function renderFixture(fixture) {
  const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
  group.dataset.fixtureId = fixture.id;

  const overlaps = fixtureOverlaps(fixture, state.fixtures, fixture.id);
  const inBounds = isFixtureInBounds(fixture, state.gridWidth, state.gridHeight);
  const selected = isSelected(fixture.id);
  const pad = 3;
  const cellClass = `fixture-body${selected ? " selected" : ""}${!overlaps && inBounds ? "" : " invalid"}`;
  const count = pixelCount(fixture);
  const midElement = Math.max(1, Math.floor((count - 1) / 2) + 1);
  /** @type {SVGRectElement[]} */
  const hitTargets = [];

  for (let element = 1; element <= count; element += 1) {
    const { x, y } = cellForElement(fixture, element);
    const cellBody = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    cellBody.setAttribute("class", cellClass);
    cellBody.setAttribute("x", String(x * CELL_SIZE + pad));
    cellBody.setAttribute("y", String(y * CELL_SIZE + pad));
    cellBody.setAttribute("width", String(CELL_SIZE - pad * 2));
    cellBody.setAttribute("height", String(CELL_SIZE - pad * 2));
    cellBody.setAttribute("rx", "4");
    group.appendChild(cellBody);
    hitTargets.push(cellBody);

    if (element !== midElement) {
      const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("class", "pixel-label");
      label.setAttribute("x", String(x * CELL_SIZE + CELL_SIZE / 2));
      label.setAttribute("y", String(y * CELL_SIZE + CELL_SIZE / 2 + 3));
      label.setAttribute("text-anchor", "middle");
      label.textContent = String(element);
      group.appendChild(label);
    }
  }

  const arrow = directionEndPx(fixture, CELL_SIZE);
  const arrowLine = document.createElementNS("http://www.w3.org/2000/svg", "line");
  arrowLine.setAttribute("class", "direction-arrow");
  arrowLine.setAttribute("x1", String(arrow.x1));
  arrowLine.setAttribute("y1", String(arrow.y1));
  arrowLine.setAttribute("x2", String(arrow.x2));
  arrowLine.setAttribute("y2", String(arrow.y2));
  group.appendChild(arrowLine);

  const start = startPointPx(fixture, CELL_SIZE);
  const marker = document.createElementNS("http://www.w3.org/2000/svg", "circle");
  marker.setAttribute("class", "start-marker");
  marker.setAttribute("cx", String(start.cx));
  marker.setAttribute("cy", String(start.cy));
  marker.setAttribute("r", "5");
  group.appendChild(marker);

  const midCell = cellForElement(fixture, midElement);
  const headLabel = document.createElementNS("http://www.w3.org/2000/svg", "text");
  headLabel.setAttribute("class", "head-label");
  headLabel.setAttribute("x", String(midCell.x * CELL_SIZE + CELL_SIZE / 2));
  headLabel.setAttribute("y", String(midCell.y * CELL_SIZE + CELL_SIZE / 2 + 6));
  headLabel.setAttribute("text-anchor", "middle");
  headLabel.textContent = `H${fixture.headNumber}`;
  group.appendChild(headLabel);

  const onMouseDown = (event) => {
    if (event.button !== 0) return;
    event.stopPropagation();

    if (event.ctrlKey || event.metaKey) {
      setSelection([fixture.id], { toggle: true });
      return;
    }

    if (!isSelected(fixture.id)) {
      setSelection([fixture.id]);
    }

    interaction.dragging = true;
    interaction.moved = false;
    interaction.fixtureId = fixture.id;
    interaction.dragOrigins = new Map();
    for (const id of state.selectedIds) {
      const fx = state.fixtures.find((f) => f.id === id);
      if (fx) interaction.dragOrigins.set(id, { x: fx.x, y: fx.y });
    }
    interaction.dragAnchorStart = { x: fixture.x, y: fixture.y };
    const pt = svgPoint(event.clientX, event.clientY);
    interaction.offsetX = pt.x - fixture.x * CELL_SIZE;
    interaction.offsetY = pt.y - fixture.y * CELL_SIZE;
  };

  const onClick = (event) => {
    event.stopPropagation();
    if (interaction.moved || interaction.marquee) return;
    if (state.selectedIds.size !== 1 || !isSelected(fixture.id)) return;
    fixture.rotation = nextRotation(fixture.rotation);
    render();
    updateSelectedPanel();
  };

  const onDblClick = (event) => {
    event.stopPropagation();
    if (state.selectedIds.size !== 1 || !isSelected(fixture.id)) return;
    const value = window.prompt("Head number:", String(fixture.headNumber));
    if (value == null) return;
    const parsed = Math.max(1, Number(value) || fixture.headNumber);
    fixture.headNumber = parsed;
    render();
    updateSelectedPanel();
  };

  for (const target of hitTargets) {
    target.addEventListener("mousedown", onMouseDown);
    target.addEventListener("click", onClick);
    target.addEventListener("dblclick", onDblClick);
  }

  viewportGroup?.appendChild(group);
}

function applyGridSize() {
  const widthInput = /** @type {HTMLInputElement} */ (document.getElementById("grid-width"));
  const heightInput = /** @type {HTMLInputElement} */ (document.getElementById("grid-height"));
  state.gridWidth = Math.max(1, Math.min(512, Number(widthInput.value) || 1));
  state.gridHeight = Math.max(1, Math.min(512, Number(heightInput.value) || 1));
  widthInput.value = String(state.gridWidth);
  heightInput.value = String(state.gridHeight);

  for (const id of [...state.selectedIds]) {
    if (!state.fixtures.some((f) => f.id === id)) state.selectedIds.delete(id);
  }
  fitToView();
  updateSelectedPanel();
}

function addFixture() {
  const headInput = /** @type {HTMLInputElement} */ (document.getElementById("head-number"));
  const pixelInput = /** @type {HTMLInputElement} */ (document.getElementById("pixel-count"));
  const headNumber = Math.max(1, Number(headInput.value) || 1);
  const linePixels = Math.max(1, Math.min(512, Number(pixelInput.value) || 1));
  const shape = createLineFixtureDefaults(linePixels);
  /** @type {import("./fixture.js").Fixture} */
  const prototype = {
    id: "probe",
    headNumber: 0,
    x: 0,
    y: 0,
    rotation: 0,
    ...shape,
  };
  const { x, y } = findPlacement(state.gridWidth, state.gridHeight, prototype, state.fixtures, {
    snapToFixtures: state.snapToFixtures,
  });

  const fixture = {
    id: uid(),
    headNumber,
    x,
    y,
    rotation: 0,
    ...shape,
  };

  state.fixtures.push(fixture);
  headInput.value = String(headNumber + 1);
  setSelection([fixture.id]);
}

function addMatrixFixture() {
  const headInput = /** @type {HTMLInputElement} */ (document.getElementById("head-number"));
  const headNumber = Math.max(1, Number(headInput.value) || 1);
  const draft = getMatrixDraft();
  /** @type {import("./fixture.js").Fixture} */
  const prototype = {
    id: "probe",
    headNumber: 0,
    x: 0,
    y: 0,
    rotation: 0,
    matrixWidth: draft.matrixWidth,
    matrixHeight: draft.matrixHeight,
    pixelOrder: draft.pixelOrder,
  };
  const { x, y } = findPlacement(state.gridWidth, state.gridHeight, prototype, state.fixtures, {
    snapToFixtures: state.snapToFixtures,
  });

  const fixture = {
    id: uid(),
    headNumber,
    x,
    y,
    rotation: 0,
    matrixWidth: draft.matrixWidth,
    matrixHeight: draft.matrixHeight,
    pixelOrder: draft.pixelOrder,
  };

  state.fixtures.push(fixture);
  headInput.value = String(headNumber + 1);
  setSelection([fixture.id]);
}

function deleteSelectedFixture() {
  if (state.selectedIds.size === 0) return;
  state.fixtures = state.fixtures.filter((f) => !state.selectedIds.has(f.id));
  state.selectedIds.clear();
  render();
  updateSelectedPanel();
}

function rotateSelectedFixture() {
  for (const fx of selectedFixtures()) {
    fx.rotation = nextRotation(fx.rotation);
  }
  render();
  updateSelectedPanel();
}

/** @param {"h" | "v"} axis */
function distributeFixtures(axis) {
  const sel = selectedFixtures();
  if (sel.length < 3) return;
  if (axis === "h") {
    sel.sort((a, b) => a.x - b.x);
    const first = sel[0].x;
    const last = sel[sel.length - 1].x;
    const step = (last - first) / (sel.length - 1);
    for (let i = 1; i < sel.length - 1; i++) {
      sel[i].x = Math.round(first + i * step);
    }
  } else {
    sel.sort((a, b) => a.y - b.y);
    const first = sel[0].y;
    const last = sel[sel.length - 1].y;
    const step = (last - first) / (sel.length - 1);
    for (let i = 1; i < sel.length - 1; i++) {
      sel[i].y = Math.round(first + i * step);
    }
  }
  render();
  updateSelectedPanel();
}

function setEvenSpacingMode(active) {
  interaction.evenSpacingPending = active;
  const hint = document.getElementById("kbd-hint");
  if (!hint) return;
  if (active) {
    hint.textContent = "Even spacing — press H (horizontal) · V (vertical) · Esc (cancel)";
    hint.removeAttribute("hidden");
  } else {
    hint.setAttribute("hidden", "");
  }
}

function updateSelectedHeadNumber() {
  const fx = selectedFixture();
  if (!fx) return;
  const value = Math.max(1, Number(editHeadNumber.value) || 1);
  fx.headNumber = value;
  render();
  updateSelectedPanel();
}

function exportFilenameInput() {
  return /** @type {HTMLInputElement} */ (document.getElementById("export-filename"));
}

function initExportSettings() {
  exportFilenameInput().value = loadExportFilename();
}

async function exportCsvFile() {
  const invalid = state.fixtures.filter(
    (fx) => !isFixtureValid(fx, state.gridWidth, state.gridHeight, state.fixtures, fx.id),
  );
  if (invalid.length > 0) {
    window.alert("Some fixtures are out of bounds or overlapping. Fix them before exporting.");
    return;
  }

  const filenameInput = exportFilenameInput();
  const filename = filenameInput.value.trim() || "grid101.csv";
  saveExportFilename(filename);
  const csv = buildGridCsv(state.gridWidth, state.gridHeight, state.fixtures);
  const result = await saveCsvWithDialog(filename, csv);

  if (!result.ok) {
    if (result.reason === "error") {
      window.alert("Could not save the CSV file.");
    }
    return;
  }

  filenameInput.value = result.filename;
  saveExportFilename(result.filename);
}

/** @param {string} content @param {string} [filename] */
function importCsvContent(content, filename) {
  const { width, height, cells } = parseGridCsv(content);
  if (width === 0 || height === 0) {
    window.alert("Could not read grid data from CSV.");
    return;
  }

  const { fixtures, nextHead, warnings } = fixturesFromGrid(cells, width, height);
  state.gridWidth = width;
  state.gridHeight = height;
  state.fixtures = fixtures;
  state.nextId = fixtures.length + 1;
  state.selectedIds.clear();
  state.snapGuides = [];

  const widthInput = /** @type {HTMLInputElement} */ (document.getElementById("grid-width"));
  const heightInput = /** @type {HTMLInputElement} */ (document.getElementById("grid-height"));
  const headInput = /** @type {HTMLInputElement} */ (document.getElementById("head-number"));
  widthInput.value = String(width);
  heightInput.value = String(height);
  headInput.value = String(nextHead);

  if (filename) {
    const exportInput = exportFilenameInput();
    exportInput.value = filename.endsWith(".csv") ? filename : `${filename}.csv`;
    saveExportFilename(exportInput.value);
  }

  fitToView();
  updateSelectedPanel();

  const summary = `Imported ${fixtures.length} fixture(s) into a ${width}×${height} grid.`;
  if (warnings.length > 0) {
    window.alert(`${summary}\n\nWarnings:\n${warnings.slice(0, 8).join("\n")}${warnings.length > 8 ? `\n… and ${warnings.length - 8} more` : ""}`);
  } else {
    window.alert(summary);
  }
}

async function importCsvFile() {
  const picked = await openCsvForImport();
  if (picked && "cancelled" in picked) return;
  if (picked) {
    importCsvContent(picked.content, picked.filename);
    return;
  }
  importInput.click();
}

function syncSnapControls() {
  const snapGrid = /** @type {HTMLInputElement} */ (document.getElementById("snap-grid"));
  const snapFixtures = /** @type {HTMLInputElement} */ (document.getElementById("snap-fixtures"));
  snapGrid.checked = state.snapToGrid;
  snapFixtures.checked = state.snapToFixtures;
  snapFixtures.disabled = !state.snapToGrid;
}

canvasWrap.addEventListener(
  "wheel",
  (event) => {
    event.preventDefault();
    const rect = canvasWrap.getBoundingClientRect();
    const anchorX = event.clientX - rect.left;
    const anchorY = event.clientY - rect.top;
    const delta = event.deltaY > 0 ? 0.9 : 1.1;
    setZoom(state.zoom * delta, anchorX, anchorY);
  },
  { passive: false },
);

canvasWrap.addEventListener("mousedown", (event) => {
  if (interaction.resizing) return;

  if (event.button === 1 || (event.button === 0 && interaction.spaceHeld)) {
    interaction.panning = true;
    interaction.lastX = event.clientX;
    interaction.lastY = event.clientY;
    canvasWrap.classList.add("panning");
    event.preventDefault();
    return;
  }

  if (event.button === 0 && !interaction.dragging) {
    interaction.marquee = true;
    interaction.moved = false;
    const pt = svgPoint(event.clientX, event.clientY);
    interaction.marqueeStart = pt;
    interaction.marqueeEnd = pt;
  }
});

window.addEventListener("mousemove", (event) => {
  if (interaction.resizing) {
    const pt = svgPoint(event.clientX, event.clientY);
    const curGx = pt.x / CELL_SIZE;
    const curGy = pt.y / CELL_SIZE;
    const dxOrig = interaction.resizeFreeOrigGx - interaction.resizeAnchorGx;
    const dyOrig = interaction.resizeFreeOrigGy - interaction.resizeAnchorGy;
    const scaleX = Math.abs(dxOrig) > 0.01 ? (curGx - interaction.resizeAnchorGx) / dxOrig : 1;
    const scaleY = Math.abs(dyOrig) > 0.01 ? (curGy - interaction.resizeAnchorGy) / dyOrig : 1;
    for (const [id, orig] of interaction.resizeOrigins) {
      const fx = state.fixtures.find((f) => f.id === id);
      if (!fx) continue;
      if (interaction.resizeAxes !== "y") {
        fx.x = Math.round(interaction.resizeAnchorGx + (orig.x - interaction.resizeAnchorGx) * scaleX);
      }
      if (interaction.resizeAxes !== "x") {
        fx.y = Math.round(interaction.resizeAnchorGy + (orig.y - interaction.resizeAnchorGy) * scaleY);
      }
    }
    render();
    return;
  }

  if (interaction.panning) {
    state.panX += event.clientX - interaction.lastX;
    state.panY += event.clientY - interaction.lastY;
    interaction.lastX = event.clientX;
    interaction.lastY = event.clientY;
    render();
    return;
  }

  if (interaction.marquee) {
    interaction.marqueeEnd = svgPoint(event.clientX, event.clientY);
    const w = Math.abs(interaction.marqueeEnd.x - interaction.marqueeStart.x);
    const h = Math.abs(interaction.marqueeEnd.y - interaction.marqueeStart.y);
    if (w > MARQUEE_MIN_PX || h > MARQUEE_MIN_PX) interaction.moved = true;
    render();
    return;
  }

  if (!interaction.dragging || !interaction.fixtureId) return;

  const fx = state.fixtures.find((f) => f.id === interaction.fixtureId);
  if (!fx) return;

  const pt = svgPoint(event.clientX, event.clientY);
  const snap = gridSnap(pt.x - interaction.offsetX, pt.y - interaction.offsetY, interaction.fixtureId, fx);
  const dx = snap.x - interaction.dragAnchorStart.x;
  const dy = snap.y - interaction.dragAnchorStart.y;

  if (dx !== 0 || dy !== 0) interaction.moved = true;

  /** @type {Map<string, { x: number, y: number }>} */
  const nextPositions = new Map();
  for (const [id, origin] of interaction.dragOrigins) {
    nextPositions.set(id, { x: origin.x + dx, y: origin.y + dy });
  }

  if (canPlaceAll(nextPositions)) {
    for (const [id, pos] of nextPositions) {
      const target = state.fixtures.find((f) => f.id === id);
      if (target) {
        target.x = pos.x;
        target.y = pos.y;
      }
    }
  }

  render();
  updateSelectedPanel();
});

window.addEventListener("mouseup", (event) => {
  if (interaction.marquee) {
    if (interaction.moved) {
      const ids = fixturesInRect(
        interaction.marqueeStart.x,
        interaction.marqueeStart.y,
        interaction.marqueeEnd.x,
        interaction.marqueeEnd.y,
      );
      if (event.shiftKey || event.ctrlKey || event.metaKey) {
        setSelection(ids, { append: true });
      } else {
        setSelection(ids);
      }
    } else {
      clearSelection();
    }
  }

  interaction.dragging = false;
  interaction.panning = false;
  interaction.marquee = false;
  interaction.resizing = false;
  interaction.resizeOrigins = new Map();
  interaction.fixtureId = null;
  interaction.dragOrigins = new Map();
  state.snapGuides = [];
  canvasWrap.classList.remove("panning");
  render();
});

window.addEventListener("keydown", (event) => {
  const inInput = document.activeElement?.tagName === "INPUT" || document.activeElement?.tagName === "SELECT";

  if (event.code === "Space" && !event.repeat) {
    interaction.spaceHeld = true;
    canvasWrap.classList.add("panning");
    event.preventDefault();
  }

  if (!inInput) {
    if (event.key === "ArrowLeft") {
      state.panX += PAN_STEP;
      render();
      event.preventDefault();
    } else if (event.key === "ArrowRight") {
      state.panX -= PAN_STEP;
      render();
      event.preventDefault();
    } else if (event.key === "ArrowUp") {
      state.panY += PAN_STEP;
      render();
      event.preventDefault();
    } else if (event.key === "ArrowDown") {
      state.panY -= PAN_STEP;
      render();
      event.preventDefault();
    } else if (event.key === "+" || event.key === "=") {
      setZoom(state.zoom * 1.25);
    } else if (event.key === "-") {
      setZoom(state.zoom / 1.25);
    }
  }

  if (!inInput) {
    if (event.key === "Escape") {
      if (interaction.evenSpacingPending) {
        setEvenSpacingMode(false);
        event.preventDefault();
      }
    } else if (event.key === "e" || event.key === "E") {
      if (state.selectedIds.size >= 3) {
        setEvenSpacingMode(!interaction.evenSpacingPending);
        event.preventDefault();
      }
    } else if (interaction.evenSpacingPending && (event.key === "h" || event.key === "H")) {
      distributeFixtures("h");
      setEvenSpacingMode(false);
      event.preventDefault();
    } else if (interaction.evenSpacingPending && (event.key === "v" || event.key === "V")) {
      distributeFixtures("v");
      setEvenSpacingMode(false);
      event.preventDefault();
    } else if (event.key === "r" || event.key === "R") {
      rotateSelectedFixture();
    } else if (event.key === "f" || event.key === "F") {
      focusSelectedFixture();
    } else if (event.key === "a" || event.key === "A") {
      const now = Date.now();
      if (now - interaction.lastAKeyTime < DOUBLE_A_MS) {
        clearSelection();
        interaction.lastAKeyTime = 0;
      } else {
        selectAllFixtures();
        interaction.lastAKeyTime = now;
      }
      event.preventDefault();
    } else if (event.key === "Delete" || event.key === "Backspace") {
      deleteSelectedFixture();
    }
  }
});

window.addEventListener("keyup", (event) => {
  if (event.code === "Space") {
    interaction.spaceHeld = false;
    if (!interaction.panning) canvasWrap.classList.remove("panning");
  }
});

window.addEventListener("resize", () => render());

document.getElementById("apply-grid")?.addEventListener("click", applyGridSize);
document.getElementById("new-grid")?.addEventListener("click", startNewGrid);
document.getElementById("add-fixture")?.addEventListener("click", addFixture);
document.getElementById("rotate-fixture")?.addEventListener("click", rotateSelectedFixture);
document.getElementById("focus-fixture")?.addEventListener("click", focusSelectedFixture);
document.getElementById("delete-fixture")?.addEventListener("click", deleteSelectedFixture);
document.getElementById("export-csv")?.addEventListener("click", () => {
  void exportCsvFile();
});
document.getElementById("import-csv")?.addEventListener("click", () => {
  void importCsvFile();
});
exportFilenameInput().addEventListener("change", (event) => {
  saveExportFilename(/** @type {HTMLInputElement} */ (event.target).value.trim() || "grid101.csv");
});
document.getElementById("snap-grid")?.addEventListener("change", (event) => {
  state.snapToGrid = /** @type {HTMLInputElement} */ (event.target).checked;
  syncSnapControls();
});
document.getElementById("snap-fixtures")?.addEventListener("change", (event) => {
  state.snapToFixtures = /** @type {HTMLInputElement} */ (event.target).checked;
});
importInput.addEventListener("change", async () => {
  const file = importInput.files?.[0];
  importInput.value = "";
  if (!file) return;
  const content = await file.text();
  importCsvContent(content, file.name);
});
document.getElementById("zoom-fit")?.addEventListener("click", fitToView);
document.getElementById("zoom-reset")?.addEventListener("click", () => {
  setZoom(1);
  state.panX = 40;
  state.panY = 40;
  render();
});

editHeadNumber.addEventListener("change", updateSelectedHeadNumber);
editHeadNumber.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    updateSelectedHeadNumber();
    editHeadNumber.blur();
  }
});

fitToView();
updateSelectedPanel();
syncSnapControls();
initExportSettings();
initMatrixPanel({ onAdd: addMatrixFixture });
