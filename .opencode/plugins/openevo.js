/**
 * OpenCode plugin entry point — a shim, deliberately.
 *
 * `opencode.json` names this file, and OpenCode loads it directly rather than
 * resolving a package. It therefore has to be the thing that knows where the
 * real plugin lives, and nothing more.
 *
 * This used to be a 285-line copy of the compiled `src/index.ts` dropped in
 * beside nothing else, so its `import "./worker.js"` and
 * `import "./brain-bridge.js"` resolved against a directory that contained
 * only this file. The plugin could not load at all, and the failure surfaced
 * as a module-resolution error naming a file nobody had ever created.
 *
 * Delegating keeps one implementation. The build output it forwards to is not
 * committed (see .gitignore); `bootstrap.sh` / `bootstrap.ps1` produce it, or
 * `npm --prefix packages/opencode-plugin run build` does.
 */

const PLUGIN_DIST = "../../packages/opencode-plugin/dist/index.js";

export const OpenEvoPlugin = async (ctx) => {
  const target = new URL(PLUGIN_DIST, import.meta.url);

  let mod;
  try {
    mod = await import(target.href);
  } catch (cause) {
    // Say which of the two likely causes it is. "Cannot find module" pointing
    // at a dist path is otherwise indistinguishable from a broken install.
    throw new Error(
      "OpenEvo plugin is not built. Run:\n" +
        "  npm --prefix packages/opencode-plugin install\n" +
        "  npm --prefix packages/opencode-plugin run build\n" +
        "(or ./bootstrap.sh, which does both).\n" +
        `Tried to load: ${target.href}`,
      { cause }
    );
  }

  if (typeof mod.OpenEvoPlugin !== "function") {
    throw new Error(
      `${target.href} loaded but exports no OpenEvoPlugin function. ` +
        "The build is stale or partial — rebuild the plugin."
    );
  }

  return mod.OpenEvoPlugin(ctx);
};

export default OpenEvoPlugin;
