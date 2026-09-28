/** Runtime configuration contracts are generated from the backend schema. */
import type { components } from "./rest-types";
import { configRequest } from "./plugin-config";

type S = components["schemas"];
export type RuntimeStatus = S["RuntimeConfigurationResponse"];
export type RuntimePlan = S["RuntimePlanResponse"];
export type RuntimeOperation = S["RuntimeOperationResponse"];
export type RuntimePlanRequest = S["RuntimePlanRequest"];

export const runtimeConfigApi = {
  status: () => configRequest<RuntimeStatus>("/runtime"),
  plan: (body: RuntimePlanRequest) => configRequest<RuntimePlan>("/apply-plan", "POST", body),
  apply: (plan_id: string, operation_id: string) =>
    configRequest<RuntimeOperation>("/apply", "POST", { plan_id, operation_id }),
  operation: (operation_id: string) => configRequest<RuntimeOperation>(`/operations/${encodeURIComponent(operation_id)}`),
};
