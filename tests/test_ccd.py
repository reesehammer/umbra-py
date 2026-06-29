import pytest

from umbra_py import ccd


def _bandlimited(rng, h, w, frac=0.5):
    """An oversampled, SAR-like complex scene (white speckle, low-passed).

    Real SICD imagery is sampled above the signal bandwidth, so sub-pixel
    coregistration and coherence behave well; pure white noise is the
    worst case and not representative.
    """
    np = pytest.importorskip("numpy")
    f = rng.standard_normal((h, w)) + 1j * rng.standard_normal((h, w))
    fy = np.fft.fftfreq(h)[:, None]
    fx = np.fft.fftfreq(w)[None, :]
    mask = (np.abs(fy) < frac / 2) & (np.abs(fx) < frac / 2)
    return np.fft.ifft2(np.fft.fft2(f) * mask)


def test_box_sum_matches_brute_force():
    np = pytest.importorskip("numpy")
    rng = np.random.default_rng(0)
    a = rng.standard_normal((9, 11))
    r = 1
    expected = np.zeros_like(a)
    for i in range(a.shape[0]):
        for j in range(a.shape[1]):
            expected[i, j] = a[max(0, i - r) : i + r + 1, max(0, j - r) : j + r + 1].sum()
    assert np.allclose(ccd._box_sum(a, 3), expected)


def test_coherence_of_identical_image_is_one():
    np = pytest.importorskip("numpy")
    rng = np.random.default_rng(1)
    ref = rng.standard_normal((64, 64)) + 1j * rng.standard_normal((64, 64))
    g = ccd.coherence(ref, ref.copy(), window=5)
    assert g.dtype == np.float32
    assert g.min() >= 0.0 and g.max() <= 1.0
    # Interior pixels (full window) are perfectly coherent.
    assert g[10:-10, 10:-10].min() == pytest.approx(1.0, abs=1e-5)


def test_subpixel_registration_restores_coherence():
    np = pytest.importorskip("numpy")
    rng = np.random.default_rng(2)
    ref = _bandlimited(rng, 160, 160)
    sec = ccd._fourier_shift(ref, (1.3, -2.7))

    # The estimate undoes the applied shift (opposite sign).
    dy, dx = ccd._register_shift(np.abs(ref), np.abs(sec), upsample=20)
    assert dy == pytest.approx(-1.3, abs=0.1)
    assert dx == pytest.approx(2.7, abs=0.1)

    with_reg = ccd.coherence(ref, sec, window=5, register=True)
    without = ccd.coherence(ref, sec, window=5, register=False)
    assert with_reg[20:-20, 20:-20].mean() > 0.98
    # Half a pixel of misregistration visibly decorrelates the pair.
    assert without[20:-20, 20:-20].mean() < 0.6


def test_coherence_drops_where_the_surface_changed():
    np = pytest.importorskip("numpy")
    rng = np.random.default_rng(3)
    ref = _bandlimited(rng, 160, 160)
    sec = ref.copy()
    # A disturbed patch: replace the complex scene (its phase decorrelates).
    sec[60:100, 60:100] = _bandlimited(rng, 160, 160)[60:100, 60:100]
    g = ccd.coherence(ref, sec, window=7)
    changed = g[70:90, 70:90].mean()
    stable = np.concatenate([g[20:40, 20:40].ravel(), g[120:140, 120:140].ravel()]).mean()
    assert changed < 0.5
    assert stable > 0.9
    assert changed < stable


def test_coherence_crops_mismatched_shapes():
    np = pytest.importorskip("numpy")
    rng = np.random.default_rng(4)
    ref = rng.standard_normal((40, 50)) + 1j * rng.standard_normal((40, 50))
    sec = ref[:30, :45].copy()
    g = ccd.coherence(ref, sec, window=3, register=False)
    assert g.shape == (30, 45)


def test_coherence_validates_inputs():
    np = pytest.importorskip("numpy")
    ref = np.zeros((8, 8), dtype=complex)
    with pytest.raises(ValueError):
        ccd.coherence(ref, ref, window=4)  # even window
    with pytest.raises(ValueError):
        ccd.coherence(np.zeros(8, dtype=complex), np.zeros(8, dtype=complex))  # not 2D


def test_ccd_image_grayscale_and_colormap_and_invert():
    np = pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    coh = np.linspace(0.0, 1.0, 64, dtype="float32").reshape(8, 8)

    gray = ccd.ccd_image(coh)
    assert gray.mode == "L"
    assert gray.size == (8, 8)

    inv = ccd.ccd_image(coh, invert=True)
    # Inverting flips bright and dark.
    assert np.asarray(gray)[0, 0] == 255 - np.asarray(inv)[0, 0]

    pytest.importorskip("matplotlib")
    colored = ccd.ccd_image(coh, colormap="viridis")
    assert colored.mode == "RGB"


def test_save_ccd_writes_image(monkeypatch, tmp_path):
    np = pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    rng = np.random.default_rng(5)
    ref = _bandlimited(rng, 96, 96)
    sec = ccd._fourier_shift(ref, (0.5, -0.5))

    # Skip the sarpy SICD read; feed synthetic complex arrays instead.
    reads = iter([ref, sec])
    monkeypatch.setattr(ccd, "_read_sicd", lambda src: next(reads))

    out = tmp_path / "ccd.png"
    result = ccd.save_ccd("ref.nitf", "sec.nitf", out, max_size=48)
    assert result == out and out.exists()

    from PIL import Image

    with Image.open(out) as im:
        assert max(im.size) <= 48
