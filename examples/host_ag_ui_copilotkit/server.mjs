/**
 * CopilotKit runtime: expose the YAI AG-UI SSE agent to the frontend.
 *
 * The browser talks to `/api/copilotkit`; this server proxies runs to the
 * YAI FastAPI AG-UI endpoint (default http://127.0.0.1:8000/agent) through
 * `HttpAgent`. After `npm run build`, this server also serves `dist/` so the
 * whole demo is a single URL.
 */

import express from "express";
import { CopilotRuntime } from "@copilotkit/runtime/v2";
import { createCopilotEndpointExpress } from "@copilotkit/runtime/v2/express";
import { HttpAgent } from "@ag-ui/client";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PORT ?? 4000);
const AGENT_URL = process.env.AG_UI_URL ?? "http://127.0.0.1:8000/agent";

process.env.COPILOTKIT_TELEMETRY_DISABLED = process.env.COPILOTKIT_TELEMETRY_DISABLED ?? "true";

const runtime = new CopilotRuntime({
  agents: {
    default: new HttpAgent({ url: AGENT_URL }),
  },
  // 单用户 demo：给 runtime 一个稳定身份，threads 列表/清空不再 403。
  identifyUser: () => ({ id: "demo-user", name: "YAI demo" }),
});

const app = express();
// Router matches paths starting with basePath itself, so mount at root.
app.use(createCopilotEndpointExpress({ runtime, basePath: "/api/copilotkit" }));

const dist = path.join(HERE, "dist");
if (existsSync(dist)) {
  app.use(express.static(dist));
  app.use((_req, res) => res.sendFile(path.join(dist, "index.html")));
}

app.listen(PORT, "127.0.0.1", () => {
  console.log(`[copilotkit-demo] http://127.0.0.1:${PORT}`);
  console.log(`[copilotkit-demo] agent -> ${AGENT_URL}`);
});
