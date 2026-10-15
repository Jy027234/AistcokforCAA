import { buildSync } from "esbuild";
import { Module, createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

// Component interaction checks run in Node, without starting a browser or API.
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);
const result = buildSync({ entryPoints: [resolve(root, "tests/user-flow.test.tsx")],
  bundle: true, write: false, platform: "node", format: "cjs", jsx: "automatic",
  external: ["react", "react/jsx-runtime", "react-dom/server", "react-test-renderer"],
  define: { "import.meta.env": "{}" },
});
const instance = new Module(resolve(root, "user-flow-tests.cjs"));
instance.filename = resolve(root, "user-flow-tests.cjs");
instance.paths = Module._nodeModulePaths(root);
instance.require = require;
instance._compile(result.outputFiles[0].text, instance.filename);
