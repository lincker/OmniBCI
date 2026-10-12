import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const [tool, ...args] = process.argv.slice(2);
if (!['prepare-runtime', 'verify-runtime'].includes(tool)) throw new Error('Unknown runtime tool');
const python = process.env.OMNIBCI_BUILD_PYTHON ?? (process.platform === 'win32' ? 'python' : 'python3');
const result = spawnSync(python, ['-B', fileURLToPath(new URL(`${tool}.py`, import.meta.url)), ...args], { stdio: 'inherit' });
if (result.error) throw result.error;
process.exit(result.status ?? 1);
