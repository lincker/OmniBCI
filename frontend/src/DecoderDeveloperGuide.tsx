/** Offline authoring reference backed by the executable repository starter. */
import manifest from "../../examples/decoder_plugin/manifest.toml?raw";
import decoder from "../../examples/decoder_plugin/decoder.py?raw";
import schema from "../../examples/decoder_plugin/default.schema.json?raw";
import readme from "../../examples/decoder_plugin/README.md?raw";
import guide from "../../docs/decoder-plugin-development.md?raw";

const files = [
  { name: "manifest.toml", content: manifest, purpose: "Required at the package root. Declares identity, entrypoint, schema and channel requirements." },
  { name: "decoder.py", content: decoder, purpose: "Required implementation. Subclass Decoder and implement should_decode() and decode()." },
  { name: "default.schema.json", content: schema, purpose: "Required for editable UI Profiles. Declares algorithm parameters, defaults and validation." },
  { name: "README.md", content: readme, purpose: "Recommended. Explain usage, model assumptions, target ids and validation." },
];
const download = (text: string) => `data:text/plain;charset=utf-8,${encodeURIComponent(text)}`;

export function DecoderDeveloperGuide() {
  return <article className="decoder-guide" aria-label="Decoder developer guide">
    <p className="decoder-guide-intro">Build a Decoder with three files: a manifest, a Python implementation and a parameter schema.
      This guide and its starter files are included in the app and work offline.</p>
    <p><a className="decoder-guide-download" href={download(guide)} download="decoder-plugin-development.md">Download complete guide</a></p>

    <section aria-labelledby="decoder-files-title"><h2 id="decoder-files-title">1. Start with these files</h2>
      <p>Save these files in one directory. Only <code>manifest.toml</code> has a fixed filename; the manifest references the implementation and schema paths.
        Model weights and other resources can be added under <code>assets/</code>.</p>
      <div className="decoder-guide-files">{files.map((file) => <div key={file.name}>
        <a href={download(file.content)} download={file.name}>{file.name}</a><p>{file.purpose}</p>
      </div>)}</div>
      <p className="meta">The starter reads the mean of Oz and returns target 1 or 2. This arbitrary tutorial rule is not a validated BCI classifier.</p>
    </section>

    <section aria-labelledby="decoder-channels-title"><h2 id="decoder-channels-title">2. Describe the channels your algorithm needs</h2>
      <p>Declare read-only requirements in the manifest. The Decoder Profile keeps the user's actual input selection and reference settings.</p>
      <p>Bind Acquisition channels on the left to algorithm inputs on the right. Bound signals pass through Epoching first;
        optional ordered filters transform the assembled window before the Decoder runs.</p>
      <dl>
        <dt>description</dt><dd>Explain the input assumptions; this text does not run validation.</dd>
        <dt>signal_type</dt><dd>Optional type constraint for every final input channel, such as eeg.</dd>
        <dt>min_count</dt><dd>Minimum signal-channel count after mapping; trigger and reference-only rows do not count.</dd>
        <dt>required_names</dt><dd>Names that must be present. Extra channels are allowed; their order is not prescribed.</dd>
        <dt>default_names</dt><dd>Ordered initial selection for new Profiles. Empty means all source channels. Existing Profiles keep their saved selection.</dd>
      </dl>
      <p>Use <code>signal_input_contract = 2</code> for these declarations. Match the final mapped names exactly.
        For strict model order, unit or reference assumptions, also validate <code>context.input_signal</code> in the constructor.</p>
      <details open><summary>Commented manifest</summary><pre><code>{manifest}</code></pre></details>
    </section>

    <section aria-labelledby="decoder-code-title"><h2 id="decoder-code-title">3. Implement the SDK contract</h2>
      <p>Import from <code>omnibci.sdk</code>. The API 2 constructor receives <code>parameters</code> and <code>context</code> as keyword arguments.
        No registry decorator is required.</p>
      <ul>
        <li><code>should_decode(window)</code> performs a cheap readiness check before filters run.</li>
        <li><code>decode(window, latest_samples)</code> returns <code>Prediction(target_id=…)</code> or <code>None</code>. Handle short windows even when decoding is forced.</li>
        <li>Arrays contain signal rows × samples, without trigger. Use <code>context.input_signal.channels</code> for mapped row names;
          <code>context.channel_names</code> describes the original Acquisition.</li>
        <li><code>window</code> passes through ordered filters. <code>latest_samples</code> contains the most recently appended samples and does not pass through those window filters. Whole-window decoders can ignore it.</li>
        <li>Use <code>context.resource_root</code> for packaged assets. Stateful decoders override <code>for_new_epoch()</code> to isolate per-epoch state.</li>
      </ul>
      <details><summary>Executable decoder.py</summary><pre><code>{decoder}</code></pre></details>
      <details><summary>Parameter schema</summary><pre><code>{schema}</code></pre></details>
      <p>JSON parameter descriptions belong in <code>description</code> fields. Use the supported Schema subset; external references and arbitrary JSON Schema keywords are rejected.</p>
    </section>

    <section aria-labelledby="decoder-install-title"><h2 id="decoder-install-title">4. Package, install and verify</h2>
      <p>From inside the starter directory, run:</p>
      <pre><code>python -m zipfile -c ../oz-polarity-demo.zip manifest.toml decoder.py default.schema.json README.md</code></pre>
      <ol>
        <li>Keep <code>manifest.toml</code> at the ZIP root. Rename the tutorial package identity for your plugin; increment its version when distributing changed files.</li>
        <li>Use <strong>Extensions → Install ZIP…</strong>, then enable the installed version.</li>
        <li>In <strong>Setup → Signal Processing → Decoder</strong>, create a Profile, review its input, select it and apply. Choose an Acquisition that provides the required named channels.</li>
        <li>Preview the mapped input and run known replay data with a suitable epoch boundary detector. Check targets, reordered inputs, missing channels and short windows.</li>
      </ol>
      <p>Installation checks metadata and schemas without executing your algorithm. The host does not install dependencies.
        Optional <code>package.host_version</code> and <code>[package.requires]</code> declare exact host and library versions, not version ranges.</p>
      <p>See the downloadable complete guide for schema, lifecycle, compatibility and testing details.</p>
    </section>
  </article>;
}
