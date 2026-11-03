// CRA-1534: exercise an installed packed artifact, not just its build directory.
// No registry/provider requests: all install, ci and pack fixtures are local.
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";

assert.ok(process.env.SECURITY_NPM_PACKAGE, "set SECURITY_NPM_PACKAGE to the installed npm directory");
assert.ok(process.env.SECURITY_NPM_LOCK, "set SECURITY_NPM_LOCK to the audited build lock");
const npmPackage = path.resolve(process.env.SECURITY_NPM_PACKAGE);
const cli = path.join(npmPackage, "bin/npm-cli.js");
const requireNpm = createRequire(path.join(npmPackage, "package.json"));
const fixtures = mkdtempSync(path.join(tmpdir(), "cra1534-npm-smoke-"));
const env = { ...process.env, NPM_CONFIG_INSTALL_STRATEGY: "hoisted" };

function json(file, value) {
  mkdirSync(path.dirname(file), { recursive: true });
  writeFileSync(file, JSON.stringify(value, null, 2) + "\n");
}

function npm(args, cwd = fixtures) {
  const result = spawnSync(process.execPath, [cli, ...args, "--no-audit", "--no-fund"], {
    cwd, env, encoding: "utf8", timeout: 20_000, maxBuffer: 8 * 1024 * 1024,
  });
  assert.equal(result.error, undefined, result.error?.message);
  assert.equal(result.status, 0, `${args.join(" ")}\n${result.stdout}\n${result.stderr}`);
  return result.stdout;
}

function project(name, manifest) {
  const dir = path.join(fixtures, name);
  json(path.join(dir, "package.json"), manifest);
  return dir;
}

test("every installed bundled package matches the complete audited build lock", () => {
  const lock = JSON.parse(readFileSync(process.env.SECURITY_NPM_LOCK, "utf8"));
  const expected = Object.fromEntries(Object.entries(lock.packages).filter(([name]) => name).map(([name, p]) => [name, p.version]));
  const actual = {};
  function scan(modules) {
    for (const entry of readdirSync(modules, { withFileTypes: true })) {
      if (entry.name === ".bin" || entry.name === ".package-lock.json") continue;
      assert.equal(entry.isSymbolicLink(), false, `unexpected package link: ${entry.name}`);
      const entries = entry.name.startsWith("@")
        ? readdirSync(path.join(modules, entry.name)).map(name => path.join(entry.name, name))
        : [entry.name];
      for (const name of entries) {
        const dir = path.join(modules, name);
        const pkg = JSON.parse(readFileSync(path.join(dir, "package.json"), "utf8"));
        actual[path.relative(npmPackage, dir).split(path.sep).join("/")] = pkg.version;
        const children = readdirSync(dir, { withFileTypes: true });
        if (children.some(child => child.name === "node_modules")) scan(path.join(dir, "node_modules"));
      }
    }
  }
  scan(path.join(npmPackage, "node_modules"));
  assert.deepEqual(actual, expected);
});

test("packed CLI keeps its honest fork version", () => {
  assert.equal(npm(["--version"]).trim(), "12.2.1-crasiak.1");
});

test("install, clean ci and list work with an offline local dependency", () => {
  const dependency = project("dependency", { name: "fixture-dependency", version: "1.0.0", main: "index.js" });
  writeFileSync(path.join(dependency, "index.js"), "module.exports = 42;\n");
  const manifest = { name: "fixture-consumer", version: "1.0.0", dependencies: { "fixture-dependency": "file:../dependency" } };
  const first = project("install", manifest);
  npm(["install", "--ignore-scripts", "--offline"], first);
  const second = project("clean-ci", manifest);
  writeFileSync(path.join(second, "package-lock.json"), readFileSync(path.join(first, "package-lock.json")));
  npm(["ci", "--ignore-scripts", "--offline"], second);
  const listed = JSON.parse(npm(["list", "--json"], second));
  assert.equal(listed.dependencies["fixture-dependency"].version, "1.0.0");
  assert.equal(createRequire(path.join(second, "package.json"))("fixture-dependency"), 42);
});

test("workspace glob discovery, listing and packing include both packages", () => {
  const root = project("workspaces", { name: "fixture-workspaces", version: "1.0.0", private: true, workspaces: ["packages/*"] });
  for (const name of ["alpha", "beta"]) {
    json(path.join(root, "packages", name, "package.json"), { name: `fixture-${name}`, version: "1.0.0" });
    writeFileSync(path.join(root, "packages", name, "index.js"), "export default 1;\n");
  }
  npm(["install", "--ignore-scripts", "--offline"], root);
  const listed = JSON.parse(npm(["list", "--workspaces", "--json"], root));
  assert.deepEqual(Object.keys(listed.dependencies).sort(), ["fixture-alpha", "fixture-beta"]);
  const packed = JSON.parse(npm(["pack", "--workspaces", "--ignore-scripts", "--json"], root));
  // npm12 returns a workspace-name-keyed object, verified against unmodified12.2.1.
  assert.deepEqual(Object.values(packed).map(p => p.name).sort(), ["fixture-alpha", "fixture-beta"]);
});

test("glob14 traverses wildcard/brace patterns with the new matcher API", () => {
  const root = project("globs", { name: "fixture-globs", version: "1.0.0" });
  for (const file of ["a.js", "b.ts", "ignore.txt", "nested/c.js"]) {
    mkdirSync(path.dirname(path.join(root, file)), { recursive: true });
    writeFileSync(path.join(root, file), "fixture\n");
  }
  const { globSync } = requireNpm("glob");
  assert.deepEqual(globSync("**/*.{js,ts}", { cwd: root }).sort(), ["a.js", "b.ts", "nested/c.js"]);
});

test("minimatch adversarial preprocessing stays bounded and retains matching", () => {
  const source = `
    const assert = require('node:assert/strict');
    const {createRequire} = require('node:module');
    const req = createRequire(${JSON.stringify(path.join(npmPackage, "package.json"))});
    const {Minimatch} = req('minimatch');
    const mm = new Minimatch(Array(30).fill('**/../a/b').join('/'), {optimizationLevel:2, braceExpandMax:64});
    assert.equal(mm.set.length, 64);
    const small = new Minimatch('**/../a/b/**/../c/d', {optimizationLevel:2});
    assert.equal(small.set.length, 4);
    assert.equal(small.match('../a/b/../c/d'), true);
    assert.equal(small.match('x/a/b/y/c/d'), true);
  `;
  const result = spawnSync(process.execPath, ["-e", source], { env, encoding: "utf8", timeout: 5000 });
  assert.equal(result.error, undefined, result.error?.message);
  assert.equal(result.status, 0, result.stderr);
});

console.log(`Owned fixtures retained: ${fixtures}`);
