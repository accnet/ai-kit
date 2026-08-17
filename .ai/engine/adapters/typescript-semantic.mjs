#!/usr/bin/env node
// Dependency-free from AI-Kit's perspective: this adapter intentionally uses
// the TypeScript Compiler API installed and locked by the inspected project.
// It emits metadata only; source text never leaves the project process.
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';

const read = () => new Promise(resolve => {
  let value = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', chunk => { value += chunk; });
  process.stdin.on('end', () => resolve(value));
});
// Node's ESM import is asynchronous; keep the protocol deliberately simple.
const crypto = await import('node:crypto');
const awaitImportCrypto = crypto.default || crypto;

function range(node, source) {
  const start = source.getLineAndCharacterOfPosition(node.getStart(source));
  const end = source.getLineAndCharacterOfPosition(node.getEnd());
  return { start_line: start.line + 1, start_column: start.character, end_line: end.line + 1, end_column: end.character };
}

function tokenParts(value) { return String(value || '').replace(/[^A-Za-z0-9_]+/g, '_'); }
function symbolId(file, kind, name, signature) {
  const digest = awaitImportCrypto.createHash('sha256').update(signature || '').digest('hex').slice(0, 12);
  return `symbol:typescript:${encodeURIComponent(file).replace(/%2F/g, '/')}#${kind}:${encodeURIComponent(name)}:${digest}`;
}

const input = JSON.parse((await read()) || '{}');
const root = path.resolve(input.root || process.cwd());
let ts;
try {
  const require = createRequire(path.join(root, 'package.json'));
  ts = require(require.resolve('typescript', { paths: [root] }));
} catch (error) {
  console.log(JSON.stringify({
    status: 'unavailable', adapter: { id: 'typescript-compiler', version: 1 }, files: [],
    diagnostics: [{ kind: 'adapter-unavailable', language: 'typescript', detail: `TypeScript Compiler API unavailable: ${error.message}` }],
  }));
  process.exit(0);
}

const configPath = ts.findConfigFile(root, ts.sys.fileExists, 'tsconfig.json');
let options = {};
if (configPath) {
  const parsed = ts.readConfigFile(configPath, ts.sys.readFile);
  if (!parsed.error) options = ts.parseJsonConfigFileContent(parsed.config, ts.sys, path.dirname(configPath)).options;
}
const files = (input.files || []).map(file => path.resolve(root, file));
const host = ts.createCompilerHost(options, true);
const program = ts.createProgram({ rootNames: files, options, host });
const output = { status: 'pass', adapter: { id: 'typescript-compiler', version: 1, parser_version: ts.version, tsconfig: configPath ? path.relative(root, configPath).replaceAll('\\', '/') : null }, files: [], diagnostics: [] };

for (const absolute of files.sort()) {
  const source = program.getSourceFile(absolute);
  const relative = path.relative(root, absolute).replaceAll('\\', '/');
  if (!source) {
    output.diagnostics.push({ kind: 'parse-error', path: relative, language: 'typescript', detail: 'source file was not included in TypeScript program' });
    continue;
  }
  const file = { path: relative, content_hash: awaitImportCrypto.createHash('sha256').update(source.text).digest('hex'), parse_status: 'pass', symbols: [], imports: [], diagnostics: [] };
  const diagnosticSlice = source.parseDiagnostics || [];
  for (const diagnostic of diagnosticSlice) file.diagnostics.push({ kind: 'parse-error', path: relative, language: 'typescript', detail: ts.flattenDiagnosticMessageText(diagnostic.messageText, '\n') });
  if (file.diagnostics.length) file.parse_status = 'fail';

  function addSymbol(node, kind, name, prefix = '') {
    const qualified = prefix ? `${prefix}.${name}` : name;
    const signature = node.getText(source).split('{', 1)[0];
    file.symbols.push({ id: symbolId(relative, kind, qualified, signature), language: 'typescript', kind, name, qualified_name: qualified, path: relative, range: range(node, source), public: !!(node.modifiers || []).find(item => item.kind === ts.SyntaxKind.ExportKeyword), signature_hash: awaitImportCrypto.createHash('sha256').update(signature).digest('hex') });
    if (ts.isClassDeclaration(node) || ts.isInterfaceDeclaration(node)) {
      for (const member of node.members || []) {
        const memberName = member.name && member.name.getText(source);
        if (memberName) addSymbol(member, ts.isMethodDeclaration(member) ? 'method' : 'member', memberName, qualified);
      }
    }
  }

  function resolve(specifier) {
    const resolution = ts.resolveModuleName(specifier, absolute, options, ts.sys).resolvedModule;
    if (!resolution || !resolution.resolvedFileName) return { to: null, resolution: 'external-or-unresolved' };
    const target = path.resolve(resolution.resolvedFileName);
    if (!target.startsWith(root + path.sep) && target !== root) return { to: null, resolution: 'external' };
    return { to: path.relative(root, target).replaceAll('\\', '/'), resolution: 'exact' };
  }

  function visit(node) {
    if (ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) {
      const specifier = node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier) ? node.moduleSpecifier.text : null;
      if (specifier) {
        const target = resolve(specifier);
        const isTypeOnly = !!node.importClause?.isTypeOnly || !!node.isTypeOnly;
        file.imports.push({ specifier, ...target, kind: ts.isExportDeclaration(node) ? 're-export' : isTypeOnly ? 'type-import' : 'runtime-import', names: [], range: range(node, source) });
      }
    } else if (ts.isImportEqualsDeclaration(node) && ts.isExternalModuleReference(node.moduleReference) && ts.isStringLiteral(node.moduleReference.expression)) {
      const specifier = node.moduleReference.expression.text;
      file.imports.push({ specifier, ...resolve(specifier), kind: 'runtime-import', names: [], range: range(node, source) });
    } else if (ts.isCallExpression(node) && node.arguments.length && ts.isStringLiteral(node.arguments[0])) {
      const isRequire = ts.isIdentifier(node.expression) && node.expression.text === 'require';
      const isDynamic = node.expression.kind === ts.SyntaxKind.ImportKeyword;
      if (isRequire || isDynamic) {
        const specifier = node.arguments[0].text;
        file.imports.push({ specifier, ...resolve(specifier), kind: isDynamic ? 'dynamic-import' : 'runtime-import', names: [], range: range(node, source) });
      }
    }
    if (ts.isClassDeclaration(node) && node.name) addSymbol(node, 'class', node.name.text);
    else if (ts.isInterfaceDeclaration(node) && node.name) addSymbol(node, 'interface', node.name.text);
    else if (ts.isFunctionDeclaration(node) && node.name) addSymbol(node, 'function', node.name.text);
    else if (ts.isTypeAliasDeclaration(node) && node.name) addSymbol(node, 'type', node.name.text);
    else if (ts.isEnumDeclaration(node) && node.name) addSymbol(node, 'enum', node.name.text);
    ts.forEachChild(node, visit);
  }
  visit(source);
  file.symbols.sort((a, b) => a.id.localeCompare(b.id));
  file.imports.sort((a, b) => `${a.specifier}:${a.kind}`.localeCompare(`${b.specifier}:${b.kind}`));
  output.files.push(file);
}
output.files.sort((a, b) => a.path.localeCompare(b.path));
console.log(JSON.stringify(output));
