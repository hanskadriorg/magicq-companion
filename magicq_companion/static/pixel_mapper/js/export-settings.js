import { downloadCsv } from "./csv.js";

const STORAGE_KEY_FILENAME = "chamsys.exportFilename";
const EXPORT_PICKER_ID = "chamsys-export-csv";
const IMPORT_PICKER_ID = "chamsys-import-csv";
const CSV_TYPES = [{ description: "CSV", accept: { "text/csv": [".csv"] } }];

export function loadExportFilename() {
  return localStorage.getItem(STORAGE_KEY_FILENAME) || "grid101.csv";
}

/** @param {string} filename */
export function saveExportFilename(filename) {
  localStorage.setItem(STORAGE_KEY_FILENAME, filename);
}

function normalizeFilename(filename) {
  return filename.endsWith(".csv") ? filename : `${filename}.csv`;
}

/**
 * Open a save dialog and write CSV content.
 * Chrome/Edge remember the last folder via the stable picker id.
 * @param {string} filename
 * @param {string} content
 * @returns {Promise<{ ok: true, filename: string } | { ok: false, reason: "cancelled" | "error" }>}
 */
export async function saveCsvWithDialog(filename, content) {
  const normalized = normalizeFilename(filename);

  if (typeof window.showSaveFilePicker === "function") {
    try {
      const fileHandle = await window.showSaveFilePicker({
        id: EXPORT_PICKER_ID,
        suggestedName: normalized,
        types: CSV_TYPES,
      });
      const writable = await fileHandle.createWritable();
      await writable.write(content);
      await writable.close();
      return { ok: true, filename: fileHandle.name };
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        return { ok: false, reason: "cancelled" };
      }
      return { ok: false, reason: "error" };
    }
  }

  downloadCsv(normalized, content);
  return { ok: true, filename: normalized };
}

/**
 * Open a file picker for CSV import.
 * Returns null when the native picker is unavailable — use a file input fallback.
 * @returns {Promise<{ content: string, filename: string } | { cancelled: true } | null>}
 */
export async function openCsvForImport() {
  if (typeof window.showOpenFilePicker !== "function") {
    return null;
  }

  try {
    const [fileHandle] = await window.showOpenFilePicker({
      id: IMPORT_PICKER_ID,
      types: CSV_TYPES,
      multiple: false,
    });
    const file = await fileHandle.getFile();
    return { content: await file.text(), filename: file.name };
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      return { cancelled: true };
    }
    return null;
  }
}
