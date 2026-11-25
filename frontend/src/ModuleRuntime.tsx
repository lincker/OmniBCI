import type { ConfigurationModule } from "./api/plugin-config";
import type { RuntimeControls } from "./hooks/useRuntimeControls";
import "./ModuleRuntime.css";

/** Shared module actions keep Setup and Run on the same lifecycle rules. */
export function ModuleRuntime({ module, controls, disabled = false, actions: scope = "all" }: {
  module: ConfigurationModule;
  controls: RuntimeControls;
  disabled?: boolean;
  /** "connection" limits Acquisition to Connect/Disconnect (Setup needs a connection to read source metadata). */
  actions?: "all" | "connection";
}) {
  const state = controls.module(module);
  const actions = [
    state.start, state.stop,
    ...(module === "acquisition" && scope === "all" ? [state.read, state.stopRead] : []),
  ];
  const guidance = actions.filter((action, index) => action.reason && (index === 0 || action === state.read || action.pending
    || action === state.stop && state.active));
  const blocked = (action: typeof actions[number]) => disabled && (action === state.start || action === state.read) || !!action.reason;
  // Highlight the one action that moves the module forward; everything else stays secondary.
  const primary = actions.find((action) => !blocked(action) && action !== state.stop && action !== state.stopRead);
  const reasonId = `${module}-action-reasons`;
  return <section className="module-runtime" data-active={state.active || undefined} aria-label={state.heading}>
    <p className="module-runtime-status" role="status"><strong>{state.status}</strong>
      {module === "acquisition" && state.available && <> · {state.readingLabel}</>}</p>
    <p className="meta">{state.parameters}</p>
    {state.progress && <p className="meta" role="status">{state.progress}</p>}
    <div className="actions">{actions.map((action) => <button key={action.label}
      className={action === primary ? "primary" : undefined} disabled={blocked(action)}
      title={action.reason ?? undefined} aria-describedby={guidance.includes(action) ? reasonId : undefined}
      onClick={() => { if (!blocked(action)) return action.run(); }}>{action.label}</button>)}</div>
    {guidance.length > 0 && <div id={reasonId} className="action-reasons meta">
      {guidance.map((action) => <p key={action.label}><strong>{action.label}:</strong> {action.reason}</p>)}
    </div>}
    {state.notice && <p className="inline-message success" role="status">{state.notice}</p>}
    {actions.filter((action) => action.error).map((action) => <p className="inline-message error" role="alert" key={action.label}>
      <strong>{action.label}:</strong> {action.error}
    </p>)}
  </section>;
}
