import { sameParameters, type ConfigurationSchema } from "./SchemaForm";
import { selectionKey, type PluginProfile, type PluginSelection } from "./api/plugin-config";
import type { RuntimeOperation, RuntimePlan, RuntimeStatus } from "./api/runtime-config";

export type ProfileReviewDraft = {
  kind: "acquisition";
  selection: PluginSelection;
  parameters: Record<string, unknown>;
  profile: PluginProfile | null;
  name: string;
  schema?: ConfigurationSchema | null;
};
export type ProfileReviewMode = "create" | "update" | "existing";
export type ProfileReviewState = {
  draft: ProfileReviewDraft;
  mode: ProfileReviewMode;
  name: string;
  createName: string;
  savedProfile: PluginProfile | null;
  saveUncertain: boolean;
};
export type SavedReviewProfile = { reviewed_plan_id: string; profile: PluginProfile; applied: boolean };
export type ReviewedRuntimeOperation = RuntimeOperation & { reviewed_plan_id?: string; profile?: PluginProfile };

export const profileParametersChanged = (draft: ProfileReviewDraft) => !!draft.profile
  && (selectionKey(draft.profile.selection) !== selectionKey(draft.selection)
    || !sameParameters(normalizedProfileParameters(draft.profile.parameters, draft.schema),
      normalizedProfileParameters(draft.parameters, draft.schema)));

export function initialProfileReview(draft: ProfileReviewDraft, copyName: string): ProfileReviewState {
  const changed = profileParametersChanged(draft);
  const name = (draft.name.trim() || draft.selection.contribution_id).slice(0, 64);
  const createName = draft.profile && changed && name === draft.profile.name ? copyName.slice(0, 64) : name;
  return { draft: structuredClone(draft), mode: !draft.profile || changed ? "create" : "existing",
    name: !draft.profile || changed ? createName : draft.profile.name, createName,
    savedProfile: null, saveUncertain: false };
}

/** Mirror explicit schema defaults only, for reconciling an interrupted save response. */
export function normalizedProfileParameters(parameters: Record<string, unknown>, schema?: ConfigurationSchema | null): Record<string, unknown> {
  const normalize = (value: unknown, definition?: ConfigurationSchema): unknown => {
    if (definition?.type === "object" && value && typeof value === "object" && !Array.isArray(value)) {
      const result = { ...value } as Record<string, unknown>;
      for (const [key, child] of Object.entries(definition.properties ?? {})) {
        if (Object.prototype.hasOwnProperty.call(result, key)) result[key] = normalize(result[key], child);
        else if (child.default !== undefined) result[key] = normalize(structuredClone(child.default), child);
      }
      return result;
    }
    return definition?.type === "array" && Array.isArray(value)
      ? value.map((item) => normalize(item, definition.items)) : value;
  };
  return normalize(structuredClone(parameters), schema ?? undefined) as Record<string, unknown>;
}

export const planIsStale = (plan: RuntimePlan, status: RuntimeStatus) =>
  plan.expected_revision !== status.accepted_revision || plan.running !== status.running
  || plan.applied_revision !== status.applied_revision || plan.generation !== status.generation;

export const sameReviewedImpact = (before: RuntimePlan, after: RuntimePlan) =>
  before.target_revision === after.target_revision && before.expected_revision === after.expected_revision
  && before.running === after.running && before.applied_revision === after.applied_revision
  && before.generation === after.generation && before.strategy === after.strategy
  && sameParameters([...(before.changed_slots ?? [])].sort(), [...(after.changed_slots ?? [])].sort())
  && sameParameters([...(before.changed_settings ?? [])].sort(), [...(after.changed_settings ?? [])].sort())
  && (before.signal_summary ?? null) === (after.signal_summary ?? null);
