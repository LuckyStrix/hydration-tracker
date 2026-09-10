"""The hydration model.

`balance.py` runs the ledger; `sweat.py`, `urine.py` and `electrolytes.py`
supply its terms; `plan.py` turns the resulting state into something to do.
Nothing in here touches the database -- it takes plain dataclasses in and
returns plain dataclasses out, which is what makes it testable without a UI.
"""
