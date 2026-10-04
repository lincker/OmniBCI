import { spawnSync } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const desktop = fileURLToPath(new URL('../', import.meta.url));
const target = `${process.platform}-${process.arch}`;
const directories = {
  'darwin-x64': 'mac/OmniBCI.app',
  'darwin-arm64': 'mac-arm64/OmniBCI.app',
  'win32-x64': 'win-unpacked',
  'linux-x64': 'linux-unpacked',
};
if (!directories[target]) throw new Error(`Unsupported installer target: ${target}`);
const application = path.join(desktop, 'release', directories[target]);
const resources = path.join(application, process.platform === 'darwin' ? 'Contents/Resources' : 'resources');
const packaged = JSON.parse(await readFile(path.join(resources, 'runtime-manifest.json'), 'utf8'));
const staged = JSON.parse(await readFile(path.join(desktop, 'resources/runtime-manifest.json'), 'utf8'));
if (packaged.target !== target || JSON.stringify(packaged) !== JSON.stringify(staged)) {
  throw new Error('Rebuild and verify the unpacked app before creating its installer');
}
const require = createRequire(import.meta.url);
const result = spawnSync(process.execPath, [
  require.resolve('electron-builder/cli.js'), '--config', 'electron-builder.yml',
  '--prepackaged', application, ...process.argv.slice(2), '--publish', 'never',
], { cwd: desktop, stdio: 'inherit' });
if (result.error) throw result.error;
process.exit(result.status ?? 1);
