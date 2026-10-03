"""The domain core: pure rules with no database, network or framework code.

Everything here takes plain values in and returns plain values out, so it can be
read and tested on its own. The services layer loads data, calls these functions
and saves what they return.
"""
