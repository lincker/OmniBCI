/** Offline authoring reference backed by the executable repository starter. */
import { useI18n } from "./i18n";
import manifest from "../../examples/decoder_plugin/manifest.toml?raw";
import decoder from "../../examples/decoder_plugin/decoder.py?raw";
import schema from "../../examples/decoder_plugin/default.schema.json?raw";
import readme from "../../examples/decoder_plugin/README.md?raw";
import guide from "../../docs/decoder-plugin-development.md?raw";

const files = [
  { name: "manifest.toml", content: manifest, purpose: "guide.file.manifest" },
  { name: "decoder.py", content: decoder, purpose: "guide.file.decoder" },
  { name: "default.schema.json", content: schema, purpose: "guide.file.schema" },
  { name: "README.md", content: readme, purpose: "guide.file.readme" },
] as const;
const download = (text: string) => `data:text/plain;charset=utf-8,${encodeURIComponent(text)}`;

export function DecoderDeveloperGuide() {
  const { t } = useI18n();
  return <article className="decoder-guide" aria-label={t("guide.ariaLabel")}>
    <p className="decoder-guide-intro">{t("guide.intro")}</p>
    <p><a className="decoder-guide-download" href={download(guide)} download="decoder-plugin-development.md">{t("guide.download")}</a></p>

    <section aria-labelledby="decoder-files-title"><h2 id="decoder-files-title">{t("guide.filesTitle")}</h2>
      <p>{t("guide.filesDirectory")}<code>manifest.toml</code>{t("guide.filesPaths")}<code>assets/</code>{t("guide.period")}</p>
      <div className="decoder-guide-files">{files.map((file) => <div key={file.name}>
        <a href={download(file.content)} download={file.name}>{file.name}</a><p>{t(file.purpose)}</p>
      </div>)}</div>
      <p className="meta">{t("guide.starterNote")}</p>
    </section>

    <section aria-labelledby="decoder-channels-title"><h2 id="decoder-channels-title">{t("guide.channelsTitle")}</h2>
      <p>{t("guide.channelRequirements")}</p>
      <p>{t("guide.channelMapping")}</p>
      <dl>
        <dt>description</dt><dd>{t("guide.descriptionField")}</dd>
        <dt>signal_type</dt><dd>{t("guide.signalTypeField")}</dd>
        <dt>min_count</dt><dd>{t("guide.minCountField")}</dd>
        <dt>required_names</dt><dd>{t("guide.requiredNamesField")}</dd>
        <dt>default_names</dt><dd>{t("guide.defaultNamesField")}</dd>
      </dl>
      <p>{t("guide.use")}<code>signal_input_contract = 2</code>{t("guide.contractValidation")}<code>context.input_signal</code>{t("guide.constructorEnd")}</p>
      <details open><summary>{t("guide.manifestSummary")}</summary><pre><code>{manifest}</code></pre></details>
    </section>

    <section aria-labelledby="decoder-code-title"><h2 id="decoder-code-title">{t("guide.sdkTitle")}</h2>
      <p>{t("guide.importFrom")}<code>omnibci.sdk</code>{t("guide.constructorReceives")}<code>parameters</code>{t("guide.and")}<code>context</code>{t("guide.constructorKeywords")}</p>
      <ul>
        <li><code>should_decode(window)</code>{t("guide.readinessCheck")}</li>
        <li><code>decode(window, latest_samples)</code>{t("guide.returns")}<code>Prediction(target_id=…)</code>{t("guide.or")}<code>None</code>{t("guide.predictionOutcome")}</li>
        <li>{t("guide.arrayShape")}<code>context.input_signal.channels</code>{t("guide.mappedRows")}<code>context.acquired_signal.channels</code>{t("guide.originalAcquisition")}</li>
        <li><code>window</code>{t("guide.orderedFilters")}<code>latest_samples</code>{t("guide.latestSamples")}</li>
        <li>{t("guide.latestRequired")}<code>latest_samples</code>{t("guide.emptyFragment")}<code>(signal_channels, 0)</code>{t("guide.noFallback")}</li>
        <li>{t("guide.boundaryAndDrain")}</li>
        <li>{t("guide.use")}<code>context.resource_root</code>{t("guide.packagedAssets")}<code>for_new_epoch()</code>{t("guide.epochState")}</li>
      </ul>
      <details><summary>{t("guide.decoderSummary")}</summary><pre><code>{decoder}</code></pre></details>
      <details><summary>{t("guide.schemaSummary")}</summary><pre><code>{schema}</code></pre></details>
      <p>{t("guide.parameterDescriptions")}<code>description</code>{t("guide.schemaSubset")}</p>
    </section>

    <section aria-labelledby="decoder-install-title"><h2 id="decoder-install-title">{t("guide.installTitle")}</h2>
      <p>{t("guide.packageCommand")}</p>
      <pre><code>python -m zipfile -c ../oz-polarity-demo.zip manifest.toml decoder.py default.schema.json README.md</code></pre>
      <ol>
        <li>{t("guide.keep")}<code>manifest.toml</code>{t("guide.packageRoot")}</li>
        <li>{t("guide.use")}<strong>{t("guide.installLocation")}</strong>{t("guide.enableInstalled")}</li>
        <li>{t("guide.in")}<strong>{t("guide.setupLocation")}</strong>{t("guide.configureProfile")}</li>
        <li>{t("guide.verifyInput")}</li>
      </ol>
      <p>{t("guide.installationChecks")}<code>package.host_version</code>{t("guide.and")}<code>[package.requires]</code>{t("guide.exactVersions")}</p>
      <p>{t("guide.completeGuideHelp")}</p>
    </section>
  </article>;
}
