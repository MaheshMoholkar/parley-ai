"""This project's evals: datasets, tasks and scorers, built on the generic evalkit.

Run them with `python -m evals --help`. They call the real model, so they need
model access (PARLEY_MODEL_PROVIDER=bedrock) and cost money; the plumbing itself
is tested in tests/evals with fake models.
"""
