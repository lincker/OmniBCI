import { sameParameters, type ConfigurationSchema } from "./SchemaForm";

export type ProfileParameterChange = {
  path: string;
  label: string;
  before: unknown;
  after: unknown;
  beforePresent: boolean;
  afterPresent: boolean;
  secret: boolean;
};

const object = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);

const containsSecret = (schema: ConfigurationSchema | undefined): boolean => !!schema &&
  (schema["x-scope"] === "secret" || containsSecret(schema.items)
    || Object.values(schema.properties ?? {}).some(containsSecret));

/** Compare semantic values; array order, removals and absent-versus-null remain visible. */
export function profileParameterChanges(before: Record<string, unknown>, after: Record<string, unknown>,
  schema?: ConfigurationSchema | null, labelText: (label: string) => string = (label) => label): ProfileParameterChange[] {
  const changes: ProfileParameterChange[] = [];
  const visit = (left: unknown, right: unknown, current: ConfigurationSchema | undefined, path: string[],
    labels: string[], beforePresent: boolean, afterPresent: boolean, inheritedSecret: boolean) => {
    if (beforePresent === afterPresent && sameParameters(left, right)) return;
    const secret = inheritedSecret || current?.["x-scope"] === "secret";
    if (!secret && (object(left) || !beforePresent) && (object(right) || !afterPresent)) {
      const leftObject = object(left) ? left : {};
      const rightObject = object(right) ? right : {};
      const keys = [...new Set([...Object.keys(leftObject), ...Object.keys(rightObject)])];
      if (keys.length) {
        for (const key of keys) {
          const child = current?.properties?.[key];
          visit(leftObject[key], rightObject[key], child, [...path, key], [...labels, labelText(child?.title ?? key)],
            Object.prototype.hasOwnProperty.call(leftObject, key), Object.prototype.hasOwnProperty.call(rightObject, key), secret);
        }
        return;
      }
    }
    changes.push({ path: JSON.stringify(path), label: labels.join(" › "), before: left, after: right,
      beforePresent, afterPresent, secret: secret || containsSecret(current) });
  };
  visit(before, after, schema ?? undefined, [], [], true, true, false);
  return changes;
}

/** Preserve distinctions such as an empty string, null, an empty list and a removed field. */
export function formatProfileValue(value: unknown): string {
  if (typeof value === "string") return value === "" ? '\"\"' : value;
  return JSON.stringify(value, null, 2) ?? String(value);
}
