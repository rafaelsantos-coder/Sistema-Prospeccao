from app.services.media_storage import _boto_addressing_style


def test_railway_virtual_host_style_maps_to_boto_virtual():
    assert _boto_addressing_style("virtual-host") == "virtual"
    assert _boto_addressing_style("virtual_host") == "virtual"
    assert _boto_addressing_style("virtual") == "virtual"


def test_path_style_is_preserved():
    assert _boto_addressing_style("path") == "path"


def test_unknown_style_falls_back_to_auto():
    assert _boto_addressing_style("") == "auto"
    assert _boto_addressing_style("unexpected") == "auto"
