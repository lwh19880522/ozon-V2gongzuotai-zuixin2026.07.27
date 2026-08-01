from __future__ import annotations

from pathlib import Path

from PIL import Image

from ozon_v2.images.worker import crop_grid


ROOT = Path(__file__).resolve().parents[1]


def test_four_by_two_grid_crops_to_eight_unstretched_three_by_four_images(
    tmp_path: Path,
) -> None:
    panel_size = (90, 120)
    colors = (
        "red",
        "green",
        "blue",
        "yellow",
        "purple",
        "orange",
        "black",
        "white",
    )
    grid = Image.new("RGB", (panel_size[0] * 4, panel_size[1] * 2))
    for index, color in enumerate(colors):
        column = index % 4
        row = index // 4
        grid.paste(
            Image.new("RGB", panel_size, color),
            (column * panel_size[0], row * panel_size[1]),
        )
    source = tmp_path / "gallery-grid.png"
    grid.save(source)

    outputs = crop_grid(
        source,
        tmp_path / "panels",
        layout="4x2",
        basename="gallery",
    )

    assert len(outputs) == 8
    for index, output in enumerate(outputs):
        with Image.open(output) as panel:
            assert panel.size == panel_size
            assert panel.width * 4 == panel.height * 3
            assert panel.getpixel((panel.width // 2, panel.height // 2)) == Image.new(
                "RGB", (1, 1), colors[index]
            ).getpixel((0, 0))


def test_active_skill_contains_only_one_eight_panel_grid_wrapper() -> None:
    root = ROOT / "skills" / "ozon-product-media-generator"
    assets = {path.name for path in (root / "assets").glob("*.txt")}
    skill = (root / "SKILL.md").read_text(encoding="utf-8")
    contract = (root / "references" / "prompt-contract.md").read_text(
        encoding="utf-8"
    )

    assert "gallery-8-grid-prompt.txt" in assets
    assert "main-grid-prompt.txt" not in assets
    assert "detail-grid-a-prompt.txt" not in assets
    assert "detail-grid-b-prompt.txt" not in assets
    assert "one 4x2 grid" in skill
    assert "exactly two image-generation calls" in contract
    assert "one white identity anchor" in contract
    assert "one 4x2 eight-panel gallery grid" in contract
    assert "referenced_image_paths=[<same frozen white anchor path>]" in contract
    assert "1x2" not in skill
    assert "1x3" not in skill
