# Test Plan: UsersModule Registration

Verify that `UsersModule` is correctly registered in the NestJS application and that user-related functionality remains intact.

## Prerequisites

- Node.js and npm installed
- Dependencies installed: `npm install`
- Environment variables configured (`.env` or `.env.local`) if the app requires DB/API keys for integration tests

---

## 1. Module wiring (static)

**Goal:** Confirm `UsersModule` is imported in the root module.

```bash
grep -n "UsersModule" src/app.module.ts
```

**Expected:** `UsersModule` appears in the `imports` array of `@Module({ ... })`.

**Also check:**

```bash
grep -rn "UsersModule" src --include="*.module.ts"
```

**Expected:** `users.module.ts` defines and exports `UsersModule`; no duplicate conflicting registrations unless intentionally feature-scoped.

---

## 2. Build / compile

**Goal:** TypeScript and Nest DI resolve `UsersService` and related providers.

```bash
npm run build
```

**Expected:** Exit code `0`; no errors such as "Nest can't resolve dependencies of UsersService".

---

## 3. Unit tests (UsersModule / UsersService)

**Goal:** Module and service tests pass after registration change.

```bash
npm test -- --testPathPattern=users
```

**Expected:** All matching tests pass.

If no dedicated users tests exist:

```bash
npm test
```

**Expected:** Full unit suite passes; no regressions from module graph changes.

---

## 4. Application bootstrap (smoke)

**Goal:** App starts with `UsersModule` loaded.

```bash
npm run start:dev
```

**Expected:** Log shows Nest application successfully started; no bootstrap errors mentioning `UsersModule` or `UsersService`.

Stop the process after confirming startup (Ctrl+C).

Alternative:

```bash
npm run build && npm run start:prod
```

**Expected:** Production build and start succeed.

---

## 5. Optional: E2E (if Playwright/Jest e2e configured)

```bash
npm run test:e2e
```

**Expected:** Auth/user flows pass or skip per project config; no failures from missing `UsersModule`.

---

## 6. Manual API check (server running)

With a valid JWT from login (adjust path if global prefix e.g. `/api`):

```bash
curl -s -H "Authorization: Bearer <token>" http://localhost:3000/users/me
```

**Expected:** `200` and user profile JSON, or documented response for your route layout.

---

## 7. Lint (if configured)

```bash
npm run lint
```

**Expected:** No new lint errors in `users.module.ts`, `users.service.ts`, or `app.module.ts`.

---

## Sign-off checklist

| Check | Pass? |
|-------|-------|
| `UsersModule` in `AppModule` imports | ☐ |
| `UsersService` injectable where needed | ☐ |
| Unit tests green | ☐ |
| Production build green | ☐ |
| App bootstrap without DI errors | ☐ |
| No duplicate/conflicting `UsersModule` unless intentional | ☐ |

**Notes:** Document required DB/env before running tests against a real database. Adjust port and route prefix to match your deployment.
