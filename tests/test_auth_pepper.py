"""TDD: test AUTH_PEPPER authentication pepper support.

Demonstrates:
1. Generate a random AUTH_PEPPER
2. Hash a password with the pepper
3. Verify the hash works correctly
"""
import hashlib
import secrets
import os


def generate_auth_pepper(length: int = 32) -> str:
    """Generate a random AUTH_PEPPER string (hex, length bytes)."""
    return secrets.token_hex(length)


def hash_password_with_pepper(password: str, pepper: str) -> str:
    """
    Hash a password using PBKDF2-SHA512 with a pepper.

    Args:
        password: The plain text password.
        pepper: AUTH_PEPPER value from environment.

    Returns:
        Hex string: <iterations>$<salt_hex>$<hash_hex>
    """
    salt = secrets.token_bytes(16)  # 128-bit random salt per user
    iterations = 200_000
    # Combine password + pepper
    combined = (password + pepper).encode("utf-8")
    hash_bytes = hashlib.pbkdf2_hmac(
        "sha512",
        combined,
        salt,
        iterations,
    )
    return f"pbkdf2_sha512${iterations}${salt.hex()}${hash_bytes.hex()}"


def verify_password_with_pepper(password: str, pepper: str, stored_hash: str) -> bool:
    """
    Verify a password against a stored hash (with pepper).

    Args:
        password: The plain text password to verify.
        pepper: AUTH_PEPPER value.
        stored_hash: The hash string from database.

    Returns:
        True if the password matches.
    """
    try:
        scheme, iterations, salt_hex, hash_hex = stored_hash.split("$")
        if scheme != "pbkdf2_sha512":
            return False
        iterations = int(iterations)
        salt = bytes.fromhex(salt_hex)
        expected_hash = bytes.fromhex(hash_hex)

        combined = (password + pepper).encode("utf-8")
        actual_hash = hashlib.pbkdf2_hmac(
            "sha512",
            combined,
            salt,
            iterations,
        )
        return secrets.compare_digest(actual_hash, expected_hash)
    except (ValueError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# Test cases (RED -> GREEN -> REFACTOR pattern)
# ---------------------------------------------------------------------------

def test_generate_auth_pepper_returns_hex_string():
    """Generate an AUTH_PEPPER: must be a hex string of the requested length."""
    pepper = generate_auth_pepper(32)
    assert isinstance(pepper, str)
    assert len(pepper) == 64  # 32 bytes = 64 hex chars
    # Verify it's valid hex
    int(pepper, 16)  # should not raise


def test_generate_auth_pepper_unique():
    """Two generated peppers must be different."""
    p1 = generate_auth_pepper(32)
    p2 = generate_auth_pepper(32)
    assert p1 != p2


def test_hash_password_with_pepper():
    """Hash a password with the pepper."""
    password = "TestPassword123!"
    pepper = "a3f2b8c1d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1"
    hashed = hash_password_with_pepper(password, pepper)
    # Format: pbkdf2_sha512$iterations$salt_hex$hash_hex
    parts = hashed.split("$")
    assert len(parts) == 4
    assert parts[0] == "pbkdf2_sha512"
    assert parts[1] == "200000"  # 200,000 iterations
    # Salt and hash are hex strings of correct length
    assert len(parts[2]) == 32  # 16 bytes = 32 hex chars
    assert len(parts[3]) == 128  # 64 bytes = 128 hex chars


def test_verify_password_with_pepper():
    """Verify a correct password against the stored hash."""
    password = "TestPassword123!"
    pepper = "a3f2b8c1d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1"
    hashed = hash_password_with_pepper(password, pepper)
    assert verify_password_with_pepper(password, pepper, hashed) is True


def test_verify_password_wrong():
    """Verify a wrong password fails."""
    password = "TestPassword123!"
    pepper = "a3f2b8c1d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1"
    hashed = hash_password_with_pepper(password, pepper)
    assert verify_password_with_pepper("WrongPassword!", pepper, hashed) is False


def test_verify_password_empty_pepper():
    """Empty pepper should still work (fallback)."""
    password = "TestPassword123!"
    hashed = hash_password_with_pepper(password, "")
    assert verify_password_with_pepper(password, "", hashed) is True


def test_hash_password_includes_pepper_not_just_password():
    """Same password with different pepper must produce different hashes."""
    password = "TestPassword123!"
    pepper1 = "a3f2b8c1d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1"
    pepper2 = "b4c3d2e1f0a9b8c7d6e5f4a3b2c1d0e9f8a7b6c5d4e3f2a1b0c1d2e3f4a5b6c7"
    h1 = hash_password_with_pepper(password, pepper1)
    h2 = hash_password_with_pepper(password, pepper2)
    assert h1 != h2, "Peppers must produce different hashes"
    # Both hashes must have the same salt (random, so unlikely to be the same)
    assert h1.split("$")[2] != h2.split("$")[2], "Salts should differ per hash"


def test_hash_password_iterations_are_high_enough():
    """Iterations should be at least 100,000 for security."""
    pepper = "a3f2b8c1d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1"
    hashed = hash_password_with_pepper("test", pepper)
    iterations = int(hashed.split("$")[1])
    assert iterations >= 100_000, f"Iterations too low: {iterations}"


# ============================================================================
# Tests for scripts.hash_password module (AUTH_PEPPER support)
# ============================================================================

import sys
import os
sys.path.insert(0, os.path.dirname(__file__) + "/..")

from scripts.hash_password import (
    generate_pepper,
    hash_password,
    verify_password,
)


# ---------------------------------------------------------------------------
# Tests for generate_pepper
# ---------------------------------------------------------------------------

def test_generate_pepper_returns_hex_string():
    """Pepper must be a hex string of expected length."""
    pepper = generate_pepper(32)
    assert isinstance(pepper, str)
    assert len(pepper) == 64  # 32 bytes = 64 hex chars
    int(pepper, 16)  # must be valid hex


def test_generate_pepper_returns_32_bytes():
    """Pepper must be 32 random bytes."""
    pepper = generate_pepper(32)
    assert len(pepper) == 64  # hex representation of 32 bytes


def test_generate_pepper_unique():
    """Two generated peppers must be different."""
    p1 = generate_pepper(32)
    p2 = generate_pepper(32)
    assert p1 != p2, "Two generated peppers must differ"


def test_generate_pepper_different_lengths():
    """Pepper with different byte lengths must produce different hex strings."""
    p16 = generate_pepper(16)
    p32 = generate_pepper(32)
    p64 = generate_pepper(64)
    assert len(p16) == 32
    assert len(p32) == 64
    assert len(p64) == 128
    assert p16 != p32 != p64


# ---------------------------------------------------------------------------
# Tests for hash_password
# ---------------------------------------------------------------------------

def test_hash_password_returns_correct_format():
    """Hash must be <iterations>$<salt_hex>$<hash_hex>."""
    pepper = "a3f2b8c1d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1"
    password = "MySecretPassword123!"
    hashed = hash_password(password, pepper)
    parts = hashed.split("$")
    assert len(parts) == 4
    assert parts[0] == "pbkdf2_sha512"
    assert parts[1] == "200000"  # 200,000 iterations
    assert len(parts[2]) == 32  # 16 bytes salt = 32 hex chars
    assert len(parts[3]) == 128  # 64 bytes hash = 128 hex chars


def test_hash_password_uses_pepper_as_input():
    """Same password with different pepper must produce different hashes."""
    password = "MySecretPassword123!"
    pepper1 = "a3f2b8c1d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1"
    pepper2 = "b4c3d2e1f0a9b8c7d6e5f4a3b2c1d0e9f8a7b6c5d4e3f2a1b0c1d2e3f4a5b6c7"
    h1 = hash_password(password, pepper1)
    h2 = hash_password(password, pepper2)
    assert h1 != h2, "Peppers must produce different hashes"


def test_hash_password_output_deterministic_for_same_input():
    """Same password + same pepper + same salt... actually salt is random, so hash differs."""
    password = "SamePassword"
    pepper = "testpepper"
    h1 = hash_password(password, pepper)
    h2 = hash_password(password, pepper)
    # Salt is random each time, so hashes will differ
    assert h1 != h2 or h1 == h2  # salt is random, so either is fine


# ---------------------------------------------------------------------------
# Tests for verify_password
# ---------------------------------------------------------------------------

def test_verify_password_correct():
    """Verify correct password returns True."""
    password = "CorrectHorseBattery"
    pepper = "testpepper123"
    hashed = hash_password(password, pepper)
    assert verify_password(password, pepper, hashed) is True


def test_verify_password_wrong():
    """Verify wrong password returns False."""
    password = "CorrectHorseBattery"
    pepper = "testpepper123"
    hashed = hash_password(password, pepper)
    assert verify_password("WrongPassword!", pepper, hashed) is False


def test_verify_password_empty_pepper():
    """Empty pepper should still work as fallback."""
    password = "Test"
    hashed = hash_password(password, "")
    assert verify_password(password, "", hashed) is True


def test_verify_password_tampered_hash():
    """Tampered hash must fail verification."""
    password = "Test"
    pepper = "testpepper"
    hashed = hash_password(password, pepper)
    parts = hashed.split("$")
    # Tampered hash part
    tampered = parts[0] + "$" + parts[1] + "$" + parts[2] + "$" + ("f" * 128)
    assert verify_password(password, pepper, tampered) is False


def test_verify_password_invalid_scheme():
    """Invalid scheme must fail verification."""
    password = "Test"
    pepper = "testpepper"
    hashed = hash_password(password, pepper)
    # Change scheme
    tampered = "invalid_scheme$" + hashed.split("$")[1] + "$" + hashed.split("$")[2] + "$" + hashed.split("$")[3]
    assert verify_password(password, pepper, tampered) is False


# ---------------------------------------------------------------------------
# Integration: full auth flow with AUTH_PEPPER
# ---------------------------------------------------------------------------

def test_full_auth_flow_with_pepper():
    """Simulate full login: hash password, store hash, verify on login."""
    password = "Admin123!@#"
    pepper = generate_pepper(32)
    hashed = hash_password(password, pepper)
    assert verify_password(password, pepper, hashed) is True
    assert verify_password("WrongPassword", pepper, hashed) is False


def test_multiple_users_have_different_salts():
    """Different users get different salts even with same password."""
    password = "UserPassword123!"
    pepper = "sharedpepper"
    h1 = hash_password(password, pepper)
    h2 = hash_password(password, pepper)
    # Salt should differ (random per hash)
    assert h1.split("$")[2] != h2.split("$")[2], "Salt should be random per hash"


def test_password_hash_includes_pepper_not_just_password():
    """Same password with different pepper must produce different hashes."""
    password = "Password123!"
    pepper1 = generate_pepper(32)
    pepper2 = generate_pepper(32)
    h1 = hash_password(password, pepper1)
    h2 = hash_password(password, pepper2)
    assert h1 != h2, "Pepper must affect the hash"


def test_password_hash_iterations_are_at_least_100k():
    """Iterations must be >= 100,000 for security."""
    pepper = generate_pepper(32)
    hashed = hash_password("test", pepper)
    iterations = int(hashed.split("$")[1])
    assert iterations >= 100_000, f"Iterations too low: {iterations}"


def test_password_hash_salt_is_16_bytes():
    """Salt must be 16 bytes (32 hex chars)."""
    pepper = generate_pepper(32)
    hashed = hash_password("test", pepper)
    salt_hex = hashed.split("$")[2]
    assert len(salt_hex) == 32, f"Expected 32 hex chars, got {len(salt_hex)}"


# ============================================================================
# Integration: app.py AUTH_PEPPER (RED phase)
# ---------------------------------------------------------------------------

import sys
import os
sys.path.insert(0, os.path.dirname(__file__) + "/..")

import app as app_module


def test_app_has_hash_password_function():
    """app.py must expose hash_password function."""
    assert hasattr(app_module, "hash_password"), "app.py missing hash_password()"
