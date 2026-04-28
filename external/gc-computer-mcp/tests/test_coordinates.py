from gc_computer_mcp.core.coordinates import Rect, center_of, point_in_rect


def test_center_of_rect():
    r = Rect(x=10, y=20, width=100, height=50)
    assert center_of(r) == (60, 45)


def test_point_in_rect_true():
    r = Rect(x=0, y=0, width=100, height=100)
    assert point_in_rect((50, 50), r) is True


def test_point_in_rect_false_outside():
    r = Rect(x=0, y=0, width=100, height=100)
    assert point_in_rect((150, 50), r) is False


def test_point_in_rect_edge_is_inside():
    r = Rect(x=0, y=0, width=100, height=100)
    assert point_in_rect((99, 99), r) is True
