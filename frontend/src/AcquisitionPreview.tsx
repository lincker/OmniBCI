import { useEffect, useRef, useState } from "react";
import { api, type AcquisitionPreview as Preview, type AcquisitionPreviewChunk,
  type PreviewLimits, type PreviewSelection } from "./api/client";
import { usePoll } from "./hooks/usePoll";
import "./AcquisitionPreview.css";

type PreviewForm = { rows: string; sampleStart: string; sampleCount: string; chunkCount: string; includeTrigger: boolean };
type Context = { generation: string; revision: string; connectionId: string | null; streaming: boolean; disabled: boolean };

function integer(value: string, name: string, minimum = 0): number {
  if (!/^\d+$/.test(value.trim()) || !Number.isSafeInteger(Number(value)) || Number(value) < minimum) {
    throw new Error(`${name} must be an integer of at least ${minimum}.`);
  }
  return Number(value);
}

export function parsePreviewSelection(form: PreviewForm, limits: PreviewLimits): PreviewSelection {
  const rows = form.rows.trim() ? form.rows.split(",").map((value) => integer(value, "Row")) : [];
  const sampleStart = integer(form.sampleStart, "Sample start");
  const sampleCount = integer(form.sampleCount, "Sample count", 1);
  const chunkCount = integer(form.chunkCount, "Chunk count", 1);
  if (new Set(rows).size !== rows.length || rows.length > limits.max_rows) {
    throw new Error(`Choose up to ${limits.max_rows} distinct signal rows.`);
  }
  if (!rows.length && !form.includeTrigger) throw new Error("Select a signal row or the trigger row.");
  if (sampleCount > limits.max_samples || chunkCount > limits.max_chunks
    || (rows.length + Number(form.includeTrigger)) * sampleCount > limits.max_values) {
    throw new Error("The selection exceeds the preview limits. Reduce rows, samples or chunks.");
  }
  return { rows, sample_start: sampleStart, sample_count: sampleCount,
    chunk_count: chunkCount, include_trigger: form.includeTrigger };
}

export function previewValueKey(value: string): string | null {
  const text = value.trim().toLowerCase();
  if (!text || text.length > 128) return null;
  if (/^(true|false|nan|[+-]?infinity)$/.test(text)) return text.replace(/^\+/, "");
  const match = /^([+-]?)(\d+\.?\d*|\.\d+)(?:e([+-]?\d+))?$/.exec(text);
  if (!match) return null;
  const [whole, fraction = ""] = match[2].split(".");
  const digits = (whole + fraction).replace(/^0+/, "");
  if (!digits) return "0";
  const significant = digits.replace(/0+$/, "");
  // Normalize decimal text without rounding exact integers or nearby float values.
  const exponent = BigInt(match[3] ?? "0") - BigInt(fraction.length) + BigInt(digits.length - significant.length);
  return `${match[1] === "-" ? "-" : ""}${significant}e${exponent}`;
}

type PreviewMatch = { chunk: AcquisitionPreviewChunk; row: number; column: number };

export function PreviewChunkTable({ chunk, snapshot, onSelectChunk }: {
  chunk: AcquisitionPreviewChunk; snapshot?: Preview; onSelectChunk?: (chunk: AcquisitionPreviewChunk) => void;
}) {
  const descriptor = chunk.descriptor;
  const [scope, setScope] = useState("current");
  const [channel, setChannel] = useState("all");
  const [query, setQuery] = useState("");
  const [matches, setMatches] = useState<PreviewMatch[] | null>(null);
  const [active, setActive] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [coverage, setCoverage] = useState<{ searched: number; total: number; unavailable: number[] } | null>(null);
  const request = useRef(0);
  const currentCell = useRef<HTMLTableCellElement>(null);
  const selected = matches?.[active];
  const highlighted = new Set(matches?.filter((match) => match.chunk.sequence_id === chunk.sequence_id)
    .map(({ row, column }) => `${row}:${column}`));
  const rows = scope === "all" && snapshot
    ? [...new Set(snapshot.chunks.flatMap((item) => item.row_indices).concat(chunk.row_indices))].sort((a, b) => a - b)
    : chunk.row_indices;
  const rowLabel = (row: number) => {
    const channel = descriptor?.channels.find((item) => item.index === row);
    return `${row}: ${row === chunk.trigger_row_index ? "Trigger"
      : `${channel?.name ?? "Unknown channel"} (${channel?.unit ?? "unknown unit"})`}`;
  };
  const resetSearch = () => {
    request.current += 1;
    setMatches(null); setActive(0); setError(null); setCoverage(null); setBusy(false);
  };
  useEffect(() => () => { request.current += 1; }, []);
  const search = async () => {
    resetSearch();
    const ticket = request.current;
    const key = previewValueKey(query);
    if (key === null) {
      setError("Enter a number, NaN, Infinity, -Infinity, true or false (up to 128 characters).");
      return;
    }
    const found: PreviewMatch[] = [];
    const inspect = (value: AcquisitionPreviewChunk) => {
      value.row_indices.forEach((row, index) => {
        if (channel !== "all" && String(row) !== channel) return;
        value.values[index].forEach((sample, column) => {
          if (previewValueKey(String(sample)) === key) found.push({ chunk: value, row, column });
        });
      });
    };
    if (scope === "all") {
      if (!snapshot?.capture_id || snapshot.generation !== chunk.generation || snapshot.capture_id !== chunk.capture_id) {
        setError("The retained window is unavailable for this capture.");
        return;
      }
      const window = snapshot.chunks.slice(-snapshot.limits.max_chunks);
      const unavailable: number[] = [];
      setBusy(true);
      for (let start = 0; start < window.length; start += 4) {
        const batch = window.slice(start, start + 4);
        const results = await Promise.allSettled(batch.map(async (item) =>
          api.acqPreviewChunk(chunk.generation, chunk.capture_id, item.sequence_id)));
        if (ticket !== request.current) return;
        results.forEach((result, index) => {
          const value = result.status === "fulfilled" ? result.value : null;
          if (!value || value.generation !== chunk.generation || value.capture_id !== chunk.capture_id
            || value.sequence_id !== batch[index].sequence_id || value.connection_id !== chunk.connection_id
            || value.source_id !== chunk.source_id || value.config_revision !== chunk.config_revision) {
            unavailable.push(batch[index].sequence_id);
          } else inspect(value);
        });
      }
      setCoverage({ searched: window.length - unavailable.length, total: window.length, unavailable });
      setBusy(false);
    } else inspect(chunk);
    setMatches(found);
    if (found.length) onSelectChunk?.(found[0].chunk);
  };
  const navigate = (index: number) => {
    setActive(index);
    if (matches?.[index]) onSelectChunk?.(matches[index].chunk);
  };
  useEffect(() => {
    const cell = currentCell.current;
    const container = cell?.closest(".preview-table");
    if (!cell || !container) return;
    const bounds = container.getBoundingClientRect();
    const target = cell.getBoundingClientRect();
    const left = bounds.left + (cell.parentElement?.querySelector("th")?.getBoundingClientRect().width ?? 0);
    const top = bounds.top + (container.querySelector("thead")?.getBoundingClientRect().height ?? 0);
    container.scrollBy({
      left: target.left < left || target.right > bounds.right ? target.left - left : 0,
      top: target.top < top || target.bottom > bounds.bottom ? target.top - top : 0,
    });
  }, [matches, active, chunk]);
  return <div className="preview-detail">
    <p><strong>Chunk #{chunk.sequence_id}</strong> · {chunk.original_shape.join(" × ")} / {chunk.dtype}</p>
    <p className="meta">Captured selection: {chunk.row_indices.length} rows, {chunk.sample_count} samples.
      All row and sample indices are zero-based. Values are acquisition output before processing.</p>
    <form className="actions preview-search" aria-label="Search displayed chunk"
      onSubmit={(event) => { event.preventDefault(); void search(); }}>
      <label className="control">Scope<select aria-label="Search scope" value={scope}
        onChange={(event) => { setScope(event.target.value); resetSearch(); }}>
        <option value="current">Current chunk</option>
        <option value="all" disabled={!snapshot || !onSelectChunk}>All retained chunks (latest 32)</option>
      </select></label>
      <label className="control">Channel<select aria-label="Search channel" value={channel}
        onChange={(event) => { setChannel(event.target.value); resetSearch(); }}>
        <option value="all">All channels (including trigger)</option>
        {rows.map((row) => <option key={row} value={String(row)}>{rowLabel(row)}</option>)}
      </select></label>
      <label className="control">Equals<input aria-label="Equals value" value={query} maxLength={128}
        placeholder="e.g. 0 or 1.25e-3" onChange={(event) => { setQuery(event.target.value); resetSearch(); }} /></label>
      <div className="preview-search-actions">
        <button type="submit" disabled={busy || (scope === "current" ? chunk.sample_count === 0 : !snapshot?.chunks.length)}>
          {busy ? "Searching…" : "Search"}</button>
        <button type="button" onClick={() => { setQuery(""); resetSearch(); }}>Clear</button>
      </div>
    </form>
    <p className="meta">{scope === "all"
      ? "Search the retained window when Search is clicked. Results stay fixed; search again to include new chunks. Only captured rows and samples are searched."
      : "Search only this displayed block."} Values must match exactly; decimal and scientific notation are equivalent.
      NaN, Infinity, -Infinity, true and false can also be searched as displayed.</p>
    {error && <p className="error" role="alert">{error}</p>}
    {busy && <p role="status">Searching retained chunks…</p>}
    {coverage && <p role="status">Searched {coverage.searched} of {coverage.total} chunks.
      {coverage.unavailable.length > 0 && ` Results are partial. Unavailable chunks: ${coverage.unavailable.map((id) => `#${id}`).join(", ")}. They may have left the live window or failed to load.`}</p>}
    {matches && <div className="actions preview-search-results">
      <span role="status">{matches.length} {matches.length === 1 ? "match" : "matches"}{selected
        ? ` · Match ${active + 1} of ${matches.length} · Chunk #${selected.chunk.sequence_id} · Row ${rowLabel(selected.row)} · Sample ${selected.chunk.sample_start + selected.column}`
        : scope === "all" ? " in the chunks searched." : " in this displayed block."}</span>
      {selected && <div className="preview-search-actions">
        <button type="button" disabled={matches.length < 2}
          onClick={() => navigate((active + matches.length - 1) % matches.length)}>Previous match</button>
        <button type="button" disabled={matches.length < 2}
          onClick={() => navigate((active + 1) % matches.length)}>Next match</button>
      </div>}
    </div>}
    {chunk.sample_count === 0 ? <p role="status">This chunk has no samples in the requested range.</p>
      : <div className="preview-table" tabIndex={0} role="region" aria-label="Captured sample values">
        <table><thead><tr><th scope="col">Row / channel</th>
          {Array.from({ length: chunk.sample_count }, (_, i) => <th scope="col" key={i}>{chunk.sample_start + i}</th>)}
        </tr></thead><tbody>{chunk.row_indices.map((row, index) => {
          return <tr key={row}><th scope="row">{rowLabel(row)}</th>
          {chunk.values[index].map((value, column) => {
            const current = selected?.chunk.sequence_id === chunk.sequence_id && selected.row === row && selected.column === column;
            return <td key={column} ref={current ? currentCell : undefined}
              className={highlighted.has(`${row}:${column}`)
                ? `preview-search-match${current ? " preview-search-current" : ""}` : undefined}
              aria-current={current ? "true" : undefined}>{String(value)}</td>;
          })}</tr>;
        })}</tbody></table>
      </div>}
    <details className="preview-metadata"><summary>Chunk metadata</summary>
      <dl className="preview-context">
        <div><dt>Chunk</dt><dd>{chunk.sequence_id}</dd></div>
        <div><dt>Original shape / dtype</dt><dd>{chunk.original_shape.join(" × ")} / {chunk.dtype}</dd></div>
        <div><dt>Host capture time (ns)</dt><dd>{chunk.timestamp_ns}</dd></div>
        <div><dt>Source / connection</dt><dd>{chunk.source_id} / {chunk.connection_id}</dd></div>
        <div><dt>Generation</dt><dd>{chunk.generation}</dd></div>
        <div><dt>Configuration revision</dt><dd>{chunk.config_revision ?? "Unknown"}</dd></div>
        <div><dt>Metadata provenance</dt><dd>{descriptor?.provenance ?? "Unavailable"}</dd></div>
        <div><dt>Sample rate</dt><dd>{descriptor ? `${descriptor.sample_rate} Hz` : "Unknown"}</dd></div>
      </dl>
    </details>
  </div>;
}

export function PreviewChunkDetail({ generation, captureId, sequenceId, snapshot, selectionKey = 0, onSelectSequence }: {
  generation: string; captureId: string; sequenceId: number; snapshot?: Preview;
  selectionKey?: number; onSelectSequence?: (sequence: number) => void;
}) {
  const [chunk, setChunk] = useState<AcquisitionPreviewChunk | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (chunk?.generation === generation && chunk.capture_id === captureId && chunk.sequence_id === sequenceId) return;
    let alive = true;
    setChunk(null); setError(null);
    api.acqPreviewChunk(generation, captureId, sequenceId).then((value) => {
      if (!alive) return;
      if (!value || value.generation !== generation || value.capture_id !== captureId || value.sequence_id !== sequenceId) {
        setError("The requested chunk is unavailable or belongs to a different capture.");
      } else setChunk(value);
    }).catch((cause: Error) => { if (alive) setError(cause.message); });
    return () => { alive = false; };
  }, [generation, captureId, sequenceId]);
  if (error) return <p className="error" role="alert">{error}</p>;
  return chunk && chunk.generation === generation && chunk.capture_id === captureId && chunk.sequence_id === sequenceId
    ? <PreviewChunkTable key={selectionKey} chunk={chunk} snapshot={snapshot} onSelectChunk={(value) => {
      setChunk(value); onSelectSequence?.(value.sequence_id);
    }} /> : <p role="status">Loading chunk…</p>;
}

export function PreviewChunks({ snapshot }: { snapshot: Preview }) {
  const [sequence, setSequence] = useState<number | null>(null);
  const [selectionKey, setSelectionKey] = useState(0);
  const [wasRetained, setWasRetained] = useState(false);
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const retained = snapshot.chunks.some((chunk) => chunk.sequence_id === sequence);
  useEffect(() => { if (retained) setWasRetained(true); }, [retained]);
  const select = (value: string) => {
    try {
      const selected = integer(value, "Chunk sequence");
      setSequence(selected);
      setSelectionKey((value) => value + 1);
      setWasRetained((previous) => selected === sequence && previous
        || snapshot.chunks.some((chunk) => chunk.sequence_id === selected));
      setError(null);
    }
    catch (cause) { setError((cause as Error).message); }
  };
  return <>
    <div className="preview-chunks" role="group" aria-label="Retained chunks">
      {snapshot.chunks.map((chunk) => {
        const label = `Chunk #${chunk.sequence_id} · ${chunk.sample_count} samples`;
        return <button key={chunk.sequence_id} type="button" className="preview-chunk-dot"
          title={label} aria-label={label} aria-pressed={chunk.sequence_id === sequence}
          onClick={() => select(String(chunk.sequence_id))}>
          <span className="preview-chunk-dot-marker" aria-hidden="true" />
        </button>;
      })}
    </div>
    {!snapshot.chunks.length && <p role="status">No chunks retained yet.</p>}
    <form className="actions" onSubmit={(event) => { event.preventDefault(); select(query); }}>
      <label>Chunk sequence <input value={query} inputMode="numeric" onChange={(event) => setQuery(event.target.value)} /></label>
      <button type="submit" disabled={!snapshot.capture_id}>View chunk</button>
    </form>
    {error && <p className="error" role="alert">{error}</p>}
    {wasRetained && !retained && <p role="status">Selected chunk #{sequence} has left the live window.</p>}
    {sequence !== null && snapshot.capture_id && <PreviewChunkDetail
      generation={snapshot.generation} captureId={snapshot.capture_id} sequenceId={sequence} snapshot={snapshot}
      selectionKey={selectionKey} onSelectSequence={(value) => {
        setSequence(value); setWasRetained(true); setError(null);
      }} />}
  </>;
}

export function PreviewStatus({ generation, onSnapshot }: {
  generation: string; onSnapshot: (snapshot: Preview | null) => void;
}) {
  const status = usePoll(() => api.acqPreview(generation), 250);
  useEffect(() => { onSnapshot(status.error ? null : status.data); }, [status.data, status.error, onSnapshot]);
  const snapshot = status.data;
  if (!snapshot) return status.error ? <p className="error" role="alert">{status.error.message}</p>
    : <p role="status">Loading preview status…</p>;
  return <>
    {status.error && <p className="error" role="alert">Live updates paused: {status.error.message}</p>}
    <p role="status">{snapshot.status}{snapshot.reason ? ` · ${snapshot.reason.replace(/_/g, " ")}` : ""}.
      Observed {snapshot.observed} · Sent {snapshot.enqueued} · Retained {snapshot.received} · Dropped {snapshot.dropped}</p>
    <p className="meta">Current stream: {snapshot.stream_chunk_count} chunks / {snapshot.stream_sample_count} samples.
      Last sequence: {snapshot.last_sequence}.</p>
    {!snapshot.available && <p className="error" role="alert">Preview delivery is unavailable. Acquisition continues independently.</p>}
    {(snapshot.dropped > 0 || snapshot.cache_errors > 0) && <p role="status">This capture is partial.
      Oversized: {snapshot.oversized}. Cache errors: {snapshot.cache_errors}.</p>}
    {!snapshot.continuous && snapshot.status === "finished" && snapshot.received < snapshot.enqueued
      && <p role="status">Some sent chunks have not reached the cache. Only retained chunks can be inspected.</p>}
    <PreviewChunks key={snapshot.capture_id ?? "idle"} snapshot={snapshot} />
  </>;
}

function CapturePanel({ generation, revision, connectionId, streaming, disabled }: Context) {
  const [form, setForm] = useState<PreviewForm>({ rows: "0", sampleStart: "0", sampleCount: "128", chunkCount: "32", includeTrigger: true });
  const [snapshot, setSnapshot] = useState<Preview | null>(null);
  const [epoch, setEpoch] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const change = (name: keyof PreviewForm, value: string | boolean) => setForm((current) => ({ ...current, [name]: value }));
  const applySelection = async () => {
    if (!snapshot) return;
    setError(null); setBusy(true);
    try {
      const result = await api.acqPreviewStart({ generation, expected_revision: revision,
        connection_id: connectionId!, selection: parsePreviewSelection(form, snapshot.limits), continuous: true });
      if (!result) throw new Error("No preview status was returned.");
      setSnapshot(result); setEpoch((value) => value + 1);
    } catch (cause) { setError((cause as Error).message); }
    finally { setBusy(false); }
  };
  return <>
    <p className="meta">Reading automatically retains the latest {snapshot?.limits.max_chunks ?? 32} chunks.
      Each green dot is a cached chunk, ordered from oldest to newest. Click a dot to inspect its matrix;
      the selected matrix stays fixed as new data arrives.</p>
    <details onToggle={(event) => {
      const selected = snapshot?.selection;
      if (event.currentTarget.open && selected) setForm({ rows: (selected.rows ?? [0]).join(", "),
        sampleStart: String(selected.sample_start ?? 0), sampleCount: String(selected.sample_count ?? 128),
        chunkCount: String(snapshot!.limits.max_chunks), includeTrigger: selected.include_trigger ?? true });
    }}>
      <summary>Matrix selection</summary>
      <p className="meta">Large matrices are limited to the selected rows and sample range.
        Applying a selection starts a new live window. Each new reading starts with the default bounded selection.</p>
      <fieldset disabled={busy || disabled} className="preview-selection">
        <legend>Rows and samples</legend>
        <label>Signal rows (comma-separated)<input value={form.rows} onChange={(event) => change("rows", event.target.value)} /></label>
        <label>Sample start<input inputMode="numeric" value={form.sampleStart} onChange={(event) => change("sampleStart", event.target.value)} /></label>
        <label>Samples per chunk<input inputMode="numeric" value={form.sampleCount} onChange={(event) => change("sampleCount", event.target.value)} /></label>
        <label className="preview-trigger"><input type="checkbox" checked={form.includeTrigger} onChange={(event) => change("includeTrigger", event.target.checked)} />Include trigger row</label>
      </fieldset>
      {snapshot && <p className="meta">Limits: {snapshot.limits.max_rows} signal rows, {snapshot.limits.max_samples} samples per chunk,{" "}
        {snapshot.limits.max_values} values including trigger.</p>}
      <button type="button" disabled={busy || disabled || !streaming || !connectionId || !snapshot?.available}
        onClick={() => void applySelection()}>Apply selection</button>
    </details>
    {!streaming && <p className="meta">Start reading to accumulate green dots. After reading stops, the last window remains available while the engine is running.</p>}
    {error && <p className="error" role="alert">{error}</p>}
    <PreviewStatus key={epoch} generation={generation} onSnapshot={setSnapshot} />
  </>;
}

export function AcquisitionPreview({ generation, revision, connectionId, streaming, disabled }: {
  generation: string | null | undefined; revision: string | null | undefined;
  connectionId: string | null; streaming: boolean; disabled: boolean;
}) {
  const [open, setOpen] = useState(true);
  return <details className="acquisition-preview" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>Inspect acquisition chunks</summary>
    {open && (generation && revision ? <CapturePanel key={`${generation}:${connectionId}:${revision}`}
      generation={generation} revision={revision} connectionId={connectionId} streaming={streaming} disabled={disabled} />
      : <p className="meta">Start the engine to inspect acquisition output.</p>)}
  </details>;
}
