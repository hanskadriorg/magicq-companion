const STORAGE_KEY = "chamsys.fixtureTemplates";

/** @typedef {{ id: string, name: string, matrixWidth: number, matrixHeight: number, pixelOrder: import("./matrix.js").PixelOrder }} FixtureTemplate */

/** @returns {FixtureTemplate[]} */
export function loadTemplates() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

/** @param {FixtureTemplate[]} templates */
function saveTemplates(templates) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(templates));
}

/** @param {Omit<FixtureTemplate, "id">} template */
export function addTemplate(template) {
  const templates = loadTemplates();
  const entry = {
    id: `tpl-${Date.now()}`,
    ...template,
  };
  templates.push(entry);
  saveTemplates(templates);
  return entry;
}

/** @param {string} id */
export function deleteTemplate(id) {
  saveTemplates(loadTemplates().filter((t) => t.id !== id));
}

/** @param {string} id */
export function getTemplate(id) {
  return loadTemplates().find((t) => t.id === id) ?? null;
}
