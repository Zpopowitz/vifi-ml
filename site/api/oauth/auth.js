// Decap CMS OAuth proxy — start endpoint.
//
// Decap's admin redirects the editor to this URL. We bounce them to
// GitHub's OAuth authorize page with the client_id from the OAuth app.
// GitHub then redirects back to /api/oauth/callback with a code.

export default function handler(req, res) {
  const clientId = process.env.OAUTH_GITHUB_CLIENT_ID;
  if (!clientId) {
    res.status(500).send("OAUTH_GITHUB_CLIENT_ID is not set in Vercel env vars");
    return;
  }

  const host = req.headers["x-forwarded-host"] || req.headers.host;
  const proto = req.headers["x-forwarded-proto"] || "https";
  const redirectUri = `${proto}://${host}/api/oauth/callback`;

  const params = new URLSearchParams({
    client_id: clientId,
    redirect_uri: redirectUri,
    scope: "repo,user",
  });

  res.writeHead(302, {
    Location: `https://github.com/login/oauth/authorize?${params}`,
  });
  res.end();
}
