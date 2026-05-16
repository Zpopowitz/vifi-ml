# vifi.health — marketing site

Single-page static site for [ViFi](https://github.com/Zpopowitz/vifi-ml). Built with Astro + Tailwind. Hosted on Vercel. Deployed at **vifi.health**.

The engineering project lives in the sibling repo [`vifi-ml`](https://github.com/Zpopowitz/vifi-ml). This repo is marketing-only.

---

## Local development

```bash
npm install
npm run dev          # http://localhost:4321
```

```bash
npm run build        # static output → dist/
npm run preview      # serve dist/ locally
```

Node 22+, npm 10+.

---

## Deploy

Pushed to `main` → Vercel auto-deploys to production. There is no separate staging environment for now (preview deploys cover it).

### First-time Vercel + Cloudflare DNS setup

1. **Vercel**: Add new project → import this repo → framework preset: **Astro** (auto-detected) → Deploy. Default settings are correct.
2. **Vercel domain**: Project → Settings → Domains → Add `vifi.health` and `www.vifi.health`.
3. **Cloudflare DNS**: For each domain Vercel asks for, copy the `A` / `CNAME` record into Cloudflare DNS:
   - Apex `vifi.health` → `A` record to `76.76.21.21` (Vercel's anycast IP — confirm exact value Vercel shows)
   - `www.vifi.health` → `CNAME` to `cname.vercel-dns.com`
4. **Cloudflare proxy**: Set the proxy status to **DNS only** (gray cloud) — Vercel handles its own TLS, double-proxying breaks redirects.
5. **TLS**: Wait ~1 minute, Vercel provisions a Let's Encrypt cert. Confirm `https://vifi.health` resolves and the padlock is clean.

---

## Editing copy

### Visual editor (recommended)

Open **[vifi.health/admin](https://vifi.health/admin)** in your browser, log in with GitHub, edit the form, hit **Publish** → it commits back to this repo on `main`, Vercel rebuilds in ~30 seconds.

The editor is [Decap CMS](https://decapcms.org) configured against `src/content/landing/main.json`. Every visible piece of text on the landing page is exposed as a form field there.

### Editing the JSON directly

If you'd rather edit raw JSON: `src/content/landing/main.json` is the single source of truth. Astro Content Collections enforces the schema (`src/content/config.ts`) at build time — `npm run build` will fail loudly if you break it.

### Regulatory framing

Regulatory framing is intentional throughout. Before changing anything in the **What we're building**, **For hospitals**, or **Footer** sections, read [the FDA's guidance on labeling for unapproved devices](https://www.fda.gov/medical-devices/overview-device-regulation/device-labeling). The current copy is conservative on purpose.

Phrases that are safe today:

- "Pre-clearance research prototype, not a medical device"
- "Building an FDA 510(k)-track contactless monitor"
- "Wellness-grade pilot, parallel to existing standard-of-care monitoring"

Phrases to **avoid**:

- "FDA-approved" / "FDA-cleared" (false until it actually is)
- "Monitors patient vitals" in present tense (implies clinical use)
- Any patient outcome claim (mortality, length of stay, etc.) without a published study

---

## CMS / OAuth setup (one-time)

The `/admin` editor uses Decap CMS with a custom GitHub OAuth proxy hosted as Vercel serverless functions (`api/oauth/auth.js`, `api/oauth/callback.js`). You only do this setup once.

1. **Create a GitHub OAuth app**
   - Go to [github.com/settings/developers](https://github.com/settings/developers) → **OAuth Apps** → **New OAuth App**
   - Application name: `ViFi CMS`
   - Homepage URL: `https://vifi.health`
   - Authorization callback URL: `https://vifi.health/api/oauth/callback`
   - Click **Register application**
   - On the next page: **copy the Client ID**, then click **Generate a new client secret** and **copy the secret immediately** (it's only shown once)

2. **Add the credentials to Vercel**
   - Vercel project → **Settings** → **Environment Variables**
   - Add `OAUTH_GITHUB_CLIENT_ID` (paste Client ID), apply to all environments
   - Add `OAUTH_GITHUB_CLIENT_SECRET` (paste the secret), apply to all environments
   - Click **Save**, then **Deployments** → redeploy the latest production deployment so the new env vars take effect

3. **Test it**
   - Open `https://vifi.health/admin`
   - Click **Login with GitHub**
   - Authorize the OAuth app
   - You should land in the editor with the landing page form populated

Without these env vars, `/admin` loads but login will fail with a 500.

---

## Branch naming

Trunk-based off `main`, same convention as `vifi-ml`:

| Prefix | Use for |
|---|---|
| `feat/` | New sections, components, integrations |
| `fix/`  | Bugs, broken links, copy errors |
| `chore/`| Refactors, deps, tooling |
| `docs/` | This README, CONTRIBUTING |
| `exp/`  | Design experiments / spikes |

Squash-merge to `main`, delete the branch.

---

## License

Site source: MIT. Brand mark and copy © 2026 Zach Popowitz.
