# Mini-program HTTPS gateway release

The deployed browse-only release exposes only GET prompt list/search/detail/similar, categories and models under `https://aigc-prompt.com/mini-api/api/...`. It does not deploy the backend, run migrations, expose login/writes, or change other sites. `/mini-api/api/auth/wechat` returns 404 until the pending auth upgrade described below is deployed with the backend.

Files: `http.conf` is the method/path allowlist; `server.conf` proxies accepted requests to existing `api:8000`; `release.py` performs plan/apply/rollback. Tests are `tests/test_mini_api_gateway.py`.

The automated backend deploy restores host nginx configuration from backup (`deploy/update-backend.sh`), so this isolated configuration release uses a manual gateway-only path. No CI, push, app rebuild, database action or Compose restart is needed.

## Review and artifact

1. Review only this directory and its test; do not include unrelated pending backend/auth/docs changes.
2. Run `python3 -m unittest discover -s tests -p test_mini_api_gateway.py`, the repository's required commit checks, and review the staged diff. Commit only this directory and the test. Capture the resulting full Git SHA.
3. Export exactly that committed directory with `git archive <SHA> deploy/mini-api-gateway`, transfer the immutable archive, and verify its SHA256 remotely before extracting. Never upload the dirty working tree. Record archive SHA and source Git SHA in the release evidence.
4. Read the live `/opt/aigc-prompt/backend/deploy/nginx.conf` and its SHA256. Locally run `release.py plan <live-copy> <candidate>`; review the diff. It must contain only one top-level `map` and one location block inside the existing `aigc-prompt.com` server. Other domains, listen/TLS settings and existing locations remain byte-identical.

## Apply after approval

Run the exported artifact as root with `python3 release.py apply --expected-sha256 <reviewed-live-hash> --git-sha <committed-sha>`.

The script acquires the existing shared `/opt/aigc-prompt/.release-deploy.lock` non-blocking (if occupied, stop and retry later), rechecks the live hash, and saves original/candidate/manifest under a private 0700 `releases/mini-api-*` directory with 0600 files. It writes **in place**, preserving the bind-mounted file's inode, ownership and mode. It runs `docker exec aigc-prompt-gateway-1 nginx -t` before reload. Any failure restores and tests/reloads the original configuration. App/frontend containers and data volumes are untouched.

Verify the container's loaded config and real HTTPS first/second pages, content type/count, categories/models/search/detail; `admin`, analytics, writes and auth through the mini prefix must return 404. Verify the existing main website and admin routes remain reachable. Switch mini-program develop config to HTTPS and recompile, then validate the actual AppID's request-domain settings and phone preview. A working HTTPS endpoint alone does not configure WeChat account domain permissions.

## Rollback

Use the **same committed artifact**: `python3 release.py rollback /opt/aigc-prompt/releases/mini-api-<recorded-id>`.

Rollback shares the release lock, verifies backup hashes and requires current live config to match the saved candidate. It refuses to overwrite intervening deployment changes. It restores the original in place, tests nginx, then reloads. If rollback application fails, it restores the pre-rollback candidate rather than leaving a partial file.

The source artifact and its SHA must be retained with the private backups. Do not expose original nginx files, TLS material, environment files or credentials in public artifacts/logs.


## Bind mount verification

The release now verifies the container's mounted nginx file hash against the host before any write, after write and after reload. Historical atomic replacements can leave the container pinned to a different inode even when the host pathname matches; in that case a reload-only release fails before touching the file. A separately approved gateway-only restart may be required to refresh that binding. The 2026-10-20 deployment encountered this historical state, restarted only the unchanged gateway image, and verified the resulting container/host hashes and public HTTPS API. No API/frontend restart or database action occurred.

## Pending WeChat/auth upgrade (local draft)

The current draft extends the prior GET-only release with exactly these frontend-used routes:

- `POST /mini-api/api/auth/wechat`
- `GET /mini-api/api/auth/me`
- `GET /mini-api/api/users/me/collects` (the actual frontend endpoint; `/api/prompts/me/collections` is deliberately denied)
- `POST` and `DELETE /mini-api/api/prompts/<canonical UUID>/like`
- `POST` and `DELETE /mini-api/api/prompts/<canonical UUID>/collect`

Email/Google login, uploads/submissions, arbitrary `me` paths, admin/crawl/analytics and unknown methods remain denied. Query parameters do not widen the path allowlist; FastAPI still validates parameters and JWT/user ownership.

`release.py merge/plan` now supports replacement of **only the exact previously reviewed browse-only fragments**, identified by both fragment SHA256 hashes and their original scopes/positions. It removes the known blocks, checks the remaining config contains no other mini-api declarations, and constructs the new blocks. Unknown/customized/repeated/misplaced markers are rejected for manual review; applying the same auth release twice is rejected. Existing original/candidate backups, hash concurrency checks, shared release lock, mounted-file hash checks, in-place writes, nginx tests, timeout and rollback behavior remain unchanged. Rollback restores the complete actual pre-upgrade file, including the browse-only blocks.

Validation before release:

```sh
python3 -m unittest discover -s tests -p test_mini_api_gateway.py
uv run ruff check deploy/mini-api-gateway/release.py tests/test_mini_api_gateway.py
python3 deploy/mini-api-gateway/release.py plan <private-live-config-copy> <private-candidate>
```

Review that the candidate differs only inside the two existing marked blocks. Backend WeChat login, its migration and credentials must be deployed and validated separately by the main release owner before treating login as available. This directory does not provision credentials or execute migrations.

### Trusted proxy source and login limits

The current nginx fragment sends `$proxy_add_x_forwarded_for`; the backend WeChat limiter consumes `request.client.host`. The checked-in Uvicorn Docker command has no explicit `--forwarded-allow-ips`, so do not assume the container gateway's forwarded client address is trusted. Without a correct policy all clients can share a proxy IP's login quota. Never use `--forwarded-allow-ips '*'` and never directly parse arbitrary request `X-Forwarded-For` in application code.

Before auth deployment, identify the exact gateway peer address(es) seen by Uvicorn and the exact upstream reverse-proxy address(es) in this deployment. Configure Uvicorn to trust only those actual proxy IPs; where an outer edge exists, it must replace/normalize untrusted incoming XFF using its socket peer, and each trusted hop must append its verified peer. Validate a normal request and a forged-XFF request yield the same true client identity. If only the gateway IP is trusted while an outer edge remains the rightmost untrusted hop, users will still share that edge's IP quota. The precise IP list and edge behavior must be verified live by the release owner; none are guessed or hardcoded by this local draft. Domain allowlisting alone does not establish trusted proxy provenance.
