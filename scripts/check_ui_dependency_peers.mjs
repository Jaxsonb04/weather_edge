import { semver } from "bun";
import { readFileSync, realpathSync } from "node:fs";
import { basename, dirname, join, resolve } from "node:path";

const readManifest = (path) => JSON.parse(readFileSync(path, "utf8"));

// Resolve peers from the consuming package, including nested installs, without
// relying on package.json exports or importing browser-only component modules.
function resolvePeerManifest(packageDirectory, name) {
  for (let directory = packageDirectory; ; directory = dirname(directory)) {
    // Node skips node_modules/node_modules when walking ancestor directories.
    if (basename(directory) !== "node_modules") {
      try {
        return readManifest(join(directory, "node_modules", name, "package.json"));
      } catch (error) {
        if (error.code !== "ENOENT" && error.code !== "ENOTDIR") throw error;
      }
    }
    if (dirname(directory) === directory) return undefined;
  }
}

export function auditUiPeerContracts(root = process.cwd()) {
  const failures = [];
  let checked = 0;
  let dependenciesChecked = 0;
  for (const name of [
    "@heroui-pro/react", "@heroui/react", "@heroui/styles",
    "react-dom", "@types/react-dom",
  ]) {
    const directory = realpathSync(resolve(root, "node_modules", name));
    const manifest = readManifest(join(directory, "package.json"));
    const peers = manifest.peerDependencies ?? {};
    if (!Object.keys(peers).length || (name === "@heroui-pro/react" &&
      (!peers["@heroui/react"] || !peers["@heroui/styles"]))) {
      failures.push(`${name}: actual peer metadata missing; install licensed artifacts first`);
      continue;
    }
    for (const [peer, range] of Object.entries(peers)) {
      const installed = resolvePeerManifest(directory, peer);
      if (!installed && manifest.peerDependenciesMeta?.[peer]?.optional) continue;
      checked += 1;
      if (!installed || !semver.satisfies(installed.version, range)) {
        failures.push(`${name}@${manifest.version} requires ${peer}@${range}; found ${installed?.version ?? "missing"}`);
      }
    }
  }
  // The explicit model override must continue to meet pm's dependency bounds
  // when the editor family advances; peer checks alone cannot verify this.
  const pmDirectory = realpathSync(resolve(root, "node_modules", "@tiptap/pm"));
  const pm = readManifest(join(pmDirectory, "package.json"));
  const dependencies = Object.entries(pm.dependencies ?? {});
  if (!dependencies.length) failures.push("@tiptap/pm: dependency metadata missing");
  for (const [name, range] of dependencies) {
    dependenciesChecked += 1;
    const installed = resolvePeerManifest(pmDirectory, name);
    if (!installed || !semver.satisfies(installed.version, range)) {
      failures.push(`@tiptap/pm@${pm.version} requires ${name}@${range}; found ${installed?.version ?? "missing"}`);
    }
  }
  return { checked, dependenciesChecked, failures };
}

if (import.meta.main) {
  const { checked, dependenciesChecked, failures } = auditUiPeerContracts();
  for (const failure of failures) console.error(failure);
  console.log(`UI dependency contracts: ${checked} peers, ${dependenciesChecked} pm dependencies, ${failures.length} failures`);
  process.exitCode = failures.length ? 1 : 0;
}
