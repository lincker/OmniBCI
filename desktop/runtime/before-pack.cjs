const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');

module.exports = async function beforePack(context) {
  const project = context.packager.projectDir;
  const manifest = JSON.parse(fs.readFileSync(path.join(project, 'resources/runtime-manifest.json'), 'utf8'));
  const versions = JSON.parse(fs.readFileSync(path.join(project, 'runtime/versions.json'), 'utf8'));
  const architectures = { 0: 'ia32', 1: 'x64', 3: 'arm64' };
  const target = `${context.electronPlatformName}-${architectures[context.arch]}`;
  if (manifest.target !== target || manifest.pythonVersion !== versions.pythonVersion) {
    throw new Error(`Stage the matching pinned runtime before packaging ${target}`);
  }
  if (manifest.applicationVersion !== context.packager.appInfo.version) {
    throw new Error('Desktop and runtime application versions differ');
  }
  const lock = fs.readFileSync(path.join(project, 'runtime/locks', `${target}.txt`));
  const digest = createHash('sha256').update(lock).digest('hex');
  if (manifest.dependencyLock.sha256 !== digest || manifest.pythonDistribution.sha256 !== versions.targets[target].sha256) {
    throw new Error('Runtime inputs changed; stage the runtime again before packaging');
  }
  if (!fs.existsSync(path.join(project, 'resources', manifest.interpreter))) {
    throw new Error('The staged Python interpreter is missing');
  }
};
