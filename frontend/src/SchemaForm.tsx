/** Render the documented plugin schema subset; the server remains authoritative. */
import { useId, useState } from "react";
import { currentConnection, desktopBridge } from "./desktop";

export interface ConfigurationSchema {
  type: "object" | "array" | "string" | "integer" | "number" | "boolean";
  title?: string;
  description?: string;
  default?: unknown;
  enum?: unknown[];
  properties?: Record<string, ConfigurationSchema>;
  required?: string[];
  items?: ConfigurationSchema;
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
  minItems?: number;
  maxItems?: number;
  "x-scope"?: "portable" | "machine" | "secret";
  "x-apply"?: "restart" | "module" | "trial";
  "x-widget"?: "file-path";
}

const object = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const clone = (value: unknown): unknown => JSON.parse(JSON.stringify(value));
const canonical = (value: unknown) => JSON.stringify(value, (_key, item) =>
  object(item) ? Object.fromEntries(Object.entries(item).sort(([left], [right]) => left.localeCompare(right))) : item,
);
export const sameParameters = (left: unknown, right: unknown) => canonical(left) === canonical(right);

export function initialSchemaValue(schema: ConfigurationSchema): unknown {
  if (schema.enum?.length) return clone(schema.default !== undefined ? schema.default : schema.enum[0]);
  if (schema.type === "object") {
    const values = object(schema.default) ? clone(schema.default) as Record<string, unknown> : {};
    for (const [key, child] of Object.entries(schema.properties ?? {})) {
      if (key in values) values[key] = initialSchemaValue({ ...child, default: values[key] });
      else if (child.default !== undefined || schema.required?.includes(key)) values[key] = initialSchemaValue(child);
    }
    return values;
  }
  if (schema.type === "array" && Array.isArray(schema.default) && schema.items) {
    return schema.default.map((value) => initialSchemaValue({ ...schema.items!, default: value }));
  }
  if (schema.default !== undefined) return clone(schema.default);
  if (schema.type === "array") return [];
  if (schema.type === "boolean") return false;
  if (schema.type === "integer" || schema.type === "number") return schema.minimum ?? 0;
  return "";
}

export function schemaErrors(schema: ConfigurationSchema, value: unknown, path = "Parameters"): string[] {
  const errors: string[] = [];
  const fail = (message: string) => errors.push(`${path}: ${message}`);
  if (schema.type === "object") {
    if (!object(value)) return [`${path}: enter an object`];
    for (const key of schema.required ?? []) if (!(key in value)) fail(`${key} is required`);
    for (const [key, item] of Object.entries(value)) {
      const child = schema.properties?.[key];
      if (child) errors.push(...schemaErrors(child, item, `${path} · ${child.title ?? key}`));
      else fail(`${key} is not a supported parameter`);
    }
  } else if (schema.type === "array") {
    if (!Array.isArray(value)) return [`${path}: enter a list`];
    if (schema.minItems !== undefined && value.length < schema.minItems) fail(`at least ${schema.minItems} items required`);
    if (schema.maxItems !== undefined && value.length > schema.maxItems) fail(`at most ${schema.maxItems} items allowed`);
    if (schema.items) value.forEach((item, index) => errors.push(...schemaErrors(schema.items!, item, `${path} [${index + 1}]`)));
  } else if (schema.type === "boolean") {
    if (typeof value !== "boolean") fail("choose true or false");
  } else if (schema.type === "string") {
    if (typeof value !== "string") return [`${path}: enter text`];
    if (schema.minLength !== undefined && value.length < schema.minLength) fail(`at least ${schema.minLength} characters required`);
    if (schema.maxLength !== undefined && value.length > schema.maxLength) fail(`at most ${schema.maxLength} characters allowed`);
  } else {
    if (typeof value !== "number" || !Number.isFinite(value)) return [`${path}: enter a finite number`];
    if (schema.type === "integer" && !Number.isInteger(value)) fail("enter a whole number");
    if (schema.minimum !== undefined && value < schema.minimum) fail(`minimum is ${schema.minimum}`);
    if (schema.maximum !== undefined && value > schema.maximum) fail(`maximum is ${schema.maximum}`);
  }
  if (schema.enum && !schema.enum.some((option) => sameParameters(option, value))) fail("choose a listed value");
  return errors;
}

interface FieldProps {
  schema: ConfigurationSchema;
  value: unknown;
  onChange: (value: unknown) => void;
  disabled?: boolean;
  label: string;
}

function SchemaField({ schema, value, onChange, disabled, label }: FieldProps) {
  const id = useId();
  const [fileError, setFileError] = useState("");
  const title = schema.title ?? label;
  if (schema.type === "object" && !schema.enum) {
    const values = object(value) ? value : {};
    return <fieldset className="schema-group" disabled={disabled}>
      <legend>{title}</legend>
      {schema.description && <p className="setting-description">{schema.description}</p>}
      {Object.entries(schema.properties ?? {}).map(([key, child]) => {
        const optional = !schema.required?.includes(key);
        const update = (next: unknown) => onChange({ ...values, [key]: next });
        return <div key={key} className="schema-property">
          {values[key] === undefined && optional
            ? <button type="button" onClick={() => update(initialSchemaValue(child))}>Add {child.title ?? key}</button>
            : <SchemaField schema={child} label={key} value={values[key]} onChange={update} disabled={disabled} />}
          {optional && values[key] !== undefined && <button type="button" className="text-button"
            onClick={() => { const next = { ...values }; delete next[key]; onChange(next); }}>Remove {child.title ?? key}</button>}
        </div>;
      })}
      {!Object.keys(schema.properties ?? {}).length && <p className="setting-description">This plugin has no editable parameters.</p>}
    </fieldset>;
  }
  if (schema.type === "array" && !schema.enum) {
    const values = Array.isArray(value) ? value : [];
    return <fieldset className="schema-group" disabled={disabled}>
      <legend>{title}</legend>
      {schema.description && <p className="setting-description">{schema.description}</p>}
      {values.map((item, index) => <div className="schema-array-item" key={index}>
        {schema.items && <SchemaField schema={schema.items} label={`Item ${index + 1}`} value={item} disabled={disabled}
          onChange={(next) => onChange(values.map((previous, position) => position === index ? next : previous))} />}
        <div className="actions">
          <button type="button" disabled={disabled || index === 0} onClick={() => {
            const next = [...values]; [next[index - 1], next[index]] = [next[index], next[index - 1]]; onChange(next);
          }} aria-label={`Move ${title} item ${index + 1} up`}>Move up</button>
          <button type="button" onClick={() => onChange(values.filter((_, position) => position !== index))}>Remove item</button>
        </div>
      </div>)}
      <button type="button" disabled={disabled || !schema.items || (schema.maxItems !== undefined && values.length >= schema.maxItems)}
        onClick={() => schema.items && onChange([...values, initialSchemaValue(schema.items)])}>Add item</button>
    </fieldset>;
  }
  return <div className="setting-row">
    <label htmlFor={id} className="setting-label">{title}</label>
    {schema.description && <span className="setting-description">{schema.description}</span>}
    {schema.enum ? <select id={id} disabled={disabled} value={schema.enum.findIndex((option) => sameParameters(option, value))}
      onChange={(event) => onChange(clone(schema.enum![Number(event.target.value)]))}>
      <option value={-1} disabled>Select a value</option>
      {schema.enum.map((option, index) => <option value={index} key={index}>{typeof option === "string" ? option : JSON.stringify(option)}</option>)}
    </select> : schema.type === "boolean" ? <input id={id} type="checkbox" checked={value === true} disabled={disabled}
      onChange={(event) => onChange(event.target.checked)} />
      : schema.type === "string" ? <div className="file-path-field"><input id={id} type={schema["x-scope"] === "secret" ? "password" : "text"}
        value={typeof value === "string" ? value : ""} minLength={schema.minLength} maxLength={schema.maxLength} disabled={disabled}
        onChange={(event) => onChange(event.target.value)} />
        {schema["x-widget"] === "file-path" && desktopBridge() && currentConnection()?.kind === "local" &&
          <button type="button" disabled={disabled} onClick={() => {
            const owner = currentConnection()?.id;
            setFileError("");
            void desktopBridge()!.chooseRecordingFile().then((path) => {
              if (path !== null && currentConnection()?.id === owner) onChange(path);
            }).catch((cause) => setFileError(String(cause)));
          }}>Browse…</button>}</div>
        : <input id={id} type="number" value={typeof value === "number" || typeof value === "string" ? value : ""}
          min={schema.minimum} max={schema.maximum} step={schema.type === "integer" ? 1 : "any"} disabled={disabled}
          onChange={(event) => onChange(event.target.value === "" ? "" : Number(event.target.value))} />}
    {schema["x-widget"] === "file-path" && <span className="setting-detail">Path on the backend computer. Remote files are not uploaded.</span>}
    {schema["x-scope"] === "machine" && <span className="setting-detail">Specific to the backend computer</span>}
    {fileError && <span className="error" role="alert">{fileError}</span>}
  </div>;
}

export function SchemaForm({ schema, value, onChange, disabled }: {
  schema: ConfigurationSchema;
  value: Record<string, unknown>;
  onChange: (value: Record<string, unknown>) => void;
  disabled?: boolean;
}) {
  return <SchemaField schema={schema} label="Parameters" value={value} disabled={disabled}
    onChange={(next) => { if (object(next)) onChange(next); }} />;
}
