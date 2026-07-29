import sys

from calculator import add, subtract


if "--verbose" in sys.argv:
    for index in range(180):
        print(f"LONG-VERIFICATION-LINE-{index:03d}")

assert add(2, 3) == 5
assert subtract(5, 3) == 2
print("PASS calculator regression")
