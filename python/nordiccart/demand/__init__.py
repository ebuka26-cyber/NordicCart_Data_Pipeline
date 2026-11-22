"""Demand model of the combined activity (Phase 3, step 5, stage S2).

Design: docs/phase3_combined_activity_design.md, sections 5 (order volume), 6 (one demand
model for orders, opening stock and reorder points) and 11.1 (calibration criteria).

    config        [demand] plus the shared [transactions] and [inventory] assumptions
    numeric       Decimal draws (gamma, Poisson, normal, uniform) and negative binomial formulas
    calendar      normalized monthly index, day probabilities per channel (weekday, closed days,
                  Black Week), daily exposure units, hour probabilities
    model         customers (exposure, channel propensity, store choice), products
                  (attractiveness), expected orders and units by market, channel, location,
                  product and period, outcome probabilities
    simulate      seeded draws of orders around the model (the engine's order source in S6)
    calibration   exact figures and the seeded statistical checks of design 11.1
    inventory     opening model comparison, recalibrated opening stock (S7), R-cal reorder points (S6)
    cli           python/run_demand_calibration.py: calibration figures and checks, no database

Nothing in this package connects to a database or changes the pilots.
"""
