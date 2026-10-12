import { spawn } from "node:child_process";
import { isAbsolute } from "node:path";
import electron from "electron";
if (!process.env.OMNIBCI_DESKTOP_PYTHON || !isAbsolute(process.env.OMNIBCI_DESKTOP_PYTHON)) {
  throw new Error("Set OMNIBCI_DESKTOP_PYTHON to an absolute Python 3.14 interpreter path with OmniBCI installed.");
}
const child = spawn(electron, ["."], { stdio: "inherit", env: process.env });
child.on("exit", code => { process.exitCode = code ?? 1; });
