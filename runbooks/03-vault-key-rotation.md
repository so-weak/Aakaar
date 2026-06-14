# 03 — Vault key rotation

Tenant credentials live as JSON files under `data/vault/<tenant_id>/...`
(one file per grant, mode 0600). With `AAKAAR_VAULT_KEY` set they are
Fernet-encrypted envelopes (`{"$aakaar_vault": "fernet.v1", "token": ...}`);
without it they are plaintext and the API logs a warning at startup.

`AAKAAR_VAULT_KEY` is a **comma-separated list** (parsed in
`aakaar/aakaar/core/config.py`):

- the **first** key encrypts every new write;
- the **remaining** keys are still accepted for decryption (MultiFernet),
  which is exactly what makes a zero-downtime rotation possible;
- plaintext (pre-encryption) entries stay readable under a keyed vault and
  are transparently re-encrypted the next time they are written.

Set `AAKAAR_VAULT_REQUIRE_ENCRYPTION=1` in every non-dev environment: with it,
a missing/empty key is a **startup failure** instead of a silent fall-back to
plaintext.

## Generate a key

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

An invalid key (wrong length / not urlsafe-base64) fails at startup with a
`VaultError`; the key material itself is never echoed in the error.

## Rotation procedure

Assume the current value is `AAKAAR_VAULT_KEY=K_old`.

1. **Backup first.** Snapshot `data/vault/` (see
   [01-sqlite-backup-restore](01-sqlite-backup-restore.md)). A rotation that
   loses both old and new keys loses every tenant credential — there is no
   recovery path other than tenants re-entering secrets.

2. **Introduce the new key at the front, keep the old one behind it:**

   ```bash
   # aakaar/.env (or your process manager's environment)
   AAKAAR_VAULT_KEY=K_new,K_old
   ```

   Restart the API. From this moment every write encrypts with `K_new`;
   everything written under `K_old` still decrypts.

3. **Re-encrypt existing entries.** Entries only pick up `K_new` when
   rewritten. Updating each grant through the UI/API works but is manual;
   for a full sweep run this one-off (API stopped, or during a quiet period —
   `put()` is atomic per file):

   ```bash
   cd ~/Codes/Aakaar/aakaar
   AAKAAR_VAULT_KEY=K_new,K_old .venv/bin/python - <<'EOF'
   import os
   from pathlib import Path
   from aakaar.vault.local import LocalVault

   keys = tuple(k.strip() for k in os.environ["AAKAAR_VAULT_KEY"].split(",") if k.strip())
   vault = LocalVault(Path("data"), keys=keys)
   root = Path("data/vault")
   count = 0
   for f in sorted(root.rglob("*.json")):
       tenant_id = f.relative_to(root).parts[0]
       ref = str(f.relative_to(root / tenant_id))[: -len(".json")]
       vault.put(tenant_id, ref, vault.fetch(tenant_id, ref))  # decrypt-with-any, encrypt-with-first
       count += 1
   print(f"re-encrypted {count} entries")
   EOF
   ```

4. **Verify nothing still needs `K_old`:** every entry file should now be a
   `fernet.v1` envelope written by `K_new`. Cheap check — restart with only
   the new key in a scratch shell and fetch each entry:

   ```bash
   AAKAAR_VAULT_KEY=K_new .venv/bin/python - <<'EOF'
   import os
   from pathlib import Path
   from aakaar.vault.local import LocalVault
   vault = LocalVault(Path("data"), keys=(os.environ["AAKAAR_VAULT_KEY"],))
   root = Path("data/vault")
   bad = []
   for f in sorted(root.rglob("*.json")):
       tenant_id = f.relative_to(root).parts[0]
       ref = str(f.relative_to(root / tenant_id))[: -len(".json")]
       try:
           vault.fetch(tenant_id, ref)
       except Exception as e:
           bad.append((tenant_id, ref, type(e).__name__))
   print("all entries decrypt with K_new" if not bad else bad)
   EOF
   ```

5. **Retire the old key:** set `AAKAAR_VAULT_KEY=K_new`, restart, and store
   `K_old` offline for as long as backups encrypted with it are retained —
   restoring an old backup needs the key that was active when it was taken
   (append it temporarily: `AAKAAR_VAULT_KEY=K_new,K_old`).

## First-time encryption of a plaintext vault

Same procedure from step 2 with a single key (`AAKAAR_VAULT_KEY=K_new`):
plaintext entries are readable as-is and the step-3 sweep converts them all
to ciphertext. Then set `AAKAAR_VAULT_REQUIRE_ENCRYPTION=1` so it can never
silently regress.

> The secret name `$aakaar_vault` is reserved (it marks the encrypted
> envelope); the vault rejects it as a plaintext secret name.

## If decryption errors appear after a rotation

Symptom: capability nodes fail with vault errors, log lines
`vault.fetch ...` followed by a `VaultError` naming the entry.

- The running process is probably missing one of the keys — confirm the live
  environment (`docker compose exec aakaar-api env | grep AAKAAR_VAULT`,
  remembering values are sensitive) lists *both* keys during the window.
- An entry restored from an old backup needs the key from that era appended
  to the list.
- Worst case (key truly lost): delete and re-create the affected grants —
  tenant admins re-enter the secret values; `PATCH /admin/grants/{id}`
  requires the complete secret set, so partial recovery is not possible by
  design.
