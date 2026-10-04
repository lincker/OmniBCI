import { build } from "esbuild";
import { cp, mkdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = fileURLToPath(new URL("../", import.meta.url));
await mkdir(path.join(root, "dist"), { recursive: true });
await build({
  entryPoints: { main: path.join(root, "src/main.ts"), preload: path.join(root, "src/preload.ts") },
  outdir: path.join(root, "dist"), outExtension: { ".js": ".cjs" },
  bundle: true, platform: "node", format: "cjs", target: "node22", external: ["electron"],
  sourcemap: true,
});
await cp(path.join(root, "../frontend/dist"), path.join(root, "dist/renderer"), { recursive: true });
