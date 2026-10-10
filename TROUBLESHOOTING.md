# Elefant Troubleshooting Guide

**Installing for the first time?** The failure modes specific to a fresh install
are at the end of each install guide:
[`docs/install/docker-compose.md`](docs/install/docker-compose.md) and
[`docs/install/kubernetes.md`](docs/install/kubernetes.md). This page is about a
stack that started once and then misbehaved.

## Memories Disappearing After Service Restarts

**Symptom:** Memories added via the MCP connector or web UI vanish after restarting the services.

**Root Cause:** The embedder recreates the memories collection when the embedding model's vector size changes. This can happen if:
- The embedding config is loaded differently on different startup attempts (API timeout → fallback to env vars)
- The embedding model is changed in settings
- The embedding provider switches between local and remote (e.g., local → OpenAI)

**Solution (current builds):** Memories are now **preserved** on vector size mismatch. The embedder will:
1. Detect the mismatch and log an error message
2. **NOT delete** the memories collection
3. Require explicit opt-in (`EMBEDDER_FORCE_COLLECTION_RECREATE=memories`) to destroy data

**To Recover Lost Memories (from before the fix):**
1. Check if backups exist:
   ```bash
   ls -la /data/memories/backups/default/
   ```
2. The auto-backup feature (enabled by default) creates daily snapshots
3. Use the `/api/admin/memories/restore` endpoint to restore from a specific backup timestamp
4. Or, access the Memory Manager UI → Admin → Backup & Restore to restore via web interface

**To Prevent This:**
- Keep the embedding model configuration stable (don't change `EMBEDDING_MODEL` env var or settings between restarts)
- If you must change the model, back up memories first via the web UI
- Verify the embedder logs show the expected vector size:
  ```bash
  docker compose logs embedder | grep "Embedding backend ready"
  ```

**Example Output (healthy):**
```
Embedding backend ready: provider=local model=multi-qa-distilbert-cos-v1 vector_size=768
```

If the vector size changes between restarts, check why the embedding config is different.

---

## Memory Backup & Restore

**Location:** `/data/memories/backups/{tenant_slug}/`

**Auto-Backup Configuration:**
- Enabled by default
- Interval: 24 hours
- Retention: 30 days
- Configure via Admin panel → Data → Backup & Restore → Auto-Backup Configuration

**Manual Backup** (requires an org-admin token):
```bash
curl -H "Authorization: Bearer <org-admin-token>" \
  "https://elefant.example.com/api/admin/memories/export?tenant_slug=default" \
  > memories-backup-manual.json
```

**Restore from Backup:**
Via web UI: Admin → Data → Backup & Restore → Select backup → Restore

Via API (org-admin token; the endpoint takes form fields, with `tenant_slug` as a query param):
```bash
curl -X POST -H "Authorization: Bearer <org-admin-token>" \
  -F backup_id=backup-20260505-120000 \
  -F mode=replace \
  "https://elefant.example.com/api/admin/memories/restore?tenant_slug=default"
```

---

## Common Issues

### Embedder keeps crashing with "Qdrant not ready"
- Ensure Qdrant container is running: `docker compose ps qdrant`
- Check Qdrant logs: `docker compose logs qdrant`
- Verify the Qdrant volume is mounted: `docker volume ls | grep qdrant_data`

### Wiki search returns no results
- Run reindex: `curl -X POST http://localhost:8001/api/reindex`
- Check embedder status: `curl http://localhost:8001/api/status`

### OAuth flow fails
- Restart the MCP connector in Claude.ai (disconnect and reconnect)
- Verify TLS certificate is valid: `curl -v https://elefant.example.com/mcp`
- Check nginx logs: `docker compose logs nginx`
- A `429` from `POST /oauth/register` means one address registered more than
  `OAUTH_REGISTER_LIMIT` clients (default 20) inside `OAUTH_REGISTER_WINDOW`
  seconds (default 3600). Wait for the `Retry-After` time, or raise the limit
  on the editor if many clients share one address.
- OAuth access tokens last `OAUTH_ACCESS_TOKEN_TTL_DAYS` (default 365). There
  is no refresh token, so when one expires the user reconnects the connector.
  Changing the setting only affects tokens issued afterwards.

### Locked out: nobody can log in to reset a password
Use the break-glass CLI inside the editor container. It sets the password on
a local account and logs out every session that account had open.
```bash
# Docker Compose
docker compose exec editor reset-password --list
docker compose exec editor reset-password alice@acme.com
# Kubernetes
kubectl -n elefant exec -it deploy/editor -- reset-password alice@acme.com
```
The login is what the user types on the login page (bare `alice` works for the
default organisation). Omit the password argument to be prompted for it, so it
stays out of your shell history. Accounts that sign in with OIDC or SAML have
no local password; fix those at the identity provider. If MFA is the problem
rather than the password, an org admin can reset MFA from the admin panel.

### Images not loading in wiki pages
- Use `get_wiki_page(path)` MCP tool to fetch full page with embedded images
- Images are included as base64 data URIs
- Check that image files exist: `ls -la /docs/assets/images/`

---

## Useful Commands

Check service health:
```bash
docker compose ps
docker compose logs -f --tail=50 embedder
```

Verify Qdrant collections:
```bash
curl http://localhost:6333/collections
```

Check memory backup status:
```bash
curl http://localhost:8080/api/admin/memories/backups?tenant_slug=default
```

View embedder status:
```bash
curl http://localhost:8001/api/status
```

---

## Getting Help

1. Check the logs: `docker compose logs -f <service>`
2. Verify configuration: Check `.env` file and docker-compose.yml
3. Run health checks: Each service exposes a `/health` endpoint
4. Check this guide for known issues above

Still stuck? Ask in [GitHub Discussions](https://github.com/Zennthic/elefant/discussions).
Say how you installed (Compose or Kubernetes), and give the version from the
bottom of **Settings** or from `/health`. Found a bug? [Open an
issue](https://github.com/Zennthic/elefant/issues/new/choose). Report a security
problem privately, as [SECURITY.md](SECURITY.md) describes, never in public.
