"""Example Heimdall script with the `value` output: prints one number.

    python count_value.py --base 10
"""
import argparse
import time

p = argparse.ArgumentParser()
p.add_argument("--base", type=int, default=10)
print(p.parse_args().base + int(time.time()) % 7)
