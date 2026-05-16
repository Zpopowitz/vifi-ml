// Decap CMS OAuth proxy — callback endpoint.

export default async function handler(req, res) {
  const code = req.query.code;
  if (!code) {
    res.status(400).send("Missing 'code' query param");
    return;
  }

  const clientId = process.env.OAUTH_GITHUB_CLIENT_ID;
  const clientSecret = process.env.OAUTH_GITHUB_CLIENT_SECRET;
  if (!clientId || !clientSecret) {
    res.status(500).send("OAuth app credentials are not configured in Vercel env vars");
    return;
  }

  let token;
  try {
    const tokenRes = await fetch("https://github.com/login/oauth/access_token", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({
        client_id: clientId,
        client_secret: clientSecret,
        code,
      }),
    });
    const data = await tokenRes.json();
    if (data.error || !data.access_token) {
      res.status(500).send(`GitHub OAuth exchange failed: ${data.error_description || data.error || "unknown"}`);
      return;
    }
    token = data.access_token;
  } catch (err) {
    res.status(500).send(`Token exchange request failed: ${err.message}`);
    return;
  }

  // Bidirectional handshake with Decap. The popup must send "authorizing:github"
  // FIRST and wait for Decap (in the opener window) to acknowledge before
  // sending the token. The previous version had this backwards, which is why
  // Decap never received the token and the editor never opened.
  const tokenJson = JSON.stringify({ token, provider: "github" });
  const successMsg = "authorization:github:success:" + tokenJson;

  const html = `<!doctype html>
<html><head><meta charset="utf-8"><title>OAuth complete</title></head>
<body>
<p>Authentication complete. You can close this window.</p>
<script>
(function () {
  var sent = false;
  function send(msg) {
    if (window.opener) {
      window.opener.postMessage(msg, "*");
    }
  }
  function sendSuccess() {
    if (sent) return;
    sent = true;
    send(${JSON.stringify(successMsg)});
    setTimeout(function () { try { window.close(); } catch (e) {} }, 500);
  }
  window.addEventListener("message", function (e) {
    if (typeof e.data === "string" && e.data.indexOf("authorizing:github") === 0) {
      sendSuccess();
    }
  });
  // Send handshake. Decap should reply, prompting sendSuccess().
  send("authorizing:github");
  // Fallback in case Decap doesn't reply for any reason.
  setTimeout(sendSuccess, 1500);
})();
</script>
</body>
</html>`;

  res.setHeader("Content-Type", "text/html; charset=utf-8");
  res.status(200).send(html);
}
