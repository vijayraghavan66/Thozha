import { execFileSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import openapiTS, { astToString } from 'openapi-typescript';
import { parse } from 'yaml';

const revision = '0f0541d903d1aaa5144dda79881d853aa1ff8443';
const path = 'openapi/thozha-v1.yaml';
const sourceRevision = process.argv[3] || (process.argv[2] ? 'supplied OpenAPI file' : revision);
const document = parse(
  process.argv[2]
    ? readFileSync(process.argv[2], 'utf8')
    : execFileSync('git', ['show', `${revision}:${path}`], { encoding: 'utf8' })
);
const paths = Object.fromEntries(
  Object.entries(document.paths).filter(([key]) => key.startsWith('/v1/web/'))
);
if (!Object.keys(paths).length) throw new Error('No /v1/web routes found in the OpenAPI spec');

const components = {
  securitySchemes: Object.fromEntries(
    ['WebSessionCookie', 'WebCsrf'].map(key => [key, document.components.securitySchemes[key]])
  ),
};
function followReferences(value) {
  if (!value || typeof value !== 'object') return;
  if (typeof value.$ref === 'string' && value.$ref.startsWith('#/components/')) {
    const [, , group, name] = value.$ref.split('/');
    components[group] ??= {};
    if (!Object.hasOwn(components[group], name)) {
      const definition = document.components[group][name];
      if (!definition) throw new Error(`Unresolved OpenAPI component: ${value.$ref}`);
      components[group][name] = definition;
      followReferences(components[group][name]);
    }
  }
  for (const entry of Object.values(value)) followReferences(entry);
}
followReferences(paths);
const result = await openapiTS({
  openapi: document.openapi,
  info: { title: 'Thozha Web API', version: document.info.version },
  paths,
  components,
});
writeFileSync(
  new URL('../src/api/generated.ts', import.meta.url),
  `// Generated from ${path} at ${sourceRevision}.\n// Do not edit. Run npm run contract:generate.\n\n${astToString(result)}`
);
