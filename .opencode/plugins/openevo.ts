export const OpenEvoPlugin = async (ctx) => { const m = await import('../../packages/opencode-plugin/dist/index.js'); return m.OpenEvoPlugin(ctx); };
