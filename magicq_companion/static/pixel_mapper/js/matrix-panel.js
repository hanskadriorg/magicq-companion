import { elementToLocal, getOrderDef } from "./matrix.js";
import { addTemplate, deleteTemplate, loadTemplates } from "./templates.js";

/** @typedef {{ matrixWidth: number, matrixHeight: number, pixelOrder: import("./matrix.js").PixelOrder }} MatrixDraft */

/** @type {MatrixDraft} */
let draft = {
  matrixWidth: 4,
  matrixHeight: 3,
  pixelOrder: "row-snake-lr",
};

/** @type {(draft: MatrixDraft) => void | null} */
let onChange = null;

function matrixWidthInput() {
  return /** @type {HTMLInputElement} */ (document.getElementById("matrix-width"));
}

function matrixHeightInput() {
  return /** @type {HTMLInputElement} */ (document.getElementById("matrix-height"));
}

function templateSelect() {
  return /** @type {HTMLSelectElement} */ (document.getElementById("matrix-template-select"));
}

function orderGridEl() {
  return document.getElementById("pixel-order-grid");
}

function previewEl() {
  return document.getElementById("matrix-order-preview");
}

function readDraftFromInputs() {
  draft.matrixWidth = Math.max(1, Math.min(64, Number(matrixWidthInput()?.value) || 1));
  draft.matrixHeight = Math.max(1, Math.min(64, Number(matrixHeightInput()?.value) || 1));
  if (matrixWidthInput()) matrixWidthInput().value = String(draft.matrixWidth);
  if (matrixHeightInput()) matrixHeightInput().value = String(draft.matrixHeight);
}

export function getMatrixDraft() {
  readDraftFromInputs();
  return { ...draft };
}


function renderLargePreview() {
  const el = previewEl();
  if (!el) return;

  readDraftFromInputs();
  const def = getOrderDef(draft.pixelOrder);
  const w = draft.matrixWidth;
  const h = draft.matrixHeight;
  const count = w * h;
  const cellSize = Math.max(14, Math.min(22, Math.floor(120 / Math.max(w, h))));
  const svgW = w * cellSize + 8;
  const svgH = h * cellSize + 8;

  let svg = "";
  for (let element = 1; element <= count; element += 1) {
    const local = elementToLocal(element, w, h, draft.pixelOrder);
    if (!local) continue;
    const px = 4 + local.x * cellSize;
    const py = 4 + local.y * cellSize;
    const isStart = element === 1;
    svg += `<rect x="${px}" y="${py}" width="${cellSize - 2}" height="${cellSize - 2}" rx="2" class="${isStart ? "preview-cell-start" : "preview-cell"}" />`;
    svg += `<text x="${px + (cellSize - 2) / 2}" y="${py + (cellSize - 2) / 2 + 4}" text-anchor="middle" class="preview-cell-num">${element}</text>`;
  }

  el.innerHTML = `<div class="matrix-preview-wrap">
    <svg viewBox="0 0 ${svgW} ${svgH}" width="${svgW}" height="${svgH}">${svg}</svg>
    <p class="hint">${def.label} · ${w}×${h} = ${count} px</p>
  </div>`;
}

function renderOrderGrid() {
  const grid = orderGridEl();
  if (!grid) return;
  grid.querySelectorAll("[data-order]").forEach((btn) => {
    const el = /** @type {HTMLElement} */ (btn);
    el.classList.toggle("selected", el.dataset.order === draft.pixelOrder);
  });
}

function bindOrderGridClicks() {
  const grid = orderGridEl();
  if (!grid) return;
  grid.querySelectorAll("[data-order]").forEach((btn) => {
    btn.addEventListener("click", () => {
      draft.pixelOrder = /** @type {import("./matrix.js").PixelOrder} */ (
        /** @type {HTMLElement} */ (btn).dataset.order
      );
      renderOrderGrid();
      renderLargePreview();
      onChange?.(getMatrixDraft());
    });
  });
}

function refreshTemplateSelect() {
  const select = templateSelect();
  if (!select) return;

  const templates = loadTemplates();
  select.replaceChildren();
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = templates.length ? "Load saved fixture…" : "No saved fixtures";
  select.appendChild(empty);

  for (const tpl of templates) {
    const opt = document.createElement("option");
    opt.value = tpl.id;
    opt.textContent = `${tpl.name} (${tpl.matrixWidth}×${tpl.matrixHeight})`;
    select.appendChild(opt);
  }
}

/** @param {string} id */
function applyTemplate(id) {
  const tpl = loadTemplates().find((t) => t.id === id);
  if (!tpl) return;

  draft.matrixWidth = tpl.matrixWidth;
  draft.matrixHeight = tpl.matrixHeight;
  draft.pixelOrder = tpl.pixelOrder;
  matrixWidthInput().value = String(tpl.matrixWidth);
  matrixHeightInput().value = String(tpl.matrixHeight);
  renderOrderGrid();
  renderLargePreview();
  onChange?.(getMatrixDraft());
}

function saveCurrentTemplate() {
  readDraftFromInputs();
  const name = window.prompt("Fixture template name:", `${draft.matrixWidth}×${draft.matrixHeight} matrix`);
  if (!name?.trim()) return;

  addTemplate({
    name: name.trim(),
    matrixWidth: draft.matrixWidth,
    matrixHeight: draft.matrixHeight,
    pixelOrder: draft.pixelOrder,
  });
  refreshTemplateSelect();
}

function deleteSelectedTemplate() {
  const select = templateSelect();
  const id = select?.value;
  if (!id) return;
  const tpl = loadTemplates().find((t) => t.id === id);
  if (!tpl) return;
  if (!window.confirm(`Delete template "${tpl.name}"?`)) return;
  deleteTemplate(id);
  select.value = "";
  refreshTemplateSelect();
}

/**
 * @param {{ onChange?: (draft: MatrixDraft) => void, onAdd?: () => void }} handlers
 */
export function initMatrixPanel(handlers = {}) {
  onChange = handlers.onChange ?? null;

  readDraftFromInputs();
  renderOrderGrid();
  bindOrderGridClicks();
  renderLargePreview();
  refreshTemplateSelect();

  matrixWidthInput()?.addEventListener("change", () => {
    readDraftFromInputs();
    renderLargePreview();
    onChange?.(getMatrixDraft());
  });
  matrixHeightInput()?.addEventListener("change", () => {
    readDraftFromInputs();
    renderLargePreview();
    onChange?.(getMatrixDraft());
  });

  templateSelect()?.addEventListener("change", (event) => {
    const id = /** @type {HTMLSelectElement} */ (event.target).value;
    if (id) applyTemplate(id);
  });

  document.getElementById("save-matrix-template")?.addEventListener("click", saveCurrentTemplate);
  document.getElementById("delete-matrix-template")?.addEventListener("click", deleteSelectedTemplate);
  document.getElementById("add-matrix-fixture")?.addEventListener("click", () => handlers.onAdd?.());
}

/** @param {MatrixDraft} config */
export function applyMatrixDraft(config) {
  draft = { ...config };
  matrixWidthInput().value = String(draft.matrixWidth);
  matrixHeightInput().value = String(draft.matrixHeight);
  renderOrderGrid();
  renderLargePreview();
}
