"""Multi-agent commodity options debate & recommendation system.

Decision-support only: deterministic regime/strike/risk layers plus LLM debate
agents produce auditable BUY/SELL/NO-TRADE recommendations for MCX NATURALGAS,
CRUDEOIL, GOLD and SILVER options. No component here places live orders —
recommendations require explicit human approval, and live execution is a
separate, config-gated phase (COMMODITY_AGENTS_LIVE, not yet implemented).
"""
