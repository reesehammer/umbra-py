"""Offline tests for the header-only SICD preflight.

The claim under test is a cost claim: that "can this product be calibrated?" is
answerable from the NITF's own header plus its XML data extension segment, and
therefore from a few kilobytes of a multi-gigabyte product. So the fixtures are
real NITF byte layouts -- a file header whose segment tables have to be walked
past image and text segments to reach the DES -- rather than a stubbed parser,
and the assertions are about what was *read* as much as what was returned.
"""

from __future__ import annotations

import json
import re

import pytest
import responses
from click.testing import CliRunner

from umbra_py.exceptions import UmbraError, UnsupportedMeasurementError
from umbra_py.preflight import (
    preflight_items,
    read_sicd_xml,
    sicd_capabilities,
)

_UNCALIBRATED = """<?xml version="1.0" encoding="UTF-8"?>
<SICD xmlns="urn:SICD:1.2.1">
  <CollectionInfo><CoreName>2024-02-08-01-02-03_UMBRA-05</CoreName></CollectionInfo>
  <ImageData>
    <NumRows>4096</NumRows><NumCols>8192</NumCols>
    <SCPPixel><Row>2048</Row><Col>4096</Col></SCPPixel>
  </ImageData>
  <Grid><Row><SS>0.15</SS></Row><Col><SS>0.12</SS></Col></Grid>
  <ImageFormation><TxRcvPolarizationProc>V:V</TxRcvPolarizationProc></ImageFormation>
</SICD>
"""

_CALIBRATED = _UNCALIBRATED.replace(
    "</SICD>",
    """  <Radiometric>
    <SigmaZeroSFPoly order1="0" order2="0">
      <Coef exponent1="0" exponent2="0">2.5</Coef>
    </SigmaZeroSFPoly>
    <BetaZeroSFPoly order1="0" order2="0">
      <Coef exponent1="0" exponent2="0">3.0</Coef>
    </BetaZeroSFPoly>
    <NoiseLevel>
      <NoiseLevelType>ABSOLUTE</NoiseLevelType>
      <NoisePoly order1="1" order2="0">
        <Coef exponent1="0" exponent2="0">-20.0</Coef>
        <Coef exponent1="1" exponent2="0">0.5</Coef>
      </NoisePoly>
    </NoiseLevel>
  </Radiometric>
</SICD>""",
)

_RELATIVE_NOISE = _CALIBRATED.replace("ABSOLUTE", "RELATIVE")


def build_nitf(
    xml: str,
    *,
    images: tuple[tuple[int, int], ...] = ((512, 4096),),
    texts: tuple[tuple[int, int], ...] = (),
    version: bytes = b"02.10",
    magic: bytes = b"NITF",
    desid: str = "XML_DATA_CONTENT",
) -> bytes:
    """A minimal but structurally real NITF 2.1 file carrying ``xml`` in a DES.

    Only the fixed-width header fields the reader walks are meaningful; the image
    and text segments are filler of the declared lengths, which is exactly what
    makes them a test of the offset arithmetic.
    """
    body = xml.encode("utf-8")
    des_sub = b"DE" + desid.encode("ascii").ljust(25) + b"01" + b" " * 172

    head = bytearray()
    head += magic + version + b"03" + b"BF01" + b" " * 10 + b"20240208010203" + b" " * 80
    head += b" " * 167  # the file security block
    head += b"00000" + b"00000" + b"0" + b"\x00\x00\x00" + b" " * 24 + b" " * 18
    assert len(head) == 342, "FL should start at byte 342"

    tables = bytearray()
    tables += f"{len(images):03d}".encode()
    for sub, data in images:
        tables += f"{sub:06d}{data:010d}".encode()
    tables += b"000"  # NUMS: no graphic segments
    tables += b"000"  # NUMX: reserved
    tables += f"{len(texts):03d}".encode()
    for sub, data in texts:
        tables += f"{sub:04d}{data:05d}".encode()
    tables += b"001"  # NUMDES
    tables += f"{len(des_sub):04d}{len(body):09d}".encode()
    tables += b"000"  # NUMRES
    tables += b"00000" + b"00000"  # UDHDL / XHDL

    header = head + b"0" * 12 + b"0" * 6 + tables
    header_len = len(header)
    segments = bytearray()
    for sub, data in images:
        segments += b"I" * sub + b"\x00" * data
    for sub, data in texts:
        segments += b"T" * sub + b"\x00" * data
    segments += des_sub + body
    file_len = header_len + len(segments)
    header[342:354] = f"{file_len:012d}".encode()
    header[354:360] = f"{header_len:06d}".encode()
    return bytes(header) + bytes(segments)


def write_nitf(tmp_path, xml: str, **kwargs):
    path = tmp_path / "product.nitf"
    path.write_bytes(build_nitf(xml, **kwargs))
    return path


def serve_ranges(url: str, data: bytes) -> None:
    """Register ``url`` as an object that honours HTTP ``Range`` like S3 does."""

    def _callback(request):
        match = re.match(r"bytes=(\d+)-(\d+)", request.headers.get("Range", ""))
        assert match, "the reader must ask for a byte range, never the whole product"
        start, end = int(match.group(1)), int(match.group(2))
        chunk = data[start : end + 1]
        headers = {
            "Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{len(data)}",
        }
        return 206, headers, chunk

    responses.add_callback(responses.GET, url, callback=_callback)


# --------------------------------------------------------------------------- #
# Locating the metadata.
# --------------------------------------------------------------------------- #


def test_reads_the_xml_without_reading_the_product(tmp_path):
    """The whole point: the metadata comes back, the pixels are never touched."""
    path = write_nitf(tmp_path, _UNCALIBRATED, images=((512, 4_000_000),))
    xml, bytes_read, product_bytes = read_sicd_xml(path)

    assert "<SICD" in xml
    assert product_bytes == path.stat().st_size
    assert bytes_read < 20_000, "a preflight that reads the image segment is not one"


def test_locates_the_des_past_image_and_text_segments(tmp_path):
    """The DES offset is the sum of every preceding segment's declared lengths,
    so a product with several image segments and a text segment is the case that
    catches an off-by-one in the header walk."""
    path = write_nitf(
        tmp_path,
        _CALIBRATED,
        images=((512, 20_000), (480, 15_000)),
        texts=((300, 900),),
    )
    xml, _, _ = read_sicd_xml(path)
    assert "SigmaZeroSFPoly" in xml


def test_a_non_nitf_source_says_so(tmp_path):
    path = tmp_path / "not.nitf"
    path.write_bytes(b"GeoTIFF or anything else at all")
    with pytest.raises(UmbraError, match="NITF file header"):
        read_sicd_xml(path)


def test_an_unsupported_nitf_version_is_refused_rather_than_misread(tmp_path):
    """NITF 2.0's security fields are a different length, so the offsets this
    reads would silently land in the wrong place."""
    path = write_nitf(tmp_path, _UNCALIBRATED, version=b"02.00")
    with pytest.raises(UmbraError, match="02.10"):
        read_sicd_xml(path)


def test_a_product_with_no_xml_segment_says_so(tmp_path):
    path = write_nitf(tmp_path, _UNCALIBRATED, desid="TRE_OVERFLOW")
    with pytest.raises(UmbraError, match="no SICD XML"):
        read_sicd_xml(path)


# --------------------------------------------------------------------------- #
# What the metadata says the product can support.
# --------------------------------------------------------------------------- #


def test_an_uncalibrated_product_declares_nothing(tmp_path):
    caps = sicd_capabilities(write_nitf(tmp_path, _UNCALIBRATED))

    assert caps.calibrations == ()
    assert caps.noise_level is None
    assert caps.core_name == "2024-02-08-01-02-03_UMBRA-05"
    assert (caps.rows, caps.cols) == (4096, 8192)
    assert caps.polarization == "V:V"


def test_a_calibrated_product_declares_what_it_carries(tmp_path):
    """Only the polynomials actually present -- gamma0 and rcs are absent here,
    and an absent scale factor is the answer, not an assumed one."""
    caps = sicd_capabilities(write_nitf(tmp_path, _CALIBRATED))

    assert caps.calibrations == ("sigma0", "beta0")
    assert caps.noise_level == "ABSOLUTE"


def test_the_refusal_is_the_conversions_own(tmp_path):
    """An uncalibrated product refuses with the message and hint `umbra convert`
    would have raised after downloading it."""
    caps = sicd_capabilities(write_nitf(tmp_path, _UNCALIBRATED))

    assert caps.refusal() is None
    refusal = caps.refusal(calibration="sigma0")
    assert isinstance(refusal, UnsupportedMeasurementError)
    assert "Radiometric" in str(refusal)
    assert refusal.hint

    noise = caps.refusal(noise_subtract=True, noise_model="measured")
    assert isinstance(noise, UnsupportedMeasurementError)
    assert "estimated" in (noise.hint or "")


def test_an_inferred_noise_model_needs_nothing_from_the_product(tmp_path):
    """`--noise-model estimated` reads the scene's own pixels, so a product that
    states no floor is not a refusal for it."""
    caps = sicd_capabilities(write_nitf(tmp_path, _UNCALIBRATED))
    assert caps.refusal(noise_subtract=True, noise_model="estimated") is None


def test_a_relative_noise_level_is_refused_by_name(tmp_path):
    caps = sicd_capabilities(write_nitf(tmp_path, _RELATIVE_NOISE))
    assert caps.noise_level == "RELATIVE"

    refusal = caps.refusal(noise_subtract=True, noise_model="measured")
    assert isinstance(refusal, UnsupportedMeasurementError)
    assert "RELATIVE" in str(refusal)


def test_a_declared_product_clears_the_check(tmp_path):
    """The polynomials are read the way the conversion reads them, so clearing
    the preflight means the conversion will not refuse."""
    pytest.importorskip("numpy")
    caps = sicd_capabilities(write_nitf(tmp_path, _CALIBRATED))

    assert caps.refusal(calibration="sigma0") is None
    assert caps.refusal(noise_subtract=True, noise_model="measured") is None
    assert caps.refusal(calibration="gamma0") is not None


def test_the_dense_coefficients_match_the_sparse_xml(tmp_path):
    """SICD writes a polynomial as exponent-addressed coefficients; the
    conversion reads a dense ``Coefs`` grid. The view is what bridges them."""
    caps = sicd_capabilities(write_nitf(tmp_path, _CALIBRATED))
    poly = caps.sicd.Radiometric.NoiseLevel.NoisePoly
    assert poly.Coefs == [[-20.0], [0.5]]


# --------------------------------------------------------------------------- #
# Over the wire, and over a selection.
# --------------------------------------------------------------------------- #


@responses.activate
def test_a_remote_product_is_read_by_range_request():
    url = "https://example.com/product.nitf"
    data = build_nitf(_CALIBRATED, images=((512, 5_000_000),))
    serve_ranges(url, data)

    caps = sicd_capabilities(url)

    assert caps.calibrations == ("sigma0", "beta0")
    assert caps.product_bytes == len(data)
    assert caps.bytes_read < len(data) / 100


@responses.activate
def test_preflight_items_answers_for_a_selection(sample_item_dict):
    from umbra_py.models import UmbraItem

    item = UmbraItem.from_dict(sample_item_dict, href="https://example.com/x.stac.v2.json")
    serve_ranges(item.asset_href("SICD"), build_nitf(_UNCALIBRATED))

    report = preflight_items([item], calibration="gamma0")

    assert len(report.results) == 1
    result = report.results[0]
    assert result.supported is False
    assert "Radiometric" in (result.reason or "")
    assert report.supported == ()
    assert report.bytes_read > 0
    assert report.to_dict()["supported_count"] == 0


@responses.activate
def test_an_unreadable_acquisition_is_recorded_rather_than_fatal(sample_item_dict):
    """A preflight that dies on the nineteenth scene has failed at the one thing
    it is for, so a read failure is a per-item verdict."""
    from umbra_py.models import UmbraItem

    first = UmbraItem.from_dict(sample_item_dict, href="https://example.com/a.stac.v2.json")
    second = UmbraItem.from_dict(sample_item_dict, href="https://example.com/b.stac.v2.json")
    responses.add(responses.GET, first.asset_href("SICD"), status=404)
    serve_ranges(second.asset_href("SICD"), build_nitf(_CALIBRATED))

    report = preflight_items([first, second])

    assert report.results[0].supported is False
    assert "404" in (report.results[0].error or "")
    assert report.results[1].supported is True


# --------------------------------------------------------------------------- #
# The CLI.
# --------------------------------------------------------------------------- #


@responses.activate
def test_cli_preflight_reports_json(monkeypatch, sample_item_dict):
    from umbra_py import cli as cli_mod
    from umbra_py.models import UmbraItem

    item = UmbraItem.from_dict(sample_item_dict, href="https://example.com/x.stac.v2.json")
    serve_ranges(item.asset_href("SICD"), build_nitf(_UNCALIBRATED))
    monkeypatch.setattr("umbra_py.cli._shared._item_from_url", lambda url: item)

    result = CliRunner().invoke(
        cli_mod.cli,
        ["preflight", "https://example.com/x.stac.v2.json", "--calibrate", "sigma0", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["calibration"] == "sigma0"
    assert payload["supported_count"] == 0
    assert payload["results"][0]["capabilities"]["calibrations"] == []


@responses.activate
def test_cli_preflight_says_what_the_answer_cost(monkeypatch, sample_item_dict):
    from umbra_py import cli as cli_mod
    from umbra_py.models import UmbraItem

    item = UmbraItem.from_dict(sample_item_dict, href="https://example.com/x.stac.v2.json")
    serve_ranges(item.asset_href("SICD"), build_nitf(_UNCALIBRATED, images=((512, 5_000_000),)))
    monkeypatch.setattr("umbra_py.cli._shared._item_from_url", lambda url: item)

    result = CliRunner().invoke(
        cli_mod.cli,
        ["preflight", "https://example.com/x.stac.v2.json", "--calibrate", "gamma0"],
    )

    assert result.exit_code == 0, result.output
    assert "0 of 1 acquisition(s) support --calibrate gamma0" in result.output
    assert "instead of" in result.output
    assert "hint:" in result.output


def test_cli_preflight_needs_something_to_look_at():
    from umbra_py import cli as cli_mod

    result = CliRunner().invoke(cli_mod.cli, ["preflight"])
    assert result.exit_code != 0
    assert "--area" in result.output


# --------------------------------------------------------------------------- #
# Failure modes of the header walk itself.
# --------------------------------------------------------------------------- #


def test_a_truncated_header_is_named_rather_than_misread(tmp_path):
    """Reading past the end of a short file would land the segment offsets
    somewhere arbitrary, so the walk stops at the field it cannot read."""
    path = tmp_path / "short.nitf"
    path.write_bytes(b"NITF02.10" + b" " * 100)
    with pytest.raises(UmbraError, match="truncated"):
        read_sicd_xml(path)


def test_a_non_numeric_length_field_is_named(tmp_path):
    data = bytearray(build_nitf(_UNCALIBRATED))
    data[354:360] = b"abcdef"  # HL
    path = tmp_path / "bad.nitf"
    path.write_bytes(bytes(data))
    with pytest.raises(UmbraError, match="not a number"):
        read_sicd_xml(path)


def test_a_segment_too_large_to_be_metadata_is_skipped(tmp_path, monkeypatch):
    """A data extension segment of implausible size is not the XML, and reading
    it would spend exactly what the preflight exists to save."""
    from umbra_py import preflight as preflight_mod

    monkeypatch.setattr(preflight_mod, "_MAX_XML_BYTES", 8)
    with pytest.raises(UmbraError, match="no SICD XML"):
        read_sicd_xml(write_nitf(tmp_path, _UNCALIBRATED))


def test_unparseable_xml_is_named(tmp_path):
    with pytest.raises(UmbraError, match="could not be parsed"):
        sicd_capabilities(write_nitf(tmp_path, "<SICD><Unclosed>"))


def test_the_view_reports_a_missing_child_as_absent(tmp_path):
    """The conversion's checks are ``getattr`` chains with defaults, so an
    element that is not there has to read as absent rather than raise."""
    caps = sicd_capabilities(write_nitf(tmp_path, _UNCALIBRATED))

    assert getattr(caps.sicd, "Radiometric", None) is None
    assert getattr(caps.sicd.Grid.Row, "Coefs", None) is None
    assert getattr(caps.sicd, "_element_like_name", None) is None
    assert caps.sicd.Grid.Row.SS == 0.15


@responses.activate
def test_a_server_that_ignores_the_range_header_is_still_read_correctly():
    """S3 honours Range; a proxy or a mirror might not, and handing back the
    whole object must not shift every offset the walk computes."""
    url = "https://example.com/whole.nitf"
    data = build_nitf(_CALIBRATED)
    responses.add(responses.GET, url, body=data, status=200)

    caps = sicd_capabilities(url)

    assert caps.calibrations == ("sigma0", "beta0")
    assert caps.product_bytes == len(data)
