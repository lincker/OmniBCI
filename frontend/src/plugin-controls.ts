/** Validate the numeric trial controls supported by the stimulation API. */
export type TrialControl = {
  label?: string;
  type?: "int" | "float";
  field: "n_trials" | "trial_params";
  default?: number;
  min?: number;
  max?: number;
};

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
  }
  return structuredClone(value) as Record<string, TrialControl>;
}
