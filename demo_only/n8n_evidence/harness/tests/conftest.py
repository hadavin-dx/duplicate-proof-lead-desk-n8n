"""Pytest setup for the harness tests: no compiled .pyc files are written by the test modules (the README runs pytest with -B as well)."""
import sys

sys.dont_write_bytecode = True
