"""Behavior check copied into each synthetic candidate."""

from app import given_name

assert given_name("Avery Example") == "Avery"
print("display name check passed")
