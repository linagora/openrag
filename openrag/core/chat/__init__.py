"""Chat logic with no I/O: small talk, history, the token budget and the route a request takes.

The services call these functions and supply what they need from outside
(the language detector, the token counter, the clock), so every rule here is
testable on its own.
"""
