"""
export_gsccca_cookies.py — Run ONCE while Chrome is closed (or with Profile 2 closed).
Reads Chrome's encrypted cookies for gsccca.org, decrypts using macOS Keychain,
and saves them as JSON for ga_ucc_scraper.py to inject into Playwright.

macOS Chrome encryption:
  - Key: PBKDF2(password=<Keychain "Chrome Safe Storage">, salt='saltysalt',
                 iterations=1003, keylen=16)
  - Cipher: AES-128-CBC, IV = 0x20 * 16 (16 spaces)
  - Prefix: 'v10' (3 bytes) stripped before decryption
  - Padding: PKCS7

USAGE:
  1. Make sure you're logged into https://www.gsccca.org in Chrome Profile 2
  2. Quit Chrome (or just profile 2 if multi-profile)
  3. python3 scrapers/export_gsccca_cookies.py
"""

import json, os, sys, sqlite3, shutil, tempfile, hashlib, subprocess
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

OUT_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "leads", "gsccca_cookies.json")

CHROME_PATHS = [
    Path.home() / "Library/Application Support/Google/Chrome/Profile 2/Cookies",
    Path.home() / "Library/Application Support/Google/Chrome/Profile 7/Cookies",
    Path.home() / "Library/Application Support/Google/Chrome/Profile 8/Cookies",
    Path.home() / "Library/Application Support/Google/Chrome/Default/Cookies",
    Path.home() / "Library/Application Support/Google/Chrome/Profile 1/Cookies",
    Path.home() / "Library/Application Support/BraveSoftware/Brave-Browser/Default/Cookies",
]


def get_chrome_aes_key() -> bytes:
    """
    Get Chrome's AES decryption key from macOS Keychain.
    macOS may show an allow/deny dialog — click 'Always Allow'.
    """
    for service in ["Chrome Safe Storage", "Chromium Safe Storage"]:
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-wa", service],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0 and result.stdout.strip():
                password = result.stdout.strip().encode("utf-8")
                # PBKDF2-SHA1: same params Chrome uses on macOS
                key = hashlib.pbkdf2_hmac(
                    "sha1", password,
                    salt=b"saltysalt",
                    iterations=1003,
                    dklen=16
                )
                print(f"✓ Got AES key from Keychain (service: {service})")
                return key
        except Exception as e:
            print(f"  Keychain error for '{service}': {e}")
    return None


def decrypt_chrome_cookie(encrypted: bytes, key: bytes) -> str:
    """Decrypt a Chrome v10 cookie value (macOS AES-128-CBC)."""
    if not encrypted:
        return ""
    try:
        # Strip v10 prefix
        if encrypted[:3] == b"v10":
            encrypted = encrypted[3:]
        iv = b" " * 16   # 16 ASCII spaces — Chrome's fixed IV on macOS
        cipher = Cipher(
            algorithms.AES(key),
            modes.CBC(iv),
            backend=default_backend()
        )
        decryptor = cipher.decryptor()
        decrypted = decryptor.update(encrypted) + decryptor.finalize()
        # Remove PKCS7 padding
        pad_len = decrypted[-1]
        return decrypted[:-pad_len].decode("utf-8", errors="replace")
    except Exception as e:
        return ""


def export_cookies():
    # Get AES key first — may trigger macOS Keychain dialog
    print("Getting Chrome AES key from macOS Keychain...")
    print("(If a dialog appears, click 'Always Allow')")
    key = get_chrome_aes_key()
    if not key:
        print("❌ Could not retrieve decryption key from Keychain.")
        sys.exit(1)

    # Search all profiles for GSCCCA cookies
    all_rows = []
    checked = []

    for path in CHROME_PATHS:
        if not path.exists():
            continue
        checked.append(str(path))

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            shutil.copy2(path, tmp.name)
            conn = sqlite3.connect(tmp.name)
            conn.row_factory = sqlite3.Row
            # Fetch encrypted_value too
            cursor = conn.execute("""
                SELECT host_key, name, value, encrypted_value,
                       path, expires_utc, is_secure, is_httponly
                FROM cookies
                WHERE host_key LIKE '%gsccca.org%'
            """)
            rows = cursor.fetchall()
            conn.close()
            if rows:
                print(f"✓ {len(rows)} GSCCCA cookies in: {path.parent.name}")
                all_rows.extend(rows)
        except Exception as e:
            print(f"  Skipped {path.parent.name}: {e}")
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass

    if not all_rows:
        print("❌ No GSCCCA cookies found in any Chrome profile.")
        print("   Checked:", checked)
        print("   Log into https://www.gsccca.org in Chrome and retry.")
        sys.exit(1)

    # Decrypt and build Playwright-compatible cookie list
    cookies = []
    seen = set()
    for row in all_rows:
        key_tuple = (row["host_key"], row["name"])
        if key_tuple in seen:
            continue
        seen.add(key_tuple)

        # Use encrypted_value if raw value is empty
        raw_value = row["value"]
        enc_value = bytes(row["encrypted_value"]) if row["encrypted_value"] else b""
        if not raw_value and enc_value:
            raw_value = decrypt_chrome_cookie(enc_value, key)

        cookies.append({
            "name":     row["name"],
            "value":    raw_value,
            "domain":   row["host_key"],
            "path":     row["path"],
            "secure":   bool(row["is_secure"]),
            "httpOnly": bool(row["is_httponly"]),
            "expires":  (row["expires_utc"] / 1_000_000) - 11644473600
                        if row["expires_utc"] else -1,
        })

    # Report what we got
    empty = sum(1 for c in cookies if not c["value"])
    filled = len(cookies) - empty
    print(f"\nDecrypted: {filled}/{len(cookies)} cookies have values")
    for c in cookies:
        status = c["value"][:20] if c["value"] else "<empty>"
        print(f"  {c['domain']:35s} {c['name']:30s} {status}")

    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w") as f:
        json.dump(cookies, f, indent=2)

    print(f"\n✅ Saved {len(cookies)} cookies → {OUT_FILE}")
    if filled > 0:
        print("   Run: python3 scrapers/ga_ucc_scraper.py")
    else:
        print("⚠️  All values empty — Chrome may still be running with this profile locked.")
        print("   Close Chrome fully and re-run this script.")


if __name__ == "__main__":
    export_cookies()
