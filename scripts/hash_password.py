"""Generate AUTH_PEPPER and hashed password for local dev.

Usage:
    python scripts/hash_password.py
    python scripts/hash_password.py --pepper <your_pepper> --password <your_password>
    python scripts/hash_password.py --pepper <your_pepper> --verify <stored_hash>
"""
from hashlib import pbkdf2_hmac
from secrets import compare_digest, token_bytes, token_hex
import sys
import os

def generate_pepper(length: int = 32) -> str:
    """Generate a random AUTH_PEPPER (hex, length bytes)."""
    return token_hex(length)

def hash_password(password: str, pepper: str) -> str:
    """Hash a password with the pepper using PBKDF2-SHA512."""
    salt = token_bytes(16)
    iterations = 100_000  # Cloudflare Workers WebCrypto caps PBKDF2 at 100k iterations
    combined = (password + pepper).encode("utf-8")
    hash_bytes = pbkdf2_hmac("sha512", combined, salt, iterations)
    return f"pbkdf2_sha512${iterations}${salt.hex()}${hash_bytes.hex()}"

def verify_password(password: str, pepper: str, stored_hash: str) -> bool:
    """Verify a password against a stored hash."""
    try:
        scheme, iterations, salt_hex, hash_hex = stored_hash.split("$")
        if scheme != "pbkdf2_sha512":
            return False
        salt = bytes.fromhex(salt_hex)
        iterations = int(iterations)
        expected_hash = bytes.fromhex(hash_hex)
        combined = (password + pepper).encode("utf-8")
        actual_hash = pbkdf2_hmac("sha512", combined, salt, iterations)
        return compare_digest(actual_hash, expected_hash)
    except (ValueError, AttributeError):
        return False

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Auth pepper utility")
    parser.add_argument("--pepper", help="Use existing pepper value")
    parser.add_argument("--password", help="Generate hash for this password")
    parser.add_argument("--verify", help="Verify a password against this stored hash")
    parser.add_argument("--show-pepper", action="store_true", help="Generate and show pepper")
    args = parser.parse_args()

    if args.show_pepper:
        pepper = generate_pepper(32)
        print(f"AUTH_PEPPER={pepper}")
        return

    if args.pepper and args.password:
        hashed = hash_password(args.password, args.pepper)
        print(f"ADMIN_PASSWORD={hashed}")
        return

    if args.pepper and args.verify:
        # Read pepper from .env
        pepper = None
        with open(".env") as f:
            for line in f:
                line = line.strip()
                if line.startswith("AUTH_PEPPER="):
                    pepper = line.split("=", 1)[1].strip()
                    if len(pepper) >= 2 and pepper[0] == pepper[-1] and pepper[0] in "\"'":
                        pepper = pepper[1:-1]
                    break
        if pepper is None:
            pepper = args.pepper

        # Try common passwords
        test_passwords = ["admin", "admin123", "vesteros", "vesterosbg", "password", "123456"]
        print(f"Testing against hash: {args.verify[:60]}...")
        for pwd in test_passwords:
            if verify_password(pwd, pepper, args.verify):
                print(f"MATCH: '{pwd}'")
                return
        print("No match with common passwords")
        return

    # Default: generate pepper and show usage
    pepper = generate_pepper(32)
    print(f"AUTH_PEPPER={pepper}")
    print("Add to .env:  AUTH_PEPPER=<this_value>")
    print("\nThen run: python scripts/hash_password.py --pepper <pepper> --password <your_new_password>")

if __name__ == "__main__":
    main()