from pathlib import Path

from click.testing import CliRunner

from umbra_py import llms_full_txt, llms_txt
from umbra_py.cli import cli
from umbra_py.constants import PRODUCT_ASSETS

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_llms_txt_is_deterministic():
    # The bundle describes the library; it must never call a model or depend on
    # anything nondeterministic, so two calls are byte-identical.
    assert llms_txt() == llms_txt()
    assert llms_full_txt() == llms_full_txt()


def test_llms_txt_index_follows_the_convention():
    text = llms_txt()
    # H1 title + blockquote summary are the llms.txt spec's required header.
    assert text.startswith("# umbra-py\n")
    assert "\n> " in text
    # The index points an agent at the fuller bundle first.
    assert "llms-full.txt" in text


def test_llms_full_documents_every_product_type():
    text = llms_full_txt()
    for name in PRODUCT_ASSETS:
        assert f"**{name}**" in text


def test_llms_full_documents_search_license_and_determinism():
    text = llms_full_txt()
    # Domain knowledge an agent needs to build a query and stay compliant.
    for param in ("bbox", "place", "area", "start", "end"):
        assert f"**{param}**" in text
    assert "CC-BY-4.0" in text
    assert "Contains Umbra open data" in text
    assert "Determinism boundary" in text


def test_llms_full_lists_the_cli_commands():
    text = llms_full_txt()
    # The reference is introspected from the live command tree, so core
    # commands must appear — including this bundle's own generator command.
    for command in ("umbra search", "umbra serve", "umbra mcp", "umbra llms-txt"):
        assert f"`{command}`" in text


def test_llms_txt_cli_matches_the_generators():
    runner = CliRunner()
    concise = runner.invoke(cli, ["llms-txt"])
    full = runner.invoke(cli, ["llms-txt", "--full"])
    assert concise.exit_code == 0
    assert full.exit_code == 0
    # click.echo appends a trailing newline to the generator output.
    assert concise.output == llms_txt() + "\n"
    assert full.output == llms_full_txt() + "\n"


def test_committed_bundle_matches_generator():
    # Golden test: the repo-root llms.txt / llms-full.txt are the rendered
    # output of the generators. If this fails, regenerate them with
    #   umbra llms-txt > llms.txt && umbra llms-txt --full > llms-full.txt
    assert (REPO_ROOT / "llms.txt").read_text(encoding="utf-8") == llms_txt() + "\n"
    assert (REPO_ROOT / "llms-full.txt").read_text(encoding="utf-8") == llms_full_txt() + "\n"
