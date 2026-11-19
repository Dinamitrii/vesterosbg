"""Generate AUTH_PEPPER and hashed password for local dev.

Usage:
    python scripts/hash_password.py
    python scripts/hash_password.py --pepper <your_pepper> --password <your_password>
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
    iterations = 200_000
    combined = (password + pepper).encode("utf-8")
    hash_bytes = pbkdf2_hmac("sha512", combined, salt, iterations)
    return f"pbkdf2_sha512${iterations}${salt.hex()}${hash_bytes.hex()}"


def verify_password(password: str, pepper: str, stored_hash: str) -> bool:
    """Verify a password against a stored hash."""
    try:
        scheme, iterations, salt_hex, hash_hex = stored_hash.split("$")
        if scheme != "pbkdf2_sha512":
            return False
        salt = token_bytes(len(salt_hex) // 2)
        salt_actual = bytes.fromhex(salt_hex)
        iterations = int(iterations)
        expected_hash = bytes.fromhex(hash_hex)
        combined = (password + pepper).encode("utf-8")
        actual_hash = pbkdf2_hmac("sha512", combined, salt_actual, iterations)
        return compare_digest(actual_hash, expected_hash)
    except (ValueError, AttributeError):
        return False


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Auth pepper utility")
    parser.add_argument("--pepper", help="Use existing pepper value")
    parser.add_argument("--password", help="Generate hash for this password")
    parser.add_argument("--show-pepper", action="store_true", help="Generate and show pepper")
    args = parser.parse_args()

    if args.show_pepper:
        pepper = generate_pepper(32)
        print(f"AUTH_PEPPER={pepper}")
        return

    if args.pepper and args.password:
        hashed = hash_password(args.password, args.pepper)
        print(f"Hash: {hashed}")
        return

    # Default: generate pepper and show usage
    pepper = generate_pepper(32)
    print(f"AUTH_PEPPER={pepper}")
    print("Add to .env:  AUTH_PEPPER=***")
    print("\nThen update admin_login() in app.py to use hash_password()")


if __name__ == "__main__":
    main()
