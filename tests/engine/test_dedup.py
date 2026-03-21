"""Tests for screencap.engine.dedup — perceptual hashing utilities."""

from __future__ import annotations

from PIL import Image

from screencap.engine.dedup import dhash, hamming_distance


def _solid_image(color: int, size: tuple[int, int] = (100, 100)) -> Image.Image:
    """Create a solid-color grayscale image."""
    return Image.new("L", size, color)


def test_identical_images_distance_zero():
    img = Image.new("RGB", (200, 150), (120, 80, 200))
    h1 = dhash(img)
    h2 = dhash(img)
    assert hamming_distance(h1, h2) == 0


def test_completely_different_images_high_distance():
    white = _solid_image(255)
    # Create a strong gradient image — very different from solid white
    grad = Image.new("L", (100, 100))
    pixels = grad.load()
    for x in range(100):
        for y in range(100):
            pixels[x, y] = (x * 255) // 99
    dist = hamming_distance(dhash(white), dhash(grad))
    assert dist > 8  # well above default threshold


def test_minor_modification_low_distance():
    """A single-pixel change should produce low distance."""
    img1 = Image.new("RGB", (200, 150), (100, 100, 100))
    img2 = img1.copy()
    img2.putpixel((100, 75), (200, 200, 200))
    dist = hamming_distance(dhash(img1), dhash(img2))
    assert dist < 8  # below default threshold


def test_hamming_distance_zero():
    assert hamming_distance(0, 0) == 0
    assert hamming_distance(0xFFFFFFFFFFFFFFFF, 0xFFFFFFFFFFFFFFFF) == 0


def test_hamming_distance_max():
    assert hamming_distance(0, 0xFFFFFFFFFFFFFFFF) == 64


def test_hamming_distance_single_bit():
    assert hamming_distance(0, 1) == 1
    assert hamming_distance(0, 1 << 63) == 1


def test_solid_black_produces_hash_zero():
    """All-black image has no horizontal gradients → dHash should be 0."""
    black = _solid_image(0)
    assert dhash(black) == 0


def test_solid_white_produces_hash_zero():
    """All-white image also has no horizontal gradients → dHash 0."""
    white = _solid_image(255)
    assert dhash(white) == 0


def test_dhash_returns_64_bit_integer():
    img = Image.new("RGB", (300, 200), (42, 128, 200))
    h = dhash(img)
    assert isinstance(h, int)
    assert 0 <= h < (1 << 64)


def test_dhash_different_sizes_same_content():
    """dHash should be size-invariant for the same visual content."""
    small = Image.new("RGB", (50, 50), (100, 150, 200))
    large = Image.new("RGB", (500, 500), (100, 150, 200))
    assert dhash(small) == dhash(large)
