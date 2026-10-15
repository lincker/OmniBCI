/** Render the documented plugin schema subset; the server remains authoritative. */
import { useId, useState } from "react";
import { currentConnection, desktopBridge } from "./desktop";
import { useI18n, translate, type Translate, type TranslationKey } from "./i18n";

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

const schemaTextKeys: Record<string, TranslationKey> = {
  "Recording path": "schema.recordingPath",
  "Chunk size (samples)": "schema.chunkSize",
  "Sample rate (Hz)": "schema.sampleRate",
  "Output rows including event row": "schema.outputRows",
  "Decode window (seconds)": "schema.decodeWindow",
  "Trigger offset (seconds)": "schema.triggerOffset",
  "Parallel port address": "schema.parallelAddress",
  "Device host": "schema.deviceHost",
  "TCP port": "schema.tcpPort",
  "Total channels sent by the device, including the final event channel. Must match the Scan45 stream.": "schema.scanChannelsDescription",
};
const schemaText = (value: string, t: Translate): string => {
  const key = Object.prototype.hasOwnProperty.call(schemaTextKeys, value) ? schemaTextKeys[value] : undefined;
  return key ? t(key) : value;
};

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

export function schemaErrors(schema: ConfigurationSchema, value: unknown, path?: string,
  t: Translate = (key, params) => translate("en", key, params)): string[] {
  const label = path ?? t("schema.parameters");
  const errors: string[] = [];
  const fail = (message: string) => errors.push(`${label}: ${message}`);
  if (schema.type === "object") {
    if (!object(value)) return [`${label}: ${t("schema.enterObject")}`];
    for (const key of schema.required ?? []) if (!(key in value)) fail(t("schema.required", { field: key }));
    for (const [key, item] of Object.entries(value)) {
      const child = schema.properties?.[key];
      if (child) errors.push(...schemaErrors(child, item, `${label} · ${schemaText(child.title ?? key, t)}`, t));
      else fail(t("schema.unsupported", { field: key }));
    }
  } else if (schema.type === "array") {
    if (!Array.isArray(value)) return [`${label}: ${t("schema.enterList")}`];
    if (schema.minItems !== undefined && value.length < schema.minItems) fail(t("schema.minItems", { count: schema.minItems }));
    if (schema.maxItems !== undefined && value.length > schema.maxItems) fail(t("schema.maxItems", { count: schema.maxItems }));
    if (schema.items) value.forEach((item, index) => errors.push(...schemaErrors(schema.items!, item, `${label} [${index + 1}]`, t)));
  } else if (schema.type === "boolean") {
    if (typeof value !== "boolean") fail(t("schema.chooseBoolean"));
  } else if (schema.type === "string") {
    if (typeof value !== "string") return [`${label}: ${t("schema.enterText")}`];
    if (schema.minLength !== undefined && value.length < schema.minLength) fail(t("schema.minLength", { count: schema.minLength }));
    if (schema.maxLength !== undefined && value.length > schema.maxLength) fail(t("schema.maxLength", { count: schema.maxLength }));
  } else {
    if (typeof value !== "number" || !Number.isFinite(value)) return [`${label}: ${t("schema.finiteNumber")}`];
    if (schema.type === "integer" && !Number.isInteger(value)) fail(t("schema.wholeNumber"));
    if (schema.minimum !== undefined && value < schema.minimum) fail(t("schema.minimum", { value: schema.minimum }));
    if (schema.maximum !== undefined && value > schema.maximum) fail(t("schema.maximum", { value: schema.maximum }));
  }
  if (schema.enum && !schema.enum.some((option) => sameParameters(option, value))) fail(t("schema.chooseListed"));
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
  const { t } = useI18n();
  const id = useId();
  const [fileError, setFileError] = useState("");
  const title = schemaText(schema.title ?? label, t);
  if (schema.type === "object" && !schema.enum) {
    const values = object(value) ? value : {};
    return <fieldset className="schema-group" disabled={disabled}>
      <legend>{title}</legend>
      {schema.description && <p className="setting-description">{schemaText(schema.description, t)}</p>}
      {Object.entries(schema.properties ?? {}).map(([key, child]) => {
        const optional = !schema.required?.includes(key);
        const update = (next: unknown) => onChange({ ...values, [key]: next });
        return <div key={key} className="schema-property">
          {values[key] === undefined && optional
            ? <button type="button" onClick={() => update(initialSchemaValue(child))}>{t("schema.addField", { field: schemaText(child.title ?? key, t) })}</button>
            : <SchemaField schema={child} label={key} value={values[key]} onChange={update} disabled={disabled} />}
          {optional && values[key] !== undefined && <button type="button" className="text-button"
            onClick={() => { const next = { ...values }; delete next[key]; onChange(next); }}>{t("schema.removeField", { field: schemaText(child.title ?? key, t) })}</button>}
        </div>;
      })}
      {!Object.keys(schema.properties ?? {}).length && <p className="setting-description">{t("schema.noParameters")}</p>}
    </fieldset>;
  }
  if (schema.type === "array" && !schema.enum) {
    const values = Array.isArray(value) ? value : [];
    return <fieldset className="schema-group" disabled={disabled}>
      <legend>{title}</legend>
      {schema.description && <p className="setting-description">{schemaText(schema.description, t)}</p>}
      {values.map((item, index) => <div className="schema-array-item" key={index}>
        {schema.items && <SchemaField schema={schema.items} label={t("schema.item", { index: index + 1 })} value={item} disabled={disabled}
          onChange={(next) => onChange(values.map((previous, position) => position === index ? next : previous))} />}
        <div className="actions">
          <button type="button" disabled={disabled || index === 0} onClick={() => {
            const next = [...values]; [next[index - 1], next[index]] = [next[index], next[index - 1]]; onChange(next);
          }} aria-label={t("schema.moveItemUp", { title, index: index + 1 })}>{t("schema.moveUp")}</button>
          <button type="button" onClick={() => onChange(values.filter((_, position) => position !== index))}>{t("schema.removeItem")}</button>
        </div>
      </div>)}
      <button type="button" disabled={disabled || !schema.items || (schema.maxItems !== undefined && values.length >= schema.maxItems)}
        onClick={() => schema.items && onChange([...values, initialSchemaValue(schema.items)])}>{t("schema.addItem")}</button>
    </fieldset>;
  }
  return <div className="setting-row">
    <label htmlFor={id} className="setting-label">{title}</label>
    {schema.description && <span className="setting-description">{schemaText(schema.description, t)}</span>}
    {schema.enum ? <select id={id} disabled={disabled} value={schema.enum.findIndex((option) => sameParameters(option, value))}
      onChange={(event) => onChange(clone(schema.enum![Number(event.target.value)]))}>
      <option value={-1} disabled>{t("schema.selectValue")}</option>
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
          }}>{t("schema.browse")}</button>}</div>
        : <input id={id} type="number" value={typeof value === "number" || typeof value === "string" ? value : ""}
          min={schema.minimum} max={schema.maximum} step={schema.type === "integer" ? 1 : "any"} disabled={disabled}
          onChange={(event) => onChange(event.target.value === "" ? "" : Number(event.target.value))} />}
    {schema["x-widget"] === "file-path" && <span className="setting-detail">{t("schema.backendPath")}</span>}
    {schema["x-scope"] === "machine" && <span className="setting-detail">{t("schema.machineSpecific")}</span>}
    {fileError && <span className="error" role="alert">{fileError}</span>}
  </div>;
}

export function SchemaForm({ schema, value, onChange, disabled }: {
  schema: ConfigurationSchema;
  value: Record<string, unknown>;
  onChange: (value: Record<string, unknown>) => void;
  disabled?: boolean;
}) {
  const { t } = useI18n();
  return <SchemaField schema={schema} label={t("schema.parameters")} value={value} disabled={disabled}
    onChange={(next) => { if (object(next)) onChange(next); }} />;
}
