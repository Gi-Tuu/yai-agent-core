> English translation of [中文](demo-deployment.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# Public Deployment Playbook (Online Demo Environment)

**English** · [简体中文](demo-deployment.md)

> Last verified: 2026-09-10. Free-tier policies change quickly — re-check the source links at the end before you start.
> Goal: make four endpoints (e.g. `https://<your-domain>/health`) reachable from the public internet, and make the commit they report match the code you deployed.
>
> **Status (2026-11-05)**: the public demo instance was shut down and deleted on 2026-11-02 (see `ROADMAP.md` and
> `docs/adr/0002-runtime-auth-boundary.md`). Any future public deployment must explicitly configure
> `YAI_API_KEY` (the global master gate) and is no longer exposed in an "unauthenticated free relay" form. The Render free-tier steps below
> are the historical route from 2026-09 and can still serve as a dry run, but under the current security stance you must add the API Key configuration before deploying.

## 0. The four things a deployment must satisfy

| Gate | How this project meets it |
|---|---|
| A deployed, publicly callable API | Docker Compose running permanently on a VPS, with Caddy reverse-proxying 443 |
| `GET /health` returns status + a 40-character commit | Already implemented in the Battery; the commit is baked in at build time via `--build-arg YAI_GIT_COMMIT` |
| `/.well-known/yai-verification.json` on the same origin with the same commit | Already implemented in the Battery; reads the same environment variable as /health |
| The commit can be verified | `/health` and the verification file return the same 40-character commit, and it matches the repository mainline |
| Reproducible instructions | This playbook + the two compose commands in the README |

## 1. Free-tier options (verified against official pages in 2026-09)

| Option | Cost | Always on? | HTTPS and domain | Verdict |
|---|---|---|---|---|
| **Oracle Cloud Always Free** | Free forever: 2 AMD micro instances (1/8 OCPU, 1GB) + ARM Ampere A1 (up to 4 OCPU/24GB in total, 1-2 instances), 200GB block storage, about 10TB/month of egress | Always on, never sleeps | Bring your own domain (free DuckDNS is enough); Caddy obtains certificates automatically | **First choice for a real deployment** |
| Google Cloud e2-micro | 1 instance free forever (limited to 3 US regions, 1GB) | Always on | Same as above | Backup option; signup still requires a card |
| Render Free Web Service | Free | **Sleeps after 15 minutes without traffic; cold start about 30-90 seconds (highly variable)**; 750 instance hours/month | Comes with a `*.onrender.com` certificate | **Good for a temporary dry run**; demo visits may be interrupted by sleep |
| Fly.io | No free tier; shared-cpu-1x 256MB from about $3.14/month | Always on | Comes with a `*.fly.dev` certificate | Cheap alternative if you don't want to deal with Oracle |
| Koyeb | Free instances have been discontinued (only paid plans remain on the official site) | — | — | Excluded |
| Hugging Face Spaces | Creating a Docker/Gradio Space already requires a paid plan; free hardware sleeps | Sleeps | Built in | Excluded |
| Alibaba Cloud / Tencent Cloud student instances | Around ¥10/month | Always on | **Domains on 443 in mainland data centers require ICP filing (too slow for 09-19/10-04)**; overseas regions need no filing, but student pricing is usually limited to mainland regions | Not recommended for long-term demos |

**Recommended route**: today, use the **Render free tier to get a public HTTPS address in 30 minutes** and walk the whole flow; before the real demo, migrate to **Oracle Always Free + Caddy** for an always-on instance. Our service only forwards to DeepSeek — no model runs locally — so even a small AMD 1GB machine is enough.

## 2. Route A: quick dry run on the Render free tier (zero cost, about 30 minutes)

The repository root already ships `render.yaml` (a Render Blueprint): the platform name, port, health check, and non-sensitive variables are all declared, so you only need to connect the repository in the web UI and paste the key once:

1. Open https://render.com → sign in with your GitHub account and authorize access to `Gi-Tuu/yai-agent-core`.
2. In the console choose **New → Blueprint**, select the `yai-agent-core` repository, and Render reads `render.yaml`
   to generate a Free Docker Web Service (region Singapore, health check path `/health`).
3. The Blueprint wizard prompts for the Secret marked `sync: false`: `OPENAI_API_KEY` = your DeepSeek key; paste it.
   The other variables (`OPENAI_BASE_URL`, `LLM_MODEL`, `YAI_PROJECT_SLUG`, etc.) are already written in the yaml.
4. Click **Apply / Deploy** and the cloud starts building (overseas environments use the official PyPI, so no `INDEX_URL` is needed).
5. Two automation conventions (already handled in the code, no manual action required):
   - **Port**: Render injects `PORT` (default 10000), and the Dockerfile CMD is
     `--port ${PORT:-8000}`, so local/compose stays on 8000 while on Render it follows automatically;
   - **commit**: Render injects `RENDER_GIT_COMMIT` automatically (the full SHA of this deployment), and
     the resolution chain in `serve_example.py` — `YAI_GIT_COMMIT → RENDER_GIT_COMMIT → git HEAD → dev` —
     picks it up, so the commit returned by /health and by the verification endpoint naturally equals the deployed commit.
     Note that resolution accepts only a full 40-character lowercase hash — the default `dev` baked into the image is not a valid hash and is skipped
     (first-deploy measurement: without skipping it, the non-empty `dev` would shadow the real SHA injected by the platform).
6. When the deployment finishes you get `https://<your-service-name>.onrender.com` (the actual name wins); verify the four endpoints as described in section 4.
7. Risk: it sleeps after 15 minutes without traffic, and the first visit may hit a cold-start page of about 30-90 seconds (not our JSON).
   **Before the real demo, move to an always-on VPS.**

> If you skip the Blueprint and use New → Web Service manually: set Language to Docker, leave Dockerfile Path empty (repository root),
> choose the Free plan, then add the same variables under Environment — the result is identical.

## 3. Route B: Oracle Always Free + Docker + Caddy (real deployment)

### 3.1 Sign up and create the machine
1. Register at https://www.oracle.com/cloud/free/ (a credit/debit card is required for verification and is not charged; one account per person, real information).
2. The Home Region **cannot be changed once chosen**; Tokyo / Seoul / Singapore are recommended (close to the mainland, with working access to both PyPI and DeepSeek).
3. Create Instance:
   - Image: Ubuntu 24.04
   - Shape: prefer `VM.Standard.A1.Flex` (ARM; 1 OCPU/6GB is enough; if capacity is unavailable use `VM.Standard.E2.1.Micro` — an AMD 1GB machine also runs it)
   - Save the SSH private key (the page can generate and download one for you)
4. In the VCN Security List allow inbound: tcp 80, tcp 443; for tcp 22 it is advisable to allow only your own egress IP.

### 3.2 Install Docker (after SSH-ing in)
```bash
sudo apt update
sudo apt install -y docker.io docker-compose-v2 git
sudo usermod -aG docker ubuntu   # log in again to drop sudo
```

### 3.3 Pull the code, configure the environment, start the service
```bash
git clone https://github.com/Gi-Tuu/yai-agent-core.git
cd yai-agent-core
cp .env.example .env
nano .env          # fill in OPENAI_API_KEY and YAI_PROJECT_SLUG; keep YAI_GIT_COMMIT commented out
nano deploy/Caddyfile   # replace the domain with yours (see 3.4)

git rev-parse HEAD   # note this 40-character commit to verify that production matches the repository
YAI_GIT_COMMIT=$(git rev-parse HEAD) \
  docker compose -f docker-compose.yml -f docker-compose.deploy.yml up -d --build
```
Notes:
- The base compose file only binds the container port to `127.0.0.1:8001` (for local debugging), so public traffic can only come in through Caddy;
- The deploy overlay starts Caddy, which takes 80/443 and requests a Let's Encrypt certificate on the first request, renewing it automatically;
- ARM machines need no Dockerfile change; `python:3.13-slim` has an arm64 image;
- An overseas VPS can build against the official PyPI; if the network misbehaves, add `INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple` at build time.

### 3.4 Free domain (DuckDNS, no domain purchase needed)
1. Open https://www.duckdns.org/ and sign in with GitHub/Google;
2. Create a subdomain (e.g. `yai-agent-core`) and point its A record at the VPS public IP;
3. Change `deploy/Caddyfile` to:
   ```
   yai-agent-core.duckdns.org {
       reverse_proxy yai-agent-core:8000
   }
   ```
4. Run `docker compose -f docker-compose.yml -f docker-compose.deploy.yml restart caddy`; Caddy obtains the certificate
   automatically on the first HTTPS visit (`docker compose logs caddy` shows the issuance log).

### 3.5 Update the version (used repeatedly when iterating on releases)
```bash
git pull
YAI_GIT_COMMIT=$(git rev-parse HEAD) \
  docker compose -f docker-compose.yml -f docker-compose.deploy.yml up -d --build
```
After rebuilding, the /health commit automatically becomes the new version — **on every redeployment, the commit in the records must be updated too**.

### 3.5 GHCR prebuilt image (optional, when you don't want to build on the VPS)

Pushing a `v*` tag automatically builds and publishes the image to GHCR (`.github/workflows/docker.yml`, with provenance and SBOM):

```bash
docker pull ghcr.io/gi-tuu/yai-agent-core:latest
docker run --rm -p 8000:8000 --env-file .env ghcr.io/gi-tuu/yai-agent-core:latest
```

- The image installs the four optional dependency groups `[server,llm,mcp,openapi]`, and CMD matches the Dockerfile;
- It does not include host-only dependencies such as `[desktop]` / `[browser]` / `[windows]`;
- Pulling the public package may first require `docker login ghcr.io` (private-repository case); versions can be pinned with `:0.32` / `:0.32.0`.

## 4. Post-deployment verification checklist (run on your own machine)

```bash
BASE=https://<your-domain>
curl $BASE/health                              # expect {"status":"ok","commit":"<40-char>"}
curl $BASE/.well-known/yai-verification.json   # expect schemaVersion/slug/commit, with the same commit as above
curl $BASE/v1/tools                            # expect the tool list
curl -X POST $BASE/v1/agent/run -H "Content-Type: application/json" \
  -d '{"task":"list all my notes"}'            # expect strategy/events/final_text
docker compose ps                              # both caddy and app are healthy
```
All four passing = the public demo deployment meets the bar.

- [x] `YAI_DB_PATH`: enabled by default in the Render Blueprint (`data/yai.db` → `/app/data` inside the container; the directory is created at startup); locally or in a container without this variable it is still the in-process memory.
  **The Render free tier uses an ephemeral disk, so the database is lost after a redeploy**: history being cleared after a rebuild is expected behavior, and the external demo wording must say so honestly; on a VPS, put that path on a mounted volume.

## 5. Security and operations checklist

- `.env` exists only on the server; it never enters git or the image (`.dockerignore` already excludes it);
- The security group only opens 22/80/443, and the container port is not exposed to the public internet directly (it is bound back to loopback);
- Caddy certificate data lives in the `caddy-data` volume; it survives restarts and renews automatically;
- All model calls are outbound to the DeepSeek cloud; the VPS runs no model, so CPU/memory pressure is minimal;
- If an overseas VPS has trouble reaching `api.deepseek.com`, first check it with `curl https://api.deepseek.com`, and switch Oracle region if necessary (pending verification on the first real deployment).

## 6. Reference sources (official)

- Oracle Cloud Free Tier: https://www.oracle.com/cloud/free/
- Render Free limits (sleep / 750 hours): https://render.com/docs/free
- Render Docker deployment (PORT / build arguments): https://render.com/docs/docker
- Environment variables injected by Render (RENDER_GIT_COMMIT): https://render.com/docs/environment-variables
- Render Blueprint (render.yaml): https://render.com/docs/blueprint-spec
- Fly.io pricing (no free tier): https://fly.io/docs/about/pricing/
- Koyeb pricing (free instances discontinued): https://www.koyeb.com/pricing
- Hugging Face Spaces (Docker Space requires payment): https://huggingface.co/docs/hub/spaces-overview
- Caddy docs (automatic HTTPS): https://caddyserver.com/docs/
- DuckDNS free domains: https://www.duckdns.org/
