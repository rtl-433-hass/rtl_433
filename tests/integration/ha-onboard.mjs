// Seed Home Assistant onboarding via its REST API so the Playwright run can log
// in deterministically (UI onboarding is multi-step and flaky to drive blind).
//
// Flow (HA's documented onboarding endpoints):
//   1. POST /api/onboarding/users   -> creates the owner, returns an auth_code
//   2. POST /auth/token (grant_type=authorization_code) -> access token
//   3. POST /api/onboarding/core_config, .../analytics, .../integration to
//      finish onboarding so the UI lands on the normal dashboard/login.
//
// Prints JSON {username, password, accessToken} on stdout for the caller.
// Idempotent-ish: if onboarding is already done, it reports that and exits 0 so
// re-runs against a warm container still succeed (the caller logs in via UI).

const BASE = process.env.HA_BASE || "http://localhost:8123";
const USERNAME = process.env.HA_USER || "harness";
const PASSWORD = process.env.HA_PASS || "harness-password-123";
const NAME = "Demo User";
const CLIENT_ID = `${BASE}/`;

async function post(path, body, token) {
  const headers = { "Content-Type": "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
  const text = await res.text();
  let json;
  try {
    json = text ? JSON.parse(text) : {};
  } catch {
    json = { raw: text };
  }
  return { status: res.status, json };
}

async function main() {
  // Step 1: create the owner. If onboarding is already complete HA returns 403.
  const user = await post("/api/onboarding/users", {
    client_id: CLIENT_ID,
    name: NAME,
    username: USERNAME,
    password: PASSWORD,
    language: "en",
  });

  if (user.status === 403 || user.status === 401) {
    // Already onboarded — the UI login path still works with whatever creds the
    // operator set on the first run. Report and let the caller log in via UI.
    process.stdout.write(
      JSON.stringify({
        alreadyOnboarded: true,
        username: USERNAME,
        password: PASSWORD,
        accessToken: null,
      }),
    );
    return;
  }

  if (user.status !== 200 || !user.json.auth_code) {
    throw new Error(
      `onboarding/users failed: ${user.status} ${JSON.stringify(user.json)}`,
    );
  }

  // Step 2: exchange the auth_code for tokens (form-encoded, per HA auth API).
  const form = new URLSearchParams({
    client_id: CLIENT_ID,
    grant_type: "authorization_code",
    code: user.json.auth_code,
  });
  const tokRes = await fetch(`${BASE}/auth/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: form.toString(),
  });
  const tok = await tokRes.json();
  if (!tok.access_token) {
    throw new Error(`auth/token failed: ${tokRes.status} ${JSON.stringify(tok)}`);
  }
  const accessToken = tok.access_token;

  // Step 3: finish the remaining onboarding steps so the UI is fully usable.
  // Errors here are non-fatal (a step may already be done); log and continue.
  for (const [path, body] of [
    [
      "/api/onboarding/core_config",
      {}, // accept HA's detected defaults
    ],
    ["/api/onboarding/analytics", {}],
    ["/api/onboarding/integration", { client_id: CLIENT_ID, redirect_uri: CLIENT_ID }],
  ]) {
    try {
      await post(path, body, accessToken);
    } catch (e) {
      process.stderr.write(`onboarding step ${path} non-fatal: ${e.message}\n`);
    }
  }

  process.stdout.write(
    JSON.stringify({
      alreadyOnboarded: false,
      username: USERNAME,
      password: PASSWORD,
      accessToken,
    }),
  );
}

main().catch((e) => {
  process.stderr.write(`ha-onboard error: ${e.stack || e}\n`);
  process.exit(1);
});
