from calculator import add, subtract


def test_add():
    assert add(2, 3) == 5


def test_subtract():
    assert subtract(5, 3) == 2


def test_add_verbose():
    for index in range(180):
        print(f"LONG-VERIFICATION-LINE-{index:03d}")
    assert add(4, 6) == 10
