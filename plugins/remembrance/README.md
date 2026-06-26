# Remembrance — offsite encrypted backups

Ships an encrypted backup of `user/` to the zero-knowledge **Remembrance vault**.
The app encrypts (with your **backup password**) *before* upload; the server stores
ciphertext only and never sees your data or the password.

## Setup
1. Set a **backup password** on Settings → Backup → Encryption (offsite reuses it).
2. Get a tenant ID + API key from the vault owner (minted via the server's `mint.py`).
3. Settings → Plugins → Remembrance: enter server URL, tenant ID, API key → **Save** → **Test**.

## Three ways to back up
- **Manual:** the "Back up now (offsite)" button on the settings page.
- **AI tool:** `remembrance_backup` — no comment → vault status; with a comment → a labeled backup (kept long-term).
- **Cron:** auto-backup at a configurable hour (default just after the local backup run).

## Restore
The backups list has a ↻ per row — enter your backup password, confirm, and Sapphire
restarts and swaps the data in (your current data is preserved as `user.old`).

## Privacy / safety
- API key stored **scrambled** in `~/.config/sapphire`, never inside a backup.
- Symlinks are skipped (no path leak); the blob is **sha256-verified** on download before decrypt.
- **Permadeath:** lose the backup password and the backups are unrecoverable. That's the cost of zero-knowledge.

Server contract: `INTEGRATION.md` in the Remembrance server repo.
