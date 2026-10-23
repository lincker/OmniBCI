/** Validate the numeric trial controls supported by the stimulation API. */
export type TrialControl = {
  label?: string;
  type?: "int" | "float";
  field: "n_trials" | "trial_params";
  default?: number;
  min?: number;
  max?: number;
  default_sequence?: string;
};

export const MAX_TRIALS = 10000;

/** Parse IDs and inclusive ranges separated by commas or whitespace. */
export function parseTrialSequence(text: string, control: TrialControl): number[] {
  const sequence: number[] = [];
  let cursor = 0;
  for (const match of text.matchAll(/\d+(?:\s*-\s*\d+)?/g)) {
    const separator = text.slice(cursor, match.index);
    if (cursor === 0 ? separator.trim() !== "" : !/^(?:\s*,\s*|\s+)$/.test(separator)) {
      throw new Error("Invalid trial sequence syntax.");
    }
    const [first, last = first] = match[0].split(/\s*-\s*/).map(Number);
    if (!Number.isSafeInteger(first) || !Number.isSafeInteger(last) || last < first
        || (control.min !== undefined && first < control.min)
        || (control.max !== undefined && last > control.max)) {
      throw new Error("Invalid trial sequence target.");
    }
    if (last - first + 1 > MAX_TRIALS - sequence.length) {
      throw new Error(`Trial sequence exceeds ${MAX_TRIALS} trials.`);
    }
    for (let target = first; target <= last; target += 1) sequence.push(target);
    cursor = match.index + match[0].length;
  }
  if (sequence.length === 0 || text.slice(cursor).trim() !== "") {
    throw new Error("Invalid trial sequence syntax.");
  }
  return sequence;
}

export function readTrialControls(value: unknown): Record<string, TrialControl> {
  if (value === undefined) return {};
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid trial controls.");
  const fields = new Set<string>();
  for (const [name, descriptor] of Object.entries(value)) {
    if (!descriptor || typeof descriptor !== "object" || Array.isArray(descriptor)) throw new Error(`Invalid trial control: ${name}.`);
    const control = descriptor as Record<string, unknown>;
    if ((control.field !== "n_trials" && control.field !== "trial_params") || fields.has(control.field)
        || (control.label !== undefined && typeof control.label !== "string")
        || (control.type !== undefined && control.type !== "int" && control.type !== "float")) {
      throw new Error(`Unsupported trial control: ${name}.`);
    }
    fields.add(control.field);
    for (const key of ["default", "min", "max"]) {
      if (control[key] !== undefined && (typeof control[key] !== "number" || !Number.isFinite(control[key]))) {
        throw new Error(`Invalid ${key} for trial control: ${name}.`);
      }
    }
    const { min, max, default: initial } = control as TrialControl;
    if ((min !== undefined && max !== undefined && min > max)
        || (initial !== undefined && (!Number.isSafeInteger(initial)
          || (min !== undefined && initial < min) || (max !== undefined && initial > max)))) {
      throw new Error(`Invalid range or default for trial control: ${name}.`);
    }
    if (control.default_sequence !== undefined) {
      if (typeof control.default_sequence !== "string" || control.field !== "trial_params" || control.type !== "int") {
        throw new Error(`Invalid default sequence for trial control: ${name}.`);
      }
      parseTrialSequence(control.default_sequence, control as TrialControl);
    }
  }
  return structuredClone(value) as Record<string, TrialControl>;
}
