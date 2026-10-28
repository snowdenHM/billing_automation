# Frontend security review

Written 2026-10-28 against `release-1`. Review of the React/Vite SPA and
the parts of the Django backend that serve or protect it.

This is a code review, not a live penetration test. Nothing was executed
against a running production system. Each finding below was traced in
source; where a risk turned out to be already mitigated, that is stated
rather than left in to inflate the list.

Severity is judged by what an attacker gets, not by how the issue looks.

---

## The main risk chain

Two findings matter far more together than apart, and they are the whole
story of this review:

> **Access *and* refresh JWTs live in `localStorage`, and there is no
> Content-Security-Policy.** Any script execution in the app's origin
> reads both tokens. The refresh token makes that access persistent
> beyond the access token's lifetime, and nothing on the page constrains
> where a stolen token can be sent.

No live XSS was found. That is the only reason this is not critical
today. It also means the whole posture rests on no XSS ever appearing —
in the app, in a dependency, or in an ad-hoc change — which is a thin
thing to rest on for a product holding other companies' books.

---

## HIGH

### H1 — Access and refresh tokens in `localStorage`

    src/pages/auth/common/LoginForm.jsx:70-71
    src/utils/tokenRefresh.js:38,80-82
    src/utils/apiClient.js:17

`localStorage` is readable by any JavaScript on the origin. Storing the
**refresh** token there is the sharper half: an access token expires, a
refresh token lets the attacker mint new ones for as long as it is
valid. One XSS becomes durable, silent account takeover — and because a
user may belong to several organisations, that reaches every tenant
they can see.

A comment at `LoginForm.jsx:147` notes that "Remember me" was ignored
and tokens always went to `localStorage` regardless.

**Fix direction.** Move the refresh token to an `httpOnly`, `Secure`,
`SameSite=Lax` cookie so JavaScript cannot read it, and keep only the
short-lived access token in memory (not `localStorage`) with a silent
refresh on load. The backend already has `SESSION_COOKIE_SECURE`,
`CSRF_COOKIE_SECURE` and `SameSite=Lax` configured in production, so the
cookie infrastructure is there. This is the single highest-value change
on this list.

If that is too large for now, the interim step is H2.

### H2 — No Content-Security-Policy

`index.html` carries no CSP, and no CSP header is set anywhere in the
Django settings. The production settings do set HSTS, nosniff,
`X_FRAME_OPTIONS = DENY`, `SECURE_SSL_REDIRECT` and secure cookies —
CSP is the one missing from an otherwise solid set.

Two things follow:

1. Nothing contains an XSS if one appears. With H1, that is immediate
   token theft and exfiltration to any host.
2. The SPA is built by Vite and served as static files, so **Django's
   security headers do not apply to it**. Whatever serves the built
   `dist/` (nginx, the VPS, a CDN) needs its own
   `Content-Security-Policy`, `X-Frame-Options`, `Referrer-Policy` and
   `X-Content-Type-Options`. Setting them only in Django protects the
   API responses, not the app the user is looking at.

**Fix direction.** A CSP on the SPA host. The app loads Google Fonts and
iconify; a starting policy would be `default-src 'self'`, `connect-src
'self' https://api.billmunshi.com`, `img-src 'self' data: blob:`,
`style-src 'self' 'unsafe-inline' https://fonts.googleapis.com`,
`font-src https://fonts.gstatic.com`, `frame-ancestors 'none'`. Roll it
out in `Content-Security-Policy-Report-Only` first.

---

## MEDIUM

### M1 — Signed media URLs are bearer capabilities, not user-scoped

    apps/common/views.py:60-75, 92-121

`serve_bill_file` accepts a `TimestampSigner` token that signs **the
file path** and nothing else. It proves the URL was minted by us within
the TTL. It does not prove *who* is asking.

So a signed URL that leaks — in a referrer header, a screenshot, a
support ticket, browser history, a proxy log — grants **anyone** that
bill document until the token expires, with no login. Bills are another
company's financial records, so this crosses the tenant boundary.

The design reason is sound: `<img>` and `<iframe>` cannot send an
`Authorization` header, so a capability URL is the practical way to
embed a document. The TTL bounds the damage. But the token is wider than
it needs to be.

**Fix direction.** Sign `(path, user_id)` rather than `path`, and when
the request carries an authenticated session, require the two to match.
Shorten the TTL. Add `Referrer-Policy: no-referrer` on the SPA so the
token cannot escape through a referrer to any third-party asset.

### M2 — Dependency vulnerabilities, four of them reachable in the browser

`npm audit`: **17 total — 10 high, 6 moderate, 1 low.**

The number matters less than the split. Separated by whether the code
ships to a user's browser:

| Reaches the browser | Build-time only |
|---|---|
| `axios` (high) — direct dependency, used in `src/utils/apiClient.js` | `vite`, `postcss`, `source-map-js`, `picomatch`, `browserslist`, `baseline-browser-mapping`, `yaml`, `form-data`, `follow-redirects` |
| `react-router` / `react-router-dom` / `@remix-run/router` (moderate) | |
| `lodash`, `lodash-es`, `nanoid` (high) — transitive, **not imported anywhere in `src/`** | |

So the genuinely exposed set is **axios and react-router**. `lodash` and
`nanoid` are pulled in by tooling and never reach the bundle — worth
upgrading, not worth alarm.

The build-time ones are not harmless either: they run on developer
machines and in CI with repository access. They are a supply-chain
concern rather than a user-facing one.

**Fix direction.** `npm audit fix` for the non-breaking set, then handle
axios and react-router deliberately since both are direct dependencies
with real surface.

### M3 — Production CORS allowlist includes localhost, with credentials

    config/settings/production.py:48-53, 62

The production default for `CORS_ALLOWED_ORIGINS` includes
`http://localhost:3000` and `http://127.0.0.1:3000`, and
`CORS_ALLOW_CREDENTIALS = True`.

Anything a victim runs on their own `localhost:3000` — a scaffolded dev
server, a malicious npm package's dev harness, a local tool — can make
credentialed cross-origin requests to the production API with the
victim's browser. Exploitability is low because the attacker must
already have something on the victim's loopback, but there is no reason
for production to allow it at all.

**Fix direction.** Drop the localhost entries from the production
default. They belong in `local.py`, which already has its own list.

### M4 — Upload validation is extension-only

    apps/common/serializers.py:29-42

`ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}`, checked by
filename. The file's actual content is never inspected.

This is less severe than it first looks, because `serve_bill_file` hands
off to `django_serve`, which derives `Content-Type` from the same
extension — so an HTML payload named `.jpg` is served as `image/jpeg`
and modern browsers will not execute it. Production also sets
`SECURE_CONTENT_TYPE_NOSNIFF = True`, and `SecurityMiddleware` is in the
chain, so the `nosniff` header is present on these responses.

What remains is that a malformed or hostile file reaches the OCR
pipeline, the worker, and whatever library opens it. That is a parser
attack surface, not a browser one.

**Fix direction.** Sniff magic bytes on upload and reject a mismatch
between content and extension. Worth adding ClamAV at the same time —
these files are opened by the client's own staff.

---

## LOW / hygiene

### L1 — PII in `localStorage`

`user` is stored alongside the tokens. Same XSS exposure as H1, lower
value. It also survives logout on any path that clears tokens without
clearing `user` — `SelectOrganization.jsx:259-260` removes the two
tokens and not the user object.

### L2 — A dead component with `dangerouslySetInnerHTML`

`src/components/ui/Accordion.jsx:48` renders `item.content` as raw HTML.
**Nothing imports it.** It is harmless today and a loaded gun tomorrow —
the next person to need an accordion will find it, wire it to API
content, and ship an XSS. Delete it.

### L3 — Unnecessary `dangerouslySetInnerHTML`

`src/pages/auth/register.jsx:106` renders a hardcoded literal array
through `dangerouslySetInnerHTML`, purely so one string can contain
`&amp;`. Write `&` in JSX and remove the sink. No risk today; it is one
careless edit away from being one.

### L4 — Unvalidated redirect to a backend-supplied URL

`src/pages/zoho/config/credentials.jsx:179` does
`window.location.href = response.authorization_url`. The value comes
from our own API, so this is only reachable if the backend is already
compromised or that field is ever influenced by user input. Cheap to
harden: assert the URL starts with `https://accounts.zoho.`.

### L5 — No `Referrer-Policy`

Not set on the SPA host. Combined with M1, a signed media token can
leave in a `Referer` header to any third-party asset the page loads.

---

## Checked and found sound

Listing these so the absence of a finding is not mistaken for an absence
of review:

- **Production security headers.** HSTS (1 year, `includeSubDomains`,
  `preload`), `SECURE_CONTENT_TYPE_NOSNIFF`, `SECURE_SSL_REDIRECT`,
  `X_FRAME_OPTIONS = DENY`, `SESSION_COOKIE_SECURE`,
  `CSRF_COOKIE_SECURE`, `SameSite=Lax`. A good set; CSP is the gap (H2).
- **No secrets in `console.*`.** The three `console.error` calls log
  error objects, not credentials.
- **No source maps in the production build.** `dist/` contains no
  `.map` files.
- **Upload limits are enforced server-side**, not just in the browser:
  25 MB per file, 60 MB per request, 15 files, with
  `DATA_UPLOAD_MAX_NUMBER_FILES` set above it as a backstop.
- **Path traversal** on media is handled by `django_serve`, in addition
  to the signature check.
- **Auth throttling exists** — a login was rate-limited to HTTP 429
  during earlier work in this session.
- **Tenant scoping.** Every org-scoped view resolves through
  `get_organization_from_request`, and `@requires(...)` now guards 39
  endpoints after the authorization work earlier in this session.
- **No `eval`, no `new Function`.** The only `innerHTML` is
  `ReCaptcha.jsx:124` clearing a container with `""`.

---

## Order of work

1. **H2 — CSP on the SPA host.** Fastest meaningful reduction in risk,
   no application changes, and it blunts H1 while H1 is being done
   properly. Ship report-only first.
2. **H1 — refresh token out of `localStorage`.** The real fix. Touches
   login, refresh and logout, so it needs its own change.
3. **M2 — axios and react-router upgrades.** The only two vulnerable
   packages that reach a browser.
4. **M3 — drop localhost from the production CORS list.** One line.
5. **M1 — bind the media signature to a user**, shorten the TTL, add
   `Referrer-Policy`.
6. **M4 — magic-byte check on upload**, and ClamAV alongside it.
7. **L1–L5** — hygiene, do them together.

---

## Not covered

- No live testing against a running instance: no fuzzing, no
  authenticated session replay, no attempt at IDOR across real tenants.
  The tenant-scoping conclusion comes from reading the code path, not
  from trying to break it.
- The Tally TCP connector's own authentication surface (API-key auth on
  15 deliberately role-ungated endpoints) was reviewed earlier in this
  session but not re-examined here.
- Mobile app, if it shares any of these tokens.
- The VPS / nginx configuration, which is where H2, M1 and L5 actually
  get fixed and which I cannot see from the repository.

---

## Fix log — 2026-10-29

Everything below was implemented and verified. `manage.py check` 0,
`makemigrations --check` 0, 85 backend tests OK, `vite build` 0.

| | Finding | Status |
|---|---|---|
| H1 | Refresh token in `localStorage` | **Fixed** |
| H2 | No CSP | **Fixed** (meta; header still needed for `frame-ancestors`) |
| M1 | Media token not user-bound | **Fixed** |
| M2 | Dependency vulnerabilities | **17 → 2**, both deferred with reason |
| M3 | localhost in production CORS | **Fixed** |
| M4 | Extension-only upload validation | **Fixed** |
| L1 | Incomplete sign-out | **Fixed** |
| L2 | Dead component with an HTML sink | **Deleted** |
| L3 | Unnecessary HTML sink | **Fixed** |
| L4 | Unvalidated OAuth redirect | **Fixed** |
| L5 | No `Referrer-Policy` | **Fixed** |
| — | **Two new findings** (below) | **Fixed** |

### H1 — refresh token out of reach of JavaScript

The refresh token now travels in an httpOnly, `Secure`, `SameSite=Lax`
cookie scoped to `/api/v1/auth/refresh/`. The SPA no longer writes it
anywhere, no longer rehydrates it into Redux, and deletes any copy the
previous bundle left behind on the first refresh.

Backward compatible on purpose: the server still accepts the token in
the request body and still returns it there (`_REFRESH_IN_BODY = True`
in `apps/users/views/auth.py`). A browser running the old bundle keeps
working through the deploy. **Flip that constant to `False` once every
session has rolled over** — only then does the token stop being readable
at all.

Verified end to end: login sets the cookie → refresh succeeds with the
cookie alone and no body → logout clears it → refresh afterwards is
rejected.

### NEW-1 — there was no logout endpoint

Discovered while doing H1, and it would have turned H1 into a
regression. Sign-out was entirely client-side: clear `localStorage` and
navigate away. With the refresh token in an httpOnly cookie, the browser
*cannot* delete it — so signing out would have cleared everything
visible and left a working refresh token in place.

Added `POST /api/v1/auth/logout/`. It blacklists the refresh token
(`BLACKLIST_AFTER_ROTATION` and the blacklist app were already
configured and unused for this) and clears the cookie. `AllowAny`,
because a user whose access token has already expired must still be able
to sign out; presenting the refresh cookie is the only authorisation the
operation needs.

The frontend calls it from all three sign-out paths, fire-and-forget: a
user who clicked "sign out" ends up signed out locally even if the
network is down.

### NEW-2 — the JWT signing key could be too short

`SIMPLE_JWT["SIGNING_KEY"]` is `SECRET_KEY`, whose development default
is `django-insecure-change-me`. PyJWT emitted
`InsecureKeyLengthWarning: The HMAC key is 26 bytes long, which is below
the minimum recommended length of 32 bytes for SHA256` on every token
operation. A short HMAC key is a brute-forceable one, and forging a
token means forging any user.

`config/settings/production.py` now raises `ImproperlyConfigured` at
startup if `SECRET_KEY` is under 50 bytes or still carries the
`django-insecure` prefix. Verified both ways: the weak key is rejected,
a `token_urlsafe(64)` key boots.

**Check the production `SECRET_KEY` before deploying** — if it is short,
the app will now refuse to start, which is the intended behaviour but
should not be a surprise at deploy time.

### H2 — CSP, and why it is build-only

Injected by a Vite plugin with `apply: "build"` rather than written into
`index.html`. An unconditional meta tag also applies to the dev server,
whose HMR opens a `ws://` connection that `connect-src` would have to
name — breaking local development for everyone to protect a policy that
only matters in production.

The inline Google Consent Mode block moved to
`public/consent-default.js`, so `script-src` is `'self'` with no
`'unsafe-inline'` and no hash that would silently break the page the
next time that script is edited. The built page now contains **zero**
executable inline scripts.

`style-src` keeps `'unsafe-inline'`. That is unavoidable — React renders
`style=""` attributes for any component taking a style prop. Inline
style is a far weaker vector than inline script, which is fully blocked.

**Still required at the web server.** `frame-ancestors` is ignored in a
meta tag, so clickjacking protection needs a real header. For nginx:

```nginx
add_header Content-Security-Policy "default-src 'self'; script-src 'self' https://www.google.com https://www.gstatic.com; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://unpkg.com; font-src 'self' data: https://fonts.gstatic.com; img-src 'self' data: blob: https://api.iconify.design https://billmunshi.com https://api.billmunshi.com; connect-src 'self' https://billmunshi.com https://api.billmunshi.com; frame-src 'self' blob: https://www.google.com https://billmunshi.com https://api.billmunshi.com; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'; upgrade-insecure-requests" always;
add_header X-Frame-Options "DENY" always;
add_header X-Content-Type-Options "nosniff" always;
add_header Referrer-Policy "strict-origin-when-cross-origin" always;
```

### M1 — the media token now names its user

`generate_signed_bill_file_url` signs `(path, user_id)`; TTL cut from 1
hour to 15 minutes; `Referrer-Policy: no-referrer` on the response so
the `?token=` cannot escape through a `Referer` header.

A different signed-in user presenting a leaked URL is refused. An
anonymous request is still accepted, because the browser fetching an
`<img>`/`<iframe>` may carry no session — that is the case the scheme
exists for. Tokens minted without a request (background jobs) stay
path-only and keep working.

Verified: right user accepted, different user refused, wrong path
refused, anonymous accepted, legacy path-only token still accepted.

### M2 — 17 vulnerabilities down to 2, and why those 2 stay

`npm audit fix` resolved 15, including **axios**, the only vulnerable
package that both shipped to the browser and had a non-breaking fix.

The remaining two are `react-router` / `react-router-dom`, both
moderate. The only fix is v6 → v7, a **semver-major** upgrade that
changes the routing API across every page. Doing that days before a
client deploy, without a full regression pass, trades a theoretical risk
for a real one.

Both CVEs were checked against this codebase rather than taken at face
value:

- *Open redirect via backslash in `<Link>` / `useNavigate`* — **not
  reachable**. Every `navigate()` call builds its path from internal
  constants (the `DETAIL_ROUTE` maps, `baseRoute`, a bill UUID from our
  own API). There is no `?next=`, `returnUrl` or `redirect_to` handling
  anywhere in `src/`.
- *Arbitrary constructor injection in `deserializeErrors()` during SSR
  hydration* — **not applicable**. This is a pure SPA with no SSR.

Plan the v7 upgrade as its own piece of work with time to test it.

### M4 — upload content is now checked, not just its name

`validate_file_extension` reads the first bytes and matches them against
the signature for the claimed extension (`%PDF-`, PNG's 8-byte header,
JPEG's `FF D8 FF`). The stream position is saved and restored, because a
consumed stream writes a zero-byte file.

Verified across 7 cases: real PDF/PNG/JPEG accepted; HTML-as-`.pdf`,
SVG-as-`.jpg`, an empty file and an `.exe` all rejected; and the file
contents intact after validation in every case.

This closes the parser-facing risk. Browser-facing exposure was already
covered by `nosniff` and the extension-derived `Content-Type`.

### L1 — one definition of what signing out clears

`src/utils/authStorage.js` now owns the list. The two Redux reducers
cleared four keys; the button on the organisation picker cleared only
the two tokens and left the cached `user` object and the selected
organisation behind. All three now call `clearAuthStorage()`, which also
removes the per-organisation `bm_adj_autopick_done_*` keys — they point
at which client was being worked on.

---

## Still outstanding

1. **Set the web-server headers** (H2 block above). `frame-ancestors`
   and the header-level CSP cannot be done from the repository.
2. **Check the production `SECRET_KEY`** before deploying — a short one
   will now stop the app from starting.
3. **Flip `_REFRESH_IN_BODY` to `False`** once sessions have rolled over.
4. **Plan the react-router v7 upgrade** as its own change.
5. **ClamAV** on uploads, as noted under M4 — these files are opened by
   the client's own staff.
