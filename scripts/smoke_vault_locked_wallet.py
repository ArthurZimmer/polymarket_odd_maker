"""Smoke for B-10: PUT /api/wallet with locked vault returns 401 (not 500).

Lock the vault in-process, then call put_wallet via FastAPI's TestClient.
Expect a clean 401 with descriptive detail.

Run: .venv/bin/python -m scripts.smoke_vault_locked_wallet
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from backend.crypto.vault import VaultState
from backend.main import app


def main() -> None:
    # First log in to obtain a JWT (vault must be set up; smoke assumes the
    # dev DB already has a known password).
    password = "test-password-456"
    with TestClient(app) as client:
        r = client.post("/api/auth/login", json={"password": password})
        if r.status_code != 200:
            print(f"Skipping — login failed ({r.status_code}). Setup vault with "
                  f"the test password first.")
            return
        token = r.json()["access_token"]

        # Lock the vault in-process (simulates the process forgetting the key
        # while the JWT is still valid in the client).
        VaultState.lock()
        print("[1] Vault locked, JWT still valid.")

        # Attempt PUT — must come back 401, not 500.
        r = client.put(
            "/api/wallet",
            headers={"Authorization": f"Bearer {token}"},
            json={"private_key": "a" * 64},
        )
        print(f"[2] PUT /api/wallet → {r.status_code}")
        print(f"    detail: {r.json().get('detail')!r}")

        # Could be 400 (invalid private key derives address first) or 401
        # depending on input — but for "a"*64 derive_address likely fails
        # at the secp256k1 layer. Use a *valid-looking* dummy hex.
        r = client.put(
            "/api/wallet",
            headers={"Authorization": f"Bearer {token}"},
            json={"private_key": "0x" + "1" * 64},
        )
        print(f"[3] PUT /api/wallet (valid-looking key) → {r.status_code}")
        print(f"    detail: {r.json().get('detail')!r}")
        assert r.status_code == 401, f"expected 401, got {r.status_code}"
        # The 401 may come from `require_auth` (which also checks vault state)
        # OR from our defense-in-depth try/except inside put_wallet. Either is
        # fine — both yield a clean 401 instead of a 500.
        detail = r.json().get("detail", "")
        assert "Vault" in detail or "vault" in detail or "locked" in detail.lower(), \
            f"expected vault-related detail, got {detail!r}"

        # Re-unlock so we leave the dev env clean
        ok = VaultState.unlock(password)
        print(f"[4] Vault re-unlocked: {ok}")

    print("\nDone — vault locked surfaces as 401.")


if __name__ == "__main__":
    main()
