import unittest

from greeting import greeting


class GreetingTest(unittest.TestCase):
    def test_greeting_uses_supplied_name(self):
        self.assertEqual(greeting("Avery"), "Hello, Avery")
