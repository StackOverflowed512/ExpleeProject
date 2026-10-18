"""Business logic.

Routers stay thin: they parse and validate input, call a service, and return
a schema. Services own the business rules and are independent of the HTTP
layer. No domain services exist yet in Phase 0.
"""
