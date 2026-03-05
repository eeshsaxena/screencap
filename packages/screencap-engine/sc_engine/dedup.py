"""Perceptual image hashing for screenshot deduplication."""

from __future__ import annotations

from PIL import Image


def dhash(image: Image.Image, hash_size: int = 8) -> int:
    """Compute difference hash (dHash) of an image.

    Resizes to (hash_size+1, hash_size), computes horizontal gradient,
    returns a hash_size*hash_size-bit integer.
    """
    resized = image.resize((hash_size + 1, hash_size), Image.Resampling.BOX)
    grayscale = resized.convert("L")
    pixels = grayscale.tobytes()
    width = hash_size + 1
    hash_value = 0
    for row in range(hash_size):
        for col in range(hash_size):
            offset = row * width + col
            if pixels[offset] < pixels[offset + 1]:
                hash_value |= 1 << (row * hash_size + col)
    return hash_value


def hamming_distance(hash1: int, hash2: int) -> int:
    """Count differing bits between two hashes."""
    return (hash1 ^ hash2).bit_count()
