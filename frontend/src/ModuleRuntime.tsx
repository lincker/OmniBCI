import type { ConfigurationModule } from "./hooks/useCurrentConfiguration";
import type { RuntimeControls } from "./hooks/useRuntimeControls";
import "./ModuleRuntime.css";

/** Shared module actions keep Setup and Run on the same lifecycle rules. */
export function ModuleRuntime({ module, controls, disabled = false, showEngineStart = true }: {
  module: ConfigurationModule;
  controls: RuntimeControls;
  disabled?: boolean;
  showEngineStart?: boolean;
}) {
  const state = controls.module(module);
  const actions = [
    state.start, state.stop,
    ...(module === "acquisition" ? [state.read, state.stopRead] : []),
    ...(showEngineStart && state.needsEngine ? [state.engineStart] : []),
  ];
  const guidance = actions.filter((action, index) => action.reason && (index === 0 || action === state.read || action.pending
    || action === state.stop && state.active || action === state.engineStart));
  const reasonId = `${module}-action-reasons`;
  return <section className="module-runtime" aria-label={state.heading}>
    <p className="module-runtime-status" role="status"><strong>{state.status}</strong>
      {module === "acquisition" && state.available && <> · {state.readingLabel}</>}</p>
    <p className="meta">{state.parameters}</p>
    {state.progress && <p className="meta" role="status">{state.progress}</p>}
    <div className="actions">{actions.map((action, index) => <button key={action.label}
      className={index === 0 ? "primary" : undefined} disabled={disabled && (action === state.start || action === state.read) || !!action.reason}
      title={action.reason ?? undefined} aria-describedby={guidance.includes(action) ? reasonId : undefined}
      onClick={() => { if (!(disabled && (action === state.start || action === state.read)) && !action.reason) return action.run(); }}>{action.label}</button>)}</div>
    {guidance.length > 0 && <div id={reasonId} className="action-reasons meta">
      {guidance.map((action) => <p key={action.label}><strong>{action.label}:</strong> {action.reason}</p>)}
    </div>}
    {state.notice && <p className="inline-message success" role="status">{state.notice}</p>}
    {actions.filter((action) => action.error).map((action) => <p className="inline-message error" role="alert" key={action.label}>
      <strong>{action.label}:</strong> {action.error}
    </p>)}
  </section>;
}
