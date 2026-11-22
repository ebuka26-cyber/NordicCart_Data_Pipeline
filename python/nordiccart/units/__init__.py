"""Shared units of work (Phase 3, step 5, stage S1).

Every write of the NordicCart simulation is one unit of work: one PostgreSQL transaction
that runs through the V006/V007 rules and functions. Until S1 the units lived as private
functions inside the two pilot loaders. They are now here, unchanged, so that the pilot
loaders and the combined activity engine (stage S6) run exactly the same SQL.

    base     errors, id lookups, the transaction-with-retries wrapper, advisory locks,
             the ledger identity query
    orders   U1 place, U2 reserve / out-of-stock cancel, U3a payment start, U3c payment
             result, customer cancel, U4 expire, U5 fulfil
    stock    R0 reorder point revision, R1 purchase receipt, T1 transfer dispatch,
             T2 transfer arrival

The loaders decide WHICH unit runs and compare the result with their plan; the units only
write and check the immediate database result (OutcomeMismatchError on a difference).
"""
