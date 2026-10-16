const fs = require('node:fs');

// `docker buildx --cache-to type=gha` reads these from the environment.
// They are injected into JavaScript actions, not into `run:` steps, and
// docker/build-push-action would set them itself. This step only republishes
// them so the retry script can call buildx. Values are never logged.
const KEYS = [
  'ACTIONS_RUNTIME_TOKEN',
  'ACTIONS_RUNTIME_URL',
  'ACTIONS_RESULTS_URL',
  'ACTIONS_CACHE_URL',
  'ACTIONS_CACHE_SERVICE_V2',
];

const envFile = process.env.GITHUB_ENV;
if (!envFile) {
  console.error('GITHUB_ENV is not set');
  process.exit(1);
}

if (process.env.GITHUB_ACTIONS === 'true' && !process.env.ACTIONS_RUNTIME_TOKEN) {
  console.error('ACTIONS_RUNTIME_TOKEN is not available to this action');
  process.exit(1);
}

const exported = [];
const lines = [];
for (const key of KEYS) {
  const value = process.env[key];
  if (!value) continue;
  const delim = `EOF_${key}`;
  lines.push(`${key}<<${delim}`, value, delim);
  exported.push(key);
}
if (lines.length) {
  fs.appendFileSync(envFile, `${lines.join('\n')}\n`);
}
console.log(exported.length ? `exported ${exported.join(', ')}` : 'exported nothing');
