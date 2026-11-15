/** Module configurations used by console tests; runtime APIs always receive separate sections. */
export const moduleNames = ["acquisition", "processing", "stimulation", "actuation"];
export const revisionMap = (revision) => ({
  ...Object.fromEntries(moduleNames.map((module) => [module, revision ?? null])),
  system: revision ? "system-revision" : null,
});
export function moduleConfiguration({ revision, bindings = [], settings = {}, revisions = revisionMap(revision) } = {}) {
  return {
    modules: Object.fromEntries(moduleNames.map((module) => [module, {
      module, revision: revisions[module],
      bindings: bindings.filter((binding) => binding.slot.startsWith(`${module}.`)),
      settings: Object.fromEntries(Object.entries(settings).filter(([key]) => key.startsWith(`${module.toUpperCase()}.`))),
    }])),
    system: { revision: revisions.system, settings: {} },
  };
}
